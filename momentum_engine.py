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
from datetime import datetime, timezone
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
    has_opposite_position,
    get_active_crypto_tokens,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

# Minimum price to enter a market (65 cents = 0.65)
MIN_ENTRY_PRICE = float(os.getenv("MOMENTUM_MIN_ENTRY_PRICE", "0.65"))

# Maximum price to enter (same as copy trader)
MAX_ENTRY_PRICE = float(os.getenv("MOMENTUM_MAX_ENTRY_PRICE", "0.95"))

# How often to poll prices (seconds)
POLL_INTERVAL = int(os.getenv("MOMENTUM_POLL_INTERVAL", "10"))

# Max entries per market (same as copy trader default)
MAX_ENTRIES_PER_MARKET = int(os.getenv("MOMENTUM_MAX_ENTRIES", "2"))


# =============================================================================
# MARKET DISCOVERY
# =============================================================================

CRYPTO_COINS = ["btc", "eth", "sol", "xrp"]
INTERVALS = ["5m", "15m", "30m", "60m"]


def _parse_market(raw: dict) -> Optional[dict]:
    """Parse a single Gamma API market into our internal format.

    Returns None if the market doesn't match crypto updown criteria.
    """
    slug = (raw.get("slug") or "").lower()

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
    coin = detect_coin(slug, raw.get("question", ""))

    # Detect interval from slug — MUST match a known interval
    interval = ""
    for tag in INTERVALS:
        if f"-{tag}-" in slug or slug.endswith(f"-{tag}"):
            interval = tag
            break

    if not coin or not interval:
        return None

    return {
        "slug": raw.get("slug", ""),
        "question": raw.get("question", ""),
        "condition_id": condition_id,
        "outcomes": outcomes,
        "token_ids": [str(clob_ids[0]), str(clob_ids[1])],
        "prices": prices,
        "coin": coin,
        "interval": interval,
    }


def discover_active_markets() -> list[dict]:
    """Find all active crypto updown markets across all intervals.

    Uses targeted slug searches (e.g. btc-updown-15m-{timestamp}) to find
    the exact crypto price-prediction interval markets, instead of relying
    on a broad search that can match non-price markets.

    Returns a list of market dicts with:
      - slug, question, condition_id
      - outcomes: ["Up", "Down"] or similar
      - token_ids: [up_token_id, down_token_id]
      - prices: [up_price, down_price]
      - coin: "btc", "eth", "sol", "xrp"
      - interval: "5m", "15m", "30m", "60m"
    """
    from datetime import datetime as dt

    now_ts = int(time.time())
    markets = []
    seen_slugs = set()

    # --- Strategy 1: Targeted slug search ---
    # Polymarket crypto updown slugs follow: {coin}-updown-{interval}-{timestamp}
    # Search for each coin + interval + nearby timestamp windows
    interval_seconds = {"5m": 300, "15m": 900, "30m": 1800, "60m": 3600}

    for coin in CRYPTO_COINS:
        for interval, secs in interval_seconds.items():
            base_ts = (now_ts // secs) * secs  # Round to current window
            timestamps = [
                base_ts,           # Current window
                base_ts + secs,    # Next window
                base_ts + secs * 2,  # +2 windows ahead
                base_ts - secs,    # Previous (might still be active)
            ]

            for ts in timestamps:
                slug_pattern = f"{coin}-updown-{interval}-{ts}"
                try:
                    resp = requests.get(
                        f"{GAMMA_API}/markets",
                        params={
                            "slug": slug_pattern,
                            "active": "true",
                            "closed": "false",
                        },
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            for raw in data:
                                s = (raw.get("slug") or "").lower()
                                if s in seen_slugs:
                                    continue
                                m = _parse_market(raw)
                                if m:
                                    markets.append(m)
                                    seen_slugs.add(s)
                except Exception:
                    pass

                time.sleep(0.03)  # Small delay to avoid rate limiting

    # --- Strategy 2: Broad fallback ---
    # Also do a general search to catch any markets the targeted search missed
    try:
        response = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        response.raise_for_status()
        all_markets = response.json()

        for raw in all_markets:
            slug = (raw.get("slug") or "").lower()
            if slug in seen_slugs:
                continue
            # Quick check: must have "updown" in slug to be an interval market
            if "updown" not in slug:
                continue
            m = _parse_market(raw)
            if m:
                markets.append(m)
                seen_slugs.add(slug)

    except Exception as e:
        print(f"[MOMENTUM] Broad search error: {e}", flush=True)

    if markets:
        coins_found = set(m["coin"] for m in markets)
        intervals_found = set(m["interval"] for m in markets)
        print(f"[MOMENTUM] Discovered {len(markets)} markets: "
              f"coins={sorted(coins_found)}, intervals={sorted(intervals_found)}",
              flush=True)
    else:
        print("[MOMENTUM] No active updown markets found", flush=True)

    return markets


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

        # Track entered markets: (condition_id, outcome_index) → last_buy_price
        # KEY DIFFERENCE from copy trader: this stores LAST buy price, not first
        self.entered_markets: dict = {}
        self.market_entry_count: dict = {}

        # Position tracking (shares file with copy trader)
        self.positions = load_positions()

        # Stats
        self.trades_entered = 0
        self.trades_skipped = 0
        self.total_spent = 0.0
        self.trade_history: list = []
        self.scans_completed = 0

        # Resolution
        self.last_resolution_check = 0
        self.resolution_check_interval = 60

        # WebSocket for live prices
        self.ws: Optional["CLOBWebSocket"] = None
        self.last_ws_refresh = 0
        self.ws_refresh_interval = 300

    def start(self):
        """Initialize the momentum engine."""
        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items()))
        print("\n" + "=" * 60)
        print("  MOMENTUM ENGINE")
        print(f"  Entry threshold: >= {self.min_entry_price*100:.0f} cents")
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
            oi = pos.get("outcome_index", 0)
            ep = pos.get("entry_price", 0)
            if cid:
                mk = (cid, oi)
                # Use entry_price as last_buy — on restart we lose the chain,
                # so this is the safest conservative default
                self.entered_markets[mk] = ep
                self.market_entry_count[mk] = self.market_entry_count.get(mk, 0) + 1
        if self.entered_markets:
            print(f"[MOMENTUM] Resumed {len(self.entered_markets)} active entries from open positions", flush=True)

    def _start_ws(self):
        """Start WebSocket for real-time prices."""
        if not HAS_CLOB_WS:
            return
        try:
            self.ws = CLOBWebSocket(
                on_connect=lambda: print("[MOMENTUM] WebSocket connected", flush=True),
                on_disconnect=lambda: print("[MOMENTUM] WebSocket disconnected", flush=True),
            )
            self.ws.start()
            time.sleep(1)
            self._refresh_ws_tokens()
        except Exception as e:
            print(f"[MOMENTUM] WebSocket start failed: {e}", flush=True)
            self.ws = None

    def _refresh_ws_tokens(self):
        """Subscribe to active crypto tokens."""
        if not self.ws:
            return
        now = time.time()
        if now - self.last_ws_refresh < self.ws_refresh_interval:
            return
        self.last_ws_refresh = now
        token_ids = get_active_crypto_tokens()
        if token_ids:
            self.ws.subscribe(token_ids)
            print(f"[MOMENTUM] WebSocket subscribed to {len(token_ids)} tokens", flush=True)

    def get_live_price(self, token_id: str) -> Optional[float]:
        """Get real-time best ask from WebSocket."""
        if not self.ws or not token_id:
            return None
        _, best_ask = self.ws.get_best_prices(token_id)
        return best_ask

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

        markets = discover_active_markets()
        if not markets:
            return 0

        entered = 0

        for market in markets:
            coin = market["coin"]
            condition_id = market["condition_id"]
            slug = market["slug"]
            question = market["question"]

            # Per-coin pause
            if coin in self.paused_coins:
                continue

            # Check each side (outcome 0 and 1)
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                gamma_price = market["prices"][oi]

                # Get best available price
                live_price = self.get_live_price(token_id)
                price = live_price if live_price else gamma_price

                # --- FILTER: Price must be in range ---
                if price < self.min_entry_price or price > self.max_entry_price:
                    continue

                market_key = (condition_id, oi)

                # --- GUARD: No opposite side ---
                if has_opposite_position(self.entered_markets, condition_id, oi):
                    self.trades_skipped += 1
                    continue

                # --- GUARD: Max entries per market ---
                if market_key in self.market_entry_count:
                    if self.market_entry_count[market_key] >= self.max_entries_per_market:
                        continue

                # --- GUARD: Upward-only re-entry ---
                # Key difference: compare against LAST buy price, not first
                if market_key in self.entered_markets:
                    last_buy_price = self.entered_markets[market_key]
                    if price <= last_buy_price:
                        # Price is at or below last buy — don't chase
                        continue
                    else:
                        print(f"[MOMENTUM] Re-entry OK ({coin.upper()} {outcome}): "
                              f"price {price*100:.1f}¢ > last buy {last_buy_price*100:.1f}¢", flush=True)

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
                continue

            entry_price = position.get("entry_price", 0)
            amount = position.get("amount", 0)
            our_index = position.get("outcome_index")

            won = None
            winning_outcome = result.get("winning_outcome")
            winning_index = result.get("winning_index")

            if "our_token_won" in result:
                won = result.get("our_token_won")
            else:
                if winning_outcome and our_outcome:
                    our_norm = our_outcome.lower().strip()
                    win_norm = winning_outcome.lower().strip()
                    if our_norm == win_norm or our_norm.startswith(win_norm) or win_norm.startswith(our_norm):
                        won = True
                    else:
                        won = False
                elif winning_index is not None and our_index is not None:
                    won = (winning_index == our_index)

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
                attempts = position.get("_resolve_attempts", 0) + 1
                position["_resolve_attempts"] = attempts
                if attempts < 5:
                    continue
                pnl = 0
                position["result"] = "UNKNOWN"
                position["pnl"] = 0

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

    def get_stats(self) -> dict:
        """Get current stats for dashboard."""
        stats = self.positions.get("stats", {})
        open_positions = list(self.positions.get("open", []))
        # Only count momentum-sourced positions
        momentum_open = [p for p in open_positions if p.get("source") == "momentum"]
        momentum_resolved = [p for p in self.positions.get("resolved", []) if p.get("source") == "momentum"]

        return {
            "trades_entered": self.trades_entered,
            "trades_skipped": self.trades_skipped,
            "total_spent": self.total_spent,
            "scans_completed": self.scans_completed,
            "open_positions": len(momentum_open),
            "resolved_positions": len(momentum_resolved),
            "min_entry_price": self.min_entry_price,
            "max_entry_price": self.max_entry_price,
            "max_entries_per_market": self.max_entries_per_market,
            "bet_amount": self.bet_amount,
            "coin_bet_amounts": {k: v for k, v in self.coin_bet_amounts.items()},
            "paused_coins": list(self.paused_coins),
            "dry_run": self.dry_run,
            "active_entries": {
                f"{cid[:12]}..._oi{oi}": f"{price*100:.1f}¢"
                for (cid, oi), price in self.entered_markets.items()
            },
        }
