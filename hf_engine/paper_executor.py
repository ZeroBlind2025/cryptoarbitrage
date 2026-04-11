"""
hf_engine.paper_executor
========================

Paper-trading executor for the Hawkes-GM engine.

The executor deliberately does **not** place any live orders, does not
touch the shared ``positions.json`` file, and does not import from
``copy_trader.py`` or ``momentum_engine.py``. It writes every hypothetical
fill to a dedicated JSONL log file under the engine's own log directory
so paper P&L can be reconciled independently of anything the momentum
or copy-trader engines are doing.

This is the only place in the HF engine where positions are opened or
closed. ``MarketState`` holds the current open position; this class
computes fills, writes logs, and updates the running exposure total.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Deque, Dict, List, Optional

from .config import HFEConfig
from .market_state import MarketState, PaperPosition, position_dollars


SIGNAL_BUFFER_SIZE = 400


class PaperExecutor:
    """Paper-only executor with its own P&L ledger."""

    def __init__(self, cfg: HFEConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._open_exposure_dollars = 0.0
        # Separate accounting for the two ways a position can close:
        #   - ``settle_at_resolution`` -> realized_* (the only honest
        #     P/L in paper mode, computed against the true outcome)
        #   - ``close_position`` -> mark_* (pre-resolution mark-to-
        #     market against the current CLOB mid; fictional unless
        #     there's a real counterparty, which there isn't in v1)
        # The dashboard shows both but labels MARK closes distinctly.
        self._realized_pnl = 0.0
        self._mark_pnl = 0.0
        self._total_fees = 0.0   # cumulative taker fees paid (entry + exit)
        self._trade_count = 0
        self._wins = 0
        self._losses = 0
        self._resolved_wins = 0
        self._resolved_losses = 0
        self._resolved_count = 0

        os.makedirs(cfg.log_dir, exist_ok=True)
        self.trade_log_path = os.path.join(cfg.log_dir, cfg.trade_log_file)
        self.signal_log_path = os.path.join(cfg.log_dir, cfg.signal_log_file)

        # In-memory ring buffer of the last N signal evaluations
        # (accepted + rejected) so the dashboard can surface gate
        # diagnostics without tailing the JSONL file. Dedup on
        # (market_id, reason) so a cascade of identical rejections
        # doesn't flood the panel — only the most recent entry for
        # each distinct reason-per-market is retained.
        self._signal_buffer: Deque[dict] = deque(maxlen=SIGNAL_BUFFER_SIZE)
        self._signal_buffer_lock = threading.Lock()
        self._last_reason_by_market: Dict[str, str] = {}
        self.signals_accepted_total = 0
        self.signals_rejected_total = 0
        self._reject_reason_counts: Dict[str, int] = {}

        # Ring buffer of resolved / closed positions so the dashboard
        # can render the trade history. The engine evicts each market
        # from its ``markets`` dict the moment it resolves, so the
        # ``MarketState.closed_positions`` list is garbage-collected
        # with the market — we must capture the history here instead.
        self._closed_positions_lock = threading.Lock()
        self._closed_positions_history: Deque[dict] = deque(maxlen=400)

    # ------------------------------------------------------------------ #
    # Exposure bookkeeping
    # ------------------------------------------------------------------ #

    @property
    def total_exposure(self) -> float:
        with self._lock:
            return self._open_exposure_dollars

    @property
    def realized_pnl(self) -> float:
        with self._lock:
            return self._realized_pnl

    def stats(self) -> dict:
        with self._lock:
            gross_realized = self._realized_pnl
            fees = self._total_fees
            return {
                "open_exposure": self._open_exposure_dollars,
                # Resolution-based P/L — gross and net of fees.
                # ``realized_pnl`` always reflects the NET number the
                # dashboard should display as SESSION P/L (it's the
                # money that would actually be in the wallet), while
                # ``realized_pnl_gross`` is kept for diagnostic
                # purposes and ``total_fees`` exposes what was paid
                # away. When ``cfg.taker_fee_bps == 0`` these are
                # identical.
                "realized_pnl": gross_realized - fees,
                "realized_pnl_gross": gross_realized,
                "total_fees": fees,
                # Mark-to-market P/L from early-exit closes. 0 in v1
                # (early exits disabled by default) but tracked
                # separately so it can never contaminate the
                # ``realized_pnl`` the dashboard and calibrator read.
                "mark_pnl": self._mark_pnl,
                "trade_count": self._trade_count,
                # Resolved-only win/loss counts — this is what WIN
                # RATE on the dashboard should use. ``wins`` and
                # ``losses`` still include mark-close counts for
                # backwards compatibility but the dashboard prefers
                # the resolved_* numbers.
                "wins": self._wins,
                "losses": self._losses,
                "resolved_count": self._resolved_count,
                "resolved_wins": self._resolved_wins,
                "resolved_losses": self._resolved_losses,
            }

    def _fee_for(self, size_dollars: float, price: float) -> float:
        """Polymarket taker fee on one leg at the given per-contract price.

        The real Polymarket schedule (per the published table) is:

            fee_usdc = (peak_bps / 2500) * size_dollars * (1 - price)

        which peaks at ``peak_bps / 10_000`` of notional at
        ``price = 0.5`` and falls toward zero at both tails. With
        the default ``peak_bps = 180`` this matches the published
        schedule exactly: $1.80 on a $50 trade at price 0.50,
        $0.65 on a $10 trade at price 0.10, $0.07 on a $1 trade
        at price 0.01, etc.

        ``price`` is the side-local per-contract price:
          - for a UP / YES entry, ``price = best_ask_yes``
          - for a DOWN / NO entry, ``price = 1 - best_ask_yes``
          - for a resolution settlement, ``price = 1.0`` (winning
            side receives $1 per contract) — but Polymarket does
            NOT charge fees on settlement, only on actual trades,
            so callers pass ``price = None`` to skip the deduction.
        """
        if price is None:
            return 0.0
        peak_bps = getattr(self.cfg, "taker_fee_peak_bps", 0.0) or 0.0
        if peak_bps <= 0:
            return 0.0
        p = float(price)
        if p <= 0.0 or p >= 1.0:
            return 0.0
        return float(size_dollars) * (peak_bps / 2500.0) * (1.0 - p)

    # ------------------------------------------------------------------ #
    # Signal logging
    # ------------------------------------------------------------------ #

    def log_signal(self, market: MarketState, signal_reason: str, accepted: bool) -> None:
        """Log every signal evaluation (accepted or rejected) so gate
        thresholds can be retuned from real paper data.

        Also updates the in-memory ring buffer used by the dashboard.
        """
        entry = {
            "ts": time.time(),
            "market_id": market.market_id,
            "interval": market.interval_label,
            "question": (market.description or "")[:80],
            "accepted": accepted,
            "reason": signal_reason,
            "posterior": market.posterior,
            "clob_mid": market.clob_mid_yes(),
            "flow_imbalance": market.flow_imbalance,
            "branching_ratio": market.branching_ratio_max,
            "cascade": market.cascade_active,
            "pi_effective": market.pi_effective,
            "trade_count": market.trade_count,
            "time_remaining_sec": market.time_remaining_sec,
            "book_depth": market.book_depth_yes,
        }

        # Persist every evaluation to the JSONL log for offline analysis.
        self._append(self.signal_log_path, entry)

        # Update the in-memory ring buffer. Dedup so an identical
        # rejection reason on the same market on consecutive trades
        # does not flood the panel — we only store transitions.
        with self._signal_buffer_lock:
            if accepted:
                self.signals_accepted_total += 1
            else:
                self.signals_rejected_total += 1
                # Bucketized gate name for the rejects histogram.
                bucket = signal_reason.split("(")[0].strip() or "unknown"
                self._reject_reason_counts[bucket] = (
                    self._reject_reason_counts.get(bucket, 0) + 1
                )

            last_reason = self._last_reason_by_market.get(market.market_id)
            if last_reason != signal_reason or accepted:
                self._last_reason_by_market[market.market_id] = signal_reason
                self._signal_buffer.append(entry)

    def recent_signals(self, limit: int = 50) -> List[dict]:
        """Return the ``limit`` most recent signal entries (newest first)."""
        with self._signal_buffer_lock:
            items = list(self._signal_buffer)
        items.reverse()
        return items[:limit]

    def reject_reason_histogram(self) -> Dict[str, int]:
        with self._signal_buffer_lock:
            return dict(self._reject_reason_counts)

    # ------------------------------------------------------------------ #
    # Open / close
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        market: MarketState,
        action: str,
        reason: str,
    ) -> Optional[PaperPosition]:
        if market.open_position is not None:
            return None

        clob_price = market.clob_mid_yes()
        if clob_price is None:
            return None

        if action == "buy_yes":
            side = "yes"
            entry_price = market.best_ask_yes or clob_price
        elif action == "buy_no":
            side = "no"
            # Cost of a No contract = 1 - ask(Yes).
            ask_yes = market.best_ask_yes or clob_price
            entry_price = 1.0 - ask_yes
        else:
            return None

        # Safety net mirroring the ``extreme-ask`` signal gate: refuse
        # to open any position priced at the tail of the book, where
        # liquidity is unreliable and mark-to-market swings are
        # dominated by the tiny entry price rather than any real
        # signal.
        if not 0.10 <= entry_price <= 0.90:
            return None

        prob_for_side = market.posterior if side == "yes" else (1.0 - market.posterior)
        dollars = position_dollars(
            cfg=self.cfg,
            probability=prob_for_side,
            entry_price=entry_price,
            time_remaining_sec=market.time_remaining_sec,
            total_market_duration_sec=market.total_duration_sec,
            current_total_exposure=self.total_exposure,
        )
        if dollars <= 0:
            return None

        pos = PaperPosition(
            side=side,
            entry_price=entry_price,
            size_dollars=dollars,
            entry_time=time.time(),
            reason=reason,
            posterior_at_entry=market.posterior,
            flow_imbalance_at_entry=market.flow_imbalance,
            branching_ratio_at_entry=market.branching_ratio_max,
        )
        market.open_position = pos

        # Entry-leg Polymarket taker fee. Uses the real published
        # schedule: fee = 0.072 * size * (1 - price) at the default
        # peak_bps of 180. Price passed in is the side-local
        # per-contract price (UP position -> ask_yes, DOWN position
        # -> 1 - ask_yes). At resolution no fee is charged, so the
        # settle_at_resolution path passes ``price=None``.
        entry_fee = self._fee_for(dollars, entry_price)
        with self._lock:
            self._open_exposure_dollars += dollars
            self._total_fees += entry_fee

        self._append(
            self.trade_log_path,
            {
                "ts": pos.entry_time,
                "event": "open",
                "market_id": market.market_id,
                "interval": market.interval_label,
                "description": market.description,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "size_dollars": pos.size_dollars,
                "posterior": pos.posterior_at_entry,
                "flow_imbalance": pos.flow_imbalance_at_entry,
                "branching_ratio": pos.branching_ratio_at_entry,
                "reason": pos.reason,
            },
        )
        print(
            f"{self.cfg.log_prefix} OPEN {market.interval_label} {pos.side.upper()} "
            f"${pos.size_dollars:.2f} @ {pos.entry_price:.3f} "
            f"posterior={pos.posterior_at_entry:.3f} "
            f"imbalance={pos.flow_imbalance_at_entry:+.3f}",
            flush=True,
        )
        return pos

    def close_position(
        self,
        market: MarketState,
        reason: str,
        mark_price: Optional[float] = None,
    ) -> Optional[PaperPosition]:
        pos = market.open_position
        if pos is None:
            return None

        if mark_price is None:
            mark_price = market.clob_mid_yes() or pos.entry_price

        if pos.side == "yes":
            exit_price = market.best_bid_yes or mark_price
            pnl_frac = (exit_price - pos.entry_price) / pos.entry_price
        else:
            bid_yes = market.best_bid_yes or mark_price
            exit_price = 1.0 - bid_yes
            pnl_frac = (exit_price - pos.entry_price) / pos.entry_price

        realized = pos.size_dollars * pnl_frac
        pos.exit_price = exit_price
        pos.exit_time = time.time()
        pos.exit_reason = reason
        pos.realized_pnl = realized

        # Early-exit mark-to-market close *is* a real trade on the
        # real book (it's filled against the other side's best bid),
        # so a taker fee applies at the side-local exit price.
        exit_fee = self._fee_for(pos.size_dollars, exit_price)
        with self._lock:
            self._open_exposure_dollars = max(0.0, self._open_exposure_dollars - pos.size_dollars)
            # Mark-to-market close: tracked in ``_mark_pnl`` and the
            # legacy ``_wins``/``_losses`` counters (which the
            # dashboard no longer uses for WIN RATE) but never in
            # ``_realized_pnl`` or ``_resolved_*``. This way the
            # dashboard's BALANCE / SESSION P/L numbers only reflect
            # settlements against actual outcomes.
            self._mark_pnl += realized
            self._total_fees += exit_fee
            self._trade_count += 1
            if realized > 0:
                self._wins += 1
            elif realized < 0:
                self._losses += 1

        market.open_position = None
        market.closed_positions.append(pos)
        market.last_close_time = pos.exit_time
        self._record_closed(market, pos, event="close", outcome_yes=None)

        self._append(
            self.trade_log_path,
            {
                "ts": pos.exit_time,
                "event": "close",
                "market_id": market.market_id,
                "interval": market.interval_label,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "size_dollars": pos.size_dollars,
                "realized_pnl": pos.realized_pnl,
                "reason": pos.exit_reason,
            },
        )
        print(
            f"{self.cfg.log_prefix} CLOSE {market.interval_label} {pos.side.upper()} "
            f"entry={pos.entry_price:.3f} exit={pos.exit_price:.3f} "
            f"pnl=${pos.realized_pnl:+.3f} ({pos.exit_reason})",
            flush=True,
        )
        return pos

    def settle_at_resolution(self, market: MarketState) -> Optional[PaperPosition]:
        """Close an open position against the binary resolution outcome."""
        pos = market.open_position
        if pos is None:
            return None
        if market.resolved_outcome is None:
            return None

        yes_wins = market.resolved_outcome == 1
        if pos.side == "yes":
            payout = 1.0 if yes_wins else 0.0
        else:
            payout = 0.0 if yes_wins else 1.0

        pnl_frac = (payout - pos.entry_price) / pos.entry_price
        realized = pos.size_dollars * pnl_frac
        pos.exit_price = payout
        pos.exit_time = market.resolution_time or time.time()
        pos.exit_reason = "resolution"
        pos.realized_pnl = realized

        # Settlement against the true outcome does NOT incur a
        # trading fee on Polymarket (it's an on-chain payout, not a
        # trade), so pass ``price=None`` and ``_fee_for`` returns 0.
        # We still stamp ``_total_fees`` unchanged for symmetry.
        settle_fee = self._fee_for(pos.size_dollars, None)
        with self._lock:
            self._open_exposure_dollars = max(0.0, self._open_exposure_dollars - pos.size_dollars)
            self._total_fees += settle_fee
            # True resolution P/L — the only honest paper P/L in v1.
            self._realized_pnl += realized
            self._trade_count += 1
            self._resolved_count += 1
            if realized > 0:
                self._wins += 1
                self._resolved_wins += 1
            elif realized < 0:
                self._losses += 1
                self._resolved_losses += 1

        market.open_position = None
        market.closed_positions.append(pos)
        market.last_close_time = pos.exit_time
        self._record_closed(market, pos, event="resolve", outcome_yes=yes_wins)

        self._append(
            self.trade_log_path,
            {
                "ts": pos.exit_time,
                "event": "resolve",
                "market_id": market.market_id,
                "interval": market.interval_label,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "size_dollars": pos.size_dollars,
                "realized_pnl": pos.realized_pnl,
                "outcome_yes": yes_wins,
            },
        )
        print(
            f"{self.cfg.log_prefix} RESOLVE {market.interval_label} {pos.side.upper()} "
            f"payout={payout:.0f} pnl=${pos.realized_pnl:+.3f}",
            flush=True,
        )
        return pos

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #

    def _append(self, path: str, entry: dict) -> None:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            print(f"{self.cfg.log_prefix} log write error ({path}): {e}", flush=True)

    # ------------------------------------------------------------------ #
    # Closed-position history (survives market eviction)
    # ------------------------------------------------------------------ #

    def _record_closed(
        self,
        market: MarketState,
        pos: PaperPosition,
        event: str,
        outcome_yes: Optional[bool],
    ) -> None:
        """Capture a self-contained record of a closed position before
        the market gets evicted from ``engine.markets``. Format mirrors
        the JS dashboard's expectations so ``hf_engine.snapshot`` can
        drop it straight into the state payload with minimal massaging.
        """
        # Translate the internal yes/no frame into the domain-specific
        # up/down labels the dashboard renders. The engine stays a
        # generic binary-market engine under the hood; only the
        # presentation layer cares about the crypto-updown naming.
        side_label = "UP" if pos.side == "yes" else "DOWN"
        if outcome_yes is None:
            outcome_label: Optional[str] = None
        else:
            outcome_label = "UP" if outcome_yes else "DOWN"

        won = (pos.realized_pnl or 0.0) > 0

        entry = {
            "ts": pos.exit_time or time.time(),
            "event": event,
            "market_id": market.market_id,
            "interval": market.interval_label,
            "description": (market.description or "")[:96],
            "side": side_label,
            "entry": pos.entry_price,
            "exit": pos.exit_price,
            "size_dollars": pos.size_dollars,
            "realized_pnl": pos.realized_pnl or 0.0,
            "won": won,
            "outcome": outcome_label,
            "posterior_at_entry": pos.posterior_at_entry,
            "excitation_at_entry": pos.branching_ratio_at_entry,
            "flow_imbalance_at_entry": pos.flow_imbalance_at_entry,
            "entry_reason": pos.reason,
            "exit_reason": pos.exit_reason,
        }
        with self._closed_positions_lock:
            self._closed_positions_history.append(entry)

    def recent_closed_positions(self, limit: int = 100) -> List[dict]:
        """Return the most recent ``limit`` closed positions, newest first."""
        with self._closed_positions_lock:
            items = list(self._closed_positions_history)
        items.reverse()
        return items[:limit]
