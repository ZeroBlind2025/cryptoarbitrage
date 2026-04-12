#!/usr/bin/env python3
"""
MIDLINE SPREAD ENGINE  (theory #21)
====================================

Theory: when a market is trading near the 50/50 midline, the side
that's slightly above 50c is overpricing an essentially coin-flip
outcome.  Buy that side at standard lot.  If the other side then
dips below 50c, stack the underdog at 150% lot.  A correct underdog
call pays 2.13x at 47c, so the stack more than covers the losing
leg.

Entry rules:
  - First entry:    any side whose live ask is in [50c, 60c) gets
                    entered at the standard per-coin lot
  - Opposite entry: once the first side is on the books, the moment
                    the opposite side's live ask drops below 50c we
                    enter it at MIDLINE_OPPOSITE_LOT_MULTIPLIER times
                    the standard lot (default 1.5x = 150%)
  - One entry per side per market, max.  Two positions total in a
    market if both conditions fire.
  - No re-entries beyond the two structural entries.  No hedges,
    no stop loss, no take profit.  Ride every trade to resolution.

Worked example (user's spec):
    Up   @ 51c  ->  $2.00 (standard lot)
    Down @ 47c  ->  $3.00 (150% of standard)

    If Up   wins: +$1.92 on Up, -$3.00 on Down  =  -$1.08 net
    If Down wins: -$2.00 on Up, +$3.38 on Down  =  +$1.38 net

Fill risk is real on the opposite side: if the market flips fast
the 47c tick may not be there to hit.  That's acknowledged and
accepted — the engine doesn't retry or chase.

Subclasses InformedMoneyEngine so market discovery, WS pricing,
the CLOB REST fallback cache, on-chain-only resolution, and per-
source stats are all inherited.  Only __init__, start,
scan_and_trade, check_stop_losses (disabled) and get_stats are
overridden.
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

# First entry qualifies when live ask is in [MIN, MAX).
MIDLINE_FIRST_PRICE_MIN = float(os.getenv("MIDLINE_FIRST_PRICE_MIN", "0.50"))
MIDLINE_FIRST_PRICE_MAX = float(os.getenv("MIDLINE_FIRST_PRICE_MAX", "0.60"))

# Opposite entry qualifies when live ask is STRICTLY below this.
MIDLINE_OPPOSITE_PRICE_MAX = float(os.getenv("MIDLINE_OPPOSITE_PRICE_MAX", "0.50"))

# Multiplier applied to the standard coin lot for the opposite-side stack.
MIDLINE_OPPOSITE_LOT_MULTIPLIER = float(
    os.getenv("MIDLINE_OPPOSITE_LOT_MULTIPLIER", "1.5")
)

# How often the background loop ticks.
POLL_INTERVAL = int(os.getenv("MIDLINE_POLL_INTERVAL", "1"))


# =============================================================================
# ENGINE
# =============================================================================

class MidlineSpreadEngine(InformedMoneyEngine):
    """Enters the side trading in [50c, 60c) at standard lot, then stacks
    the opposite side at 150% lot once it drops under 50c.  One entry per
    side per market, two total.  Rides to resolution.
    """

    SOURCE_TAG = "midline"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Entry-range thresholds (the parent's min_entry_price is only
        # relevant for the momentum flow, keep it in sync for dashboards).
        self.first_price_min = MIDLINE_FIRST_PRICE_MIN
        self.first_price_max = MIDLINE_FIRST_PRICE_MAX
        self.opposite_price_max = MIDLINE_OPPOSITE_PRICE_MAX
        self.opposite_lot_multiplier = MIDLINE_OPPOSITE_LOT_MULTIPLIER

        self.min_entry_price = MIDLINE_FIRST_PRICE_MIN
        self.max_entry_price = MIDLINE_FIRST_PRICE_MAX
        self.interval_price_brackets = {}
        # 1 entry per (condition_id, token_id); 2 positions possible per market
        self.max_entries_per_market = 1

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
        print("#" + "  MIDLINE SPREAD ENGINE  —  STARTING".center(68) + "#")
        print("#" + " " * 68 + "#")
        print("#" * 70)
        print(f"  Class:    {type(self).__name__} "
              f"(scan_and_trade={type(self).scan_and_trade.__qualname__})")
        print("  Theory:   spread the midline")
        print(f"  First:    side in [{self.first_price_min*100:.0f}c, "
              f"{self.first_price_max*100:.0f}c) @ standard lot")
        print(f"  Opposite: side <  {self.opposite_price_max*100:.0f}c @ "
              f"{self.opposite_lot_multiplier * 100:.0f}% lot")
        print("  Stacking: 1 entry per side (max 2 positions per market)")
        print("  Re-entry: DISABLED beyond the two structural entries")
        print("  Hedge:    DISABLED")
        print("  Stop loss / Take profit: DISABLED (ride to resolution)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance:  ${balance:.2f}")
        print(f"  Mode:     {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll:     {POLL_INTERVAL}s")
        print("#" * 70 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[MIDLINE] Failed to init CLOB client. "
                      "Falling back to DRY RUN.", flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_markets from existing open midline positions only.
        for pos in self.positions.get("open", []):
            if pos.get("source") != self.SOURCE_TAG:
                continue
            cid = pos.get("condition_id", "")
            tid = pos.get("token_id", "")
            ep = pos.get("entry_price", 0)
            if cid and tid:
                self.entered_markets[(cid, tid)] = ep
                self.market_entry_count[(cid, tid)] = 1

        if self.entered_markets:
            print(f"[MIDLINE] Resumed {len(self.entered_markets)} "
                  f"open entries from positions file", flush=True)

    # -------------------------------------------------------------------------
    # Main entry loop
    # -------------------------------------------------------------------------

    def scan_and_trade(self) -> int:
        self.scans_completed += 1

        # --- WS health check ---
        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (
                (datetime.now() - last_msg).total_seconds()
                if last_msg else float("inf")
            )
            print(f"[MIDLINE] WS STALE: no messages in {elapsed:.0f}s, "
                  f"forcing reconnect", flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()
        self._check_5m_boundary()

        # --- Market discovery (cached, background refresh) ---
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
                        prev = len(self._cached_markets)
                        self._cached_markets = new_markets
                        if len(new_markets) != prev:
                            self._refresh_ws_tokens(force=True)
                    except Exception as e:
                        print(f"[MIDLINE] Background discovery error: {e}",
                              flush=True)
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

            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # Iterate both sides.  Each side can qualify for either the
            # FIRST-entry path (in-range, and nothing in the market yet)
            # or the OPPOSITE-entry path (below floor, and the other side
            # is already held).  Same-side re-entries are structurally
            # impossible because we skip keys that are already in
            # self.entered_markets.
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                other_token_id = market["token_ids"][1 - oi]
                gamma_price = market["prices"][oi]

                market_key = (condition_id, token_id)
                opposite_key = (condition_id, other_token_id)

                # Already holding this exact side? Nothing to do.
                if market_key in self.entered_markets:
                    continue

                live_price = self.get_live_price(token_id)
                price = live_price if live_price is not None else gamma_price
                if price is None:
                    continue

                opposite_entered = opposite_key in self.entered_markets

                # --- FIRST-ENTRY PATH ---
                # Market is empty, this side is in the [50c, 60c) range.
                if not opposite_entered:
                    if not (self.first_price_min <= price < self.first_price_max):
                        continue
                    standard_lot = self.coin_bet_amounts.get(
                        coin, self.bet_amount
                    )
                    trade_amount = round(standard_lot, 2)
                    entry_type = "SPREAD-INIT"
                    lot_multiplier = 1.0

                # --- OPPOSITE-ENTRY PATH ---
                # We already hold the other side; this side is strictly
                # below the opposite-price floor (50c by default).
                else:
                    if price >= self.opposite_price_max:
                        continue
                    standard_lot = self.coin_bet_amounts.get(
                        coin, self.bet_amount
                    )
                    trade_amount = round(
                        standard_lot * self.opposite_lot_multiplier, 2
                    )
                    entry_type = "SPREAD-FADE"
                    lot_multiplier = self.opposite_lot_multiplier

                title = (question or slug)[:50]

                print(
                    f"\n[MIDLINE] {entry_type}: {coin.upper()} "
                    f"{outcome} @ {price * 100:.1f}c",
                    flush=True,
                )
                print(f"          Market:   {title}", flush=True)
                print(f"          Interval: {market['interval']}", flush=True)
                print(
                    f"          Amount:   ${trade_amount:.2f} "
                    f"(lot x {lot_multiplier})",
                    flush=True,
                )

                trade_record = {
                    "id": f"midline_{condition_id[:12]}_{oi}_{int(time.time())}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": title,
                    "slug": slug,
                    "outcome": outcome,
                    "outcome_index": oi,
                    "side": "BUY",
                    "amount": trade_amount,
                    "coin": coin,
                    "price": price,
                    "entry_type": entry_type,
                    "lot_multiplier": lot_multiplier,
                    "interval": market["interval"],
                    "source": self.SOURCE_TAG,
                }

                if self.dry_run:
                    print(
                        f"          DRY RUN - would buy @ {price * 100:.1f}c",
                        flush=True,
                    )
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
                        print(
                            f"          EXECUTED @ {price * 100:.1f}c",
                            flush=True,
                        )
                        trade_record["status"] = "filled"
                        entered += 1
                        self.total_spent += trade_amount
                    else:
                        print("          FAILED!", flush=True)
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
                    "entry_type": entry_type,
                    "lot_multiplier": lot_multiplier,
                    "minutes_until_close": minutes_left,
                    "engine": "midline",
                })

                if trade_record["status"] in ("filled", "dry_run"):
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
                        "amount": trade_amount,
                        "potential_payout": (
                            trade_amount / price if price > 0 else 0
                        ),
                        "dry_run": self.dry_run,
                        "source": self.SOURCE_TAG,
                        "entry_type": entry_type,
                        "lot_multiplier": lot_multiplier,
                    }
                    self.positions["open"].append(position)

                    try:
                        stats = self.positions["stats"]
                        stats["balance"] = (
                            stats.get("balance", ALGO_STARTING_BALANCE)
                            - trade_amount
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
                            "event": "midline_trade",
                            "detail": (
                                f"{coin.upper()} {outcome} {entry_type} "
                                f"{title[:30]}"
                            ),
                        })
                    except Exception:
                        pass

                    save_positions(self.positions)
                    print(
                        f"          Position saved. Balance: "
                        f"${self.positions['stats'].get('balance', 0):.2f}",
                        flush=True,
                    )
                    self.trades_entered += 1

                    if self.on_trade:
                        try:
                            self.on_trade(trade_record)
                        except Exception as e:
                            print(f"[MIDLINE] Callback error: {e}", flush=True)

                    # Important: do NOT break here.  If we just made the
                    # FIRST entry, we want the same scan iteration to
                    # also check the other side for the OPPOSITE entry.
                    # The other-side iteration will then see
                    # opposite_entered=True and check the below-50c path.

        return entered

    # -------------------------------------------------------------------------
    # Stop loss / take profit — both disabled
    # -------------------------------------------------------------------------

    def check_stop_losses(self) -> int:
        return 0

    def check_take_profits(self) -> int:
        return 0

    # -------------------------------------------------------------------------
    # Stats — inherit the informed flow, just relabel the engine and add
    # the midline-specific thresholds.
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["engine"] = "midline_spread"
        stats["first_price_min"] = self.first_price_min
        stats["first_price_max"] = self.first_price_max
        stats["opposite_price_max"] = self.opposite_price_max
        stats["opposite_lot_multiplier"] = self.opposite_lot_multiplier
        return stats
