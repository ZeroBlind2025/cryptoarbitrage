#!/usr/bin/env python3
"""
CONTRARIAN FADE ENGINE
=======================

The inverse of the informed money engine.  Theory: when one side of
a crypto up/down market crosses 60¢, the OTHER side is worth buying
at market — the ~40¢ fade captures the revert if the move is noise.

Entry rules (single, simple):
  - A side reaches live price >= 60¢ (CONTRARIAN_TRIGGER_PRICE)
  - Buy the OPPOSITE side at market (FOK) at its current ask
  - One entry per market total — no re-entries, no hedges, no
    both-sides stacking, no stop loss

Subclasses InformedMoneyEngine so market discovery, WebSocket
pricing, the CLOB REST fallback cache, on-chain-only resolution and
per-source stats are all inherited.  Only __init__, start and
scan_and_trade are overridden.
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

# Any side crossing this price triggers a fade on the opposite side.
CONTRARIAN_TRIGGER_PRICE = float(os.getenv("CONTRARIAN_TRIGGER_PRICE", "0.60"))

# How often the background loop ticks.
POLL_INTERVAL = int(os.getenv("CONTRARIAN_POLL_INTERVAL", "1"))


# =============================================================================
# ENGINE
# =============================================================================

class ContrarianEngine(InformedMoneyEngine):
    """Fade engine: when either side of a market crosses 60¢, buy the opposite
    side at market.

    One entry per market, period.  Tracks entries at the condition_id level
    (not per-side) so re-entries and both-sides stacking are structurally
    impossible.
    """

    SOURCE_TAG = "contrarian"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The "60¢ trigger" is read per-scan from self.trigger_price.  We
        # keep min_entry_price in sync so any dashboard that peeks at it
        # shows the real threshold.
        self.trigger_price = CONTRARIAN_TRIGGER_PRICE
        self.min_entry_price = CONTRARIAN_TRIGGER_PRICE
        self.max_entry_price = 1.0
        self.interval_price_brackets = {}
        # Structural "one per market" — max entries across BOTH sides = 1
        self.max_entries_per_market = 1
        # Track entered markets at the condition_id level
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
        print("#" + "  CONTRARIAN FADE ENGINE  —  STARTING".center(68) + "#")
        print("#" + " " * 68 + "#")
        print("#" * 70)
        print(f"  Class:   {type(self).__name__} "
              f"(scan_and_trade={type(self).scan_and_trade.__qualname__})")
        print("  Theory:  fade the move — when one side crosses 60¢, buy the opposite")
        print(f"  Trigger: any side >= {self.trigger_price * 100:.0f}¢")
        print("  Entry:   opposite side at market (FOK)")
        print("  Stacking: 1 entry per market total (no re-entries, no hedges)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance: ${balance:.2f}")
        print(f"  Mode:    {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll:    {POLL_INTERVAL}s")
        print("#" * 70 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[CONTRARIAN] Failed to init CLOB client. Falling back to DRY RUN.",
                      flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_condition_ids from existing open contrarian positions
        for pos in self.positions.get("open", []):
            if pos.get("source") != self.SOURCE_TAG:
                continue
            cid = pos.get("condition_id", "")
            if cid:
                self.entered_condition_ids.add(cid)

        if self.entered_condition_ids:
            print(f"[CONTRARIAN] Resumed {len(self.entered_condition_ids)} "
                  f"open entries from positions file", flush=True)

    # -------------------------------------------------------------------------
    # Main entry loop
    # -------------------------------------------------------------------------

    def scan_and_trade(self) -> int:
        """When a side crosses 60¢, buy the OPPOSITE side at market."""
        self.scans_completed += 1

        # --- WS health check ---
        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (datetime.now() - last_msg).total_seconds() if last_msg else float("inf")
            print(f"[CONTRARIAN] WS STALE: no messages in {elapsed:.0f}s, "
                  f"forcing reconnect", flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()
        self._check_5m_boundary()

        # --- Market discovery (inherited cache + background refresh) ---
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
                        print(f"[CONTRARIAN] Background discovery error: {e}",
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

            # --- GUARD: one entry per market, period ---
            if condition_id in self.entered_condition_ids:
                continue

            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # --- Scan both sides for the trigger, fire on the first hit ---
            for trigger_oi in range(2):
                trigger_outcome = market["outcomes"][trigger_oi]
                trigger_token_id = market["token_ids"][trigger_oi]
                trigger_gamma = market["prices"][trigger_oi]

                trigger_live = self.get_live_price(trigger_token_id)
                trigger_price = (
                    trigger_live if trigger_live is not None else trigger_gamma
                )
                if trigger_price is None:
                    continue

                if trigger_price < self.trigger_price:
                    continue

                # --- TRIGGER HIT — fade the opposite side ---
                fade_oi = 1 - trigger_oi
                fade_outcome = market["outcomes"][fade_oi]
                fade_token_id = market["token_ids"][fade_oi]
                fade_gamma = market["prices"][fade_oi]

                fade_live = self.get_live_price(fade_token_id)
                fade_price = fade_live if fade_live is not None else fade_gamma
                if fade_price is None:
                    print(
                        f"[CONTRARIAN] SKIP {_mkt_label}: "
                        f"trigger {trigger_outcome} @ {trigger_price * 100:.1f}¢ "
                        f"but no price for fade side {fade_outcome}",
                        flush=True,
                    )
                    continue

                trade_amount = self.coin_bet_amounts.get(coin, self.bet_amount)
                title = (question or slug)[:50]

                print(
                    f"\n[CONTRARIAN] FADE TRIGGER {coin.upper()} "
                    f"{trigger_outcome} @ {trigger_price * 100:.1f}¢ "
                    f"→ buying {fade_outcome} @ {fade_price * 100:.1f}¢",
                    flush=True,
                )
                print(f"             Market:   {title}", flush=True)
                print(f"             Interval: {market['interval']}", flush=True)
                print(f"             Amount:   ${trade_amount:.2f}", flush=True)

                trade_record = {
                    "id": f"contrarian_{condition_id[:12]}_{fade_oi}_{int(time.time())}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": title,
                    "slug": slug,
                    "outcome": fade_outcome,
                    "outcome_index": fade_oi,
                    "side": "BUY",
                    "amount": trade_amount,
                    "coin": coin,
                    "price": fade_price,
                    "trigger_side": trigger_outcome,
                    "trigger_price": trigger_price,
                    "interval": market["interval"],
                    "source": self.SOURCE_TAG,
                }

                if self.dry_run:
                    print(
                        f"             DRY RUN — would buy {fade_outcome} "
                        f"@ {fade_price * 100:.1f}¢",
                        flush=True,
                    )
                    trade_record["status"] = "dry_run"
                    entered += 1
                else:
                    buffer = PRICE_BUFFER_BPS / 10000
                    max_price = min(fade_price * (1 + buffer), 0.99)
                    fill = place_bet(
                        self.client, fade_token_id, trade_amount, max_price=max_price
                    )
                    if fill.get("success"):
                        if fill.get("fill_price"):
                            fade_price = fill["fill_price"]
                            trade_record["price"] = fade_price
                        print(
                            f"             EXECUTED @ {fade_price * 100:.1f}¢",
                            flush=True,
                        )
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
                    "outcome": fade_outcome,
                    "price": fade_price,
                    "trigger_side": trigger_outcome,
                    "trigger_price": trigger_price,
                    "amount": trade_amount,
                    "slug": slug,
                    "market": title,
                    "condition_id": condition_id,
                    "token_id": fade_token_id,
                    "minutes_until_close": minutes_left,
                    "engine": "contrarian",
                })

                if trade_record["status"] in ("filled", "dry_run"):
                    # One entry per market — mark it and move on
                    self.entered_condition_ids.add(condition_id)
                    # Also populate entered_markets / market_entry_count /
                    # last_trade_time so inherited bookkeeping stays
                    # consistent (these aren't consulted for contrarian
                    # gating but a few inherited helpers read them).
                    self.entered_markets[(condition_id, fade_token_id)] = fade_price
                    self.market_entry_count[(condition_id, fade_token_id)] = 1
                    self.last_trade_time[(condition_id, fade_token_id)] = time.time()

                    position = {
                        "id": trade_record["id"],
                        "timestamp": trade_record["timestamp"],
                        "condition_id": condition_id,
                        "token_id": fade_token_id,
                        "outcome_index": fade_oi,
                        "outcome": fade_outcome,
                        "market": title,
                        "slug": slug,
                        "interval": market.get("interval", ""),
                        "end_date": end_date.isoformat() if end_date else None,
                        "entry_price": fade_price,
                        "amount": trade_amount,
                        "potential_payout": (
                            trade_amount / fade_price if fade_price > 0 else 0
                        ),
                        "dry_run": self.dry_run,
                        "source": self.SOURCE_TAG,
                        "trigger_side": trigger_outcome,
                        "trigger_price": trigger_price,
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
                            "event": "contrarian_trade",
                            "detail": f"{coin.upper()} {fade_outcome} {title[:30]}",
                        })
                    except Exception:
                        pass

                    save_positions(self.positions)
                    print(
                        f"             Position saved. Balance: "
                        f"${self.positions['stats'].get('balance', 0):.2f}",
                        flush=True,
                    )
                    self.trades_entered += 1

                    if self.on_trade:
                        try:
                            self.on_trade(trade_record)
                        except Exception as e:
                            print(f"[CONTRARIAN] Callback error: {e}", flush=True)

                    # One entry per market — break out of the side loop
                    break

        return entered

    # -------------------------------------------------------------------------
    # Stats — re-use informed's get_stats, just relabel the engine field
    # (SOURCE_TAG filters to contrarian positions automatically)
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["engine"] = "contrarian_fade"
        stats["trigger_price"] = self.trigger_price
        return stats
