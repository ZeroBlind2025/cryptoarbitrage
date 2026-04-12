#!/usr/bin/env python3
"""
INFORMED MONEY ENGINE
======================

Theory: smart money enters crypto up/down markets EARLY at good
liquidity.  When either side of a market crosses 60¢, that's the
signal informed traders have taken a directional view — follow them
at market price.

Setup is similar to the momentum engine but stripped to the bone:
  - Guard 1: price >= 60¢ on either side
  - Guard 2: betting both sides disabled (once any side of a market
    has been bought we never touch the opposite side)
  - Entry:   market order (FOK) at the current ask

No entry delays.  No no-trade hours.  No max-price ceiling beyond a
sanity cap to avoid already-resolved tokens.  No re-entry.  No
cooldown.  No hedging.  No stop loss.  Pure theory test.
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from copy_trader import (
    ALGO_STARTING_BALANCE,
    PRICE_BUFFER_BPS,
    detect_coin,
    get_clob_client,
    place_bet,
    save_positions,
)

from momentum_engine import (
    MomentumEngine,
    _log_trade,
    discover_active_markets,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

# Minimum price to enter — 60¢ on either side
INFORMED_MIN_ENTRY_PRICE = float(os.getenv("INFORMED_MIN_ENTRY_PRICE", "0.60"))

# Sanity cap — avoid buying outcomes that are essentially already resolved
INFORMED_MAX_ENTRY_PRICE = float(os.getenv("INFORMED_MAX_ENTRY_PRICE", "0.99"))

# How often the background loop ticks
POLL_INTERVAL = int(os.getenv("INFORMED_POLL_INTERVAL", "1"))


# =============================================================================
# ENGINE
# =============================================================================

class InformedMoneyEngine(MomentumEngine):
    """Minimal-guard entry engine testing the 'smart money enters early' theory.

    Subclasses MomentumEngine to reuse market discovery, WebSocket price
    streaming, the CLOB REST cache and position plumbing.  Overrides the
    entry loop, resolution loop, stats and start banner so it operates on
    `source == "informed"` positions instead of momentum ones.
    """

    SOURCE_TAG = "informed"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Override entry thresholds inherited from the momentum engine
        self.min_entry_price = INFORMED_MIN_ENTRY_PRICE
        self.max_entry_price = INFORMED_MAX_ENTRY_PRICE
        # Global min/max only — no per-interval brackets
        self.interval_price_brackets = {}
        # One entry per market (either Up OR Down, never both)
        self.max_entries_per_market = 1

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self):
        """Initialise WS, client and seed state from open informed positions."""
        # Delays/no-trade-hours/entry-delay guards are not honoured anyway because
        # we override scan_and_trade; keep these attrs set for the parent helpers.
        self._dry_run_no_probe = True
        self._dry_run_no_delays = True

        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(
            f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items())
        )
        print("\n" + "#" * 70)
        print("#" + " " * 68 + "#")
        print("#" + "  INFORMED MONEY ENGINE  —  STARTING".center(68) + "#")
        print("#" + " " * 68 + "#")
        print("#" * 70)
        print(f"  Class:  {type(self).__name__} "
              f"(scan_and_trade={type(self).scan_and_trade.__qualname__})")
        print("  Theory: smart money enters early at good liquidity")
        print(f"  Entry:  price >= {self.min_entry_price * 100:.0f}¢ "
              f"on EITHER side (Up AND Down, market order, FOK)")
        print(f"  Sanity cap: price < {self.max_entry_price * 100:.0f}¢")
        print("  Both-sides betting: DISABLED (one entry per market total)")
        print("  Stop loss: DISABLED (ride each bet to resolution)")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance: ${balance:.2f}")
        print(f"  Mode:    {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Poll:    {POLL_INTERVAL}s")
        print("#" * 70 + "\n", flush=True)

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[INFORMED] Failed to init client. Running in dry-run mode.",
                      flush=True)
                self.dry_run = True

        self._start_ws()

        # Seed entered_markets from existing open informed positions only.
        # Momentum-sourced positions share the same positions dict but belong
        # to a different engine, so we leave them alone.
        for pos in self.positions.get("open", []):
            if pos.get("source") != self.SOURCE_TAG:
                continue
            cid = pos.get("condition_id", "")
            tid = pos.get("token_id", "")
            ep = pos.get("entry_price", 0)
            if cid and tid:
                mk = (cid, tid)
                self.entered_markets[mk] = ep
                self.market_entry_count[mk] = self.market_entry_count.get(mk, 0) + 1

        if self.entered_markets:
            print(f"[INFORMED] Resumed {len(self.entered_markets)} "
                  f"open entries from positions file", flush=True)

    # -------------------------------------------------------------------------
    # Main scan/entry loop
    # -------------------------------------------------------------------------

    def scan_and_trade(self) -> int:
        """Find markets priced >= 60¢ on either side and buy the first qualifier.

        Only two guards:
          1. price >= min_entry_price (60¢ by default)
          2. neither side of this market has been bought yet
        """
        self.scans_completed += 1

        # --- WS health check ---
        if self.ws and self.ws.is_stale():
            last_msg = self.ws.last_message_time
            elapsed = (datetime.now() - last_msg).total_seconds() if last_msg else float("inf")
            print(f"[INFORMED] WS STALE: no messages in {elapsed:.0f}s, "
                  f"forcing reconnect", flush=True)
            self.ws.force_reconnect()

        self._refresh_ws_tokens()
        self._check_5m_boundary()

        # --- Market discovery (cached, refreshed in background) ---
        now = time.time()
        need_discovery = (
            now - self._last_market_discovery >= self._market_discovery_interval
            or not self._cached_markets
        )
        if need_discovery and not self._discovery_in_progress:
            if not self._cached_markets:
                # First run: block to get initial market list
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
                        print(f"[INFORMED] Background discovery error: {e}",
                              flush=True)
                    finally:
                        self._discovery_in_progress = False

                threading.Thread(target=_bg_discover, daemon=True).start()

        markets = self._cached_markets
        if not markets:
            return 0

        # CLOB REST price refresh (periodic fallback for tokens without WS)
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

            # Per-coin pause still honoured (dashboard convenience)
            if coin in self.paused_coins:
                continue

            # Skip already-resolved markets (one side == 1, other == 0)
            prices_raw = market.get("prices", [])
            if (len(prices_raw) == 2
                    and prices_raw[0] in (0.0, 1.0)
                    and prices_raw[1] in (0.0, 1.0)):
                continue

            # Skip expired markets
            if end_date is not None:
                minutes_left = (
                    end_date - datetime.now(timezone.utc)
                ).total_seconds() / 60
                if minutes_left < 0:
                    continue
            else:
                minutes_left = market.get("minutes_until_close")

            # --- GUARD: betting both sides disabled ---
            # If either side of this market has already been bought, skip it.
            tid_a = market["token_ids"][0]
            tid_b = market["token_ids"][1]
            if ((condition_id, tid_a) in self.entered_markets
                    or (condition_id, tid_b) in self.entered_markets):
                continue

            _mkt_label = f"{coin.upper()}_{market['interval']} {slug[:30]}"

            # --- DIAGNOSTIC: log BOTH sides' prices so DOWN can't silently
            # be "ignored".  Logs every ~30s (periodic summary) AND every
            # scan when either side is within 5¢ of the entry threshold.
            _side_prices = []
            for _oi in range(2):
                _tid = market["token_ids"][_oi]
                _out = market["outcomes"][_oi]
                _lp = self.get_live_price(_tid)
                _gp = market["prices"][_oi] if _oi < len(market.get("prices", [])) else None
                _p = _lp if _lp is not None else _gp
                _src = "live" if _lp is not None else "gamma"
                _side_prices.append((_out, _p, _src))

            _near_threshold = any(
                p is not None and p >= (self.min_entry_price - 0.05)
                for _, p, _ in _side_prices
            )
            _periodic = (self.scans_completed % 30 == 1)
            if _near_threshold or _periodic:
                _mk_str = " ".join(
                    f"{o}={p*100:.1f}¢({s})" if p is not None else f"{o}=?"
                    for o, p, s in _side_prices
                )
                print(f"[INFORMED] EVAL {_mkt_label}: {_mk_str}", flush=True)

            # Check each side; enter the first one that qualifies.
            for oi in range(2):
                outcome = market["outcomes"][oi]
                token_id = market["token_ids"][oi]
                gamma_price = market["prices"][oi]

                live_price = self.get_live_price(token_id)
                price = live_price if live_price is not None else gamma_price
                if price is None:
                    if _near_threshold:
                        print(f"[INFORMED] SKIP  {_mkt_label} {outcome}: "
                              f"no price available", flush=True)
                    continue

                # --- GUARD: price must be >= 60¢ ---
                if price < self.min_entry_price:
                    if _near_threshold:
                        print(
                            f"[INFORMED] SKIP  {_mkt_label} {outcome} "
                            f"@ {price * 100:.1f}¢: below "
                            f"{self.min_entry_price * 100:.0f}¢ threshold",
                            flush=True,
                        )
                    continue

                # Sanity cap — don't buy effectively-resolved outcomes
                if price >= self.max_entry_price:
                    print(
                        f"[INFORMED] SKIP  {_mkt_label} {outcome} "
                        f"@ {price * 100:.1f}¢: above sanity cap "
                        f"{self.max_entry_price * 100:.0f}¢",
                        flush=True,
                    )
                    continue

                market_key = (condition_id, token_id)

                # --- ENTER THE TRADE (market order) ---
                full_lot = self.coin_bet_amounts.get(coin, self.bet_amount)
                trade_amount = full_lot
                title = (question or slug)[:50]

                print(f"\n[INFORMED] ENTERING {coin.upper()} {outcome} "
                      f"@ {price * 100:.1f}¢", flush=True)
                print(f"           Market:   {title}", flush=True)
                print(f"           Interval: {market['interval']}", flush=True)
                print(f"           Amount:   ${trade_amount:.2f}", flush=True)

                trade_record = {
                    "id": f"informed_{condition_id[:12]}_{oi}_{int(time.time())}",
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
                    print(f"           DRY RUN — would buy @ {price * 100:.1f}¢",
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
                        print(f"           EXECUTED @ {price * 100:.1f}¢",
                              flush=True)
                        trade_record["status"] = "filled"
                        entered += 1
                        self.total_spent += trade_amount
                    else:
                        print("           FAILED!", flush=True)
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
                    "engine": "informed",
                })

                if trade_record["status"] in ("filled", "dry_run"):
                    # Track the entry so we never buy the opposite side later
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
                            "event": "informed_trade",
                            "detail": f"{coin.upper()} {outcome} {title[:30]}",
                        })
                    except Exception:
                        pass

                    save_positions(self.positions)
                    print(f"           Position saved. Balance: "
                          f"${self.positions['stats'].get('balance', 0):.2f}",
                          flush=True)

                    self.trades_entered += 1

                    if self.on_trade:
                        try:
                            self.on_trade(trade_record)
                        except Exception as e:
                            print(f"[INFORMED] Callback error: {e}", flush=True)

                    # One entry per market — stop checking the other side
                    break

        return entered

    # -------------------------------------------------------------------------
    # Stop loss — disabled.  The point of the experiment is to ride each bet
    # to resolution and measure whether 60¢ crosses predict the winner.
    # -------------------------------------------------------------------------

    def check_stop_losses(self) -> int:
        return 0

    # -------------------------------------------------------------------------
    # Resolution — same shape as MomentumEngine.check_resolutions but filtered
    # to source == "informed" positions.
    # -------------------------------------------------------------------------

    def check_resolutions(self):
        from copy_trader import get_market_resolution, retry_pending_redemptions

        now = time.time()

        try:
            retry_pending_redemptions(dry_run=self.dry_run)
        except Exception:
            pass

        if now - self.last_resolution_check < self.resolution_check_interval:
            return

        self.last_resolution_check = now

        open_positions = self.positions.get("open", [])
        informed_positions = [
            p for p in open_positions if p.get("source") == self.SOURCE_TAG
        ]
        if not informed_positions:
            return

        resolved_count = 0
        _resolution_cache: dict = {}

        for position in informed_positions[:]:
            condition_id = position.get("condition_id", "")
            slug = position.get("slug", "")
            token_id = position.get("token_id", "")
            our_outcome = position.get("outcome")

            if not token_id and not condition_id and not slug:
                continue

            cache_key = condition_id or slug or token_id
            if cache_key in _resolution_cache:
                result = dict(_resolution_cache[cache_key])
                winning_tid = result.get("winning_token_id", "")
                if winning_tid and token_id:
                    result["our_token_won"] = (
                        str(token_id).strip() == str(winning_tid).strip()
                    )
                elif "our_token_won" in result:
                    del result["our_token_won"]
            else:
                result = get_market_resolution(
                    condition_id=condition_id,
                    slug=slug,
                    token_id=token_id,
                    our_outcome=our_outcome,
                )
                _resolution_cache[cache_key] = result

            if not result or not result.get("resolved"):
                # On-chain only — no live-price shortcuts.  Last-second
                # WS price spikes have caused paper-trading mismarks,
                # so we wait for the CLOB API winner to come back before
                # marking a position WIN/LOSS.
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

            if "our_token_won" in result and result["our_token_won"] is not None:
                won = result["our_token_won"]

            if won is None and winning_outcome and our_outcome:
                our_norm = our_outcome.lower().strip()
                win_norm = winning_outcome.lower().strip()
                if (our_norm == win_norm
                        or our_norm.startswith(win_norm)
                        or win_norm.startswith(our_norm)):
                    won = True
                else:
                    won = False

            if won is None and winning_index is not None and our_index is not None:
                won = (winning_index == our_index)

            # Safety net: token id says LOSS but outcome name matches — trust name
            if won is False and winning_outcome and our_outcome:
                our_norm = our_outcome.lower().strip()
                win_norm = winning_outcome.lower().strip()
                if (our_norm == win_norm
                        or our_norm.startswith(win_norm)
                        or win_norm.startswith(our_norm)):
                    print("[INFORMED] OVERRIDE: token_id said LOSS but outcome "
                          "name matches. Correcting to WIN.", flush=True)
                    won = True

            # No live-price fallback: if on-chain said "resolved" but we
            # can't derive a winner from outcome name / index / token id,
            # leave won=None so the unknown-timeout path below handles it.

            pnl = 0.0
            if won is True:
                if entry_price > 0:
                    payout = amount / entry_price
                    pnl = payout - amount
                else:
                    pnl = amount * 3
                position["result"] = "WIN"
                position["pnl"] = pnl
                self.positions["stats"]["wins"] = (
                    self.positions["stats"].get("wins", 0) + 1
                )
                print(f"[INFORMED] WIN: {position['market'][:30]} | "
                      f"+${pnl:.2f}", flush=True)

                # Auto-redeem winning shares on-chain → USDC
                try:
                    from copy_trader import (
                        _is_pending_redemption,
                        _log_copy_trade,
                        _queue_pending_redemption,
                        redeem_winning_position,
                    )
                    if _is_pending_redemption(condition_id):
                        position["redeemed"] = False
                    else:
                        redeemed = redeem_winning_position(
                            condition_id=condition_id,
                            token_id=token_id,
                            dry_run=self.dry_run,
                            slug=slug,
                        )
                        position["redeemed"] = bool(redeemed)
                        _log_copy_trade("informed_redeem", {
                            "market": position.get("market", ""),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "redeemed": bool(redeemed),
                            "dry_run": self.dry_run,
                            "pnl": pnl,
                        })
                        if redeemed is True:
                            print(f"[INFORMED] Auto-redeemed: "
                                  f"{position['market'][:30]}", flush=True)
                        elif redeemed == "rate_limited":
                            print(f"[INFORMED] Relay rate-limited — queuing: "
                                  f"{position['market'][:30]}", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                        elif redeemed is None:
                            print(f"[INFORMED] Not redeemable yet — queuing: "
                                  f"{position['market'][:30]}", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                        else:
                            print(f"[INFORMED] Redemption failed — queued: "
                                  f"{position['market'][:30]}", flush=True)
                            _queue_pending_redemption(condition_id, token_id, slug)
                except Exception as e:
                    position["redeemed"] = False
                    print(f"[INFORMED] Redemption error: {e}", flush=True)
                    try:
                        from copy_trader import (
                            _is_pending_redemption,
                            _queue_pending_redemption,
                        )
                        if not _is_pending_redemption(condition_id):
                            _queue_pending_redemption(condition_id, token_id, slug)
                    except Exception:
                        pass

            elif won is False:
                pnl = -amount
                position["result"] = "LOSS"
                position["pnl"] = pnl
                self.positions["stats"]["losses"] = (
                    self.positions["stats"].get("losses", 0) + 1
                )
                print(f"[INFORMED] LOSS: {position['market'][:30]} | "
                      f"-${amount:.2f}", flush=True)
            else:
                # Interval-aware retry window before marking UNKNOWN
                if "_first_resolve_check" not in position:
                    position["_first_resolve_check"] = time.time()
                first_check = position["_first_resolve_check"]
                elapsed_mins = (time.time() - first_check) / 60
                interval = position.get("interval", "")
                if interval == "5m":
                    max_wait_mins = 15
                elif interval == "15m":
                    max_wait_mins = 30
                else:
                    max_wait_mins = 120
                if elapsed_mins < max_wait_mins:
                    continue
                position["result"] = "UNKNOWN"
                position["pnl"] = 0
                print(f"[INFORMED] UNKNOWN after {elapsed_mins:.0f}min: "
                      f"{position['market'][:30]}", flush=True)

            self.positions["stats"]["total_pnl"] = (
                self.positions["stats"].get("total_pnl", 0) + pnl
            )

            try:
                stats = self.positions["stats"]
                bal = stats.get("balance", ALGO_STARTING_BALANCE)
                if won is True:
                    payout = amount / entry_price if entry_price > 0 else amount
                    bal += payout
                stats["balance"] = bal
                open_staked = sum(
                    p.get("amount", 0)
                    for p in self.positions.get("open", [])
                    if p is not position
                )
                event_type = (
                    "win" if won is True
                    else "loss" if won is False
                    else "resolved"
                )
                stats.setdefault("balance_history", []).append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "balance": bal,
                    "pnl": stats.get("total_pnl", 0.0),
                    "equity": bal + open_staked,
                    "event": f"informed_{event_type}",
                    "detail": f"{position.get('outcome', '?')} "
                              f"{position.get('market', '?')[:30]}",
                })
            except Exception:
                pass

            position["resolved_at"] = datetime.now(timezone.utc).isoformat()
            position["winning_outcome"] = winning_outcome
            position["won"] = won
            _log_trade("resolved", {
                "id": position.get("id", ""),
                "coin": (
                    position.get("slug", "").split("-")[0]
                    if position.get("slug") else ""
                ),
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
                "engine": "informed",
            })
            self.positions["open"].remove(position)
            self.positions.setdefault("resolved", []).append(position)
            resolved_count += 1

            if self.on_resolution:
                try:
                    self.on_resolution(position)
                except Exception as e:
                    print(f"[INFORMED] Resolution callback error: {e}", flush=True)

        if resolved_count > 0:
            save_positions(self.positions)
            stats = self.positions["stats"]
            print(f"[INFORMED] {resolved_count} resolved. "
                  f"Record: {stats.get('wins', 0)}W/{stats.get('losses', 0)}L, "
                  f"PnL: ${stats.get('total_pnl', 0):+.2f}", flush=True)

    # -------------------------------------------------------------------------
    # Stats — filters shared positions dict by source == "informed"
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = self.positions.get("stats", {})
        open_positions = list(self.positions.get("open", []))
        informed_open = [
            p for p in open_positions if p.get("source") == self.SOURCE_TAG
        ]
        informed_resolved = [
            p for p in self.positions.get("resolved", [])
            if p.get("source") == self.SOURCE_TAG
        ]

        m_wins = 0
        m_losses = 0
        m_total_pnl = 0.0
        for p in informed_resolved:
            pnl = p.get("pnl", 0)
            won = p.get("won")
            if won is True:
                m_wins += 1
            elif won is False:
                m_losses += 1
            m_total_pnl += float(pnl) if pnl else 0.0

        m_total = m_wins + m_losses
        m_win_rate = (m_wins / m_total * 100) if m_total > 0 else 0.0
        m_open_staked = sum(float(p.get("amount", 0)) for p in informed_open)

        coin_roi = self._compute_coin_roi(informed_open, informed_resolved)

        open_by_coin: dict = {}
        for pos in informed_open:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            open_by_coin.setdefault(coin, []).append({
                "market": pos.get("market", "")[:60],
                "outcome": pos.get("outcome", ""),
                "entry_price": float(pos.get("entry_price", 0) or 0),
                "amount": float(pos.get("amount", 0) or 0),
                "timestamp": pos.get("timestamp", ""),
            })

        raw_history = [
            h for h in stats.get("balance_history", [])
            if str(h.get("event", "")).startswith(self.SOURCE_TAG)
        ]
        balance_history = self._thin_balance_history(raw_history)

        return {
            "engine": "informed_money",
            "trades_entered": self.trades_entered,
            "trades_skipped": self.trades_skipped,
            "total_spent": self.total_spent,
            "scans_completed": self.scans_completed,
            "open_positions": len(informed_open),
            "resolved_positions": len(informed_resolved),
            "wins": m_wins,
            "losses": m_losses,
            "stop_losses": 0,
            "total_pnl": m_total_pnl,
            "win_rate": m_win_rate,
            "open_staked": m_open_staked,
            "coin_roi": coin_roi,
            "open_by_coin": open_by_coin,
            "balance_history": balance_history,
            "min_entry_price": self.min_entry_price,
            "max_entry_price": self.max_entry_price,
            "max_entries_per_market": self.max_entries_per_market,
            "bet_amount": self.bet_amount,
            "coin_bet_amounts": {k: v for k, v in self.coin_bet_amounts.items()},
            "paused_coins": list(self.paused_coins),
            "dry_run": self.dry_run,
            "active_entries": {
                f"{cid[:12]}..._t{tid[-8:]}": f"{price * 100:.1f}¢"
                for (cid, tid), price in self.entered_markets.items()
            },
        }
