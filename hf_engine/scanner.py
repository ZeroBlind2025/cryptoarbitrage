"""
hf_engine.scanner
=================

Polymarket market scanner for short-duration (5m / 15m / 30m / 60m)
crypto updown markets.

This is a minimal, standalone discovery routine that hits the public
Gamma REST API and yields a small list of candidate markets each time
it is polled. It intentionally does not import from ``ws_scanner.py`` or
``step_c_scanner_v4.py`` to keep the HF engine free of shared state
with the existing momentum code.

The scanner returns plain ``MarketMeta`` records; it is the engine's
job to wrap each one in a ``MarketState`` and hand its tokens to the
feed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from .config import HFEConfig


# Polymarket short-duration markets have used several slug shapes:
#   bitcoin-updown-5m-1772397000
#   ethereum-up-or-down-5m-1772397000
#   sol-higher-or-lower-15m-1772397000
# The regex accepts any of those and extracts the interval (e.g. "5m").
INTERVAL_REGEX = re.compile(
    r"(?:updown|up-or-down|higher-or-lower|above-or-below)-(\d+)\s*([mh])\b",
    re.IGNORECASE,
)
# Cheap membership test used to short-circuit slugs that obviously
# aren't short-duration up/down markets.
KNOWN_SLUG_FRAGMENTS = ("updown", "up-or-down", "higher-or-lower", "above-or-below")


@dataclass
class MarketMeta:
    market_id: str           # Polymarket condition id
    yes_token_id: str
    no_token_id: str
    slug: str
    description: str
    interval_label: str      # "5m" / "15m" / "30m" / "1h"
    duration_sec: float
    resolves_at: float       # epoch seconds
    volume_usd: float
    initial_yes_price: float


class MarketScanner:
    """Polymarket Gamma API scanner for short-duration crypto markets."""

    def __init__(self, cfg: HFEConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "hf-engine/1.0"})
        self._seen: Dict[str, float] = {}   # market_id -> first-seen epoch
        self._fetch_errors = 0
        self._last_scan = 0.0
        # Scan count so we can log a summary every N scans.
        self._scan_count = 0
        # Running totals of why raw markets were rejected; handy for
        # diagnosing an empty scanner on Railway.
        self._reject_reasons: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def scan(self) -> List[MarketMeta]:
        """Return all candidate markets the engine has not seen before.

        Any market that has already been returned by a previous scan is
        filtered out so the engine can safely call ``scan()`` on a
        short poll interval without double-processing.
        """
        now = time.time()
        self._last_scan = now
        self._scan_count += 1

        raw_markets = self._fetch_candidate_markets()
        raw_count = len(raw_markets)
        kept_count = 0
        new_count = 0
        fixed_token_ids = 0
        out: List[MarketMeta] = []
        for raw in raw_markets:
            meta = self._normalize(raw, now)
            if meta is None:
                continue
            kept_count += 1
            if meta.market_id in self._seen:
                continue

            # CRITICAL: Polymarket's Gamma ``clobTokenIds`` sometimes
            # differ in format from the canonical CLOB order-book token
            # IDs that the WebSocket keys events on. If we subscribe
            # with the Gamma IDs the feed silently drops every trade
            # and every book update. Re-query the CLOB API to get the
            # exact IDs the WS uses.
            canonical = self._fetch_canonical_token_ids(meta.market_id)
            if canonical and len(canonical) == 2:
                gamma_ids = (meta.yes_token_id, meta.no_token_id)
                if canonical != list(gamma_ids) and canonical != [gamma_ids[1], gamma_ids[0]]:
                    fixed_token_ids += 1
                    # The CLOB endpoint returns tokens in the same
                    # order as ``outcomes`` (usually Up/Yes first).
                    meta.yes_token_id = canonical[0]
                    meta.no_token_id = canonical[1]

            new_count += 1
            self._seen[meta.market_id] = now
            out.append(meta)

        # Housekeeping: drop entries older than an hour so the seen
        # cache cannot grow unbounded.
        cutoff = now - 3600
        for k in list(self._seen.keys()):
            if self._seen[k] < cutoff:
                del self._seen[k]

        if self._scan_count == 1 or self._scan_count % 6 == 0 or new_count > 0:
            top_rejects = sorted(
                self._reject_reasons.items(), key=lambda kv: -kv[1]
            )[:3]
            reject_str = ", ".join(f"{k}={v}" for k, v in top_rejects) or "-"
            print(
                f"{self.cfg.log_prefix} scan#{self._scan_count} "
                f"raw={raw_count} kept={kept_count} new={new_count} "
                f"token-id-fixed={fixed_token_ids} "
                f"total_seen={len(self._seen)} "
                f"top_rejects={reject_str} "
                f"errors={self._fetch_errors}",
                flush=True,
            )

        return out

    def _reject(self, reason: str) -> None:
        self._reject_reasons[reason] = self._reject_reasons.get(reason, 0) + 1

    def mark_processed(self, market_id: str) -> None:
        self._seen[market_id] = time.time()

    # ------------------------------------------------------------------ #
    # Fetch
    # ------------------------------------------------------------------ #

    # Coin-name mapping used to compute canonical Polymarket slugs for
    # the time-bucketed event lookups. This mirrors the table used by
    # ``momentum_engine.discover_active_markets`` so we inherit the same
    # discovery reliability, without importing from that module.
    _COIN_SLUG_NAMES = {
        "btc": "bitcoin",
        "eth": "ethereum",
        "sol": "solana",
        "xrp": "xrp",
    }
    # (label, window-seconds)
    _INTERVALS = (("5m", 300), ("15m", 900))
    # ``slug_contains`` fallback queries when the direct event-slug
    # lookups miss (Polymarket occasionally ships off-grid slugs for
    # edge-of-interval markets).
    _SLUG_CONTAINS_TERMS = (
        "updown-5m",
        "updown-15m",
        "btc-updown",
        "eth-updown",
        "sol-updown",
        "xrp-updown",
        "bitcoin-updown",
        "ethereum-updown",
        "solana-updown",
    )

    def _fetch_candidate_markets(self) -> List[dict]:
        """Multi-strategy parallel fetch for crypto short-duration markets.

        Polymarket's ``/markets?slug_contains=updown`` alone is unreliable
        because:
        - the API sometimes returns partial pages for broad slug queries,
        - and the canonical way to find each coin's current 5m / 15m
          market is to hit ``/events?slug={coin}-updown-{interval}-{ts}``
          with the time-bucketed unix timestamp.

        We therefore fire a batch of direct event-slug lookups **plus**
        a handful of ``slug_contains`` fallback queries in parallel, via
        a ``ThreadPoolExecutor``, and merge all results dedupe'd by
        ``conditionId``. Per-scan wall time is typically ~1.5 seconds on
        the Railway network.

        Every fetch failure is counted; a periodic summary log prints
        the top rejection reasons so a Gamma-side change cannot silently
        kill discovery.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        now_ts = int(time.time())
        event_slugs: List[str] = []
        for coin_abbr, coin_full in self._COIN_SLUG_NAMES.items():
            for tag, window_secs in self._INTERVALS:
                base_ts = (now_ts // window_secs) * window_secs
                for offset in (window_secs, 0, -window_secs, -2 * window_secs):
                    ts = base_ts + offset
                    event_slugs.append(f"{coin_abbr}-updown-{tag}-{ts}")
                    if coin_full != coin_abbr:
                        event_slugs.append(f"{coin_full}-updown-{tag}-{ts}")

        merged_by_id: Dict[str, dict] = {}
        errors_this_scan = 0

        gamma_base = self.cfg.gamma_api_base

        def _fetch_event_slug(slug: str):
            """Return a list of market dicts (possibly empty) for an
            event slug, or None on error."""
            try:
                r = self._session.get(
                    f"{gamma_base}/events",
                    params={"slug": slug},
                    timeout=6,
                )
                if r.status_code != 200:
                    return []
                data = r.json()
            except Exception:
                return None

            events = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            markets: List[dict] = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                nested = event.get("markets") or []
                for m in nested:
                    if isinstance(m, dict):
                        markets.append(m)
                # Some events put the market fields at the top level.
                if "conditionId" in event or "clobTokenIds" in event:
                    markets.append(event)
            return markets

        def _fetch_contains(term: str):
            try:
                r = self._session.get(
                    f"{gamma_base}/markets",
                    params={
                        "slug_contains": term,
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                    },
                    timeout=6,
                )
                if r.status_code != 200:
                    return []
                data = r.json()
            except Exception:
                return None
            if isinstance(data, dict):
                data = data.get("data", data.get("markets", []))
            return data if isinstance(data, list) else []

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = []
            for slug in event_slugs:
                futures.append(pool.submit(_fetch_event_slug, slug))
            for term in self._SLUG_CONTAINS_TERMS:
                futures.append(pool.submit(_fetch_contains, term))

            for fut in as_completed(futures):
                try:
                    result = fut.result()
                except Exception:
                    errors_this_scan += 1
                    continue
                if result is None:
                    errors_this_scan += 1
                    continue
                for m in result:
                    if not isinstance(m, dict):
                        continue
                    mid = m.get("conditionId") or m.get("id") or m.get("slug")
                    if not mid or mid in merged_by_id:
                        continue
                    merged_by_id[mid] = m

        if errors_this_scan:
            self._fetch_errors += errors_this_scan

        return list(merged_by_id.values())

    # ------------------------------------------------------------------ #
    # Canonical CLOB token IDs
    # ------------------------------------------------------------------ #

    def _fetch_canonical_token_ids(self, condition_id: str) -> Optional[List[str]]:
        """Re-fetch the canonical CLOB token IDs for a market.

        The Gamma API's ``clobTokenIds`` field sometimes has a different
        string format than the token IDs the CLOB WebSocket keys events
        on. Subscribing with the Gamma IDs then silently drops every
        incoming trade / book update because ``_handle_book`` can't find
        the asset id in ``self._registry``.

        This helper hits ``GET /markets/{condition_id}`` on the CLOB
        REST API which returns the canonical pair. Mirrors
        ``momentum_engine._fetch_clob_token_ids`` so the HF engine
        inherits the same fix without importing from that module.

        Returns ``[token_id_0, token_id_1]`` on success, ``None`` on
        any error. A soft 3-second timeout keeps a slow CLOB API from
        stalling the scanner.
        """
        if not condition_id:
            return None
        try:
            resp = self._session.get(
                f"{self.cfg.clob_api_base}/markets/{condition_id}",
                timeout=3,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None

        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, list) or len(tokens) < 2:
            return None

        out = []
        for t in tokens[:2]:
            if not isinstance(t, dict):
                return None
            tid = t.get("token_id")
            if not tid:
                return None
            out.append(str(tid))
        return out

    # ------------------------------------------------------------------ #
    # Normalization
    # ------------------------------------------------------------------ #

    def _normalize(self, raw: dict, now: float) -> Optional[MarketMeta]:
        slug = (raw.get("slug") or "").lower()
        if not slug:
            self._reject("no-slug")
            return None
        if not any(frag in slug for frag in KNOWN_SLUG_FRAGMENTS):
            self._reject("not-updown-family")
            return None

        interval_match = INTERVAL_REGEX.search(slug)
        if not interval_match:
            self._reject("no-interval-regex")
            return None

        interval_n = int(interval_match.group(1))
        interval_unit = interval_match.group(2).lower()
        if interval_unit == "m":
            duration_sec = interval_n * 60.0
            interval_label = f"{interval_n}m"
        else:
            duration_sec = interval_n * 3600.0
            interval_label = f"{interval_n}h"

        duration_min = duration_sec / 60.0
        if duration_min < self.cfg.min_market_duration_min:
            self._reject(f"duration<{self.cfg.min_market_duration_min:.0f}m")
            return None
        if duration_min > self.cfg.max_market_duration_min:
            self._reject(f"duration>{self.cfg.max_market_duration_min:.0f}m")
            return None

        # Optional coin filter.
        if self.cfg.tracked_coin_slugs:
            if not any(coin in slug for coin in self.cfg.tracked_coin_slugs):
                self._reject("coin-filter")
                return None

        # End date
        end_raw = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date")
        if not end_raw:
            self._reject("no-end-date")
            return None
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            resolves_at = end_dt.timestamp()
        except Exception:
            self._reject("bad-end-date")
            return None

        if resolves_at <= now + self.cfg.min_time_remaining_sec:
            self._reject("too-late")
            return None

        # Token IDs
        clob_token_ids_raw = raw.get("clobTokenIds")
        token_ids = _parse_token_ids(clob_token_ids_raw)
        if len(token_ids) != 2:
            self._reject("missing-token-ids")
            return None
        outcomes = raw.get("outcomes")
        outcomes = _parse_outcomes(outcomes)
        yes_token_id, no_token_id = _order_tokens(token_ids, outcomes)

        # Volume / traders
        try:
            volume_usd = float(raw.get("volume") or raw.get("volumeNum") or 0.0)
        except (TypeError, ValueError):
            volume_usd = 0.0
        if volume_usd < self.cfg.min_pre_volume_usd:
            # Still accept — small fresh markets are common and the
            # first 15 seconds of trading will establish baseline rate.
            pass

        # Initial yes price
        try:
            last_price = float(raw.get("lastTradePrice") or raw.get("outcomePrice1") or 0.5)
        except (TypeError, ValueError):
            last_price = 0.5
        if not 0.0 < last_price < 1.0:
            last_price = 0.5

        market_id = raw.get("conditionId") or raw.get("id") or raw.get("questionID") or slug
        description = raw.get("question") or raw.get("title") or slug

        return MarketMeta(
            market_id=str(market_id),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            slug=slug,
            description=description,
            interval_label=interval_label,
            duration_sec=duration_sec,
            resolves_at=resolves_at,
            volume_usd=volume_usd,
            initial_yes_price=last_price,
        )

    # ------------------------------------------------------------------ #
    # Resolution lookup
    # ------------------------------------------------------------------ #

    def lookup_resolution(self, market_id: str) -> Optional[int]:
        """Check whether a market has resolved and return the outcome.

        Returns ``1`` if the Up/Yes side won, ``0`` if the Down/No side
        won, or ``None`` if it has not resolved yet.

        Uses the CLOB ``/markets/{condition_id}`` endpoint, which is
        Polymarket's definitive resolution source — every resolved
        market has ``closed: true`` plus a per-token ``winner`` boolean.
        This is the same endpoint ``copy_trader._check_clob_resolution``
        uses, so the HF engine inherits the same reliability without
        importing from that module.

        The previous implementation hit
        ``GET {gamma}/markets?id={condition_id}`` — Gamma ignores the
        ``id`` query parameter, so the request returned an unfiltered
        page of markets and the resolution check silently failed for
        every market. That's what made every position close via
        ``unresolved-timeout`` (mark-to-market) instead of
        ``settle_at_resolution``, which is why the dashboard's
        BALANCE / SESSION P/L / WIN RATE headline stats all sat at
        zero even though the paper executor was clearly firing.
        """
        if not market_id:
            return None
        try:
            resp = self._session.get(
                f"{self.cfg.clob_api_base}/markets/{market_id}",
                timeout=4,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        # Must be closed before we can claim resolution.
        if not data.get("closed"):
            return None

        tokens = data.get("tokens") or []
        if not isinstance(tokens, list):
            return None

        for t in tokens:
            if not isinstance(t, dict):
                continue
            if t.get("winner") is not True:
                continue
            outcome = str(t.get("outcome") or "").strip().lower()
            if outcome in ("up", "yes", "higher", "above", "true"):
                return 1
            if outcome in ("down", "no", "lower", "below", "false"):
                return 0
            # Unknown label but marked as winner — fall back to
            # positional matching against ``tokens[0]`` which is
            # conventionally the Up/Yes side.
            if tokens and tokens[0] is t:
                return 1
            return 0

        # ``closed: true`` with no ``winner: true`` — edge case where
        # Polymarket marked the market closed but hasn't finalised the
        # winner. Treat as unresolved so the engine keeps waiting.
        return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_token_ids(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except Exception:
            return []
    return []


def _parse_outcomes(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip().lower() for x in raw]
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip().lower() for x in parsed]
        except Exception:
            return []
    return []


def _order_tokens(tokens: List[str], outcomes: List[str]) -> Tuple[str, str]:
    """Return (yes_token_id, no_token_id).

    Polymarket up/down markets use outcomes like ``["Up", "Down"]``.
    We treat "Up" as Yes (the Yes token is the one we are Bayesian about).
    If outcome labels are missing we fall back to positional ordering,
    assuming index 0 is Yes.
    """
    if len(tokens) < 2:
        raise ValueError("need two tokens")
    if len(outcomes) == 2:
        yes_keywords = {"up", "yes", "higher", "above", "true"}
        if outcomes[0] in yes_keywords and outcomes[1] not in yes_keywords:
            return tokens[0], tokens[1]
        if outcomes[1] in yes_keywords and outcomes[0] not in yes_keywords:
            return tokens[1], tokens[0]
    return tokens[0], tokens[1]
