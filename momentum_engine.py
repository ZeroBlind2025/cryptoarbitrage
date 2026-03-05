#!/usr/bin/env python3
"""
MOMENTUM ENGINE - Independent price-momentum trader
=====================================================

Alternative trading engine that can be toggled on when the copy trader's
target goes quiet.  Instead of following a trader, it polls live market
prices for BTC, ETH, SOL, XRP and enters markets that show upward
momentum.

Entry rules:
  - Only enter when price >= 65 cents (configurable MIN_ENTRY_PRICE)
  - Can buy Up or Down — whichever side qualifies
  - Never buy both sides of the same market

Re-entry rules (upward-only):
  - After an initial buy, only re-enter if current price > last buy price
  - "Last buy price" = the price of the MOST RECENT buy, not the original
  - This means: buy at 67, re-buy at 70, price drops to 69 → NO rebuy
  - Only re-buy if price goes to 71+ (above the 70 last-buy)
  - Caps downward spirals — only trades upward movements

Guards (same as copy trader):
  - No opposite sides on same market
  - Per-coin pause support
  - Per-coin lot sizes
  - Max entries per market

Usage:
    Toggled on/off via dashboard API endpoints.
    When active, the copy trader stops polling for new trades
    (but keeps resolving open positions).
"""

import os
import json
import time
import requests
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False

try:
    from clob_ws import CLOBWebSocket
    HAS_CLOB_WS = True
except ImportError:
    HAS_CLOB_WS = False

from copy_trader import (
    FUNDER_ADDRESS,
    PRIVATE_KEY,
    SIGNATURE_TYPE,
    CLOB_API,
    GAMMA_API,
    COIN_BET_AMOUNTS,
    BET_AMOUNT,
    PRICE_BUFFER_BPS,
    ALGO_STARTING_BALANCE,
    CRYPTO_SLUGS,
    detect_coin,
    get_clob_client,
    place_bet,
    load_positions,
    save_positions,
    get_active_crypto_tokens,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

# Minimum price to enter a market (65 cents = 0.65)
MIN_ENTRY_PRICE = float(os.getenv("MOMENTUM_MIN_ENTRY_PRICE", "0.65"))

# Maximum price to enter — 98.9¢ cap filters out 99¢+ "last second" entries
# that linger at market close and likely wouldn't fill in live trading
MAX_ENTRY_PRICE = float(os.getenv("MOMENTUM_MAX_ENTRY_PRICE", "0.989"))

# ---------------------------------------------------------------------------
# Per-interval entry price brackets (data-driven from bracket analysis)
# Each interval maps to a list of (min, max) tuples.  A price must fall in
# at least ONE bracket to qualify.  Intervals not listed here use the global
# MIN_ENTRY_PRICE / MAX_ENTRY_PRICE range.
#
# 5m  bracket: 80-<99¢ (upper exclusive — 99¢+ filtered out)
# 15m bracket: 76-<99¢
# ---------------------------------------------------------------------------
INTERVAL_PRICE_BRACKETS: dict[str, list[tuple[float, float]]] = {
    "5m":  [(0.80, 0.99)],
    "15m": [(0.76, 0.99)],
}

# How often to poll prices (seconds)
POLL_INTERVAL = int(os.getenv("MOMENTUM_POLL_INTERVAL", "1"))

# Max entries per market (same as copy trader default)
MAX_ENTRIES_PER_MARKET = int(os.getenv("COPY_MAX_ENTRIES_PER_MARKET", "2"))

# Minimum minutes before market close to allow entry.
# Prevents placing trades after (or right at) the close time.
MIN_MINUTES_BEFORE_CLOSE = float(os.getenv("MOMENTUM_MIN_MINUTES_BEFORE_CLOSE", "1.0"))


# =============================================================================
# MARKET DISCOVERY
# =============================================================================

CRYPTO_COINS = ["btc", "eth", "sol", "xrp"]

# Full names used in Polymarket slugs (e.g. "bitcoin-updown-15m-1740844800")
COIN_SLUG_NAMES = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
}
INTERVALS = ["5m", "15m"]

# Interval detection patterns for question text
# e.g. "9:00AM-9:15AM" = 15m, "9:00AM-9:05AM" = 5m, "12PM" (hourly) = 60m
_INTERVAL_MINUTES = {5: "5m", 15: "15m", 60: "60m"}


def _detect_interval(slug: str, question: str) -> str:
    """Detect interval from slug or question text.

    Checks slug for tags like '-15m-', '-5m-', 'updown-15m' and also parses
    question time ranges like '9:00AM-9:15AM' or hourly format like
    'March 1, 12PM ET'.
    """
    import re

    slug_lower = slug.lower()

    # Check slug for interval tags (e.g. -15m-, -5m-, updown-15m-{ts})
    for tag in INTERVALS:
        if f"-{tag}-" in slug_lower or slug_lower.endswith(f"-{tag}"):
            return tag

    # Also check for "updown-{interval}" pattern (e.g. btc-updown-15m-1740844800)
    updown_match = re.search(r'updown-(\d+m)\b', slug_lower)
    if updown_match:
        tag = updown_match.group(1)
        if tag in INTERVALS:
            return tag

    # Check for "1h" or "1hr" in slug (hourly markets)
    if re.search(r'-1h[r]?[-\d]', slug_lower) or slug_lower.endswith("-1h") or slug_lower.endswith("-1hr"):
        return "60m"

    # Parse question time range: "HH:MMAM-HH:MMAM" or "H:MMAM-H:MMAM"
    time_range = re.search(
        r'(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)',
        question, re.IGNORECASE
    )
    if time_range:
        h1, m1, p1, h2, m2, p2 = time_range.groups()
        h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
        if p1.upper() == "PM" and h1 != 12:
            h1 += 12
        if p2.upper() == "PM" and h2 != 12:
            h2 += 12
        if p1.upper() == "AM" and h1 == 12:
            h1 = 0
        if p2.upper() == "AM" and h2 == 12:
            h2 = 0
        diff = (h2 * 60 + m2) - (h1 * 60 + m1)
        if diff in _INTERVAL_MINUTES:
            return _INTERVAL_MINUTES[diff]

    # Hourly format: just a single time like "March 1, 12PM ET" (no range)
    if re.search(r'\b\d{1,2}(AM|PM)\s+ET\b', question, re.IGNORECASE):
        # Single time = hourly market
        if not time_range:
            return "60m"

    return ""


def _parse_market(raw: dict) -> Optional[dict]:
    """Parse a single Gamma API market into our internal format.

    Returns None if the market doesn't match crypto updown criteria.
    Accepts markets with "Up or Down" in question text OR "updown" in slug
    (Polymarket uses both naming conventions).
    """
    slug = (raw.get("slug") or "").lower()
    question = raw.get("question", "")

    # MUST be an "Up or Down" market (check question text AND slug)
    is_updown = ("up or down" in question.lower()
                 or "updown" in slug
                 or "up-or-down" in slug)
    if not is_updown:
        return None

    # Parse token IDs
    clob_ids_raw = raw.get("clobTokenIds", "[]")
    if isinstance(clob_ids_raw, str):
        try:
            clob_ids = json.loads(clob_ids_raw)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        clob_ids = clob_ids_raw or []

    # Parse prices
    prices_raw = raw.get("outcomePrices", "[]")
    if isinstance(prices_raw, str):
        try:
            prices = [float(p) for p in json.loads(prices_raw)]
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        try:
            prices = [float(p) for p in (prices_raw or [])]
        except (ValueError, TypeError):
            return None

    # Parse outcomes
    outcomes = raw.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, ValueError):
            outcomes = ["Up", "Down"]

    if len(clob_ids) != 2 or len(prices) != 2 or len(outcomes) != 2:
        return None

    condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
    coin = detect_coin(slug, question)

    # Detect interval from slug OR question text
    interval = _detect_interval(slug, question)

    if not coin or not interval:
        return None

    # Only trade intervals we're configured for
    if interval not in INTERVALS:
        return None

    # Parse market close time so we can avoid entering after close
    end_date_str = raw.get("endDate") or raw.get("end_date_iso")
    minutes_until_close = None
    if end_date_str:
        try:
            if "T" in str(end_date_str):
                end_date = datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
            else:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                end_date = end_date.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            minutes_until_close = (end_date - now).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    return {
        "slug": raw.get("slug", ""),
        "question": question,
        "condition_id": condition_id,
        "outcomes": outcomes,
        "token_ids": [str(clob_ids[0]), str(clob_ids[1])],
        "prices": prices,
        "coin": coin,
        "interval": interval,
        "minutes_until_close": minutes_until_close,
    }


def _fetch_clob_token_ids(condition_id: str) -> Optional[list[str]]:
    """Fetch canonical CLOB token IDs for a market from the CLOB API.

    The Gamma API returns clobTokenIds that may differ in format from
    the actual CLOB order book token IDs. This queries the CLOB API
    directly to get the exact IDs that the WebSocket uses.

    Returns [token_id_0, token_id_1] or None on failure.
    """
    if not condition_id:
        return None
    try:
        resp = requests.get(
            f"{CLOB_API}/markets/{condition_id}",
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            tokens = data.get("tokens", [])
            if len(tokens) >= 2:
                return [str(tokens[0].get("token_id", "")),
                        str(tokens[1].get("token_id", ""))]
    except Exception:
        pass
    return None


def _is_crypto_updown_event(title: str, slug: str) -> bool:
    """Check if an event is a crypto up-or-down event by title/slug."""
    combined = (title + " " + slug).lower()
    crypto_terms = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple"]
    has_crypto = any(t in combined for t in crypto_terms)
    has_updown = "up or down" in combined or "updown" in combined or "up-or-down" in combined
    return has_crypto and has_updown


def discover_active_markets() -> list[dict]:
    """Find all active crypto updown markets across all intervals (5m, 15m, 1hr).

    Uses three strategies in order:
      1. /events endpoint — primary discovery via events with nested markets
      2. Timestamp-based slug search — for specific interval windows
      3. Broad /markets fallback — catch anything missed

    Returns a list of market dicts with:
      - slug, question, condition_id
      - outcomes: ["Up", "Down"] or similar
      - token_ids: [up_token_id, down_token_id]
      - prices: [up_price, down_price]
      - coin: "btc", "eth", "sol", "xrp"
      - interval: "5m", "15m", "60m"
    """
    markets = []
    seen_conditions = set()

    def _add_market(raw: dict):
        """Parse and add a market if valid and not seen."""
        cid = raw.get("conditionId") or raw.get("condition_id") or ""
        if cid in seen_conditions:
            return False
        m = _parse_market(raw)
        if m:
            markets.append(m)
            seen_conditions.add(cid)
            return True
        return False

    # ================================================================
    # Strategy 0: Event slug lookup with computed timestamps
    #
    # 5m/15m use unix-timestamp slugs:
    #   /event/{coin}-updown-{5m|15m}-{unix_ts}
    #   e.g. btc-updown-5m-1772397000
    #
    # Hourly uses human-readable ET date/time:
    #   /event/{coin}-up-or-down-{month}-{day}-{hour}{am/pm}-et
    #   e.g. bitcoin-up-or-down-march-3-9pm-et
    # ================================================================
    now_ts = int(time.time())

    # --- 5m / 15m: unix-timestamp based ---
    # Polymarket slug timestamps follow floor-aligned unix seconds:
    #   5m:  Math.floor(now / 300000) * 300  (i.e. floor to 300s boundary)
    #   15m: Math.floor(now / 900000) * 900  (i.e. floor to 900s boundary)
    ts_slug_configs = [
        ("5m", 300), ("15m", 900),
    ]
    event_slug_coins = ["btc", "eth", "sol", "xrp"]
    event_slug_found = 0

    for coin_abbr in event_slug_coins:
        coin_full = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
        for tag, window_secs in ts_slug_configs:
            base_ts = (now_ts // window_secs) * window_secs
            # Check next, current, and TWO previous windows.
            # "Half Missing" fix: Polymarket keeps the previous interval open
            # for a few minutes while the new one starts. Check 2 back to catch
            # markets that are still settling.
            for ts in [base_ts + window_secs, base_ts, base_ts - window_secs, base_ts - 2 * window_secs]:
                slug_variants = [f"{coin_abbr}-updown-{tag}-{ts}"]
                if coin_full != coin_abbr:
                    slug_variants.append(f"{coin_full}-updown-{tag}-{ts}")

                for event_slug in slug_variants:
                    try:
                        resp = requests.get(
                            f"{GAMMA_API}/events",
                            params={"slug": event_slug},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            events_list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                            for event in events_list:
                                if not isinstance(event, dict):
                                    continue
                                for mkt in event.get("markets", []):
                                    if _add_market(mkt):
                                        event_slug_found += 1
                                if "conditionId" in event:
                                    if _add_market(event):
                                        event_slug_found += 1
                    except Exception:
                        pass
                    time.sleep(0.02)

    if event_slug_found > 0:
        print(f"[MOMENTUM] Event slugs: found {event_slug_found} markets "
              f"via computed slugs", flush=True)

    # ================================================================
    # Strategy 1: /events endpoint (broad listing)
    # Events contain nested markets — catches any we missed above.
    # ================================================================
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            events = resp.json()
            events_checked = 0
            for event in events:
                event_title = event.get("title", "")
                event_slug = event.get("slug", "")

                # Quick filter: is this a crypto updown event?
                if not _is_crypto_updown_event(event_title, event_slug):
                    continue

                events_checked += 1
                event_markets = event.get("markets", [])
                for mkt in event_markets:
                    _add_market(mkt)

            if events_checked > 0:
                print(f"[MOMENTUM] Events: checked {events_checked} crypto events "
                      f"from {len(events)} total", flush=True)
        else:
            print(f"[MOMENTUM] Events endpoint returned {resp.status_code}", flush=True)

    except Exception as e:
        print(f"[MOMENTUM] Events search error: {e}", flush=True)

    # ================================================================
    # Strategy 2: Timestamp-based slug search (5m/15m only)
    # Polymarket uses unix-ts slugs for short intervals:
    #   "bitcoin-updown-15m-1740844800"
    #   "ethereum-updown-5m-1740844500"
    # Hourly uses human-readable format (handled in Strategy 0).
    # ================================================================

    # Interval configs: (slug_tag, seconds_per_window)
    interval_configs = [
        ("5m", 300),
        ("15m", 900),
    ]

    for coin_abbr in CRYPTO_COINS:
        coin_name = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
        for tag, window_secs in interval_configs:
            # Next + current + 2 previous windows (settling markets stay open)
            base_ts = (now_ts // window_secs) * window_secs
            timestamps = [base_ts + window_secs, base_ts, base_ts - window_secs, base_ts - 2 * window_secs]

            for ts in timestamps:
                # Try BOTH abbreviated and full coin names
                # Polymarket uses both: "eth-updown-15m-{ts}" AND
                # "ethereum-updown-15m-{ts}" depending on market type
                slug_variants = [f"{coin_name}-updown-{tag}-{ts}"]
                if coin_abbr != coin_name:
                    slug_variants.append(f"{coin_abbr}-updown-{tag}-{ts}")

                for slug_pattern in slug_variants:
                    try:
                        # Don't filter active/closed for exact slug lookups —
                        # settling markets may have active=false but still be tradeable
                        resp = requests.get(
                            f"{GAMMA_API}/markets",
                            params={"slug": slug_pattern},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, list):
                                for raw in data:
                                    _add_market(raw)
                    except Exception:
                        pass

            time.sleep(0.02)

    # Also search with slug_contains for partial matches
    # Prioritise short-interval terms first (5m/15m most likely to be missed)
    slug_search_terms = [
        "updown-5m", "updown-15m",
        "btc-updown", "eth-updown", "sol-updown", "xrp-updown",
        "bitcoin-updown", "ethereum-updown", "solana-updown",
    ]
    slug_contains_found = 0
    for search_term in slug_search_terms:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "slug_contains": search_term,
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for raw in data:
                        if _add_market(raw):
                            slug_contains_found += 1
        except Exception:
            pass
        time.sleep(0.02)

    # ================================================================
    # Strategy 3: Broad /markets search with question-text filtering
    # This is the most reliable discovery method — fetch active markets
    # and filter locally for crypto "Up or Down" markets by question text.
    # Uses the same approach proven by Polymarket's own discovery scripts.
    # ================================================================
    _ASSET_NAMES = ["Bitcoin", "Ethereum", "Solana", "XRP",
                    "bitcoin", "ethereum", "solana", "xrp"]
    broad_found = 0
    try:
        response = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        response.raise_for_status()
        all_markets = response.json()

        for raw in all_markets:
            question = raw.get("question") or ""
            slug = (raw.get("slug") or "").lower()
            # Filter: must be a crypto updown market
            q_lower = question.lower()
            is_updown = ("up or down" in q_lower or "updown" in slug
                         or "up-or-down" in slug)
            is_crypto = any(a.lower() in q_lower or a.lower() in slug
                          for a in _ASSET_NAMES[:4])
            if is_updown and is_crypto:
                if _add_market(raw):
                    broad_found += 1

    except Exception as e:
        print(f"[MOMENTUM] Broad search error: {e}", flush=True)

    # Also try fetching from /events with active+closed filter
    # Events endpoint groups markets by event, may surface different results
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        if resp.status_code == 200:
            events = resp.json()
            for event in events:
                event_title = event.get("title", "")
                event_slug = event.get("slug", "")
                if not _is_crypto_updown_event(event_title, event_slug):
                    continue
                for mkt in event.get("markets", []):
                    if _add_market(mkt):
                        broad_found += 1
    except Exception as e:
        print(f"[MOMENTUM] Events broad search error: {e}", flush=True)

    if broad_found > 0:
        print(f"[MOMENTUM] Broad search: found {broad_found} new markets", flush=True)

    if markets:
        coins_found = set(m["coin"] for m in markets)
        intervals_found = set(m["interval"] for m in markets)
        # Per-interval count for debugging discovery gaps
        interval_counts = {}
        for m in markets:
            ivl = m["interval"]
            interval_counts[ivl] = interval_counts.get(ivl, 0) + 1
        ivl_str = ", ".join(f"{k}={v}" for k, v in sorted(interval_counts.items()))
        print(f"[MOMENTUM] Discovered {len(markets)} markets: "
              f"coins={sorted(coins_found)}, intervals=[{ivl_str}]",
              flush=True)

        # Explicit per-market log so gaps are visible in real time
        import re as _re
        for m in markets:
            coin_upper = m["coin"].upper()
            ivl = m["interval"]
            slug = m.get("slug", "")
            # Extract unix timestamp from slug (e.g. xrp-updown-15m-1772675100)
            ts_match = _re.search(r'-(\d{10})$', slug)
            ts_label = ts_match.group(1) if ts_match else ""
            # Try to show human-readable time window from question
            q = m.get("question", "")
            time_match = _re.search(
                r'(\d{1,2}:\d{2}\s*[AP]M\s*[-–]\s*\d{1,2}:\d{2}\s*[AP]M)',
                q, _re.IGNORECASE
            )
            if not time_match:
                time_match = _re.search(r'(\d{1,2}[AP]M\s+ET)', q, _re.IGNORECASE)
            time_window = time_match.group(1) if time_match else ""
            up_price = m["prices"][0] if len(m.get("prices", [])) >= 2 else "?"
            dn_price = m["prices"][1] if len(m.get("prices", [])) >= 2 else "?"
            print(f"  FOUND {coin_upper}_{ivl}  {time_window}  "
                  f"ts={ts_label}  Up={up_price}  Down={dn_price}  "
                  f"slug={slug[:50]}", flush=True)
    else:
        print("[MOMENTUM] No active updown markets found", flush=True)

    # --- Filter to only CURRENT + NEXT time window markets ---
    # We only want ~8 active markets (4 coins × 2 intervals), not 32
    # that include already-resolved and future windows.
    now_ts = int(time.time())
    active_markets = []
    for m in markets:
        minutes_left = m.get("minutes_until_close")
        prices = m.get("prices", [])
        # Skip resolved (0/1 prices)
        if len(prices) == 2 and prices[0] in (0.0, 1.0) and prices[1] in (0.0, 1.0):
            continue
        # Skip markets that are already closed
        if minutes_left is not None and minutes_left < 0:
            continue
        active_markets.append(m)

    if len(active_markets) < len(markets):
        print(f"[MOMENTUM] Filtered to {len(active_markets)} active markets "
              f"(from {len(markets)} total)", flush=True)

    # --- Enrich with canonical CLOB token IDs ---
    # The Gamma API clobTokenIds may differ from the actual CLOB WS token IDs.
    # Query CLOB API to get the exact IDs the WebSocket uses.
    enriched = 0
    for m in active_markets:
        cid = m.get("condition_id", "")
        clob_ids = _fetch_clob_token_ids(cid)
        if clob_ids and len(clob_ids) == 2 and all(clob_ids):
            gamma_ids = m["token_ids"]
            if gamma_ids != clob_ids:
                print(f"[MOMENTUM] Token ID FIX for {m['coin'].upper()}_{m['interval']}: "
                      f"gamma={gamma_ids[0][:20]}... clob={clob_ids[0][:20]}...", flush=True)
            m["token_ids"] = clob_ids
            enriched += 1
        time.sleep(0.02)

    if enriched > 0:
        print(f"[MOMENTUM] Enriched {enriched}/{len(active_markets)} markets "
              f"with CLOB token IDs", flush=True)

    # --- Log discovered markets to CSV (dedup by condition_id) ---
    _log_discovered_markets(active_markets)

    return active_markets


# Path for the discovery log CSV
DISCOVERY_LOG = Path(__file__).parent / "market_discovery_log.csv"

# Persistent trade log — one JSON object per line, append-only
TRADE_LOG = Path(__file__).parent / "momentum_trades.jsonl"


def _log_trade(event_type: str, data: dict):
    """Append a trade event to the JSONL log.

    event_type: 'entry', 'fill', 'failed', 'resolved', 'dry_run'
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    try:
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

# In-memory set of condition_ids already written (avoids re-reading CSV each scan)
_logged_condition_ids: set = set()


def _log_discovered_markets(markets: list[dict]):
    """Append newly-seen markets to the discovery CSV.

    Each unique market (by condition_id) is logged exactly once so
    after 24 hours you can count rows per coin/interval and compare
    to expected totals to find gaps.
    """
    import csv

    global _logged_condition_ids

    # On first call, seed the set from existing CSV rows
    if not _logged_condition_ids and DISCOVERY_LOG.exists():
        try:
            with open(DISCOVERY_LOG, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row.get("condition_id", "")
                    if cid:
                        _logged_condition_ids.add(cid)
        except Exception:
            pass

    new_rows = []
    for m in markets:
        cid = m.get("condition_id", "")
        if not cid or cid in _logged_condition_ids:
            continue
        _logged_condition_ids.add(cid)
        new_rows.append({
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "coin": m.get("coin", ""),
            "interval": m.get("interval", ""),
            "slug": m.get("slug", ""),
            "condition_id": cid,
            "question": (m.get("question", ""))[:80],
            "up_price": m["prices"][0] if len(m.get("prices", [])) >= 2 else "",
            "down_price": m["prices"][1] if len(m.get("prices", [])) >= 2 else "",
        })

    if not new_rows:
        return

    write_header = not DISCOVERY_LOG.exists()
    fieldnames = [
        "discovered_at", "coin", "interval", "slug",
        "condition_id", "question", "up_price", "down_price",
    ]
    try:
        with open(DISCOVERY_LOG, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(new_rows)
    except Exception as e:
        print(f"[MOMENTUM] Discovery log write error: {e}", flush=True)


# =============================================================================
# MOMENTUM ENGINE
# =============================================================================

class MomentumEngine:
    """Price-momentum trader for crypto updown markets.

    Polls market prices and enters when price >= threshold,
    only re-entering on upward price movement.
    """

    def __init__(
        self,
        dry_run: bool = True,
        on_trade: Optional[callable] = None,
        on_resolution: Optional[callable] = None,
        bet_amount: Optional[float] = None,
        coin_bet_amounts: Optional[dict] = None,
        shared_positions: Optional[dict] = None,
    ):
        self.dry_run = dry_run
        self.on_trade = on_trade
        self.on_resolution = on_resolution
        self.client: Optional["ClobClient"] = None

        # Trade amounts (same structure as copy trader)
        self.bet_amount = bet_amount if bet_amount is not None else BET_AMOUNT
        self.coin_bet_amounts = dict(coin_bet_amounts) if coin_bet_amounts else dict(COIN_BET_AMOUNTS)

        # Per-coin pause (e.g. {"sol", "xrp"})
        self.paused_coins: set = set()

        # Entry thresholds
        self.min_entry_price = MIN_ENTRY_PRICE
        self.max_entry_price = MAX_ENTRY_PRICE
        self.max_entries_per_market = MAX_ENTRIES_PER_MARKET
        self.interval_price_brackets = dict(INTERVAL_PRICE_BRACKETS)

        # Track entered markets: (condition_id, token_id) → last_buy_price
        # KEY: uses token_id (stable identifier) not outcome_index (array position
        # that can flip between API calls).
        self.entered_markets: dict = {}
        self.market_entry_count: dict = {}

        # Position tracking — share the same dict as copy trader when available
        # so both engines' trades appear in the combined P&L / balance chart
        self.positions = shared_positions if shared_positions is not None else load_positions()

        # Stats
        self.trades_entered = 0
        self.trades_skipped = 0
        self.total_spent = 0.0
        self.trade_history: list = []
        self.scans_completed = 0

        # Market discovery cache — REST calls are slow, WebSocket prices are fast.
        # Only re-discover markets every N seconds; use cached list + WS prices
        # for the fast per-second price checks.
        self._cached_markets: list = []
        self._last_market_discovery = 0.0
        self._market_discovery_interval = 30  # re-discover every 30s

        # Fast 5m boundary detection — track which 5m epochs we've already
        # discovered so we can do targeted slug lookups right at boundaries
        self._last_5m_epoch = 0

        # Resolution
        self.last_resolution_check = 0
        self.resolution_check_interval = 60

        # WebSocket for live prices
        self.ws: Optional["CLOBWebSocket"] = None
        self.last_ws_refresh = 0
        self.ws_refresh_interval = 30

        # CLOB REST price cache — populated every N seconds (not every scan)
        self._clob_price_cache: dict[str, float] = {}
        self._last_clob_fetch = 0.0
        self._clob_fetch_interval = 10  # seconds between REST batch fetches

        # WS live price cache — populated directly by WS callback.
        # Keyed by the EXACT asset_id the WS server sends back (which may
        # differ from the token_ids in our discovered markets).
        self._ws_price_cache: dict[str, float] = {}  # asset_id -> best_ask

        # Bidirectional token ID map: ws_asset_id <-> our_token_id
        # Built once after first WS prices arrive.
        self._ws_to_market_token: dict[str, str] = {}
        self._market_token_to_ws: dict[str, str] = {}

    def start(self):
        """Initialize the momentum engine."""
        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items()))
        print("\n" + "=" * 60)
        print("  MOMENTUM ENGINE")
        print(f"  Global entry range: {self.min_entry_price*100:.0f}-{self.max_entry_price*100:.0f}¢")
        if self.interval_price_brackets:
            print("  Per-interval brackets:")
            for ivl, brackets in sorted(self.interval_price_brackets.items()):
                ranges = " | ".join(f"{lo*100:.0f}-{hi*100:.0f}¢" for lo, hi in brackets)
                print(f"    {ivl}: {ranges}")
            other = [i for i in INTERVALS if i not in self.interval_price_brackets]
            if other:
                print(f"    {', '.join(other)}: global range ({self.min_entry_price*100:.0f}-{self.max_entry_price*100:.0f}¢)")
        print(f"  Re-entry: upward only (current > last buy)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance: ${balance:.2f}")
        print(f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll interval: {POLL_INTERVAL}s")
        print("=" * 60 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[MOMENTUM] Failed to init client. Running in dry-run mode.", flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_markets from existing open positions
        for pos in self.positions.get("open", []):
            if pos.get("source") != "momentum":
                continue
            cid = pos.get("condition_id", "")
            tid = pos.get("token_id", "")
            ep = pos.get("entry_price", 0)
            if cid and tid:
                mk = (cid, tid)
                # Use entry_price as last_buy — on restart we lose the chain,
                # so this is the safest conservative default
                self.entered_markets[mk] = ep
                self.market_entry_count[mk] = self.market_entry_count.get(mk, 0) + 1
        if self.entered_markets:
            print(f"[MOMENTUM] Resumed {len(self.entered_markets)} active entries from open positions", flush=True)

    def _on_ws_price(self, ws_asset_id: str, bid: float, ask: float):
        """Handle WS price update — cache it and try to map to our markets."""
        self._ws_price_cache[ws_asset_id] = ask

        # Try to build the bidirectional map if not yet mapped
        if ws_asset_id not in self._ws_to_market_token:
            # Direct match: ws_asset_id IS one of our market token_ids
            for m in self._cached_markets:
                tids = m.get("token_ids", [])
                if ws_asset_id in tids:
                    self._ws_to_market_token[ws_asset_id] = ws_asset_id
                    self._market_token_to_ws[ws_asset_id] = ws_asset_id
                    idx = tids.index(ws_asset_id)
                    outcome = m["outcomes"][idx] if idx < len(m.get("outcomes", [])) else "?"
                    print(f"[WS MAP] Mapped {ws_asset_id[:20]}... → "
                          f"{m['coin'].upper()}_{m['interval']} {outcome}", flush=True)
                    break

    def _start_ws(self):
        """Start WebSocket for real-time prices."""
        if not HAS_CLOB_WS:
            return
        try:
            self.ws = CLOBWebSocket(
                on_price_change=self._on_ws_price,
                on_connect=lambda: print("[MOMENTUM] WebSocket connected", flush=True),
                on_disconnect=lambda: print("[MOMENTUM] WebSocket disconnected", flush=True),
            )
            self.ws.start()
            time.sleep(1)
            self._refresh_ws_tokens()
        except Exception as e:
            print(f"[MOMENTUM] WebSocket start failed: {e}", flush=True)
            self.ws = None

    def _refresh_ws_tokens(self, force: bool = False):
        """Subscribe to active crypto tokens from discovered markets.

        Uses the momentum engine's own discovered markets (which are
        comprehensive) instead of get_active_crypto_tokens() which only
        fetches 50 generic markets from Gamma.
        """
        if not self.ws:
            return
        now = time.time()
        if not force and now - self.last_ws_refresh < self.ws_refresh_interval:
            return
        self.last_ws_refresh = now

        # Collect token IDs from our discovered markets (most reliable source).
        # Skip already-resolved markets (prices 0.0/1.0) — subscribing to
        # expired tokens triggers "INVALID OPERATION" from the WS server.
        token_ids = []
        for m in self._cached_markets:
            prices = m.get("prices", [])
            if len(prices) == 2:
                # Resolved: one side is 0.0 and the other is 1.0
                if (prices[0] in (0.0, 1.0) and prices[1] in (0.0, 1.0)):
                    continue
            token_ids.extend(m.get("token_ids", []))

        # Fallback to generic fetch only if we have no cached markets yet
        if not token_ids:
            token_ids = get_active_crypto_tokens()

        if token_ids:
            self.ws.subscribe(token_ids)
            print(f"[MOMENTUM] WebSocket subscribed to {len(token_ids)} tokens "
                  f"from {len(self._cached_markets)} markets", flush=True)

            # One-time diagnostic: log full token ID format for debugging
            if self.scans_completed <= 1 and token_ids:
                print(f"[MOMENTUM] Token ID sample (full): {token_ids[0]}", flush=True)
                if len(token_ids) > 1:
                    print(f"[MOMENTUM] Token ID sample (full): {token_ids[1]}", flush=True)

    def get_live_price(self, token_id: str) -> Optional[float]:
        """Get real-time price from WS or CLOB REST cache.

        Tries multiple lookups to handle token ID format mismatches
        between Gamma API and the WS server.
        Returns best_ask (the price you'd pay to buy).
        """
        if not token_id:
            return None

        # 1. Direct WS book lookup (token_id matches WS asset_id)
        if self.ws:
            _, best_ask = self.ws.get_best_prices(token_id)
            if best_ask is not None:
                return best_ask

        # 2. WS price cache via mapped asset_id
        ws_id = self._market_token_to_ws.get(token_id)
        if ws_id and ws_id in self._ws_price_cache:
            return self._ws_price_cache[ws_id]

        # 3. Direct WS price cache (in case token_id == ws_asset_id)
        if token_id in self._ws_price_cache:
            return self._ws_price_cache[token_id]

        # 4. CLOB REST cache (populated once per scan cycle)
        return self._clob_price_cache.get(token_id)

    def _fetch_clob_prices_batch(self, markets: list[dict]):
        """Fetch real-time best-ask prices for all active markets via CLOB REST.

        Called every ~10s. Fetches /price endpoint (lighter than /book)
        for tokens missing WS data. Prioritises non-resolved markets.
        """
        self._clob_price_cache = {}
        tokens_to_fetch = []

        for m in markets:
            # Skip resolved markets (0/1 prices)
            prices = m.get("prices", [])
            if len(prices) == 2 and prices[0] in (0.0, 1.0) and prices[1] in (0.0, 1.0):
                continue
            for tid in m.get("token_ids", []):
                # Skip tokens that already have live WS prices
                if self.ws:
                    _, ws_ask = self.ws.get_best_prices(tid)
                    if ws_ask is not None:
                        continue
                # Also skip if already in ws_price_cache
                ws_id = self._market_token_to_ws.get(tid)
                if tid in self._ws_price_cache or (ws_id and ws_id in self._ws_price_cache):
                    continue
                tokens_to_fetch.append(tid)

        if not tokens_to_fetch:
            return

        fetched = 0
        empty = 0
        for token_id in tokens_to_fetch:
            try:
                # Use /price endpoint (lighter than /book, returns just the price)
                resp = requests.get(
                    f"{CLOB_API}/price",
                    params={"token_id": token_id, "side": "buy"},
                    timeout=2,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # /price returns {"price": "0.935"} or similar
                    price_val = data.get("price")
                    if price_val is not None:
                        p = float(price_val)
                        if p > 0:
                            self._clob_price_cache[token_id] = p
                            fetched += 1
                        else:
                            empty += 1
                    else:
                        empty += 1
                elif resp.status_code == 429:
                    print(f"[MOMENTUM] CLOB REST rate limited after {fetched} fetches", flush=True)
                    break
            except Exception:
                pass

        print(f"[MOMENTUM] CLOB REST: {fetched} prices, {empty} empty "
              f"({len(tokens_to_fetch)} tokens queried)", flush=True)

    def _check_5m_boundary(self) -> list[dict]:
        """Fast targeted discovery when a new 5m epoch starts.

        Instead of waiting up to 30s for the next full discovery cycle,
        this does ~8 REST calls (4 coins × 2 slug variants) to grab
        the brand-new 5m markets right as they appear.
        """
        now_ts = int(time.time())
        current_epoch = (now_ts // 300) * 300

        if current_epoch <= self._last_5m_epoch:
            return []  # same epoch, nothing new

        self._last_5m_epoch = current_epoch
        new_markets = []

        for coin_abbr in CRYPTO_COINS:
            coin_full = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
            # The new market uses the current epoch timestamp
            slug_variants = [f"{coin_full}-updown-5m-{current_epoch}"]
            if coin_abbr != coin_full:
                slug_variants.append(f"{coin_abbr}-updown-5m-{current_epoch}")

            for event_slug in slug_variants:
                try:
                    resp = requests.get(
                        f"{GAMMA_API}/events",
                        params={"slug": event_slug},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        events_list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                        for event in events_list:
                            if not isinstance(event, dict):
                                continue
                            for mkt in event.get("markets", []):
                                parsed = _parse_market(mkt)
                                if parsed:
                                    new_markets.append(parsed)
                            if "conditionId" in event:
                                parsed = _parse_market(event)
                                if parsed:
                                    new_markets.append(parsed)
                except Exception:
                    pass

        if new_markets:
            print(f"[MOMENTUM] 5m boundary: grabbed {len(new_markets)} new markets "
                  f"at epoch {current_epoch}", flush=True)

            # Merge into cache (dedup by condition_id)
            existing_cids = {m["condition_id"] for m in self._cached_markets}
            for m in new_markets:
                if m["condition_id"] not in existing_cids:
                    self._cached_markets.append(m)
                    existing_cids.add(m["condition_id"])

            # Subscribe new tokens to WebSocket
            if self.ws:
                new_token_ids = []
                for m in new_markets:
                    new_token_ids.extend(m.get("token_ids", []))
                if new_token_ids:
                    try:
                        self.ws.subscribe(new_token_ids)
                    except Exception:
                        pass

        return new_markets

    def stop(self):
        """Clean up."""
        if self.ws:
            try:
                self.ws.stop()
            except Exception:
                pass
            self.ws = None

    def scan_and_trade(self) -> int:
        """Main loop iteration: discover markets, check prices, enter trades.

        Returns number of trades entered this cycle.
        """
        self.scans_completed += 1
        self._refresh_ws_tokens()

        # Fast 5m boundary check — grab new markets immediately when
        # a new 5-minute epoch starts (only ~8 REST calls)
        self._check_5m_boundary()

        # Use cached markets for fast WS-driven price checks.
        # Only re-discover via REST every _market_discovery_interval seconds.
        now = time.time()
        if now - self._last_market_discovery >= self._market_discovery_interval or not self._cached_markets:
            prev_count = len(self._cached_markets)
            self._cached_markets = discover_active_markets()
            self._last_market_discovery = now

            # Force WS resubscription when markets change so new tokens
            # get live prices immediately instead of waiting 30s
            if len(self._cached_markets) != prev_count:
                self._refresh_ws_tokens(force=True)

        markets = self._cached_markets
        if not markets:
            return 0

        # Fetch real-time CLOB prices periodically (not every 1s scan cycle).
        # 31 REST calls at 2s timeout = ~6s worst case, so run every 10s.
        if now - self._last_clob_fetch >= self._clob_fetch_interval:
            self._fetch_clob_prices_batch(markets)
            self._last_clob_fetch = now

        # Diagnostic: log price source stats every 30 scans (~30s)
        if self.scans_completed % 30 == 1:
            ws_direct = 0
            ws_mapped = 0
            clob_count = 0
            gamma_count = 0
            for m in markets:
                for tid in m.get("token_ids", []):
                    if self.ws:
                        _, ask = self.ws.get_best_prices(tid)
                        if ask is not None:
                            ws_direct += 1
                            continue
                    ws_id = self._market_token_to_ws.get(tid)
                    if ws_id and ws_id in self._ws_price_cache:
                        ws_mapped += 1
                    elif tid in self._ws_price_cache:
                        ws_direct += 1
                    elif tid in self._clob_price_cache:
                        clob_count += 1
                    else:
                        gamma_count += 1
            total = ws_direct + ws_mapped + clob_count + gamma_count
            print(f"[MOMENTUM] Price sources: ws_direct={ws_direct} ws_mapped={ws_mapped} "
                  f"clob_rest={clob_count} gamma={gamma_count} | "
                  f"ws_cache={len(self._ws_price_cache)} map={len(self._ws_to_market_token)} "
                  f"(total {total} tokens)", flush=True)

        # Every 30s: dump prices for markets above 60¢ (tradeable range)
        if self.scans_completed % 30 == 1:
            price_lines = []
            for m in markets:
                prices_raw = m.get("prices", [])
                if len(prices_raw) == 2 and prices_raw[0] in (0.0, 1.0) and prices_raw[1] in (0.0, 1.0):
                    continue
                for oi in range(min(2, len(m.get("token_ids", [])))):
                    tid = m["token_ids"][oi]
                    lp = self.get_live_price(tid)
                    gp = m["prices"][oi] if oi < len(m.get("prices", [])) else None
                    outcome = m["outcomes"][oi] if oi < len(m.get("outcomes", [])) else "?"
                    src = "live" if lp is not None else "gamma"
                    p = lp if lp is not None else gp
                    if p is not None and p > 0.60:
                        gp_str = f" gamma={gp*100:.1f}¢" if gp is not None else ""
                        price_lines.append(
                            f"  {m['coin'].upper()}_{m['interval']} {outcome}: "
                            f"{p*100:.1f}¢ ({src}){gp_str}"
                        )
            if price_lines:
                print(f"[MOMENTUM] Prices >60¢ ({len(price_lines)}):", flush=True)
                for line in price_lines[:16]:
                    print(line, flush=True)

        entered = 0

        for market in markets:
            coin = market["coin"]
            condition_id = market["condition_id"]
            slug = market["slug"]
            question = market["question"]

            # Per-coin pause
            if coin in self.paused_coins:
                continue

            # --- GUARD: Market must still be open ---
            minutes_left = market.get("minutes_until_close")
            if minutes_left is not None and minutes_left < MIN_MINUTES_BEFORE_CLOSE:
                # Market is closed or about to close — skip
                continue

            # Build a short label for rejection logging
            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # Check each side (outcome 0 and 1)
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                other_token_id = market["token_ids"][1 - oi]
                gamma_price = market["prices"][oi]

                # Get best available price (WS → WS cache → CLOB REST → Gamma)
                live_price = self.get_live_price(token_id)
                if live_price is not None:
                    price = live_price
                    # Determine source for logging
                    _price_src = "gamma"  # default
                    if self.ws:
                        _, ws_ask = self.ws.get_best_prices(token_id)
                        if ws_ask is not None:
                            _price_src = "ws"
                        elif token_id in self._ws_price_cache or self._market_token_to_ws.get(token_id) in self._ws_price_cache:
                            _price_src = "ws_cache"
                        elif token_id in self._clob_price_cache:
                            _price_src = "clob_rest"
                else:
                    price = gamma_price
                    _price_src = "gamma"

                # --- FILTER: Price must be in range ---
                # Use per-interval brackets if defined, else global min/max
                interval = market.get("interval", "")
                if interval in self.interval_price_brackets:
                    brackets = self.interval_price_brackets[interval]
                    in_bracket = any(lo <= price < hi for lo, hi in brackets)
                    if not in_bracket:
                        continue
                else:
                    if price < self.min_entry_price or price > self.max_entry_price:
                        continue

                # --- Price qualifies! Log that we're evaluating this candidate ---
                print(f"[MOMENTUM] CANDIDATE {_mkt_label} {outcome} @ {price*100:.1f}¢ ({_price_src})", flush=True)

                # Key by (condition_id, token_id) — stable across API ordering changes
                market_key = (condition_id, token_id)

                # --- GUARD: No opposite side ---
                # Check if we already hold the OTHER token on this condition_id
                opposite_key = (condition_id, other_token_id)
                if opposite_key in self.entered_markets:
                    self.trades_skipped += 1
                    print(f"  REJECT opposite_held: already hold other side of {_mkt_label}", flush=True)
                    continue

                # --- GUARD: Max entries per market ---
                if market_key in self.market_entry_count:
                    if self.market_entry_count[market_key] >= self.max_entries_per_market:
                        print(f"  REJECT max_entries: {self.market_entry_count[market_key]}/{self.max_entries_per_market} for {_mkt_label} {outcome}", flush=True)
                        continue

                # --- GUARD: Upward-only re-entry ---
                # Key difference: compare against LAST buy price, not first.
                # With a LIVE price source (ws/clob_rest), allow re-entry at
                # the same price since it's a fresh observation. With stale
                # gamma prices, require strictly higher to avoid spamming
                # the same static price every scan cycle.
                if market_key in self.entered_markets:
                    last_buy_price = self.entered_markets[market_key]
                    if _price_src == "gamma":
                        # Stale source: require strictly higher
                        if price <= last_buy_price:
                            continue  # silent — this fires every cycle
                    else:
                        # Live source: require higher (not equal — equal means
                        # the market hasn't moved, no new signal)
                        if price <= last_buy_price:
                            continue
                    print(f"[MOMENTUM] Re-entry OK ({coin.upper()} {outcome}): "
                          f"price {price*100:.1f}¢ > last buy {last_buy_price*100:.1f}¢ ({_price_src})", flush=True)

                # --- ENTER THE TRADE ---
                trade_amount = self.coin_bet_amounts.get(coin, self.bet_amount)
                title = (question or slug)[:50]

                print(f"\n[MOMENTUM] ENTERING {coin.upper()} {outcome} @ {price*100:.1f}¢", flush=True)
                print(f"           Market: {title}", flush=True)
                print(f"           Interval: {market['interval']}", flush=True)
                print(f"           Amount: ${trade_amount:.2f}", flush=True)

                trade_record = {
                    "id": f"momentum_{condition_id[:12]}_{oi}_{int(time.time())}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": title,
                    "slug": slug,
                    "outcome": outcome,
                    "outcome_index": oi,
                    "side": "BUY",
                    "amount": trade_amount,
                    "coin": coin,
                    "price": price,
                    "interval": market["interval"],
                    "source": "momentum",
                }

                if self.dry_run:
                    print(f"           DRY RUN - would buy @ {price*100:.1f}¢", flush=True)
                    trade_record["status"] = "dry_run"
                    entered += 1
                else:
                    buffer = PRICE_BUFFER_BPS / 10000
                    max_price = min(price * (1 + buffer), 0.99)
                    fill = place_bet(self.client, token_id, trade_amount, max_price=max_price)
                    if fill.get("success"):
                        if fill.get("fill_price"):
                            price = fill["fill_price"]
                            trade_record["price"] = price
                        print(f"           EXECUTED @ {price*100:.1f}¢", flush=True)
                        trade_record["status"] = "filled"
                        entered += 1
                        self.total_spent += trade_amount
                    else:
                        print(f"           FAILED!", flush=True)
                        trade_record["status"] = "failed"

                self.trade_history.append(trade_record)
                _log_trade(trade_record["status"], {
                    "id": trade_record["id"],
                    "coin": coin,
                    "interval": market["interval"],
                    "outcome": outcome,
                    "price": price,
                    "amount": trade_amount,
                    "slug": slug,
                    "market": title,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "minutes_until_close": market.get("minutes_until_close"),
                })

                if trade_record["status"] in ("filled", "dry_run"):
                    # Update last buy price (NOT first — this is the key difference)
                    self.entered_markets[market_key] = price
                    self.market_entry_count[market_key] = self.market_entry_count.get(market_key, 0) + 1

                    # Save position
                    position = {
                        "id": trade_record["id"],
                        "timestamp": trade_record["timestamp"],
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "outcome_index": oi,
                        "outcome": outcome,
                        "market": title,
                        "slug": slug,
                        "interval": market.get("interval", ""),
                        "entry_price": price,
                        "amount": trade_amount,
                        "potential_payout": trade_amount / price if price > 0 else 0,
                        "dry_run": self.dry_run,
                        "source": "momentum",
                    }
                    self.positions["open"].append(position)

                    # Deduct balance
                    try:
                        stats = self.positions["stats"]
                        stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - trade_amount
                        open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
                        stats.setdefault("balance_history", []).append({
                            "timestamp": trade_record["timestamp"],
                            "balance": stats["balance"],
                            "pnl": stats.get("total_pnl", 0.0),
                            "equity": stats["balance"] + open_staked,
                            "event": "momentum_trade",
                            "detail": f"{coin.upper()} {outcome} {title[:30]}",
                        })
                    except Exception:
                        pass

                    save_positions(self.positions)
                    print(f"           Position saved. Balance: ${self.positions['stats'].get('balance', 0):.2f}", flush=True)

                    self.trades_entered += 1

                    # Callback
                    if self.on_trade:
                        try:
                            self.on_trade(trade_record)
                        except Exception as e:
                            print(f"[MOMENTUM] Callback error: {e}", flush=True)

        return entered

    def check_resolutions(self):
        """Check if any momentum-sourced open positions have resolved.

        Delegates to the same resolution logic as the copy trader.
        """
        from copy_trader import get_market_resolution

        now = time.time()
        if now - self.last_resolution_check < self.resolution_check_interval:
            return

        self.last_resolution_check = now

        open_positions = self.positions.get("open", [])
        # Only check positions from this engine
        momentum_positions = [p for p in open_positions if p.get("source") == "momentum"]
        if not momentum_positions:
            return

        resolved_count = 0

        for position in momentum_positions[:]:
            condition_id = position.get("condition_id", "")
            slug = position.get("slug", "")
            token_id = position.get("token_id", "")
            our_outcome = position.get("outcome")

            if not token_id and not condition_id and not slug:
                continue

            result = get_market_resolution(
                condition_id=condition_id,
                slug=slug,
                token_id=token_id,
                our_outcome=our_outcome,
            )

            if not result or not result.get("resolved"):
                # Fallback: use live WebSocket price for resolution
                # If price has hit an extreme (≤0.02 or ≥0.98), the market
                # has effectively settled even if the API hasn't flagged it.
                # This is critical for 60m markets which were never in the
                # copy trader and may not resolve via target-trader checks.
                live_price = self.get_live_price(token_id) if token_id else None
                if live_price is not None and (live_price >= 0.98 or live_price <= 0.02):
                    our_token_won = live_price >= 0.98
                    print(f"[MOMENTUM] Price-based resolution: {position['market'][:30]} "
                          f"| price={live_price:.4f} → {'WIN' if our_token_won else 'LOSS'}", flush=True)
                    result = {
                        "resolved": True,
                        "our_token_won": our_token_won,
                        "winning_outcome": our_outcome if our_token_won else None,
                    }
                else:
                    # Track unresolved attempts with timestamps for smarter retry
                    attempts = position.get("_resolve_attempts", 0) + 1
                    position["_resolve_attempts"] = attempts
                    if attempts == 1:
                        position["_first_resolve_check"] = time.time()
                    continue

            entry_price = position.get("entry_price", 0)
            amount = position.get("amount", 0)
            our_index = position.get("outcome_index")

            won = None
            winning_outcome = result.get("winning_outcome")
            winning_index = result.get("winning_index")

            # --- Determine win/loss using multiple signals ---
            # Priority 1: Direct token_id comparison from CLOB API
            if "our_token_won" in result and result["our_token_won"] is not None:
                won = result["our_token_won"]

            # Priority 2: Outcome name comparison
            if won is None and winning_outcome and our_outcome:
                our_norm = our_outcome.lower().strip()
                win_norm = winning_outcome.lower().strip()
                if our_norm == win_norm or our_norm.startswith(win_norm) or win_norm.startswith(our_norm):
                    won = True
                else:
                    won = False

            # Priority 3: Outcome index comparison
            if won is None and winning_index is not None and our_index is not None:
                won = (winning_index == our_index)

            # Safety net: if CLOB token comparison said LOSS but outcome name
            # says WIN (e.g. token_id formatting mismatch), trust the name match.
            # A genuine loss can't have matching outcome names.
            if won is False and winning_outcome and our_outcome:
                our_norm = our_outcome.lower().strip()
                win_norm = winning_outcome.lower().strip()
                if our_norm == win_norm or our_norm.startswith(win_norm) or win_norm.startswith(our_norm):
                    print(f"[MOMENTUM] OVERRIDE: token_id said LOSS but outcome name matches "
                          f"(ours={our_outcome}, winner={winning_outcome}). Correcting to WIN.", flush=True)
                    won = True

            if won is True:
                if entry_price > 0:
                    payout = amount / entry_price
                    pnl = payout - amount
                else:
                    pnl = amount * 3
                position["result"] = "WIN"
                position["pnl"] = pnl
                self.positions["stats"]["wins"] = self.positions["stats"].get("wins", 0) + 1
                print(f"[MOMENTUM] WIN: {position['market'][:30]} | +${pnl:.2f}", flush=True)
            elif won is False:
                pnl = -amount
                position["result"] = "LOSS"
                position["pnl"] = pnl
                self.positions["stats"]["losses"] = self.positions["stats"].get("losses", 0) + 1
                print(f"[MOMENTUM] LOSS: {position['market'][:30]} | -${amount:.2f}", flush=True)
            else:
                # Interval-aware retry: 60m markets need much longer to settle
                # than 5m/15m. Use time-based threshold instead of fixed attempts.
                first_check = position.get("_first_resolve_check", time.time())
                elapsed_mins = (time.time() - first_check) / 60
                # Max wait: 15 min for 5m, 30 min for 15m, 120 min for 60m
                interval = position.get("interval", "")
                if interval == "5m":
                    max_wait_mins = 15
                elif interval == "15m":
                    max_wait_mins = 30
                else:
                    max_wait_mins = 120  # 60m markets can take a while
                if elapsed_mins < max_wait_mins:
                    continue
                pnl = 0
                position["result"] = "UNKNOWN"
                position["pnl"] = 0
                print(f"[MOMENTUM] UNKNOWN after {elapsed_mins:.0f}min: {position['market'][:30]}", flush=True)

            # Update totals
            self.positions["stats"]["total_pnl"] = self.positions["stats"].get("total_pnl", 0) + pnl

            # Update balance
            try:
                stats = self.positions["stats"]
                bal = stats.get("balance", ALGO_STARTING_BALANCE)
                if won is True:
                    payout = amount / entry_price if entry_price > 0 else amount
                    bal += payout
                stats["balance"] = bal
                open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []) if p is not position)
                event_type = "win" if won is True else "loss" if won is False else "resolved"
                stats.setdefault("balance_history", []).append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "balance": bal,
                    "pnl": stats.get("total_pnl", 0.0),
                    "equity": bal + open_staked,
                    "event": f"momentum_{event_type}",
                    "detail": f"{position.get('outcome', '?')} {position.get('market', '?')[:30]}",
                })
            except Exception:
                pass

            # Move to resolved
            position["resolved_at"] = datetime.now(timezone.utc).isoformat()
            position["winning_outcome"] = winning_outcome
            position["won"] = won
            _log_trade("resolved", {
                "id": position.get("id", ""),
                "coin": position.get("slug", "").split("-")[0] if position.get("slug") else "",
                "interval": position.get("interval", ""),
                "outcome": position.get("outcome", ""),
                "entry_price": entry_price,
                "amount": amount,
                "result": position.get("result", ""),
                "pnl": pnl,
                "won": won,
                "winning_outcome": winning_outcome,
                "slug": position.get("slug", ""),
                "market": position.get("market", ""),
                "condition_id": position.get("condition_id", ""),
                "entered_at": position.get("timestamp", ""),
            })
            self.positions["open"].remove(position)
            self.positions["resolved"].append(position)
            resolved_count += 1

            if self.on_resolution:
                try:
                    self.on_resolution(position)
                except Exception as e:
                    print(f"[MOMENTUM] Resolution callback error: {e}", flush=True)

        if resolved_count > 0:
            save_positions(self.positions)
            stats = self.positions["stats"]
            print(f"[MOMENTUM] {resolved_count} resolved. "
                  f"Record: {stats['wins']}W/{stats['losses']}L, "
                  f"PnL: ${stats['total_pnl']:+.2f}", flush=True)

    def _compute_coin_roi(self, open_positions: list, resolved_positions: list) -> dict:
        """Compute per-coin W/L/PnL/ROI from momentum position data."""
        coin_data: dict = {}
        all_coins = set(self.coin_bet_amounts.keys())

        for pos in resolved_positions:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            all_coins.add(coin)
            if coin not in coin_data:
                coin_data[coin] = {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []}
            amount = pos.get("amount", 0)
            pnl = pos.get("pnl", 0) or 0
            result = pos.get("result", "")
            coin_data[coin]["deployed"] += amount
            coin_data[coin]["pnl"] += pnl
            if result == "WIN":
                coin_data[coin]["wins"] += 1
                coin_data[coin]["results"].append("W")
            elif result == "LOSS":
                coin_data[coin]["losses"] += 1
                coin_data[coin]["results"].append("L")

        for pos in open_positions:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            all_coins.add(coin)
            if coin not in coin_data:
                coin_data[coin] = {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []}
            coin_data[coin]["open"] += 1
            coin_data[coin]["deployed"] += pos.get("amount", 0)

        result = {}
        for coin in all_coins:
            d = coin_data.get(coin, {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []})
            total = d["wins"] + d["losses"]
            win_rate = (d["wins"] / total * 100) if total > 0 else 0
            roi = (d["pnl"] / d["deployed"] * 100) if d["deployed"] > 0 else 0

            streak = 0
            streak_type = ""
            for r in reversed(d["results"]):
                if not streak_type:
                    streak_type = r
                    streak = 1
                elif r == streak_type:
                    streak += 1
                else:
                    break

            result[coin] = {
                "wins": d["wins"], "losses": d["losses"],
                "win_rate": round(win_rate, 1),
                "pnl": round(d["pnl"], 2),
                "deployed": round(d["deployed"], 2),
                "roi": round(roi, 1),
                "open": d["open"],
                "streak": streak, "streak_type": streak_type,
            }
        return result

    def get_stats(self) -> dict:
        """Get current stats for dashboard."""
        stats = self.positions.get("stats", {})
        open_positions = list(self.positions.get("open", []))
        # Only count momentum-sourced positions
        momentum_open = [p for p in open_positions if p.get("source") == "momentum"]
        momentum_resolved = [p for p in self.positions.get("resolved", []) if p.get("source") == "momentum"]

        # Compute momentum-specific P&L from resolved positions
        m_wins = 0
        m_losses = 0
        m_total_pnl = 0.0
        for p in momentum_resolved:
            pnl = p.get("pnl", 0)
            won = p.get("won")
            if won is True:
                m_wins += 1
            elif won is False:
                m_losses += 1
            m_total_pnl += float(pnl) if pnl else 0.0

        m_total = m_wins + m_losses
        m_win_rate = (m_wins / m_total * 100) if m_total > 0 else 0.0

        # Momentum-only staked amount
        m_open_staked = sum(float(p.get("amount", 0)) for p in momentum_open)

        # Per-coin performance breakdown
        coin_roi = self._compute_coin_roi(momentum_open, momentum_resolved)

        # Group open positions by coin for detail drill-down
        open_by_coin: dict = {}
        for pos in momentum_open:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            if coin not in open_by_coin:
                open_by_coin[coin] = []
            open_by_coin[coin].append({
                "market": pos.get("market", "")[:60],
                "outcome": pos.get("outcome", ""),
                "entry_price": float(pos.get("entry_price", 0) or 0),
                "amount": float(pos.get("amount", 0) or 0),
                "timestamp": pos.get("timestamp", ""),
            })

        return {
            "trades_entered": self.trades_entered,
            "trades_skipped": self.trades_skipped,
            "total_spent": self.total_spent,
            "scans_completed": self.scans_completed,
            "open_positions": len(momentum_open),
            "resolved_positions": len(momentum_resolved),
            "wins": m_wins,
            "losses": m_losses,
            "total_pnl": m_total_pnl,
            "win_rate": m_win_rate,
            "open_staked": m_open_staked,
            "coin_roi": coin_roi,
            "open_by_coin": open_by_coin,
            "min_entry_price": self.min_entry_price,
            "max_entry_price": self.max_entry_price,
            "max_entries_per_market": self.max_entries_per_market,
            "interval_price_brackets": {
                k: [{"min": lo, "max": hi} for lo, hi in v]
                for k, v in self.interval_price_brackets.items()
            },
            "bet_amount": self.bet_amount,
            "coin_bet_amounts": {k: v for k, v in self.coin_bet_amounts.items()},
            "paused_coins": list(self.paused_coins),
            "dry_run": self.dry_run,
            "active_entries": {
                f"{cid[:12]}..._t{tid[-8:]}": f"{price*100:.1f}¢"
                for (cid, tid), price in self.entered_markets.items()
            },
        }
