#!/usr/bin/env python3
"""
WebSocket-First Crypto Market Scanner
======================================

Discovers crypto updown markets and streams real-time prices via WebSocket.
Solves the bootstrap problem: REST APIs haven't returned crypto markets,
so we try EVERY possible discovery method, then switch to WebSocket for
sub-100ms price feeds.

Discovery strategies (in order):
  1. CLOB REST /markets with cursor pagination (different from Gamma!)
  2. Gamma /events endpoint (events contain nested markets)
  3. Gamma slug-based search (bitcoin-updown-15m-{timestamp})
  4. Gamma broad /markets search
  5. CSV bootstrap (extract token IDs from historical trade CSVs)

Once ANY tokens are found, subscribes via WebSocket for live prices.

Usage:
    python ws_scanner.py              # Run discovery + WebSocket stream
    python ws_scanner.py --discover   # Discovery only (no WebSocket)
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# Ensure sibling modules are importable
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

try:
    from clob_ws import CLOBWebSocket, OrderBookState
    HAS_WS = True
except ImportError:
    HAS_WS = False
    CLOBWebSocket = None
    OrderBookState = None
    print("[WARN] clob_ws.py not found — WebSocket streaming disabled")

# =============================================================================
# CONFIG
# =============================================================================

CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")

CRYPTO_COINS = ["btc", "eth", "sol", "xrp"]
COIN_FULL_NAMES = {
    "btc": "bitcoin", "eth": "ethereum",
    "sol": "solana", "xrp": "xrp",
}
CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple",
]
UPDOWN_KEYWORDS = ["up or down", "updown", "up-or-down"]
INTERVALS = ["5m", "30m", "60m", "1h"]


# =============================================================================
# DATA
# =============================================================================

@dataclass
class CryptoMarket:
    """A discovered crypto updown market with token IDs."""
    condition_id: str
    slug: str
    question: str
    coin: str  # btc, eth, sol, xrp
    interval: str  # 5m, 15m, etc.
    token_ids: list  # [up_token_id, down_token_id]
    outcomes: list  # ["Up", "Down"]
    prices: list  # [up_price, down_price] (indicative)
    source: str  # which discovery strategy found it
    end_date: str = ""
    volume: float = 0.0

    @property
    def price_sum(self) -> Optional[float]:
        if len(self.prices) == 2 and all(p is not None for p in self.prices):
            return self.prices[0] + self.prices[1]
        return None


# =============================================================================
# DISCOVERY ENGINE
# =============================================================================

class MarketDiscovery:
    """Tries every possible method to find crypto updown markets."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "CryptoArbScanner/1.0",
        })
        self.markets: dict[str, CryptoMarket] = {}  # condition_id -> market
        self.all_token_ids: set[str] = set()
        self.errors: list[str] = []

    def discover_all(self) -> list[CryptoMarket]:
        """Run all discovery strategies. Returns list of unique markets."""
        print("\n[DISCOVERY] Starting multi-strategy market discovery...\n")

        strategies = [
            ("Event slug lookup (computed timestamps)", self._try_event_slugs),
            ("CLOB REST /markets (paginated)", self._try_clob_rest),
            ("Gamma /events", self._try_gamma_events),
            ("Gamma slug search", self._try_gamma_slugs),
            ("Gamma broad /markets", self._try_gamma_broad),
            ("CSV bootstrap", self._try_csv_bootstrap),
        ]

        for name, fn in strategies:
            before = len(self.markets)
            try:
                fn()
            except Exception as e:
                self.errors.append(f"{name}: {e}")
                print(f"  [{name}] ERROR: {e}")

            found = len(self.markets) - before
            print(f"  [{name}] +{found} new markets (total: {len(self.markets)})")

            # If we found markets, collect token IDs
            if found > 0:
                for m in self.markets.values():
                    self.all_token_ids.update(m.token_ids)

        result = list(self.markets.values())
        print(f"\n[DISCOVERY] Complete: {len(result)} markets, "
              f"{len(self.all_token_ids)} token IDs")
        return result

    # ------------------------------------------------------------------
    # Strategy 0: Event slug lookup with computed timestamps
    # Polymarket URLs follow: /event/{coin}-updown-{interval}-{unix_ts}
    # The timestamp is floor-aligned to the interval boundary.
    # e.g. btc-updown-5m-1772397000, eth-updown-15m-1772396400
    # We generate current + nearby windows and query Gamma /events.
    # ------------------------------------------------------------------
    def _try_event_slugs(self):
        now_ts = int(time.time())

        # Interval configs: (slug_tag, seconds_per_window)
        interval_configs = [
            ("5m", 300),
            ("30m", 1800),
            ("1h", 3600),
        ]

        # Coin abbreviations as used in URL slugs
        coin_slugs = ["btc", "eth", "sol", "xrp"]

        slugs_to_try = []
        for coin in coin_slugs:
            for tag, window_secs in interval_configs:
                # Current + next + 2 previous windows (settling markets stay open)
                base_ts = (now_ts // window_secs) * window_secs
                for offset in [window_secs, 0, -window_secs, -2 * window_secs]:
                    slugs_to_try.append(f"{coin}-updown-{tag}-{base_ts + offset}")

        print(f"    Generated {len(slugs_to_try)} event slugs to check")

        found = 0
        checked = 0
        for slug in slugs_to_try:
            # Try Gamma /events?slug= (event-level lookup)
            try:
                resp = self.session.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        # Events contain nested markets
                        for mkt in event.get("markets", []):
                            if self._try_add_gamma_market(mkt, source="event_slug"):
                                found += 1
                        # Sometimes the event itself has market fields
                        if "conditionId" in event:
                            if self._try_add_gamma_market(event, source="event_slug"):
                                found += 1
                checked += 1
            except requests.RequestException:
                pass

            # Also try Gamma /markets?slug= (market-level lookup)
            try:
                resp = self.session.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for raw in data:
                            if self._try_add_gamma_market(raw, source="event_slug"):
                                found += 1
                    elif isinstance(data, dict) and "conditionId" in data:
                        if self._try_add_gamma_market(data, source="event_slug"):
                            found += 1
            except requests.RequestException:
                pass

            time.sleep(0.02)

        print(f"    Checked {checked} event slugs, found {found} markets")

    # ------------------------------------------------------------------
    # Strategy 1: CLOB REST API with cursor pagination
    # This is DIFFERENT from Gamma — it's the order book API and may
    # list markets that Gamma doesn't return.
    # ------------------------------------------------------------------
    def _try_clob_rest(self):
        cursor = "MA=="  # Base64 for "0"
        pages = 0
        max_pages = 50  # Safety limit

        while cursor and pages < max_pages:
            try:
                resp = self.session.get(
                    f"{CLOB_API}/markets",
                    params={"next_cursor": cursor},
                    timeout=15,
                )
                if resp.status_code != 200:
                    print(f"    CLOB /markets returned {resp.status_code}")
                    break

                data = resp.json()

                # CLOB returns: {"data": [...], "next_cursor": "..."}
                # or just a list
                if isinstance(data, dict):
                    markets_list = data.get("data", [])
                    cursor = data.get("next_cursor", "")
                    if cursor == "LTE=":  # End marker
                        cursor = ""
                elif isinstance(data, list):
                    markets_list = data
                    cursor = ""
                else:
                    break

                for raw in markets_list:
                    self._try_add_clob_market(raw, source="clob_rest")

                pages += 1
                if not cursor:
                    break

                time.sleep(0.1)

            except requests.RequestException as e:
                self.errors.append(f"CLOB REST page {pages}: {e}")
                break

        if pages > 0:
            print(f"    Scanned {pages} pages from CLOB REST API")

    def _try_add_clob_market(self, raw: dict, source: str):
        """Parse a CLOB API market response and add if crypto updown."""
        condition_id = raw.get("condition_id", "")
        question = raw.get("question", "")
        # CLOB format: tokens is a list of {token_id, outcome}
        tokens = raw.get("tokens", [])
        market_slug = raw.get("market_slug", "")

        if not condition_id or condition_id in self.markets:
            return

        # Check if crypto updown
        text = f"{question} {market_slug}".lower()
        is_crypto = any(kw in text for kw in CRYPTO_KEYWORDS)
        is_updown = any(kw in text for kw in UPDOWN_KEYWORDS)

        if not (is_crypto and is_updown):
            return

        coin = self._detect_coin(text)
        interval = self._detect_interval(market_slug, question)

        token_ids = []
        outcomes = []
        prices = []
        for tok in tokens:
            tid = tok.get("token_id", "")
            outcome = tok.get("outcome", "")
            price = tok.get("price")
            if tid:
                token_ids.append(str(tid))
                outcomes.append(outcome)
                try:
                    prices.append(float(price)) if price else prices.append(None)
                except (ValueError, TypeError):
                    prices.append(None)

        if not token_ids:
            return

        self.markets[condition_id] = CryptoMarket(
            condition_id=condition_id,
            slug=market_slug,
            question=question,
            coin=coin,
            interval=interval,
            token_ids=token_ids,
            outcomes=outcomes,
            prices=prices,
            source=source,
            end_date=raw.get("end_date_iso", ""),
        )

    # ------------------------------------------------------------------
    # Strategy 2: Gamma /events endpoint
    # Events contain nested markets — most reliable for finding groups
    # ------------------------------------------------------------------
    def _try_gamma_events(self):
        try:
            resp = self.session.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "closed": "false", "limit": 100},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"    Gamma /events returned {resp.status_code}")
                return

            events = resp.json()
            for event in events:
                title = (event.get("title") or "").lower()
                slug = (event.get("slug") or "").lower()
                combined = f"{title} {slug}"

                is_crypto = any(kw in combined for kw in CRYPTO_KEYWORDS)
                is_updown = any(kw in combined for kw in UPDOWN_KEYWORDS)
                if not (is_crypto or is_updown):
                    continue

                for mkt in event.get("markets", []):
                    self._try_add_gamma_market(mkt, source="gamma_events")

        except requests.RequestException as e:
            self.errors.append(f"Gamma events: {e}")

    # ------------------------------------------------------------------
    # Strategy 3: Gamma slug-based search
    # Polymarket slugs follow: bitcoin-updown-15m-{unix_timestamp}
    # ------------------------------------------------------------------
    def _try_gamma_slugs(self):
        now_ts = int(time.time())

        interval_configs = [
            ("5m", 300), ("30m", 1800), ("1h", 3600),
        ]

        searches = 0
        for coin_abbr in CRYPTO_COINS:
            coin_name = COIN_FULL_NAMES.get(coin_abbr, coin_abbr)
            for tag, window_secs in interval_configs:
                base_ts = (now_ts // window_secs) * window_secs
                timestamps = [
                    base_ts - window_secs,
                    base_ts,
                    base_ts + window_secs,
                ]

                for ts in timestamps:
                    slug = f"{coin_name}-updown-{tag}-{ts}"
                    try:
                        resp = self.session.get(
                            f"{GAMMA_API}/markets",
                            params={"slug": slug, "active": "true", "closed": "false"},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, list):
                                for raw in data:
                                    self._try_add_gamma_market(raw, source="gamma_slug")
                        searches += 1
                    except requests.RequestException:
                        pass
                    time.sleep(0.02)

        # Also try slug_contains for partial matches
        slug_terms = [
            "updown-5m", "updown-30m", "updown-1h",
            "bitcoin-updown", "ethereum-updown", "solana-updown", "xrp-updown",
            "up-or-down",
        ]
        for term in slug_terms:
            try:
                resp = self.session.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "slug_contains": term,
                        "active": "true", "closed": "false", "limit": 50,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for raw in data:
                            self._try_add_gamma_market(raw, source="gamma_slug_contains")
                searches += 1
            except requests.RequestException:
                pass
            time.sleep(0.02)

        print(f"    Made {searches} slug-based queries")

    # ------------------------------------------------------------------
    # Strategy 4: Gamma broad /markets
    # ------------------------------------------------------------------
    def _try_gamma_broad(self):
        for offset in range(0, 500, 100):
            try:
                resp = self.session.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active": "true", "closed": "false",
                        "limit": 100, "offset": offset,
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    break

                data = resp.json()
                if not isinstance(data, list) or not data:
                    break

                for raw in data:
                    question = (raw.get("question") or "").lower()
                    slug = (raw.get("slug") or "").lower()
                    combined = f"{question} {slug}"
                    if any(kw in combined for kw in CRYPTO_KEYWORDS):
                        self._try_add_gamma_market(raw, source="gamma_broad")

                time.sleep(0.1)

            except requests.RequestException:
                break

    def _try_add_gamma_market(self, raw: dict, source: str) -> bool:
        """Parse a Gamma API market and add if crypto updown. Returns True if added."""
        condition_id = raw.get("conditionId", "")
        if not condition_id or condition_id in self.markets:
            return False

        question = raw.get("question", "")
        slug = raw.get("slug", "")
        combined = f"{question} {slug}".lower()

        is_crypto = any(kw in combined for kw in CRYPTO_KEYWORDS)
        is_updown = any(kw in combined for kw in UPDOWN_KEYWORDS)
        if not (is_crypto and is_updown):
            return False

        # Parse token IDs
        clob_ids_raw = raw.get("clobTokenIds", [])
        if isinstance(clob_ids_raw, str):
            try:
                clob_ids = json.loads(clob_ids_raw)
            except json.JSONDecodeError:
                clob_ids = []
        else:
            clob_ids = clob_ids_raw if isinstance(clob_ids_raw, list) else []

        # Parse prices
        prices_raw = raw.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except json.JSONDecodeError:
                prices_raw = []

        prices = []
        for p in prices_raw:
            try:
                prices.append(float(p))
            except (ValueError, TypeError):
                prices.append(None)

        # Parse outcomes
        outcomes_raw = raw.get("outcomes", [])
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes = []
        else:
            outcomes = outcomes_raw if isinstance(outcomes_raw, list) else []

        if not clob_ids:
            return False

        coin = self._detect_coin(combined)
        interval = self._detect_interval(slug, question)

        # Volume
        volume = 0.0
        for vf in ["volume24hr", "volume_24h", "volume24h", "volume"]:
            v = raw.get(vf)
            if v is not None:
                try:
                    volume = float(v)
                    break
                except (ValueError, TypeError):
                    pass

        self.markets[condition_id] = CryptoMarket(
            condition_id=condition_id,
            slug=slug,
            question=question,
            coin=coin,
            interval=interval,
            token_ids=[str(t) for t in clob_ids],
            outcomes=outcomes,
            prices=prices,
            source=source,
            end_date=raw.get("endDate", ""),
            volume=volume,
        )
        return True

    # ------------------------------------------------------------------
    # Strategy 5: CSV bootstrap
    # Extract token IDs from historical trade CSVs. These are expired
    # markets, but we can use the condition IDs to query for related
    # active markets, and the naming patterns help discovery.
    # ------------------------------------------------------------------
    def _try_csv_bootstrap(self):
        csv_dir = Path("/home/user/cryptoarbitrage")
        csv_files = sorted(csv_dir.glob("*.csv"), reverse=True)

        if not csv_files:
            return

        # Collect unique condition IDs and their market names
        csv_markets: dict[str, dict] = {}  # condition_id -> info
        for csv_file in csv_files[:5]:  # Most recent 5
            try:
                with open(csv_file) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        cid = row.get("condition_id", "")
                        name = row.get("market") or row.get("target_market", "")
                        tid = row.get("token_id", "")
                        if cid and cid not in csv_markets:
                            csv_markets[cid] = {
                                "name": name,
                                "token_id": tid,
                                "csv": csv_file.name,
                            }
            except Exception:
                pass

        print(f"    Found {len(csv_markets)} unique condition IDs in CSVs")

        # These are expired markets — we can't subscribe to them.
        # But we can try querying the CLOB API for them to see if
        # Polymarket returns related/replacement markets.
        checked = 0
        for cid, info in list(csv_markets.items())[:10]:
            try:
                resp = self.session.get(
                    f"{CLOB_API}/markets/{cid}",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Even if resolved, the response might contain
                    # related_markets or sibling markets
                    if isinstance(data, dict):
                        # Check if this market is still active
                        active = data.get("active", False)
                        if active:
                            self._try_add_clob_market(data, source="csv_bootstrap")
                checked += 1
            except requests.RequestException:
                pass
            time.sleep(0.05)

        if checked > 0:
            print(f"    Checked {checked} historical condition IDs against CLOB API")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _detect_coin(self, text: str) -> str:
        text = text.lower()
        for abbr, name in COIN_FULL_NAMES.items():
            if name in text or abbr in text.split():
                return abbr
        return ""

    def _detect_interval(self, slug: str, question: str) -> str:
        slug_lower = slug.lower()
        for tag in INTERVALS:
            if f"-{tag}-" in slug_lower or slug_lower.endswith(f"-{tag}"):
                return tag

        match = re.search(r'updown-(\d+m)\b', slug_lower)
        if match and match.group(1) in INTERVALS:
            return match.group(1)

        if re.search(r'-1h[r]?[-\d]', slug_lower) or slug_lower.endswith("-1h"):
            return "60m"

        # Parse time range from question
        time_range = re.search(
            r'(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)',
            question, re.IGNORECASE,
        )
        if time_range:
            h1, m1, p1, h2, m2, p2 = time_range.groups()
            h1, m1 = int(h1), int(m1)
            h2, m2 = int(h2), int(m2)
            if p1.upper() == "PM" and h1 != 12: h1 += 12
            if p2.upper() == "PM" and h2 != 12: h2 += 12
            if p1.upper() == "AM" and h1 == 12: h1 = 0
            if p2.upper() == "AM" and h2 == 12: h2 = 0
            diff = (h2 * 60 + m2) - (h1 * 60 + m1)
            interval_map = {5: "5m", 30: "30m", 60: "60m"}
            if diff in interval_map:
                return interval_map[diff]

        return ""


# =============================================================================
# WEBSOCKET PRICE STREAMER
# =============================================================================

class WSPriceStreamer:
    """Subscribes to discovered markets via WebSocket for real-time prices."""

    def __init__(self, markets: list[CryptoMarket]):
        self.markets = {m.condition_id: m for m in markets}
        self.token_to_market: dict[str, str] = {}  # token_id -> condition_id

        # Map token IDs back to markets
        for m in markets:
            for tid in m.token_ids:
                self.token_to_market[tid] = m.condition_id

        self.ws_client: Optional[CLOBWebSocket] = None
        self.price_updates = 0
        self.last_prices: dict[str, dict] = {}  # condition_id -> {bid, ask, ts}

    def start(self):
        """Start WebSocket connection and subscribe to all tokens."""
        if not HAS_WS:
            print("[WS] WebSocket client not available")
            return

        all_token_ids = list(self.token_to_market.keys())
        if not all_token_ids:
            print("[WS] No token IDs to subscribe to")
            return

        print(f"\n[WS] Starting WebSocket price stream for {len(all_token_ids)} tokens...")

        self.ws_client = CLOBWebSocket(
            on_price_change=self._on_price_change,
            on_book_update=self._on_book_update,
            on_connect=lambda: self._on_connect(all_token_ids),
        )

        self.ws_client.start()

    def _on_connect(self, token_ids: list[str]):
        """Subscribe to all tokens on connect."""
        print(f"[WS] Connected! Subscribing to {len(token_ids)} tokens...")
        self.ws_client.subscribe(token_ids)

    def _on_price_change(self, token_id: str, bid: float, ask: float):
        """Handle real-time price change."""
        self.price_updates += 1
        cid = self.token_to_market.get(token_id)

        if cid and cid in self.markets:
            market = self.markets[cid]
            self.last_prices[cid] = {
                "token_id": token_id,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
                "ts": datetime.now(timezone.utc).isoformat(),
            }

            # Find which outcome this token is
            outcome_idx = market.token_ids.index(token_id) if token_id in market.token_ids else -1
            outcome = market.outcomes[outcome_idx] if 0 <= outcome_idx < len(market.outcomes) else "?"

            # Check if we have prices for both sides → compute arb
            other_idx = 1 - outcome_idx if outcome_idx in (0, 1) else -1
            if other_idx >= 0 and len(market.token_ids) > other_idx:
                other_tid = market.token_ids[other_idx]
                other_prices = self.last_prices.get(
                    # Look up by the other token's condition_id (same market)
                    cid, {}
                )
                # Also check if we have the other side from the WS client
                other_book = self.ws_client.get_best_prices(other_tid) if self.ws_client else (None, None)
                other_bid, other_ask = other_book

                if other_ask is not None:
                    # Arb check: buy both sides at ask prices
                    total_cost = ask + other_ask
                    if total_cost < 1.0:
                        edge = (1.0 - total_cost) * 100
                        print(f"  *** ARB SIGNAL *** {market.coin.upper()} {market.slug[:40]}")
                        print(f"      {outcome} ask=${ask:.4f} + {market.outcomes[other_idx]} ask=${other_ask:.4f} "
                              f"= ${total_cost:.4f}  edge={edge:.2f}%")

            # Log price update
            if self.price_updates <= 20 or self.price_updates % 100 == 0:
                print(f"[WS #{self.price_updates}] {market.coin.upper()} {outcome}: "
                      f"bid=${bid:.4f} ask=${ask:.4f} | {market.slug[:40]}")

    def _on_book_update(self, token_id: str, book: OrderBookState):
        """Handle full book update."""
        pass  # Price changes are handled above

    def stop(self):
        if self.ws_client:
            self.ws_client.stop()

    def get_stats(self) -> dict:
        ws_stats = self.ws_client.get_stats() if self.ws_client else {}
        return {
            "markets_tracked": len(self.markets),
            "tokens_subscribed": len(self.token_to_market),
            "price_updates": self.price_updates,
            "ws": ws_stats,
        }


# =============================================================================
# MAIN
# =============================================================================

def print_discovery_report(markets: list[CryptoMarket]):
    """Print summary of discovered markets."""
    print(f"\n{'='*70}")
    print(f"  MARKET DISCOVERY REPORT")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}")

    if not markets:
        print("\n  No crypto updown markets discovered.")
        print("  Possible reasons:")
        print("    - Network blocked (this environment can't reach Polymarket)")
        print("    - No active crypto updown markets right now")
        print("    - API structure changed")
        print("\n  Try running from a machine with direct internet access.")
        return

    # Group by coin
    by_coin: dict[str, list[CryptoMarket]] = {}
    for m in markets:
        by_coin.setdefault(m.coin or "unknown", []).append(m)

    for coin in sorted(by_coin.keys()):
        coin_markets = by_coin[coin]
        print(f"\n  {coin.upper()} ({len(coin_markets)} markets)")
        print(f"  {'─'*60}")

        for m in sorted(coin_markets, key=lambda x: x.slug)[:10]:
            ps = m.price_sum
            ps_str = f"sum={ps:.4f}" if ps else "no prices"
            print(f"    {m.slug[:50]}")
            print(f"      interval={m.interval or '?'} {ps_str} "
                  f"source={m.source}")
            print(f"      tokens: {len(m.token_ids)} IDs")

        if len(coin_markets) > 10:
            print(f"    ... and {len(coin_markets) - 10} more")

    # Source breakdown
    sources = {}
    for m in markets:
        sources[m.source] = sources.get(m.source, 0) + 1
    print(f"\n  Discovery sources: {json.dumps(sources, indent=4)}")

    # Token ID count
    all_tokens = set()
    for m in markets:
        all_tokens.update(m.token_ids)
    print(f"  Total unique token IDs: {len(all_tokens)}")


def main():
    discover_only = "--discover" in sys.argv

    print("""
╔══════════════════════════════════════════════════════════════╗
║  WebSocket-First Crypto Market Scanner                       ║
║  Discovers markets via REST, streams prices via WebSocket    ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # Phase 1: Discovery
    discovery = MarketDiscovery()
    markets = discovery.discover_all()

    print_discovery_report(markets)

    if discovery.errors:
        print(f"\n  Errors encountered:")
        for err in discovery.errors:
            print(f"    - {err}")

    # Save discovery results
    outfile = Path("/home/user/cryptoarbitrage/ws_discovery_results.json")
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets_found": len(markets),
        "markets": [
            {
                "condition_id": m.condition_id,
                "slug": m.slug,
                "question": m.question,
                "coin": m.coin,
                "interval": m.interval,
                "token_ids": m.token_ids,
                "outcomes": m.outcomes,
                "prices": m.prices,
                "source": m.source,
                "price_sum": m.price_sum,
            }
            for m in markets
        ],
        "errors": discovery.errors,
    }
    with open(outfile, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {outfile}")

    if discover_only or not markets:
        return

    # Phase 2: WebSocket streaming
    if not HAS_WS:
        print("\n[WS] clob_ws.py not available. Install: pip install websocket-client")
        return

    streamer = WSPriceStreamer(markets)
    streamer.start()

    print(f"\n[WS] Streaming live prices. Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(15)
            stats = streamer.get_stats()
            print(f"[STATS] updates={stats['price_updates']} "
                  f"markets={stats['markets_tracked']} "
                  f"tokens={stats['tokens_subscribed']}")
    except KeyboardInterrupt:
        print("\nStopping...")
        streamer.stop()


if __name__ == "__main__":
    main()
