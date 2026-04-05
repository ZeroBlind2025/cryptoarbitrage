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
    PROBE_AMOUNT,
    PRICE_BUFFER_BPS,
    ALGO_STARTING_BALANCE,
    CRYPTO_SLUGS,
    STOP_LOSS_PCT,
    PRICE_BUFFER_BPS,
    ARB_HEDGE_MIN_ENTRY,
    detect_coin,
    get_clob_client,
    place_bet,
    place_sell,
    calc_arb_hedge,
    load_positions,
    save_positions,
    get_active_crypto_tokens,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

# Minimum price to enter a market (88 cents = 0.88)
MIN_ENTRY_PRICE = float(os.getenv("MOMENTUM_MIN_ENTRY_PRICE", "0.88"))
MOMENTUM_HEDGE_ENABLE = os.getenv("MOMENTUM_HEDGE_ENABLE", "true").lower() == "true"
MOMENTUM_HEDGE_PCT = float(os.getenv("MOMENTUM_HEDGE_PCT", "5"))  # Gap in cents (e.g. 5 = 5¢)

MOMENTUM_5M_MARKET = os.getenv("MOMENTUM_5M_MARKET", "true").lower() == "true"
MOMENTUM_15M_MARKET = os.getenv("MOMENTUM_15M_MARKET", "false").lower() == "true"

# Maximum price to enter — 98.9¢ cap filters out 99¢+ "last second" entries
# that linger at market close and likely wouldn't fill in live trading
MAX_ENTRY_PRICE = float(os.getenv("MOMENTUM_MAX_ENTRY_PRICE", "0.989"))

# ---------------------------------------------------------------------------
# LIMIT SIMULATION MODE — mimics a live limit order in dry run
# When enabled, entry only triggers when the live WS price crosses the
# threshold. The position is recorded at the threshold price (= simulated
# limit fill price), not the live price. This simulates placing a
# $5 @ 75¢ Up limit order that fills as the market crosses 75¢.
# ---------------------------------------------------------------------------
LIMIT_SIM_ENABLED = os.getenv("MOMENTUM_LIMIT_SIM", "false").lower() == "true"
LIMIT_SIM_PRICE = float(os.getenv("MOMENTUM_LIMIT_SIM_PRICE", "0.75"))
LIMIT_SIM_LOT = float(os.getenv("MOMENTUM_LIMIT_SIM_LOT", "5.0"))
LIMIT_SIM_SIDE = os.getenv("MOMENTUM_LIMIT_SIM_SIDE", "Up")
LIMIT_SIM_SETTLE_SECS = float(os.getenv("MOMENTUM_LIMIT_SIM_SETTLE", "10"))  # skip first N seconds after market open

# ---------------------------------------------------------------------------
# Per-interval entry price brackets (data-driven from bracket analysis)
# Each interval maps to a list of (min, max) tuples.  A price must fall in
# at least ONE bracket to qualify.  Intervals not listed here use the global
# MIN_ENTRY_PRICE / MAX_ENTRY_PRICE range.
#
# 5m  bracket: 85-<98.9¢ (uber conservative)
# ---------------------------------------------------------------------------
INTERVAL_PRICE_BRACKETS: dict[str, list[tuple[float, float]]] = {
    "5m":  [(MIN_ENTRY_PRICE, 0.989)],
}

# How often to poll prices (seconds)
POLL_INTERVAL = int(os.getenv("MOMENTUM_POLL_INTERVAL", "1"))

# Max entries per market (same as copy trader default)
MAX_ENTRIES_PER_MARKET = int(os.getenv("MOMENTUM_MAX_ENTRIES_PER_MARKET", "2"))

# Cooldown between re-entries into the same market (seconds).
# Prevents rapid-fire follow-ups when price ticks up within the same scan cycle.
FOLLOW_UP_COOLDOWN = int(os.getenv("MOMENTUM_FOLLOW_UP_COOLDOWN", "30"))
FOLLOW_UP_COOLDOWN_15M = int(os.getenv("MOMENTUM_15m_COOLDOWN", "240"))  # 4 minutes for 15m markets
REENTRY_MIN_PRICE = float(os.getenv("MOMENTUM_REENTRY_MIN_PRICE", "0.899"))  # only re-enter above 89.9¢
UPGUARD_ROLLING = os.getenv("MOMENTUM_UPGUARD_ROLLING", "true").lower() == "true"  # compare vs last entry (True) or initial entry (False)
TAKE_PROFIT_PRICE = float(os.getenv("MOMENTUM_TAKE_PROFIT", "0.98"))  # sell when bid >= this price (0 = disabled)
OPPOSITE_SIDE_BLOCK = os.getenv("MOMENTUM_OPPOSITE_SIDE_BLOCK", "false").lower() == "true"  # block buying both sides

# Minimum minutes before market close to allow entry.
# Prevents placing trades after (or right at) the close time.
MIN_MINUTES_BEFORE_CLOSE = float(os.getenv("MOMENTUM_MIN_MINUTES_BEFORE_CLOSE", "1.0"))

# Maximum entry price — skip entries at or above this price (default 0.90 = 90¢).
# High-price entries win often but the tiny upside doesn't cover losses.
MAX_PRICE_ENTRY = float(os.getenv("MOMENTUM_MAX_PRICE_ENTRY", "0.90"))

# No-trade hours (ET).  Comma-separated list of hour starts (24h format).
# Trading pauses for 60 minutes starting at each listed hour.
# Default: 4,7,8,9 (4AM, 7AM, 8AM, 9AM ET — historically worst hours).
_NO_TRADE_HOURS_RAW = os.getenv("MOMENTUM_NO_TRADE_HOURS", "4,7,8,9")
NO_TRADE_HOURS: set[int] = set()
if _NO_TRADE_HOURS_RAW.strip():
    NO_TRADE_HOURS = {int(h.strip()) for h in _NO_TRADE_HOURS_RAW.split(",") if h.strip()}

# Allow DOWN bets.  Default FALSE — only take UP positions.
DOWN_BETS_ENABLED = os.getenv("MOMENTUM_DOWN_BETS_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Market entry delay — wait N seconds after a market opens before entering.
# Early-market prices are volatile and reversals are common.  By waiting,
# we only enter once the direction has stabilised.
#
#  5m market  → wait 180s (3 min)
# 15m market  → wait 540s (9 min)
# ---------------------------------------------------------------------------
MARKET_ENTRY_DELAY: dict[str, float] = {
    "5m":  float(os.getenv("MOMENTUM_5M_ENTRY_DELAY",  "180")) / 60,
    "15m": float(os.getenv("MOMENTUM_15M_ENTRY_DELAY", "540")) / 60,
}

# Interval durations in minutes (used to derive market start time from end time)
_INTERVAL_DURATION_MINUTES: dict[str, float] = {
    "5m": 5,
    "15m": 15,
    "60m": 60,
}


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
INTERVALS = [i for i in ["5m", "15m"] if
             (i == "5m" and MOMENTUM_5M_MARKET) or
             (i == "15m" and MOMENTUM_15M_MARKET)]

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
    end_date_parsed = None
    minutes_until_close = None
    if end_date_str:
        try:
            if "T" in str(end_date_str):
                end_date_parsed = datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
            else:
                end_date_parsed = datetime.strptime(end_date_str, "%Y-%m-%d")
                end_date_parsed = end_date_parsed.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            minutes_until_close = (end_date_parsed - now).total_seconds() / 60
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
        "end_date": end_date_parsed,  # stored for live recalculation
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

    Uses three strategies IN PARALLEL:
      0. Event slug lookup with computed timestamps (parallel batch)
      1. /events endpoint — broad listing with nested markets
      2. Timestamp-based slug search on /markets (parallel batch)
      3. slug_contains partial match searches (parallel batch)
      4. Broad /markets + /events fallback

    All HTTP calls are parallelized via ThreadPoolExecutor for speed.
    Previous sequential implementation took ~10s; parallel takes ~1-2s.

    Returns a list of market dicts with:
      - slug, question, condition_id
      - outcomes: ["Up", "Down"] or similar
      - token_ids: [up_token_id, down_token_id]
      - prices: [up_price, down_price]
      - coin: "btc", "eth", "sol", "xrp"
      - interval: "5m", "15m", "60m"
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    markets = []
    seen_conditions = set()
    _lock = threading.Lock()

    def _add_market(raw: dict):
        """Parse and add a market if valid and not seen (thread-safe)."""
        cid = raw.get("conditionId") or raw.get("condition_id") or ""
        with _lock:
            if cid in seen_conditions:
                return False
        m = _parse_market(raw)
        if m:
            with _lock:
                if m["condition_id"] in seen_conditions:
                    return False
                markets.append(m)
                seen_conditions.add(m["condition_id"])
            return True
        return False

    now_ts = int(time.time())

    # ================================================================
    # Build all slug-based requests upfront, then fire in parallel
    # ================================================================

    # Helper: fetch a single URL and return raw JSON
    def _fetch_json(url, params, timeout=10):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    # --- Strategy 0: Event slug lookups (parallel) ---
    # 4 coins × 2 intervals × 4 windows × ~2 variants = up to 64 URLs
    ts_slug_configs = [("5m", 300), ("15m", 900)]
    event_slug_coins = ["btc", "eth", "sol", "xrp"]

    strategy0_slugs = []  # list of event_slug strings
    for coin_abbr in event_slug_coins:
        coin_full = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
        for tag, window_secs in ts_slug_configs:
            base_ts = (now_ts // window_secs) * window_secs
            for ts in [base_ts + window_secs, base_ts, base_ts - window_secs, base_ts - 2 * window_secs]:
                strategy0_slugs.append(f"{coin_abbr}-updown-{tag}-{ts}")
                if coin_full != coin_abbr:
                    strategy0_slugs.append(f"{coin_full}-updown-{tag}-{ts}")

    # --- Strategy 2: Timestamp-based /markets slug lookups (parallel) ---
    strategy2_slugs = []
    for coin_abbr in CRYPTO_COINS:
        coin_name = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
        for tag, window_secs in ts_slug_configs:
            base_ts = (now_ts // window_secs) * window_secs
            for ts in [base_ts + window_secs, base_ts, base_ts - window_secs, base_ts - 2 * window_secs]:
                strategy2_slugs.append(f"{coin_name}-updown-{tag}-{ts}")
                if coin_abbr != coin_name:
                    strategy2_slugs.append(f"{coin_abbr}-updown-{tag}-{ts}")

    # --- Strategy 2b: slug_contains partial matches (parallel) ---
    slug_search_terms = [
        "updown-5m", "updown-15m",
        "btc-updown", "eth-updown", "sol-updown", "xrp-updown",
        "bitcoin-updown", "ethereum-updown", "solana-updown",
    ]

    # ================================================================
    # Fire ALL strategies in parallel via ThreadPoolExecutor
    # ================================================================
    event_slug_found = 0
    _slug_hits = 0
    _slug_misses = 0
    _slug_errors = 0
    _slug_miss_examples = []

    def _do_strategy0(event_slug):
        """Fetch one event slug from /events."""
        return ("s0", event_slug, _fetch_json(
            f"{GAMMA_API}/events", {"slug": event_slug}, timeout=10))

    def _do_strategy1_events():
        """Broad /events listing."""
        return ("s1_events", None, _fetch_json(
            f"{GAMMA_API}/events",
            {"active": "true", "closed": "false", "limit": 100},
            timeout=15))

    def _do_strategy2(slug_pattern):
        """Fetch one slug from /markets."""
        return ("s2", slug_pattern, _fetch_json(
            f"{GAMMA_API}/markets", {"slug": slug_pattern}, timeout=10))

    def _do_strategy2b(search_term):
        """slug_contains partial match on /markets."""
        return ("s2b", search_term, _fetch_json(
            f"{GAMMA_API}/markets",
            {"slug_contains": search_term, "active": "true",
             "closed": "false", "limit": 100},
            timeout=10))

    def _do_strategy3_markets():
        """Broad /markets search."""
        return ("s3_markets", None, _fetch_json(
            f"{GAMMA_API}/markets",
            {"active": "true", "closed": "false", "limit": 200},
            timeout=15))

    def _do_strategy3_events():
        """Broad /events search."""
        return ("s3_events", None, _fetch_json(
            f"{GAMMA_API}/events",
            {"active": "true", "closed": "false", "limit": 200},
            timeout=15))

    # Submit all work to the thread pool
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = []

        # Strategy 0: event slug lookups
        for slug in strategy0_slugs:
            futures.append(pool.submit(_do_strategy0, slug))

        # Strategy 1: broad events listing
        futures.append(pool.submit(_do_strategy1_events))

        # Strategy 2: /markets slug lookups
        for slug in strategy2_slugs:
            futures.append(pool.submit(_do_strategy2, slug))

        # Strategy 2b: slug_contains searches
        for term in slug_search_terms:
            futures.append(pool.submit(_do_strategy2b, term))

        # Strategy 3: broad /markets + /events
        futures.append(pool.submit(_do_strategy3_markets))
        futures.append(pool.submit(_do_strategy3_events))

        # Process results as they complete
        _ASSET_NAMES_LOWER = ["bitcoin", "ethereum", "solana", "xrp"]
        slug_contains_found = 0
        broad_found = 0

        for fut in as_completed(futures):
            strategy, label, data = fut.result()

            if data is None:
                if strategy == "s0":
                    _slug_errors += 1
                continue

            if strategy == "s0":
                # Event slug lookup result
                events_list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                slug_found_any = False
                for event in events_list:
                    if not isinstance(event, dict):
                        continue
                    for mkt in event.get("markets", []):
                        if _add_market(mkt):
                            event_slug_found += 1
                            slug_found_any = True
                    if "conditionId" in event:
                        if _add_market(event):
                            event_slug_found += 1
                            slug_found_any = True
                if slug_found_any or events_list:
                    _slug_hits += 1
                else:
                    _slug_misses += 1
                    if len(_slug_miss_examples) < 4:
                        _slug_miss_examples.append(label)

            elif strategy == "s1_events":
                # Broad events listing
                events = data if isinstance(data, list) else []
                events_checked = 0
                for event in events:
                    event_title = event.get("title", "")
                    event_slug = event.get("slug", "")
                    if not _is_crypto_updown_event(event_title, event_slug):
                        continue
                    events_checked += 1
                    for mkt in event.get("markets", []):
                        _add_market(mkt)
                if events_checked > 0:
                    print(f"[MOMENTUM] Events: checked {events_checked} crypto events "
                          f"from {len(events)} total", flush=True)

            elif strategy == "s2":
                # /markets slug lookup
                if isinstance(data, list):
                    for raw in data:
                        _add_market(raw)

            elif strategy == "s2b":
                # slug_contains partial match
                if isinstance(data, list):
                    for raw in data:
                        if _add_market(raw):
                            slug_contains_found += 1

            elif strategy == "s3_markets":
                # Broad /markets search
                if isinstance(data, list):
                    for raw in data:
                        question = raw.get("question") or ""
                        slug = (raw.get("slug") or "").lower()
                        q_lower = question.lower()
                        is_updown = ("up or down" in q_lower or "updown" in slug
                                     or "up-or-down" in slug)
                        is_crypto = any(a in q_lower or a in slug
                                       for a in _ASSET_NAMES_LOWER)
                        if is_updown and is_crypto:
                            if _add_market(raw):
                                broad_found += 1

            elif strategy == "s3_events":
                # Broad /events search
                events = data if isinstance(data, list) else []
                for event in events:
                    event_title = event.get("title", "")
                    event_slug = event.get("slug", "")
                    if not _is_crypto_updown_event(event_title, event_slug):
                        continue
                    for mkt in event.get("markets", []):
                        if _add_market(mkt):
                            broad_found += 1

    print(f"[MOMENTUM] Event slugs: found={event_slug_found} "
          f"hits={_slug_hits} misses={_slug_misses} errors={_slug_errors}",
          flush=True)
    if _slug_miss_examples:
        print(f"[MOMENTUM] Slug miss examples: {_slug_miss_examples}", flush=True)

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
        # Skip future markets that haven't started yet (negative age)
        # e.g. a 15m market with 21.8 minutes left hasn't opened yet
        interval = m.get("interval", "")
        duration = _INTERVAL_DURATION_MINUTES.get(interval)
        if duration and minutes_left is not None and minutes_left > duration:
            continue
        active_markets.append(m)

    if len(active_markets) < len(markets):
        print(f"[MOMENTUM] Filtered to {len(active_markets)} active markets "
              f"(from {len(markets)} total)", flush=True)

    # --- Enrich with canonical CLOB token IDs (parallel) ---
    # The Gamma API clobTokenIds may differ from the actual CLOB WS token IDs.
    # Query CLOB API to get the exact IDs the WebSocket uses.
    enriched = 0
    if active_markets:
        def _enrich_one(m_idx):
            cid = active_markets[m_idx].get("condition_id", "")
            clob_ids = _fetch_clob_token_ids(cid)
            return m_idx, clob_ids

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_enrich_one, i): i for i in range(len(active_markets))}
            for fut in as_completed(futs):
                m_idx, clob_ids = fut.result()
                if clob_ids and len(clob_ids) == 2 and all(clob_ids):
                    m = active_markets[m_idx]
                    gamma_ids = m["token_ids"]
                    if gamma_ids != clob_ids:
                        print(f"[MOMENTUM] Token ID FIX for {m['coin'].upper()}_{m['interval']}: "
                              f"gamma={gamma_ids[0][:20]}... clob={clob_ids[0][:20]}...", flush=True)
                    m["token_ids"] = clob_ids
                    enriched += 1

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
    except Exception as e:
        print(f"[MOMENTUM] WARNING: Failed to write trade log: {e}", flush=True)

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
        self.entered_markets: dict = {}  # market_key → last_buy_price (updated on every fill)
        self.initial_entry_price: dict = {}  # market_key → first entry price (never updated)
        self.market_entry_count: dict = {}
        self._limit_sim_entered: set = set()  # condition_ids already entered in limit sim mode
        self.hedged_markets: set = set()  # condition_ids already arb-hedged
        self.last_trade_time: dict = {}  # (condition_id, token_id) → epoch timestamp

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
        # Discovery runs in a background thread so it never blocks the scan loop.
        self._cached_markets: list = []
        self._last_market_discovery = 0.0
        self._market_discovery_interval = 30  # re-discover every 30s
        self._discovery_in_progress = False

        # Fast 5m boundary detection — track which 5m epochs we've already
        # discovered so we can do targeted slug lookups right at boundaries
        self._last_5m_epoch = 0

        # Resolution
        self.last_resolution_check = 0
        self.resolution_check_interval = 60  # Check every 60 seconds

        # WebSocket for live prices
        self.ws: Optional["CLOBWebSocket"] = None
        self.last_ws_refresh = 0
        self.ws_refresh_interval = 30

        # CLOB REST price cache — populated every N seconds (not every scan)
        self._clob_price_cache: dict[str, float] = {}
        self._last_clob_fetch = 0.0
        self._clob_fetch_interval = 3  # seconds between REST batch fetches (parallel)

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

        # No probe sizing (applies to both demo and live)
        self._dry_run_no_probe = True
        # Entry delays: enforce even in dry runs
        self._dry_run_no_delays = False

        # --- DRY RUN OVERRIDES ---
        # None — dry run uses identical rules to live mode

        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items()))
        print("\n" + "=" * 60)
        if LIMIT_SIM_ENABLED:
            print("  MOMENTUM ENGINE (LIMIT SIM)")
            print(f"  Limit trigger: WS >= {LIMIT_SIM_PRICE*100:.0f}¢ → fill @ {LIMIT_SIM_PRICE*100:.0f}¢")
            print(f"  Lot: ${LIMIT_SIM_LOT:.2f} | Side: {LIMIT_SIM_SIDE}")
            print(f"  Simulates: limit BUY {LIMIT_SIM_SIDE} @ {LIMIT_SIM_PRICE*100:.0f}¢")
        else:
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
        print(f"  Markets: {', '.join(INTERVALS) if INTERVALS else 'NONE (all disabled!)'}")
        print(f"  Hedge: {'ENABLED' if MOMENTUM_HEDGE_ENABLE else 'DISABLED'} | gap: {MOMENTUM_HEDGE_PCT:.0f}¢")
        _guard_type = "rolling (vs last entry)" if UPGUARD_ROLLING else "initial (vs first entry)"
        print(f"  Re-entry: upward only ({_guard_type}), max {self.max_entries_per_market} entries/market")
        _delay_status = "DISABLED" if getattr(self, '_dry_run_no_delays', False) else "ENABLED"
        _delay_info = ", ".join(f"{k}={int(v*60)}s" for k, v in MARKET_ENTRY_DELAY.items()) if _delay_status == "ENABLED" else ""
        print(f"  Delays/cooldowns: {_delay_status}" + (f" ({_delay_info})" if _delay_info else ""))
        print(f"  Max entry price: {MAX_PRICE_ENTRY*100:.0f}¢")
        _no_trade_str = ", ".join(f"{h}:00" for h in sorted(NO_TRADE_HOURS)) if NO_TRADE_HOURS else "NONE"
        print(f"  No-trade hours (ET): {_no_trade_str}")
        print(f"  DOWN bets: {'ENABLED' if DOWN_BETS_ENABLED else 'DISABLED'}")
        print(f"  Take profit: {TAKE_PROFIT_PRICE*100:.0f}¢" if TAKE_PROFIT_PRICE > 0 else "  Take profit: DISABLED")
        print(f"  Probe sizing: DISABLED (using dashboard lot sizes)")
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
        # Seed hedged_markets from existing arb_hedge positions
        for pos in self.positions.get("open", []):
            if pos.get("source") == "arb_hedge":
                cid = pos.get("condition_id", "")
                if cid:
                    self.hedged_markets.add(cid)

        if self.entered_markets:
            print(f"[MOMENTUM] Resumed {len(self.entered_markets)} active entries from open positions", flush=True)
        if self.hedged_markets:
            print(f"[MOMENTUM] Resumed {len(self.hedged_markets)} arb-hedged markets from open positions", flush=True)

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

        # CRITICAL: Also subscribe to tokens from open positions — they may
        # have rotated out of the scan list but still need live prices for
        # stop-loss checks.
        token_set = set(token_ids)
        for pos in self.positions.get("open", []):
            tid = pos.get("token_id", "")
            if tid and tid not in token_set:
                token_ids.append(tid)
                token_set.add(tid)

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

        Uses parallel requests (ThreadPoolExecutor) to fetch all tokens
        in ~1s instead of ~10s sequential. Called every 3s.
        """
        self._clob_price_cache = {}
        tokens_to_fetch = []
        seen_tokens = set()

        for m in markets:
            # Skip resolved markets (0/1 prices)
            prices = m.get("prices", [])
            if len(prices) == 2 and prices[0] in (0.0, 1.0) and prices[1] in (0.0, 1.0):
                continue
            for tid in m.get("token_ids", []):
                seen_tokens.add(tid)
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

        # CRITICAL: Also fetch prices for open position tokens that aren't in
        # the scan list — needed for stop-loss price checks.
        for pos in self.positions.get("open", []):
            tid = pos.get("token_id", "")
            if tid and tid not in seen_tokens:
                seen_tokens.add(tid)
                # Same WS skip logic
                if self.ws:
                    _, ws_ask = self.ws.get_best_prices(tid)
                    if ws_ask is not None:
                        continue
                ws_id = self._market_token_to_ws.get(tid)
                if tid in self._ws_price_cache or (ws_id and ws_id in self._ws_price_cache):
                    continue
                tokens_to_fetch.append(tid)

        if not tokens_to_fetch:
            return

        def _fetch_one(token_id):
            try:
                resp = requests.get(
                    f"{CLOB_API}/price",
                    params={"token_id": token_id, "side": "buy"},
                    timeout=2,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    price_val = data.get("price")
                    if price_val is not None:
                        p = float(price_val)
                        if p > 0:
                            return token_id, p, "ok"
                        return token_id, None, "empty"
                    return token_id, None, "empty"
                elif resp.status_code == 429:
                    return token_id, None, "ratelimit"
                else:
                    return token_id, None, f"http_{resp.status_code}"
            except Exception as e:
                return token_id, None, f"error:{e}"

        fetched = 0
        empty = 0
        errors = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, tid): tid for tid in tokens_to_fetch}
            for fut in as_completed(futures):
                tid, price, status = fut.result()
                if status == "ok" and price is not None:
                    self._clob_price_cache[tid] = price
                    fetched += 1
                elif status == "empty":
                    empty += 1
                elif status == "ratelimit":
                    print(f"[MOMENTUM] CLOB REST rate limited after {fetched} fetches", flush=True)
                else:
                    errors += 1

        err_str = f", {errors} errors" if errors else ""
        print(f"[MOMENTUM] CLOB REST: {fetched} prices, {empty} empty{err_str} "
              f"({len(tokens_to_fetch)} tokens queried)", flush=True)

    def _check_5m_boundary(self) -> list[dict]:
        """Fast targeted discovery when a new 5m epoch starts.

        Instead of waiting up to 30s for the next full discovery cycle,
        this does ~8 REST calls (4 coins × 2 slug variants) IN PARALLEL
        to grab the brand-new 5m markets right as they appear.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        now_ts = int(time.time())
        current_epoch = (now_ts // 300) * 300

        if current_epoch <= self._last_5m_epoch:
            return []  # same epoch, nothing new

        self._last_5m_epoch = current_epoch
        new_markets = []

        # Build all slug variants upfront
        all_slugs = []
        for coin_abbr in CRYPTO_COINS:
            coin_full = COIN_SLUG_NAMES.get(coin_abbr, coin_abbr)
            all_slugs.append(f"{coin_full}-updown-5m-{current_epoch}")
            if coin_abbr != coin_full:
                all_slugs.append(f"{coin_abbr}-updown-5m-{current_epoch}")

        def _fetch_slug(event_slug):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/events",
                    params={"slug": event_slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_slug, s): s for s in all_slugs}
            for fut in as_completed(futs):
                data = fut.result()
                if data is None:
                    continue
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

        # --- WS health check: force reconnect if stale ---
        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (datetime.now() - last_msg).total_seconds() if last_msg else float('inf')
            print(f"[MOMENTUM] WS STALE: no messages in {elapsed:.0f}s, forcing reconnect", flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()

        # Fast 5m boundary check — grab new markets immediately when
        # a new 5-minute epoch starts (only ~8 REST calls)
        self._check_5m_boundary()

        # Use cached markets for fast WS-driven price checks.
        # Only re-discover via REST every _market_discovery_interval seconds.
        # Discovery runs in a background thread so the scan loop stays fast (~1/sec).
        now = time.time()
        need_discovery = (now - self._last_market_discovery >= self._market_discovery_interval
                          or not self._cached_markets)
        if need_discovery and not self._discovery_in_progress:
            if not self._cached_markets:
                # First run: must block to get initial markets
                self._cached_markets = discover_active_markets()
                self._last_market_discovery = time.time()
                self._refresh_ws_tokens(force=True)
            else:
                # Subsequent runs: discover in background, don't block scan
                self._discovery_in_progress = True
                self._last_market_discovery = now  # prevent re-trigger

                def _bg_discover():
                    try:
                        new_markets = discover_active_markets()
                        prev_count = len(self._cached_markets)
                        self._cached_markets = new_markets
                        if len(new_markets) != prev_count:
                            self._refresh_ws_tokens(force=True)
                    except Exception as e:
                        print(f"[MOMENTUM] Background discovery error: {e}", flush=True)
                    finally:
                        self._discovery_in_progress = False

                threading.Thread(target=_bg_discover, daemon=True).start()

        markets = self._cached_markets
        if not markets:
            return 0

        # Fetch real-time CLOB prices periodically (not every 1s scan cycle).
        # 31 REST calls at 2s timeout = ~6s worst case, so run every 10s.
        if now - self._last_clob_fetch >= self._clob_fetch_interval:
            self._fetch_clob_prices_batch(markets)
            self._last_clob_fetch = now

        # Diagnostic: log WS + price source stats every 30 scans (~30s)
        if self.scans_completed % 30 == 1:
            # WS connection health
            if self.ws:
                ws_connected = self.ws.connected
                ws_msgs = self.ws.messages_received
                ws_last = self.ws.last_message_time
                ws_age = f"{(datetime.now() - ws_last).total_seconds():.0f}s ago" if ws_last else "never"
                ws_stale = self.ws.is_stale()
                stale_tag = " STALE!" if ws_stale else ""
                print(f"[MOMENTUM] WS health: connected={ws_connected} msgs={ws_msgs} last_msg={ws_age}{stale_tag}", flush=True)
            else:
                print("[MOMENTUM] WS health: not initialized", flush=True)
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
                # Compute minutes left so closed markets are clearly labelled
                _ml = None
                _ed = m.get("end_date")
                if _ed is not None:
                    try:
                        if isinstance(_ed, str):
                            _ed = datetime.fromisoformat(_ed.replace("Z", "+00:00"))
                        _ml = (_ed - datetime.now(timezone.utc)).total_seconds() / 60
                    except Exception:
                        pass
                _ml_label = f"{_ml:.1f}m" if _ml is not None and _ml > 0 else "CLOSED"
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
                            f"{p*100:.1f}¢ ({src}) [{_ml_label}]{gp_str}"
                        )
            if price_lines:
                print(f"[MOMENTUM] Prices >60¢ ({len(price_lines)}):", flush=True)
                for line in price_lines[:16]:
                    print(line, flush=True)

        entered = 0

        # --- LIMIT SIM status dump (every 30 scans) ---
        if LIMIT_SIM_ENABLED and self.scans_completed % 30 == 1:
            _open_5m = []
            for m in markets:
                if m.get("interval") != "5m":
                    continue
                _ed = m.get("end_date")
                if _ed is None:
                    continue
                try:
                    if isinstance(_ed, str):
                        _ed = datetime.fromisoformat(_ed.replace("Z", "+00:00"))
                    _ml = (_ed - datetime.now(timezone.utc)).total_seconds() / 60
                except Exception:
                    continue
                if _ml <= 0 or _ml > 6:
                    continue
                _tids = m.get("token_ids", [])
                if len(_tids) < 2:
                    continue
                _up = self.get_live_price(_tids[0])
                _dn = self.get_live_price(_tids[1])
                _up_s = f"{_up*100:.0f}¢" if _up is not None else "?"
                _dn_s = f"{_dn*100:.0f}¢" if _dn is not None else "?"
                _already = " [entered]" if m.get("condition_id") in self._limit_sim_entered else ""
                _open_5m.append(f"{m['coin'].upper()} U={_up_s} D={_dn_s} {_ml:.1f}m{_already}")
            if _open_5m:
                print(f"[LIMIT-SIM] Watching {len(_open_5m)} open 5m markets (threshold {LIMIT_SIM_PRICE*100:.0f}¢):", flush=True)
                for _line in _open_5m:
                    print(f"  {_line}", flush=True)
            else:
                print(f"[LIMIT-SIM] No open 5m markets (discovered {len(markets)} total)", flush=True)

        # --- GUARD: No-trade hours (ET) ---
        if NO_TRADE_HOURS:
            _utc_now = datetime.now(timezone.utc)
            _et_hour = (_utc_now.hour - 4) % 24  # UTC → ET (EDT)
            if _et_hour in NO_TRADE_HOURS:
                if self.scans_completed % 60 == 1:
                    print(f"[MOMENTUM] NO-TRADE HOUR: {_et_hour}:00 ET — skipping scan", flush=True)
                return 0

        for market in markets:
            coin = market["coin"]
            condition_id = market["condition_id"]
            slug = market["slug"]
            question = market["question"]

            # Per-coin pause
            if coin in self.paused_coins:
                continue

            # =================== LIMIT SIMULATION MODE ===================
            # Simulates a live limit order fill. Waits for WS price to cross
            # the threshold, then records entry at the threshold price.
            if LIMIT_SIM_ENABLED:
                # One entry per market (only one side can fill since Up+Down=$1)
                if condition_id in self._limit_sim_entered:
                    continue

                # Market must still be open (any time remaining is fine — limit
                # orders can fill right up to close)
                _end_date = market.get("end_date")
                if _end_date is None:
                    if self.scans_completed % 60 == 1:
                        print(f"[LIMIT-SIM] {coin.upper()} skip: no end_date", flush=True)
                    continue
                try:
                    if isinstance(_end_date, str):
                        _end_date = datetime.fromisoformat(_end_date.replace("Z", "+00:00"))
                    mins_left = (_end_date - datetime.now(timezone.utc)).total_seconds() / 60
                except Exception as _e:
                    if self.scans_completed % 60 == 1:
                        print(f"[LIMIT-SIM] {coin.upper()} skip: end_date parse error {_e}", flush=True)
                    continue
                if mins_left <= 0 or mins_left > 6:
                    continue

                # Settle delay: skip entries in first N seconds after market open.
                # 5m markets have 5.0 min at open, so `mins_left > 5.0 - settle/60`
                # means we're still in the settle window where WS prices spike to
                # stale 99¢ values before real order book settles.
                _interval_mins = 5.0 if market.get("interval") == "5m" else 15.0
                _settle_floor = _interval_mins - (LIMIT_SIM_SETTLE_SECS / 60.0)
                if mins_left > _settle_floor:
                    if self.scans_completed % 60 == 1:
                        print(f"[LIMIT-SIM] {coin.upper()} settling ({mins_left:.2f}m left, need ≤{_settle_floor:.2f}m)", flush=True)
                    continue

                token_ids = market.get("token_ids", [])
                outcomes = market.get("outcomes", [])
                if len(token_ids) < 2 or len(outcomes) < 2:
                    continue

                # Check both sides — whichever crosses the threshold first fills.
                # Since Up + Down = $1, only one side can be >= 75¢ at a time.
                fill_side_idx = None
                fill_price = None
                fill_token_id = None
                fill_outcome = None
                _up_price = None
                _down_price = None
                for _idx in (0, 1):
                    _tid = token_ids[_idx]
                    _lp = self.get_live_price(_tid)
                    if _idx == 0:
                        _up_price = _lp
                    else:
                        _down_price = _lp
                    if _lp is None:
                        continue
                    if _lp >= LIMIT_SIM_PRICE:
                        fill_side_idx = _idx
                        fill_price = _lp
                        fill_token_id = _tid
                        fill_outcome = outcomes[_idx]
                        break

                if fill_side_idx is None:
                    # No side crossed threshold — log periodically so we can see
                    # what prices markets are hovering at vs our threshold.
                    if self.scans_completed % 60 == 1:
                        _up_str = f"{_up_price*100:.0f}¢" if _up_price is not None else "?"
                        _down_str = f"{_down_price*100:.0f}¢" if _down_price is not None else "?"
                        print(f"[LIMIT-SIM] {coin.upper()}_{market.get('interval','?')} "
                              f"Up={_up_str} Down={_down_str} "
                              f"(need >={LIMIT_SIM_PRICE*100:.0f}¢, {mins_left:.1f}m left)", flush=True)
                    continue

                # Simulate limit fill at the threshold price (not live price)
                entry_price = LIMIT_SIM_PRICE
                trade_amount = LIMIT_SIM_LOT
                title = (question or slug)[:50]

                print(f"\n[LIMIT-SIM] FILL {coin.upper()} {fill_outcome} @ {entry_price*100:.1f}¢ "
                      f"(WS={fill_price*100:.1f}¢) ${trade_amount:.2f}", flush=True)
                print(f"           Market: {title}", flush=True)

                trade_record = {
                    "id": f"limsim_{condition_id[:12]}_{int(time.time())}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": title,
                    "slug": slug,
                    "outcome": fill_outcome,
                    "outcome_index": fill_side_idx,
                    "side": "BUY",
                    "amount": trade_amount,
                    "coin": coin,
                    "price": entry_price,
                    "interval": market.get("interval", "5m"),
                    "source": "momentum",
                    "status": "dry_run" if self.dry_run else "filled",
                }

                position = {
                    "id": trade_record["id"],
                    "timestamp": trade_record["timestamp"],
                    "condition_id": condition_id,
                    "token_id": fill_token_id,
                    "outcome_index": fill_side_idx,
                    "outcome": fill_outcome,
                    "market": title,
                    "slug": slug,
                    "entry_price": entry_price,
                    "amount": trade_amount,
                    "potential_payout": trade_amount / entry_price if entry_price > 0 else 0,
                    "dry_run": self.dry_run,
                    "source": "momentum",
                    "interval": market.get("interval", "5m"),
                    "end_date": _end_date.isoformat() if hasattr(_end_date, "isoformat") else str(_end_date),
                }
                self.positions.setdefault("open", []).append(position)
                self._limit_sim_entered.add(condition_id)
                self.entered_markets[(condition_id, fill_token_id)] = entry_price
                self.market_entry_count[(condition_id, fill_token_id)] = 1

                # Deduct balance
                try:
                    stats = self.positions.setdefault("stats", {})
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - trade_amount
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats.get("total_pnl", 0.0),
                        "equity": stats["balance"] + open_staked,
                        "event": "momentum_trade",
                        "detail": f"LIMSIM {coin.upper()} {fill_outcome} @{entry_price*100:.0f}¢",
                    })
                except Exception:
                    pass
                save_positions(self.positions)
                self.trades_entered += 1
                entered += 1

                if self.on_trade:
                    try:
                        self.on_trade(trade_record)
                    except Exception:
                        pass

                continue  # skip normal momentum logic for this market
            # ================ END LIMIT SIMULATION MODE ================

            # --- Recalculate minutes_left live from stored end_date ---
            # The cached minutes_until_close goes stale; recompute from
            # the absolute end_date so delay/close guards are accurate.
            end_date = market.get("end_date")
            if end_date is not None:
                minutes_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 60
            else:
                minutes_left = market.get("minutes_until_close")

            # --- GUARD: Market must still be open ---
            if minutes_left is not None and minutes_left < 0:
                continue

            # --- GUARD: Market must be old enough (entry delay) ---
            # Derive market age from end_date and interval duration.
            # If the market just opened, early prices are volatile and
            # reversals are common — wait for the direction to stabilise.
            # SKIP in dry run mode — no delays.
            interval = market.get("interval", "")
            if not getattr(self, '_dry_run_no_delays', False):
                entry_delay = MARKET_ENTRY_DELAY.get(interval)
                if entry_delay and minutes_left is not None:
                    duration = _INTERVAL_DURATION_MINUTES.get(interval)
                    if duration:
                        market_age_minutes = duration - minutes_left
                        if market_age_minutes < entry_delay:
                            wait_remaining = entry_delay - market_age_minutes
                            # Log once per market per scan cycle (only when candidate price would qualify)
                            _delay_label = f"{coin.upper()}_{interval} {slug[:30]}"
                            _wait_secs = int(wait_remaining * 60)
                            _wait_m, _wait_s = divmod(_wait_secs, 60)
                            _wait_display = f"{_wait_m}m{_wait_s:02d}s" if _wait_m else f"{_wait_s}s"
                            print(f"[MOMENTUM] DELAY {_delay_label}: market age {market_age_minutes:.1f}m < {entry_delay:.0f}m delay "
                                  f"(wait {_wait_display} more)", flush=True)
                            continue
                        else:
                            # Delay satisfied — log that we're past the wait
                            _delay_label = f"{coin.upper()}_{interval} {slug[:30]}"
                            print(f"[MOMENTUM] DELAY OK {_delay_label}: market age {market_age_minutes:.1f}m >= {entry_delay:.0f}m delay", flush=True)

            # Build a short label for rejection logging
            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # Check each side (outcome 0 and 1)
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                other_token_id = market["token_ids"][1 - oi]
                gamma_price = market["prices"][oi]

                # --- GUARD: Skip DOWN bets if disabled ---
                if not DOWN_BETS_ENABLED and outcome.lower() == "down":
                    continue

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
                    # Warn once per scan when using gamma fallback (likely stale)
                    if self.scans_completed % 60 == 1:
                        print(f"  WARN gamma_fallback: {_mkt_label} {outcome} no live price, using gamma={gamma_price*100:.1f}¢", flush=True)

                # --- FILTER: Price must be in range ---
                # Use per-interval brackets if defined, else global min/max
                interval = market.get("interval", "")
                if interval in self.interval_price_brackets:
                    brackets = self.interval_price_brackets[interval]
                    in_bracket = any(lo <= price < hi for lo, hi in brackets)
                    if not in_bracket:
                        _ranges = " | ".join(f"{lo*100:.0f}-{hi*100:.0f}¢" for lo, hi in brackets)
                        print(f"  REJECT price_range: {_mkt_label} {outcome} @ {price*100:.1f}¢ not in [{_ranges}]", flush=True)
                        continue
                else:
                    if price < self.min_entry_price or price > self.max_entry_price:
                        print(f"  REJECT price_range: {_mkt_label} {outcome} @ {price*100:.1f}¢ outside {self.min_entry_price*100:.0f}-{self.max_entry_price*100:.0f}¢", flush=True)
                        continue

                # --- GUARD: Hard ceiling on entry price ---
                if price >= MAX_PRICE_ENTRY:
                    continue

                # --- Price qualifies! Log that we're evaluating this candidate ---
                print(f"[MOMENTUM] CANDIDATE {_mkt_label} {outcome} @ {price*100:.1f}¢ ({_price_src})", flush=True)

                # Key by (condition_id, token_id) — stable across API ordering changes
                market_key = (condition_id, token_id)

                # --- GUARD: No opposite side (optional) ---
                if OPPOSITE_SIDE_BLOCK:
                    opposite_key = (condition_id, other_token_id)
                    if opposite_key in self.entered_markets:
                        self.trades_skipped += 1
                        print(f"  REJECT opposite_held: already hold other side of {_mkt_label}", flush=True)
                        continue

                # --- HEDGE TRIGGER: Price risen 5¢+ above probe → hedge opposite side ---
                # This runs BEFORE the max-entries / cooldown guards because
                # it's not a re-entry on primary — it's a hedge on the opposite side.
                is_first_entry = market_key not in self.entered_markets
                if market_key in self.entered_markets:
                    # Rolling: compare vs last entry. Non-rolling: compare vs initial entry.
                    if UPGUARD_ROLLING:
                        guard_price = self.entered_markets[market_key]
                    else:
                        guard_price = self.initial_entry_price.get(market_key, self.entered_markets[market_key])
                    last_buy_price = self.entered_markets[market_key]  # still used for hedge calc
                    price_rise = round(price - guard_price, 4)

                    if MOMENTUM_HEDGE_ENABLE:
                        # Hedge mode: require minimum gap before triggering hedge
                        if price_rise < MOMENTUM_HEDGE_PCT / 100:
                            continue
                    else:
                        # Re-entry mode: just require upward movement
                        if price_rise <= 0:
                            continue

                    # Price has risen 5¢+ — trigger hedge instead of re-entry
                    if (MOMENTUM_HEDGE_ENABLE
                            and other_token_id
                            and condition_id not in self.hedged_markets):
                        title = (question or slug)[:50]
                        print(f"\n[ARB] HEDGE TRIGGER: {coin.upper()} {outcome} risen {price_rise*100:.1f}¢ "
                              f"({last_buy_price*100:.1f}¢ → {price*100:.1f}¢)", flush=True)

                        # Sum primary spend on this condition_id
                        total_primary = sum(
                            p.get("amount", 0) for p in self.positions.get("open", [])
                            if p.get("condition_id") == condition_id
                            and p.get("source") in ("momentum", "copy")
                        )

                        arb = calc_arb_hedge(last_buy_price, total_primary)
                        if arb:
                            # Use full price chain (WS → WS cache → CLOB REST → gamma)
                            opp_ask = self.get_live_price(other_token_id)
                            if opp_ask is None:
                                # Fallback to gamma price for opposite side
                                opp_gamma = market["prices"][1 - oi] if (1 - oi) < len(market.get("prices", [])) else None
                                if opp_gamma is not None:
                                    opp_ask = opp_gamma

                            opp_price_ok = opp_ask is not None and opp_ask <= arb["hedge_max_price"]
                            if opp_price_ok:
                                buffer = PRICE_BUFFER_BPS / 10000
                                hedge_limit = min(opp_ask * (1 + buffer), arb["hedge_max_price"]) if opp_ask else arb["hedge_max_price"]
                                actual_gap = round(1 - price - hedge_limit, 4)
                                actual_arb = calc_arb_hedge(price, total_primary, gap=actual_gap) if actual_gap > 0 else None
                                hedge_amt = (actual_arb or arb)["hedge_amount"]
                                hedge_max = hedge_limit
                                arb_info = actual_arb or arb

                                other_outcome = market["outcomes"][1 - oi]
                                print(f"       Primary: {outcome} @ {last_buy_price*100:.1f}¢ (${total_primary:.2f})", flush=True)
                                print(f"       Hedge:   {other_outcome} @ {hedge_max*100:.1f}¢ (${hedge_amt:.2f})", flush=True)
                                print(f"       Total cost: ${arb_info['total_cost']:.2f} | "
                                      f"Profit either way: +${arb_info['profit_if_primary_wins']:.2f} / +${arb_info['profit_if_hedge_wins']:.2f}", flush=True)

                                if not self.dry_run:
                                    hedge_fill = place_bet(self.client, other_token_id, hedge_amt, max_price=hedge_max)
                                    if hedge_fill.get("success"):
                                        h_price = hedge_fill.get("fill_price", hedge_max)
                                        print(f"[ARB] HEDGE FILLED @ {h_price*100:.1f}¢!", flush=True)
                                        hedge_position = {
                                            "id": f"arb_{condition_id[:12]}_{int(time.time())}",
                                            "timestamp": datetime.now(timezone.utc).isoformat(),
                                            "condition_id": condition_id,
                                            "token_id": other_token_id,
                                            "outcome_index": 1 - oi,
                                            "outcome": other_outcome,
                                            "market": title,
                                            "slug": slug,
                                            "interval": market.get("interval", ""),
                                            "end_date": end_date.isoformat() if end_date else None,
                                            "entry_price": h_price,
                                            "amount": hedge_amt,
                                            "potential_payout": hedge_amt / h_price if h_price > 0 else 0,
                                            "dry_run": False,
                                            "source": "arb_hedge",
                                            "hedge_of": f"momentum_{condition_id[:12]}",
                                        }
                                        self.positions["open"].append(hedge_position)
                                        self.total_spent += hedge_amt
                                        stats = self.positions["stats"]
                                        stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - hedge_amt
                                        save_positions(self.positions)
                                        self.hedged_markets.add(condition_id)
                                        _log_trade("arb_hedge", {
                                            "market": title, "outcome": other_outcome,
                                            "price": h_price, "amount": hedge_amt,
                                            "total_primary": total_primary,
                                            "primary_outcome": outcome, "probe_price": last_buy_price,
                                            "current_price": price, "rise": price_rise,
                                            "condition_id": condition_id,
                                        })
                                    else:
                                        print(f"[ARB] Hedge order FAILED", flush=True)
                                else:
                                    print(f"[ARB] DRY RUN — hedge would execute @ {hedge_max*100:.1f}¢", flush=True)
                                    hedge_position = {
                                        "id": f"arb_{condition_id[:12]}_{int(time.time())}",
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                        "condition_id": condition_id,
                                        "token_id": other_token_id,
                                        "outcome_index": 1 - oi,
                                        "outcome": other_outcome,
                                        "market": title,
                                        "slug": slug,
                                        "interval": market.get("interval", ""),
                                        "entry_price": hedge_max,
                                        "amount": hedge_amt,
                                        "potential_payout": hedge_amt / hedge_max if hedge_max > 0 else 0,
                                        "dry_run": True,
                                        "source": "arb_hedge",
                                        "hedge_of": f"momentum_{condition_id[:12]}",
                                    }
                                    self.positions["open"].append(hedge_position)
                                    self.hedged_markets.add(condition_id)
                                    # Deduct balance for dry-run hedge so equity stays accurate
                                    stats = self.positions["stats"]
                                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - hedge_amt
                                    save_positions(self.positions)
                            elif opp_ask is not None:
                                print(f"[ARB] Skip: opposite ask {opp_ask*100:.1f}¢ > max {arb['hedge_max_price']*100:.1f}¢", flush=True)
                            else:
                                print(f"[ARB] Skip: no WS price for opposite token", flush=True)
                        else:
                            print(f"[ARB] Skip: no profitable arb at {price*100:.1f}¢", flush=True)
                    else:
                        if condition_id in self.hedged_markets:
                            pass  # already hedged, silent
                        elif not MOMENTUM_HEDGE_ENABLE:
                            pass  # hedge disabled — fall through to re-entry

                    # If hedge was placed or already hedged, skip re-entry
                    if condition_id in self.hedged_markets:
                        continue

                    # --- RE-ENTRY GATE: allow re-entry if under max entries ---
                    entry_count = self.market_entry_count.get(market_key, 0)
                    if entry_count >= self.max_entries_per_market:
                        continue  # hit max entries for this market

                    # --- FOLLOW-UP COOLDOWN: prevent rapid-fire re-entries ---
                    last_t = self.last_trade_time.get(market_key, 0)
                    if last_t:
                        cooldown = FOLLOW_UP_COOLDOWN_15M if market.get("interval") == "15m" else FOLLOW_UP_COOLDOWN
                        elapsed = time.time() - last_t
                        if elapsed < cooldown:
                            continue  # still in cooldown

                    # Update last buy price for upward-only tracking
                    # (fall through to ENTER THE TRADE block below)

                # --- ENTER THE TRADE (probe / re-entry) ---
                full_lot = self.coin_bet_amounts.get(coin, self.bet_amount)
                trade_amount = PROBE_AMOUNT if not getattr(self, '_dry_run_no_probe', False) else full_lot
                title = (question or slug)[:50]
                entry_type = "RE-ENTRY" if not is_first_entry else "PROBE"

                print(f"\n[MOMENTUM] ENTERING {coin.upper()} {outcome} @ {price*100:.1f}¢ ({entry_type})", flush=True)
                print(f"           Market: {title}", flush=True)
                print(f"           Interval: {market['interval']}", flush=True)
                print(f"           Amount: ${trade_amount:.2f} ({entry_type})", flush=True)

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
                    "minutes_until_close": minutes_left,
                })

                if trade_record["status"] in ("filled", "dry_run"):
                    # Track initial entry (never changes) and last entry (rolling)
                    if market_key not in self.initial_entry_price:
                        self.initial_entry_price[market_key] = price
                    self.entered_markets[market_key] = price
                    self.market_entry_count[market_key] = self.market_entry_count.get(market_key, 0) + 1
                    self.last_trade_time[market_key] = time.time()

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
                        "end_date": end_date.isoformat() if end_date else None,
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

    def check_stop_losses(self) -> int:
        """Auto-sell momentum positions that dropped STOP_LOSS_PCT% from peak.

        Trailing stop: tracks peak price seen since entry and triggers when
        price drops STOP_LOSS_PCT% from that peak. Returns number stopped out.
        """
        if STOP_LOSS_PCT <= 0:
            return 0
        if not self.ws:
            print("[MOMENTUM] SL check skipped: no WebSocket", flush=True)
            return 0

        open_positions = self.positions.get("open", [])
        momentum_open = [p for p in open_positions if p.get("source") == "momentum"]
        if not momentum_open:
            return 0

        stopped = 0
        threshold = 1 - (STOP_LOSS_PCT / 100)  # e.g. 0.65 for 35% trailing stop
        no_price_count = 0

        for position in momentum_open[:]:  # copy — list mutated during iteration
            entry_price = position.get("entry_price", 0)
            token_id = position.get("token_id", "")
            if entry_price <= 0 or not token_id:
                continue

            # Skip hedged positions — both sides locked in, SL would break the arb
            cid = position.get("condition_id", "")
            if cid and cid in self.hedged_markets:
                continue

            # Get live price — use ask (same as get_live_price) since bids
            # are often empty in short-term crypto prediction markets.
            live_price = self.get_live_price(token_id)
            if not live_price or live_price <= 0:
                no_price_count += 1
                continue

            # Update peak price (trailing stop tracks highest price seen)
            peak_price = position.get("peak_price", entry_price)
            if live_price > peak_price:
                peak_price = live_price
                position["peak_price"] = peak_price

            # --- TAKE PROFIT: sell if price >= threshold (e.g. 98¢) ---
            if TAKE_PROFIT_PRICE > 0 and live_price >= TAKE_PROFIT_PRICE:
                title = position.get("market", "?")[:40]
                shares = position.get("potential_payout", 0)

                # Query actual on-chain balance to avoid selling more than we hold
                try:
                    from copy_trader import check_token_balance
                    actual_balance = check_token_balance(token_id)
                    if actual_balance is not None and actual_balance < shares:
                        if actual_balance <= 0:
                            print(f"[MOMENTUM] TP skip {title}: 0 balance on-chain", flush=True)
                            continue
                        print(f"[MOMENTUM] TP balance adjust: {shares:.2f} → {actual_balance:.2f} (on-chain)", flush=True)
                        shares = actual_balance
                except Exception:
                    pass  # use tracked shares if balance check fails

                print(f"\n[MOMENTUM] TAKE PROFIT: {title}")
                print(f"       Price: {live_price*100:.1f}¢ >= {TAKE_PROFIT_PRICE*100:.0f}¢ threshold")
                print(f"       Entry: {entry_price*100:.1f}¢ | Shares: {shares:.2f}", flush=True)

                if self.dry_run:
                    print(f"       DRY RUN — would sell {shares:.2f} shares @ {live_price*100:.1f}¢", flush=True)
                    proceeds = shares * live_price
                    pnl = proceeds - position.get("amount", 0)
                    position["result"] = "TAKE_PROFIT"
                    position["won"] = True
                    position["sell_price"] = live_price
                    position["pnl"] = pnl
                    position["proceeds"] = proceeds
                    self.positions.get("open", []).remove(position)
                    self.positions.setdefault("resolved", []).append(position)
                    stats = self.positions.setdefault("stats", {})
                    stats["wins"] = stats.get("wins", 0) + 1
                    stats["total_pnl"] = stats.get("total_pnl", 0) + pnl
                    # Credit proceeds back to balance
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) + proceeds
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
                    stats.setdefault("balance_history", []).append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "balance": stats["balance"],
                        "pnl": stats["total_pnl"],
                        "equity": stats["balance"] + open_staked,
                        "event": "momentum_take_profit",
                        "detail": f"TP {title[:30]}",
                    })
                    save_positions(self.positions)
                    stopped += 1
                    print(f"       PnL: ${pnl:.2f} | Balance: ${stats['balance']:.2f}", flush=True)
                elif token_id and self.client:
                    from copy_trader import place_sell_gtc
                    # GTC limit sell at take-profit price — rests on book if no immediate match
                    fill = place_sell_gtc(self.client, token_id, shares, TAKE_PROFIT_PRICE)
                    if fill.get("success"):
                        fill_price = fill.get("fill_price", live_price)
                        proceeds = shares * fill_price
                        pnl = proceeds - position.get("amount", 0)
                        position["result"] = "TAKE_PROFIT"
                        position["won"] = True
                        position["sell_price"] = fill_price
                        position["pnl"] = pnl
                        position["proceeds"] = proceeds
                        self.positions.get("open", []).remove(position)
                        self.positions.setdefault("resolved", []).append(position)
                        stats = self.positions.setdefault("stats", {})
                        stats["wins"] = stats.get("wins", 0) + 1
                        stats["total_pnl"] = stats.get("total_pnl", 0) + pnl
                        # Credit proceeds back to balance
                        stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) + proceeds
                        open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
                        stats.setdefault("balance_history", []).append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "balance": stats["balance"],
                            "pnl": stats["total_pnl"],
                            "equity": stats["balance"] + open_staked,
                            "event": "momentum_take_profit",
                            "detail": f"TP {title[:30]}",
                        })
                        save_positions(self.positions)
                        stopped += 1
                        print(f"       SOLD @ {fill_price*100:.1f}¢ | PnL: ${pnl:.2f} | Balance: ${stats['balance']:.2f}", flush=True)
                    else:
                        print(f"       SELL FAILED", flush=True)
                continue  # don't also check stop loss on this position

            stop_price = peak_price * threshold
            if live_price >= stop_price:
                continue  # price is fine

            # --- STOP LOSS TRIGGERED ---
            condition_id = position.get("condition_id", "")
            outcome_index = position.get("outcome_index", 0)
            shares = position.get("potential_payout", 0)
            title = position.get("market", "Unknown")[:50]
            outcome = position.get("outcome", "?")
            amount_spent = position.get("amount", 0)
            coin = detect_coin(position.get("slug", ""), title)
            drop_from_peak = (1 - live_price / peak_price) * 100
            drop_from_entry = (1 - live_price / entry_price) * 100

            print(f"\n[MOMENTUM] *** STOP LOSS TRIGGERED ***", flush=True)
            print(f"       Market: {title}", flush=True)
            print(f"       {outcome} | Entry: {entry_price*100:.1f}¢ | Peak: {peak_price*100:.1f}¢ | Now: {live_price*100:.1f}¢ ({drop_from_entry:.1f}% from entry)", flush=True)
            print(f"       Stop level: {stop_price*100:.1f}¢ (-{STOP_LOSS_PCT:.0f}% from peak)", flush=True)
            print(f"       Selling {shares:.2f} shares", flush=True)

            trade_record = {
                "id": f"momentum_sl_{position.get('id', '')}_{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": position.get("slug", ""),
                "outcome": outcome,
                "side": "SELL",
                "reason": "stop_loss",
                "shares": shares,
                "entry_price": entry_price,
                "peak_price": peak_price,
                "stop_price": stop_price,
                "trigger_price": live_price,
                "coin": coin,
                "source": "momentum",
            }

            if self.dry_run:
                trade_record["price"] = live_price
                trade_record["status"] = "dry_run"
            else:
                if token_id and self.client and shares > 0:
                    # Stop loss = emergency exit. Use min_price=1¢ to sell at
                    # whatever bid is available. Getting out matters more than price.
                    fill = place_sell(self.client, token_id, shares, min_price=0.01)
                    if fill.get("success"):
                        trade_record["status"] = "filled"
                        fill_price = fill.get("fill_price") or live_price
                        trade_record["price"] = fill_price
                        print(f"       STOP LOSS SELL EXECUTED! Fill: {fill_price:.4f}", flush=True)
                    else:
                        print(f"       STOP LOSS SELL FAILED!", flush=True)
                        trade_record["status"] = "failed"
                else:
                    trade_record["status"] = "error"

            if trade_record["status"] in ["filled", "dry_run"]:
                sell_price = trade_record.get("price", 0)
                if sell_price > 0 and shares > 0:
                    proceeds = sell_price * shares
                    pnl = proceeds - amount_spent
                else:
                    proceeds = 0
                    pnl = -amount_spent

                position["result"] = "STOP_LOSS"
                position["won"] = False
                position["pnl"] = pnl
                position["proceeds"] = proceeds
                position["sold_at"] = trade_record["timestamp"]
                position["sell_price"] = sell_price

                try:
                    stats = self.positions["stats"]
                    stats["total_pnl"] = stats.get("total_pnl", 0.0) + pnl
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) + proceeds
                    stats["losses"] = stats.get("losses", 0) + 1
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []) if p is not position)
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats["total_pnl"],
                        "equity": stats["balance"] + open_staked,
                        "event": "momentum_stop_loss",
                        "detail": f"STOP LOSS {outcome} {title[:30]} drop={drop_from_peak:.1f}%peak pnl={pnl:+.2f} (returned ${proceeds:.2f})"
                    })
                except Exception:
                    pass

                if position in self.positions.get("open", []):
                    self.positions["open"].remove(position)
                self.positions.setdefault("resolved", []).append(position)
                save_positions(self.positions)
                trade_record["pnl"] = pnl
                # Log to JSONL trade log (was missing — caused SL count mismatch)
                _log_trade("resolved", {
                    "id": position.get("id", ""),
                    "coin": position.get("slug", "").split("-")[0] if position.get("slug") else "",
                    "interval": position.get("interval", ""),
                    "outcome": position.get("outcome", ""),
                    "entry_price": position.get("entry_price", 0),
                    "amount": amount_spent,
                    "result": "STOP_LOSS",
                    "pnl": pnl,
                    "won": False,
                    "slug": position.get("slug", ""),
                    "market": position.get("market", ""),
                    "condition_id": position.get("condition_id", ""),
                    "entered_at": position.get("timestamp", ""),
                    "sell_price": sell_price,
                })
                print(f"       Position stopped out. PnL: ${pnl:+.2f} | Proceeds: ${proceeds:.2f}", flush=True)
                stopped += 1

            if self.on_trade:
                try:
                    self.on_trade(trade_record)
                except Exception as e:
                    print(f"[MOMENTUM] Stop loss callback error: {e}", flush=True)

        if stopped > 0:
            print(f"[MOMENTUM] {stopped} position(s) stopped out", flush=True)
        if no_price_count > 0:
            # Log details for positions missing prices (critical for debugging)
            missing_tokens = []
            for p in momentum_open:
                tid = p.get("token_id", "")
                if tid and not self.get_live_price(tid):
                    in_scan = any(tid in m.get("token_ids", []) for m in self._cached_markets)
                    missing_tokens.append(f"{p.get('market', '?')[:25]}(scan={in_scan},tid={tid[:12]}...)")
            print(f"[MOMENTUM] SL check: {len(momentum_open)} positions, {no_price_count} had no live price: "
                  f"{'; '.join(missing_tokens[:3])}", flush=True)

        return stopped

    def check_resolutions(self):
        """Check if any momentum-sourced open positions have resolved.

        Delegates to the same resolution logic as the copy trader.
        """
        from copy_trader import get_market_resolution, retry_pending_redemptions

        now = time.time()

        # Retry any queued redemptions (rate-limited relay, no gas, etc.)
        try:
            retry_pending_redemptions(dry_run=self.dry_run)
        except Exception:
            pass

        if now - self.last_resolution_check < self.resolution_check_interval:
            return

        self.last_resolution_check = now

        open_positions = self.positions.get("open", [])
        # Only check positions from this engine (including its arb hedges)
        momentum_positions = [p for p in open_positions if p.get("source") == "momentum"
                              or (p.get("source") == "arb_hedge" and str(p.get("hedge_of", "")).startswith("momentum_"))]
        if not momentum_positions:
            return

        resolved_count = 0

        # Cache resolution results by condition_id to avoid duplicate API calls
        # when multiple position entries exist for the same market (e.g. re-entries).
        _resolution_cache = {}

        for position in momentum_positions[:]:
            condition_id = position.get("condition_id", "")
            slug = position.get("slug", "")
            token_id = position.get("token_id", "")
            our_outcome = position.get("outcome")

            if not token_id and not condition_id and not slug:
                continue

            # Use cached result if we already checked this condition_id this cycle
            cache_key = condition_id or slug or token_id
            if cache_key in _resolution_cache:
                result = dict(_resolution_cache[cache_key])  # copy — don't mutate cache
                # Re-derive our_token_won for THIS position's token_id
                # (probe and hedge share condition_id but have different tokens)
                winning_tid = result.get("winning_token_id", "")
                if winning_tid and token_id:
                    result["our_token_won"] = (str(token_id).strip() == str(winning_tid).strip())
                elif "our_token_won" in result:
                    del result["our_token_won"]  # stale — fall through to outcome/index match
            else:
                result = get_market_resolution(
                    condition_id=condition_id,
                    slug=slug,
                    token_id=token_id,
                    our_outcome=our_outcome,
                )
                _resolution_cache[cache_key] = result

            if not result or not result.get("resolved"):
                # API hasn't returned resolution yet — wait and retry next cycle.
                # NO price-based fallbacks — only trust the CLOB API resolution.
                if True:
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

            # Priority 4 REMOVED — no price-based resolution fallback.
            # Only trust CLOB API winner data.

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

                # Auto-redeem winning shares on-chain → converts back to USDC
                try:
                    from copy_trader import redeem_winning_position, _log_copy_trade, _queue_pending_redemption, _is_pending_redemption
                    # Skip if already queued — retry_pending_redemptions will handle it
                    if _is_pending_redemption(condition_id):
                        position["redeemed"] = False
                        # Don't spam — just note once and let the queue handle it
                    else:
                        redeemed = redeem_winning_position(
                            condition_id=condition_id,
                            token_id=token_id,
                            dry_run=self.dry_run,
                            slug=slug,
                        )
                        position["redeemed"] = bool(redeemed)
                        _log_copy_trade("momentum_redeem", {
                            "market": position.get("market", ""),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "redeemed": bool(redeemed),
                            "dry_run": self.dry_run,
                            "pnl": pnl,
                        })
                        if redeemed is True:
                            print(f"[MOMENTUM] Auto-redeemed: {position['market'][:30]}", flush=True)
                        elif redeemed == "rate_limited":
                            print(f"[MOMENTUM] Relay rate-limited — queuing: {position['market'][:30]}", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                        elif redeemed is None:
                            print(f"[MOMENTUM] Not redeemable yet (oracle pending) — queuing: {position['market'][:30]}", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                        else:
                            print(f"[MOMENTUM] Redemption failed for {position['market'][:30]} — queued for retry", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                except Exception as e:
                    position["redeemed"] = False
                    print(f"[MOMENTUM] Redemption error: {e}", flush=True)
                    try:
                        from copy_trader import _queue_pending_redemption, _is_pending_redemption
                        if not _is_pending_redemption(condition_id):
                            _queue_pending_redemption(condition_id, token_id, slug)
                    except Exception:
                        pass

            elif won is False:
                pnl = -amount
                position["result"] = "LOSS"
                position["pnl"] = pnl
                self.positions["stats"]["losses"] = self.positions["stats"].get("losses", 0) + 1
                print(f"[MOMENTUM] LOSS: {position['market'][:30]} | -${amount:.2f}", flush=True)
            else:
                # Interval-aware retry: 60m markets need much longer to settle
                # than 5m/15m. Use time-based threshold instead of fixed attempts.
                if "_first_resolve_check" not in position:
                    position["_first_resolve_check"] = time.time()
                first_check = position["_first_resolve_check"]
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
            if result in ("WIN", "TAKE_PROFIT"):
                coin_data[coin]["wins"] += 1
                coin_data[coin]["results"].append("W")
            elif result in ("LOSS", "STOP_LOSS"):
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

    @staticmethod
    def _thin_balance_history(history: list) -> list:
        """Downsample balance history so the chart stays responsive.

        Same strategy as CopyTrader._thin_balance_history: keep full resolution
        for recent data, progressively thin older data.
        """
        if len(history) <= 2000:
            return history

        now_ms = time.time() * 1000
        buckets = [
            (1 * 3600 * 1000, 1),
            (6 * 3600 * 1000, 5),
            (24 * 3600 * 1000, 20),
            (7 * 24 * 3600 * 1000, 100),
            (30 * 24 * 3600 * 1000, 500),
        ]

        result = []
        for idx, entry in enumerate(history):
            try:
                ts = entry.get("timestamp", "")
                entry_ms = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                continue
            age_ms = max(0, now_ms - entry_ms)

            keep = False
            for boundary, step in buckets:
                if age_ms < boundary:
                    keep = (idx % step == 0)
                    break
            else:
                keep = (idx % 500 == 0)

            if keep:
                result.append(entry)

        return result

    def get_stats(self) -> dict:
        """Get current stats for dashboard."""
        stats = self.positions.get("stats", {})
        open_positions = list(self.positions.get("open", []))
        # Count momentum-sourced positions AND their arb hedges
        momentum_open = [p for p in open_positions if p.get("source") == "momentum"
                         or (p.get("source") == "arb_hedge" and str(p.get("hedge_of", "")).startswith("momentum_"))]
        momentum_resolved = [p for p in self.positions.get("resolved", []) if p.get("source") == "momentum"
                             or (p.get("source") == "arb_hedge" and str(p.get("hedge_of", "")).startswith("momentum_"))]

        # Compute momentum-specific P&L from resolved positions
        m_wins = 0
        m_losses = 0
        m_stop_losses = 0
        m_sl_proceeds = 0.0
        m_sl_spent = 0.0
        m_total_pnl = 0.0
        for p in momentum_resolved:
            pnl = p.get("pnl", 0)
            won = p.get("won")
            if won is True:
                m_wins += 1
            elif won is False:
                m_losses += 1
            if p.get("result") == "STOP_LOSS":
                m_stop_losses += 1
                m_sl_proceeds += float(p.get("proceeds", 0) or 0)
                m_sl_spent += float(p.get("amount", 0) or 0)
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

        # Build momentum-only balance_history (filter shared history to momentum events)
        raw_history = [
            h for h in stats.get("balance_history", [])
            if str(h.get("event", "")).startswith("momentum")
        ]
        raw_len = len(raw_history)
        cache = getattr(self, "_mbh_cache", None)
        if cache and cache[0] == raw_len:
            balance_history = cache[1]
        else:
            balance_history = self._thin_balance_history(raw_history)
            self._mbh_cache = (raw_len, balance_history)

        return {
            "trades_entered": self.trades_entered,
            "trades_skipped": self.trades_skipped,
            "total_spent": self.total_spent,
            "scans_completed": self.scans_completed,
            "open_positions": len(momentum_open),
            "resolved_positions": len(momentum_resolved),
            "wins": m_wins,
            "losses": m_losses,
            "stop_losses": m_stop_losses,
            "sl_proceeds": m_sl_proceeds,
            "sl_spent": m_sl_spent,
            "total_pnl": m_total_pnl,
            "win_rate": m_win_rate,
            "open_staked": m_open_staked,
            "coin_roi": coin_roi,
            "open_by_coin": open_by_coin,
            "balance_history": balance_history,
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
