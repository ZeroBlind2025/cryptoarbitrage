"""Minimal Polymarket client for the BTC 5m updown market.

Scope is deliberately narrow:

  * ``discover_btc_updown_5m()`` -- given a unix timestamp, locate the
    Polymarket ``bitcoin-updown-5m-<resolve_ts>`` event and return the
    YES-UP and YES-DOWN CLOB token ids, the resolve timestamp, and
    the slug. No coupling to ``hf_engine``.
  * ``get_top_of_book(token_id)`` -- CLOB order-book call, returns
    ``(bid, ask, bid_size, ask_size)``. Ask is what a paper BUY would
    pay at the top of book; if either side is empty, returns ``None``
    for that field.

Both endpoints are publicly readable, no auth / signing required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("candle_reaction.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Slug root candidates (Polymarket ships both short and long forms).
_SLUG_ROOTS = ("btc-updown-5m", "bitcoin-updown-5m")

WINDOW_SEC = 300  # 5m


@dataclass(frozen=True)
class PolyMarket:
    slug: str
    condition_id: str
    yes_up_token_id: str
    yes_down_token_id: str
    resolve_ts: int  # unix seconds the market settles


@dataclass(frozen=True)
class TopOfBook:
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]


def _bucket_ts(now_ts: int, offset_windows: int = 0) -> int:
    """Return the start of the 5m bucket shifted by ``offset_windows``."""
    base = (now_ts // WINDOW_SEC) * WINDOW_SEC
    return base + offset_windows * WINDOW_SEC


def discover_btc_updown_5m(
    now_ts: int,
    session: Optional[requests.Session] = None,
) -> Optional[PolyMarket]:
    """Find the BTC 5m updown market that resolves at the *next* 5m close.

    Polymarket names these markets by the settlement timestamp
    (e.g. ``bitcoin-updown-5m-1772397000``). We try the next bucket
    first, then the current bucket as a fallback for edge-of-interval
    cases.
    """
    sess = session or requests.Session()

    candidates: list[str] = []
    for root in _SLUG_ROOTS:
        for offset in (1, 0, 2):  # next bucket, current, two-ahead
            candidates.append(f"{root}-{_bucket_ts(now_ts, offset)}")

    for slug in candidates:
        market = _fetch_event(sess, slug)
        if market is not None:
            return market
    return None


def _fetch_event(sess: requests.Session, slug: str) -> Optional[PolyMarket]:
    try:
        r = sess.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=6)
    except requests.RequestException as e:
        log.debug("gamma slug %s network error: %s", slug, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for ev in events:
        for mkt in ev.get("markets", []) or []:
            parsed = _parse_market(mkt, slug)
            if parsed is not None:
                return parsed
    return None


def _parse_market(mkt: dict, slug: str) -> Optional[PolyMarket]:
    """Pull token ids + resolve ts out of a Gamma market dict."""
    token_ids = _decode_list(mkt.get("clobTokenIds") or mkt.get("tokens"))
    if not token_ids or len(token_ids) < 2:
        return None

    # `outcomes` carries the canonical label order that matches both
    # clobTokenIds and outcomePrices. Pick the UP-labelled index
    # explicitly; fall back to [0] = UP when labels are missing.
    outcomes = _decode_list(mkt.get("outcomes"))
    up_idx = _up_outcome_index(outcomes)
    down_idx = 1 - up_idx

    yes_up_id = str(token_ids[up_idx])
    yes_down_id = str(token_ids[down_idx])

    condition_id = str(mkt.get("conditionId") or mkt.get("id") or "")

    # Resolution timestamp: prefer endDateIso -> endDate -> slug tail.
    resolve_ts = _parse_resolve_ts(mkt, slug)
    if resolve_ts is None:
        return None

    return PolyMarket(
        slug=slug,
        condition_id=condition_id,
        yes_up_token_id=yes_up_id,
        yes_down_token_id=yes_down_id,
        resolve_ts=resolve_ts,
    )


def _decode_list(value):
    """Gamma often ships list fields as JSON-encoded strings; normalise."""
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


_UP_WORDS = {"up", "yes", "higher", "above"}


def _up_outcome_index(outcomes) -> int:
    if not outcomes:
        return 0
    for i, name in enumerate(outcomes[:2]):
        if isinstance(name, str) and name.strip().lower() in _UP_WORDS:
            return i
    return 0


def _parse_resolve_ts(mkt: dict, slug: str) -> Optional[int]:
    import datetime as _dt

    iso = mkt.get("endDateIso") or mkt.get("endDate")
    if iso:
        try:
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            return int(_dt.datetime.fromisoformat(iso).timestamp())
        except Exception:
            pass

    tail = slug.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def get_top_of_book(
    token_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[TopOfBook]:
    """Return best bid/ask for a CLOB token. Prices are in dollars (0..1)."""
    sess = session or requests.Session()
    try:
        r = sess.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=6)
    except requests.RequestException as e:
        log.debug("clob book %s network error: %s", token_id, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid = _top(bids, pick_max=True)
    ask = _top(asks, pick_max=False)
    return TopOfBook(
        bid=bid[0] if bid else None,
        ask=ask[0] if ask else None,
        bid_size=bid[1] if bid else None,
        ask_size=ask[1] if ask else None,
    )


def _top(levels: list, pick_max: bool) -> Optional[tuple[float, float]]:
    """Return (price, size) of the best level. CLOB returns unordered lists."""
    if not levels:
        return None
    best: Optional[tuple[float, float]] = None
    for lvl in levels:
        try:
            p = float(lvl.get("price"))
            s = float(lvl.get("size"))
        except (TypeError, ValueError):
            continue
        if best is None or (pick_max and p > best[0]) or (not pick_max and p < best[0]):
            best = (p, s)
    return best


def get_resolution(
    slug: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Return 'UP', 'DOWN', 'VOID', or None if the market hasn't resolved.

    Polymarket settles the BTC updown-5m markets off the Chainlink
    BTC/USD feed. Reading the resolved outcome direct from Gamma is
    equivalent to reading the Chainlink settlement without replicating
    their resolution rule locally.
    """
    sess = session or requests.Session()
    try:
        r = sess.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=6)
    except requests.RequestException as e:
        log.debug("gamma resolve %s network error: %s", slug, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None

    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for ev in events:
        for mkt in ev.get("markets", []) or []:
            res = _parse_resolution(mkt)
            if res is not None:
                return res
    return None


def _parse_resolution(mkt: dict) -> Optional[str]:
    """Read outcomePrices to determine if/how the market resolved.

    On an unresolved market the prices are live book levels (e.g. 0.45/0.55).
    Once Polymarket settles, they collapse to [1.0, 0.0] or [0.0, 1.0].
    """
    prices = _decode_list(mkt.get("outcomePrices"))
    if not prices or len(prices) < 2:
        return None
    outcomes = _decode_list(mkt.get("outcomes"))
    up_idx = _up_outcome_index(outcomes)
    down_idx = 1 - up_idx
    try:
        up_price = float(prices[up_idx])
        down_price = float(prices[down_idx])
    except (TypeError, ValueError):
        return None

    # Definitive-resolution threshold: one side is ~1 and the other ~0.
    if up_price >= 0.99 and down_price <= 0.01:
        return "UP"
    if down_price >= 0.99 and up_price <= 0.01:
        return "DOWN"

    # Some markets void to a 50/50 split.
    closed = bool(mkt.get("closed"))
    if closed and abs(up_price - 0.5) < 0.01 and abs(down_price - 0.5) < 0.01:
        return "VOID"
    return None
