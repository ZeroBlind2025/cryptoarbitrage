#!/usr/bin/env python3
"""
SIMPLE MOMENTUM ENGINE
=======================

Stripped-down momentum engine testing a single hypothesis:
enter any market when the WS price crosses 75¢, stop loss at
12% off entry.  That's it.

Rules:
  - Enter when price >= 75¢ (SIMPLE_MIN_ENTRY_PRICE)
  - Stop loss at 12% below ENTRY price (fixed, not trailing)
  - One entry per market total — no re-entries, no hedges,
    no cooldowns, no no-trade hours, no per-interval brackets

Subclasses InformedMoneyEngine to reuse market discovery,
WebSocket streaming, CLOB REST cache, resolution logic and
per-source stats.  Overrides scan_and_trade (same-side entry)
and check_stop_losses (12% fixed from entry).
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from copy_trader import (
    ALGO_STARTING_BALANCE,
    PRICE_BUFFER_BPS,
    STOP_LOSS_PCT,
    detect_coin,
    get_clob_client,
    place_bet,
    place_sell,
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

# Minimum price to enter — 75¢ on either side
SIMPLE_MIN_ENTRY_PRICE = float(os.getenv("SIMPLE_MIN_ENTRY_PRICE", "0.75"))

# Sanity cap — avoid buying outcomes that are essentially already resolved
SIMPLE_MAX_ENTRY_PRICE = float(os.getenv("SIMPLE_MAX_ENTRY_PRICE", "0.99"))

# Fixed stop loss: sell when price drops this % below ENTRY price
SIMPLE_STOP_LOSS_PCT = float(os.getenv("SIMPLE_STOP_LOSS_PCT", "12"))

# How often the background loop ticks
POLL_INTERVAL = int(os.getenv("SIMPLE_POLL_INTERVAL", "1"))


# =============================================================================
# ENGINE
# =============================================================================

class SimpleMomentumEngine(InformedMoneyEngine):
    """Bare-bones momentum engine: enter >= 75¢, stop loss 12% from entry.

    Inherits market discovery, WS prices, CLOB REST fallback, resolution
    and per-source stats from InformedMoneyEngine/MomentumEngine.
    """

    SOURCE_TAG = "simple_momentum"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_entry_price = SIMPLE_MIN_ENTRY_PRICE
        self.max_entry_price = SIMPLE_MAX_ENTRY_PRICE
        self.stop_loss_pct = SIMPLE_STOP_LOSS_PCT
        # No per-interval brackets
        self.interval_price_brackets = {}
        # One entry per market, no re-entries
        self.max_entries_per_market = 1
        # Track at condition_id level — structurally prevents both-sides entry
        self.entered_condition_ids: set = set()

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
        print("#" + "  SIMPLE MOMENTUM ENGINE  —  STARTING".center(68) + "#")
        print("#" + " " * 68 + "#")
        print("#" * 70)
        print(f"  Class:     {type(self).__name__}")
        print(f"  Entry:     price >= {self.min_entry_price * 100:.0f}c "
              f"on EITHER side")
        print(f"  Sanity:    price < {self.max_entry_price * 100:.0f}c")
        print(f"  Stop loss: {self.stop_loss_pct:.0f}% below entry (fixed)")
        print("  Re-entry:  DISABLED (one entry per market)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance:   ${balance:.2f}")
        print(f"  Mode:      {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll:      {POLL_INTERVAL}s")
        print("#" * 70 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[SIMPLE] Failed to init client. Running in dry-run mode.",
                      flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_condition_ids + entered_markets from open positions
        for pos in self.positions.get("open", []):
            if pos.get("source") != self.SOURCE_TAG:
                continue
            cid = pos.get("condition_id", "")
            tid = pos.get("token_id", "")
            ep = pos.get("entry_price", 0)
            if cid:
                self.entered_condition_ids.add(cid)
            if cid and tid:
                mk = (cid, tid)
                self.entered_markets[mk] = ep
                self.market_entry_count[mk] = self.market_entry_count.get(mk, 0) + 1

        if self.entered_condition_ids:
            print(f"[SIMPLE] Resumed {len(self.entered_condition_ids)} "
                  f"open entries from positions file", flush=True)

    # -------------------------------------------------------------------------
    # Main scan/entry loop
    # -------------------------------------------------------------------------

    def scan_and_trade(self) -> int:
        """Enter any market where either side prices >= 75c.

        Guards:
          1. price >= min_entry_price
          2. price < max_entry_price (sanity cap)
          3. neither side of this market has been bought yet
        """
        self.scans_completed += 1

        # --- WS health ---
        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (datetime.now() - last_msg).total_seconds() if last_msg else float("inf")
            print(f"[SIMPLE] WS STALE: no messages in {elapsed:.0f}s, "
                  f"forcing reconnect", flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()
        self._check_5m_boundary()

        # --- Market discovery ---
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
                        print(f"[SIMPLE] Background discovery error: {e}",
                              flush=True)
                    finally:
                        self._discovery_in_progress = False

                threading.Thread(target=_bg_discover, daemon=True).start()

        markets = self._cached_markets
        if not markets:
            return 0

        # CLOB REST fallback
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

            # Skip resolved
            prices_raw = market.get("prices", [])
            if (len(prices_raw) == 2
                    and prices_raw[0] in (0.0, 1.0)
                    and prices_raw[1] in (0.0, 1.0)):
                continue

            # Skip expired
            if end_date is not None:
                minutes_left = (
                    end_date - datetime.now(timezone.utc)
                ).total_seconds() / 60
                if minutes_left < 0:
                    continue
            else:
                minutes_left = market.get("minutes_until_close")

            # --- GUARD: one entry per market (condition_id level) ---
            if condition_id in self.entered_condition_ids:
                continue

            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # Check each side
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                gamma_price = market["prices"][oi]

                live_price = self.get_live_price(token_id)
                price = live_price if live_price is not None else gamma_price
                if price is None:
                    continue

                # --- GUARD: price >= 75c ---
                if price < self.min_entry_price:
                    continue

                # --- GUARD: sanity cap ---
                if price >= self.max_entry_price:
                    continue

                market_key = (condition_id, token_id)

                # --- ENTER ---
                full_lot = self.coin_bet_amounts.get(coin, self.bet_amount)
                trade_amount = full_lot
                title = (question or slug)[:50]

                print(f"\n[SIMPLE] ENTERING {coin.upper()} {outcome} "
                      f"@ {price * 100:.1f}c", flush=True)
                print(f"         Market:   {title}", flush=True)
                print(f"         Interval: {market['interval']}", flush=True)
                print(f"         Amount:   ${trade_amount:.2f}", flush=True)

                trade_record = {
                    "id": f"simple_{condition_id[:12]}_{oi}_{int(time.time())}",
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
                    "source": self.SOURCE_TAG,
                }

                if self.dry_run:
                    print(f"         DRY RUN — would buy @ {price * 100:.1f}c",
                          flush=True)
                    trade_record["status"] = "dry_run"
                    entered += 1
                else:
                    buffer = PRICE_BUFFER_BPS / 10000
                    max_price = min(price * (1 + buffer), 0.99)
                    fill = place_bet(
                        self.client, token_id, trade_amount, max_price=max_price
                    )
                    if fill.get("success"):
                        if fill.get("fill_price"):
                            price = fill["fill_price"]
                            trade_record["price"] = price
                        print(f"         EXECUTED @ {price * 100:.1f}c",
                              flush=True)
                        trade_record["status"] = "filled"
                        entered += 1
                        self.total_spent += trade_amount
                    else:
                        print("         FAILED!", flush=True)
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
                    "engine": "simple_momentum",
                })

                if trade_record["status"] in ("filled", "dry_run"):
                    self.entered_condition_ids.add(condition_id)
                    self.entered_markets[market_key] = price
                    self.market_entry_count[market_key] = 1
                    self.last_trade_time[market_key] = time.time()

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
                        "peak_price": price,
                        "amount": trade_amount,
                        "potential_payout": trade_amount / price if price > 0 else 0,
                        "dry_run": self.dry_run,
                        "source": self.SOURCE_TAG,
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
                            "event": "simple_momentum_trade",
                            "detail": f"{coin.upper()} {outcome} {title[:30]}",
                        })
                    except Exception:
                        pass

                    save_positions(self.positions)
                    print(f"         Position saved. Balance: "
                          f"${self.positions['stats'].get('balance', 0):.2f}",
                          flush=True)

                    self.trades_entered += 1

                    if self.on_trade:
                        try:
                            self.on_trade(trade_record)
                        except Exception as e:
                            print(f"[SIMPLE] Callback error: {e}", flush=True)

                    # One entry per market — stop checking the other side
                    break

        return entered

    # -------------------------------------------------------------------------
    # Stop loss — 12% fixed from entry price (NOT trailing)
    # -------------------------------------------------------------------------

    def check_stop_losses(self) -> int:
        """Sell when price drops SIMPLE_STOP_LOSS_PCT% below entry price.

        Fixed stop (not trailing): threshold = entry_price * (1 - pct/100).
        """
        if self.stop_loss_pct <= 0:
            return 0

        open_positions = self.positions.get("open", [])
        our_positions = [
            p for p in open_positions if p.get("source") == self.SOURCE_TAG
        ]
        if not our_positions:
            return 0

        stopped = 0
        no_price_count = 0
        threshold_mult = 1 - (self.stop_loss_pct / 100)  # 0.875 for 12.5%

        for position in our_positions[:]:
            entry_price = position.get("entry_price", 0)
            token_id = position.get("token_id", "")
            if entry_price <= 0 or not token_id:
                continue

            live_price = self.get_live_price(token_id)
            if not live_price or live_price <= 0:
                no_price_count += 1
                continue

            stop_price = entry_price * threshold_mult
            if live_price >= stop_price:
                continue

            # --- STOP LOSS TRIGGERED ---
            condition_id = position.get("condition_id", "")
            shares = position.get("potential_payout", 0)
            title = position.get("market", "Unknown")[:50]
            outcome = position.get("outcome", "?")
            amount_spent = position.get("amount", 0)
            coin = detect_coin(position.get("slug", ""), title)
            drop_from_entry = (1 - live_price / entry_price) * 100

            print(f"\n[SIMPLE] *** STOP LOSS TRIGGERED ***", flush=True)
            print(f"         Market: {title}", flush=True)
            print(f"         {outcome} | Entry: {entry_price*100:.1f}c | "
                  f"Now: {live_price*100:.1f}c "
                  f"({drop_from_entry:.1f}% from entry)", flush=True)
            print(f"         Stop level: {stop_price*100:.1f}c "
                  f"(-{self.stop_loss_pct:.0f}% from entry)", flush=True)
            print(f"         Selling {shares:.2f} shares", flush=True)

            trade_record = {
                "id": f"simple_sl_{position.get('id', '')}_{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": position.get("slug", ""),
                "outcome": outcome,
                "side": "SELL",
                "reason": "stop_loss",
                "shares": shares,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "trigger_price": live_price,
                "coin": coin,
                "source": self.SOURCE_TAG,
            }

            if self.dry_run:
                trade_record["price"] = live_price
                trade_record["status"] = "dry_run"
            else:
                if token_id and self.client and shares > 0:
                    fill = place_sell(self.client, token_id, shares, min_price=0.01)
                    if fill.get("success"):
                        trade_record["status"] = "filled"
                        fill_price = fill.get("fill_price") or live_price
                        trade_record["price"] = fill_price
                        print(f"         STOP LOSS SELL EXECUTED! "
                              f"Fill: {fill_price:.4f}", flush=True)
                    else:
                        print(f"         STOP LOSS SELL FAILED!", flush=True)
                        trade_record["status"] = "failed"
                else:
                    trade_record["status"] = "error"

            if trade_record["status"] in ("filled", "dry_run"):
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
                    open_staked = sum(
                        p.get("amount", 0)
                        for p in self.positions.get("open", [])
                        if p is not position
                    )
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats["total_pnl"],
                        "equity": stats["balance"] + open_staked,
                        "event": "simple_momentum_stop_loss",
                        "detail": (
                            f"STOP LOSS {outcome} {title[:30]} "
                            f"drop={drop_from_entry:.1f}% pnl={pnl:+.2f} "
                            f"(returned ${proceeds:.2f})"
                        ),
                    })
                except Exception:
                    pass

                if position in self.positions.get("open", []):
                    self.positions["open"].remove(position)
                self.positions.setdefault("resolved", []).append(position)
                save_positions(self.positions)
                trade_record["pnl"] = pnl

                _log_trade("resolved", {
                    "id": position.get("id", ""),
                    "coin": (
                        position.get("slug", "").split("-")[0]
                        if position.get("slug") else ""
                    ),
                    "interval": position.get("interval", ""),
                    "outcome": position.get("outcome", ""),
                    "entry_price": entry_price,
                    "amount": amount_spent,
                    "result": "STOP_LOSS",
                    "pnl": pnl,
                    "won": False,
                    "slug": position.get("slug", ""),
                    "market": position.get("market", ""),
                    "condition_id": condition_id,
                    "engine": "simple_momentum",
                })

                stopped += 1
                print(f"         PnL: {pnl:+.2f} | "
                      f"Balance: ${self.positions['stats'].get('balance', 0):.2f}",
                      flush=True)

            self.trade_history.append(trade_record)

        if no_price_count > 0 and self.scans_completed % 60 == 0:
            print(f"[SIMPLE] SL check: {len(our_positions)} positions, "
                  f"{no_price_count} with no price data", flush=True)

        return stopped

    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["engine"] = "simple_momentum"
        stats["stop_loss_pct"] = self.stop_loss_pct

        # Count stop losses and compute spent/proceeds from resolved positions
        resolved = [
            p for p in self.positions.get("resolved", [])
            if p.get("source") == self.SOURCE_TAG
        ]
        sl_count = 0
        sl_spent = 0.0
        sl_proceeds = 0.0
        for p in resolved:
            if p.get("result") == "STOP_LOSS":
                sl_count += 1
                sl_spent += float(p.get("amount", 0) or 0)
                sl_proceeds += float(p.get("proceeds", 0) or 0)

        stats["stop_losses"] = sl_count
        stats["sl_spent"] = sl_spent
        stats["sl_proceeds"] = sl_proceeds

        # Filter balance history to simple_momentum events
        all_history = self.positions.get("stats", {}).get("balance_history", [])
        raw_history = [
            h for h in all_history
            if str(h.get("event", "")).startswith("simple_momentum")
        ]
        stats["balance_history"] = self._thin_balance_history(raw_history)

        return stats
