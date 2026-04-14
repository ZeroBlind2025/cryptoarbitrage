#!/usr/bin/env python3
"""
MARTINGALE ENGINE
==================

Two-entry strategy:
  1. When either side of a market hits 65c, buy that side at base lot.
  2. If the OPPOSITE side subsequently hits 80c (first bet going
     against us), buy the opposite side at base lot *
     MAX_RENTRY_LOT_MULTIPLIER.

No take profit. No stop loss. Positions resolve on-chain.

Subclasses InformedMoneyEngine for market discovery, WS streaming,
CLOB REST cache, per-source stats, and on-chain resolution.
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from copy_trader import (
    ALGO_STARTING_BALANCE,
    PRICE_BUFFER_BPS,
    get_clob_client,
    place_bet,
    save_positions,
)

from momentum_engine import (
    _log_trade,
    discover_active_markets,
)

from informed_money_engine import InformedMoneyEngine


# =============================================================================
# CONFIGURATION
# =============================================================================

# Trigger for the first entry — any side >= this → buy that side at base lot
MARTINGALE_TRIGGER_1 = float(os.getenv("MARTINGALE_TRIGGER_1", "0.65"))

# Trigger for the hedge — opposite side >= this → buy opposite at base * mult
MARTINGALE_TRIGGER_2 = float(os.getenv("MARTINGALE_TRIGGER_2", "0.80"))

# Lot multiplier for the second (hedge) entry
MAX_RENTRY_LOT_MULTIPLIER = float(os.getenv("MAX_RENTRY_LOT_MULTIPLIER", "2.5"))

# How often the background loop ticks
POLL_INTERVAL = int(os.getenv("MARTINGALE_POLL_INTERVAL", "1"))


# =============================================================================
# ENGINE
# =============================================================================

class MartingaleEngine(InformedMoneyEngine):
    """Two-entry strategy: 65c first entry, 80c hedge at 2.5x."""

    SOURCE_TAG = "martingale"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Re-read env vars fresh on every start (don't rely on import-time cache)
        self.trigger_1 = float(os.getenv("MARTINGALE_TRIGGER_1", "0.65"))
        self.trigger_2 = float(os.getenv("MARTINGALE_TRIGGER_2", "0.80"))
        self.lot_multiplier = float(os.getenv("MAX_RENTRY_LOT_MULTIPLIER", "2.5"))
        self.min_entry_price = self.trigger_1
        self.max_entry_price = 1.0
        self.interval_price_brackets = {}
        self.max_entries_per_market = 2
        # Track per-side entries: (condition_id, outcome_index)
        self.entered_sides: set = set()
        print(f"[MARTINGALE] init: trigger_1={self.trigger_1}, "
              f"trigger_2={self.trigger_2}, "
              f"lot_multiplier={self.lot_multiplier}x "
              f"(MAX_RENTRY_LOT_MULTIPLIER env var)", flush=True)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self):
        self._dry_run_no_probe = True
        self._dry_run_no_delays = True

        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(
            f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items())
        )
        print("\n" + "#" * 70)
        print("#" + " " * 68 + "#")
        print("#" + "  MARTINGALE ENGINE  —  STARTING".center(68) + "#")
        print("#" + " " * 68 + "#")
        print("#" * 70)
        print(f"  Entry 1:  any side >= {self.trigger_1 * 100:.0f}c -> buy that side "
              f"at base lot")
        print(f"  Entry 2:  opposite side >= {self.trigger_2 * 100:.0f}c -> buy "
              f"opposite at base * {self.lot_multiplier:.1f}x")
        print("  TP / SL:  disabled (hold to resolution)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance:   ${balance:.2f}")
        print(f"  Mode:      {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll:      {POLL_INTERVAL}s")
        print("#" * 70 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[MARTINGALE] Failed to init client. Running in dry-run mode.",
                      flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_sides from open positions
        for pos in self.positions.get("open", []):
            if pos.get("source") != self.SOURCE_TAG:
                continue
            cid = pos.get("condition_id", "")
            oi = pos.get("outcome_index")
            if cid and oi is not None:
                self.entered_sides.add((cid, oi))

        if self.entered_sides:
            print(f"[MARTINGALE] Resumed {len(self.entered_sides)} open entries",
                  flush=True)

    # -------------------------------------------------------------------------
    # Scan / entry
    # -------------------------------------------------------------------------

    def scan_and_trade(self) -> int:
        self.scans_completed += 1

        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (datetime.now() - last_msg).total_seconds() if last_msg else float("inf")
            print(f"[MARTINGALE] WS STALE: {elapsed:.0f}s, forcing reconnect",
                  flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()
        self._check_5m_boundary()

        # Market discovery
        now = time.time()
        need_discovery = (
            now - self._last_market_discovery >= self._market_discovery_interval
            or not self._cached_markets
        )
        if need_discovery and not self._discovery_in_progress:
            if not self._cached_markets:
                self._cached_markets = discover_active_markets()
                self._last_market_discovery = time.time()
                self._refresh_ws_tokens(force=True)
            else:
                self._discovery_in_progress = True
                self._last_market_discovery = now

                def _bg_discover():
                    try:
                        new_markets = discover_active_markets()
                        prev_count = len(self._cached_markets)
                        self._cached_markets = new_markets
                        if len(new_markets) != prev_count:
                            self._refresh_ws_tokens(force=True)
                    except Exception as e:
                        print(f"[MARTINGALE] Discovery error: {e}", flush=True)
                    finally:
                        self._discovery_in_progress = False

                threading.Thread(target=_bg_discover, daemon=True).start()

        markets = self._cached_markets
        if not markets:
            return 0

        if now - self._last_clob_fetch >= self._clob_fetch_interval:
            self._fetch_clob_prices_batch(markets)
            self._last_clob_fetch = now

        entered = 0

        for market in markets:
            coin = market["coin"]
            condition_id = market["condition_id"]
            slug = market["slug"]
            question = market["question"]
            end_date = market.get("end_date")

            if coin in self.paused_coins:
                continue

            prices_raw = market.get("prices", [])
            if (len(prices_raw) == 2
                    and prices_raw[0] in (0.0, 1.0)
                    and prices_raw[1] in (0.0, 1.0)):
                continue

            if end_date is not None:
                minutes_left = (
                    end_date - datetime.now(timezone.utc)
                ).total_seconds() / 60
                if minutes_left < 0:
                    continue
            else:
                minutes_left = market.get("minutes_until_close")

            # --- GUARD: no late entries ---
            # 5m markets: block last 90s.  15m markets: block last 300s.
            interval = market.get("interval", "")
            if minutes_left is not None:
                seconds_left = minutes_left * 60
                if interval == "5m" and seconds_left < 90:
                    continue
                if interval == "15m" and seconds_left < 300:
                    continue

            # Side-level state for this market
            side_a_entered = (condition_id, 0) in self.entered_sides
            side_b_entered = (condition_id, 1) in self.entered_sides

            # If both sides already entered, nothing to do
            if side_a_entered and side_b_entered:
                continue

            # Get live prices for both sides
            tid_a = market["token_ids"][0]
            tid_b = market["token_ids"][1]
            gp_a = market["prices"][0]
            gp_b = market["prices"][1]
            lp_a = self.get_live_price(tid_a)
            lp_b = self.get_live_price(tid_b)
            price_a = lp_a if lp_a is not None else gp_a
            price_b = lp_b if lp_b is not None else gp_b

            if price_a is None or price_b is None:
                continue

            # Upper caps: keep entries close to the trigger price, not resolution.
            # First entry: trigger_1 (65c) .. +10c.  Hedge: trigger_2 (80c) .. +10c.
            cap_1 = self.trigger_1 + 0.10
            cap_2 = self.trigger_2 + 0.10

            # Decide which side (if any) to enter
            buy_oi = None
            buy_lot_mult = 1.0
            entry_reason = ""

            if not side_a_entered and not side_b_entered:
                # --- First entry: trigger_1 on either side, capped ---
                if self.trigger_1 <= price_a <= cap_1:
                    buy_oi = 0
                    entry_reason = "entry_1"
                elif self.trigger_1 <= price_b <= cap_1:
                    buy_oi = 1
                    entry_reason = "entry_1"
            else:
                # --- Hedge entry: opposite side at trigger_2, capped ---
                if (side_a_entered and not side_b_entered
                        and self.trigger_2 <= price_b <= cap_2):
                    buy_oi = 1
                    buy_lot_mult = self.lot_multiplier
                    entry_reason = "entry_2_hedge"
                elif (side_b_entered and not side_a_entered
                        and self.trigger_2 <= price_a <= cap_2):
                    buy_oi = 0
                    buy_lot_mult = self.lot_multiplier
                    entry_reason = "entry_2_hedge"

            if buy_oi is None:
                continue

            # --- Execute buy ---
            buy_outcome = market["outcomes"][buy_oi]
            buy_token_id = market["token_ids"][buy_oi]
            buy_price = price_a if buy_oi == 0 else price_b
            title = (question or slug)[:50]
            base_lot = self.coin_bet_amounts.get(coin, self.bet_amount)
            trade_amount = round(base_lot * buy_lot_mult, 2)

            print(f"\n[MARTINGALE] {entry_reason.upper()} {coin.upper()} "
                  f"{buy_outcome} @ {buy_price * 100:.1f}c "
                  f"(lot ${trade_amount:.2f} = ${base_lot:.2f} x {buy_lot_mult:.2f})",
                  flush=True)
            print(f"             Market:   {title}", flush=True)
            print(f"             Interval: {market['interval']}", flush=True)

            trade_record = {
                "id": f"martingale_{condition_id[:12]}_{buy_oi}_{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": slug,
                "outcome": buy_outcome,
                "outcome_index": buy_oi,
                "side": "BUY",
                "amount": trade_amount,
                "coin": coin,
                "price": buy_price,
                "interval": market["interval"],
                "reason": entry_reason,
                "lot_multiplier": buy_lot_mult,
                "source": self.SOURCE_TAG,
            }

            if self.dry_run:
                print(f"             DRY RUN — would buy @ {buy_price * 100:.1f}c",
                      flush=True)
                trade_record["status"] = "dry_run"
                entered += 1
            else:
                buffer = PRICE_BUFFER_BPS / 10000
                max_price = min(buy_price * (1 + buffer), 0.99)
                fill = place_bet(
                    self.client, buy_token_id, trade_amount, max_price=max_price
                )
                if fill.get("success"):
                    if fill.get("fill_price"):
                        buy_price = fill["fill_price"]
                        trade_record["price"] = buy_price
                    print(f"             EXECUTED @ {buy_price * 100:.1f}c",
                          flush=True)
                    trade_record["status"] = "filled"
                    entered += 1
                    self.total_spent += trade_amount
                else:
                    print("             FAILED!", flush=True)
                    trade_record["status"] = "failed"

            self.trade_history.append(trade_record)
            _log_trade(trade_record["status"], {
                "id": trade_record["id"],
                "coin": coin,
                "interval": market["interval"],
                "outcome": buy_outcome,
                "price": buy_price,
                "amount": trade_amount,
                "reason": entry_reason,
                "lot_multiplier": buy_lot_mult,
                "slug": slug,
                "market": title,
                "condition_id": condition_id,
                "token_id": buy_token_id,
                "minutes_until_close": minutes_left,
                "engine": "martingale",
            })

            if trade_record["status"] in ("filled", "dry_run"):
                self.entered_sides.add((condition_id, buy_oi))
                market_key = (condition_id, buy_token_id)
                self.entered_markets[market_key] = buy_price

                position = {
                    "id": trade_record["id"],
                    "timestamp": trade_record["timestamp"],
                    "condition_id": condition_id,
                    "token_id": buy_token_id,
                    "outcome_index": buy_oi,
                    "outcome": buy_outcome,
                    "market": title,
                    "slug": slug,
                    "interval": market.get("interval", ""),
                    "end_date": end_date.isoformat() if end_date else None,
                    "entry_price": buy_price,
                    "amount": trade_amount,
                    "potential_payout": (
                        trade_amount / buy_price if buy_price > 0 else 0
                    ),
                    "dry_run": self.dry_run,
                    "source": self.SOURCE_TAG,
                    "reason": entry_reason,
                    "lot_multiplier": buy_lot_mult,
                }
                self.positions["open"].append(position)

                try:
                    stats = self.positions["stats"]
                    stats["balance"] = (
                        stats.get("balance", ALGO_STARTING_BALANCE) - trade_amount
                    )
                    open_staked = sum(
                        p.get("amount", 0)
                        for p in self.positions.get("open", [])
                    )
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats.get("total_pnl", 0.0),
                        "equity": stats["balance"] + open_staked,
                        "event": "martingale_trade",
                        "detail": (
                            f"{entry_reason.upper()} {coin.upper()} "
                            f"{buy_outcome} {title[:30]}"
                        ),
                    })
                except Exception:
                    pass

                save_positions(self.positions)
                self.trades_entered += 1

                if self.on_trade:
                    try:
                        self.on_trade(trade_record)
                    except Exception as e:
                        print(f"[MARTINGALE] Callback error: {e}", flush=True)

        return entered

    # -------------------------------------------------------------------------
    # TP / SL disabled — hold to resolution
    # -------------------------------------------------------------------------

    def check_stop_losses(self) -> int:
        return 0

    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["engine"] = "martingale"
        stats["trigger_1"] = self.trigger_1
        stats["trigger_2"] = self.trigger_2
        stats["lot_multiplier"] = self.lot_multiplier

        all_history = self.positions.get("stats", {}).get("balance_history", [])
        raw_history = [
            h for h in all_history
            if str(h.get("event", "")).startswith("martingale")
        ]
        stats["balance_history"] = self._thin_balance_history(raw_history)
        return stats
