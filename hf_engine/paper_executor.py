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
from dataclasses import asdict
from typing import Dict, List, Optional

from .config import HFEConfig
from .market_state import MarketState, PaperPosition, position_dollars


class PaperExecutor:
    """Paper-only executor with its own P&L ledger."""

    def __init__(self, cfg: HFEConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._open_exposure_dollars = 0.0
        self._realized_pnl = 0.0
        self._trade_count = 0
        self._wins = 0
        self._losses = 0

        os.makedirs(cfg.log_dir, exist_ok=True)
        self.trade_log_path = os.path.join(cfg.log_dir, cfg.trade_log_file)
        self.signal_log_path = os.path.join(cfg.log_dir, cfg.signal_log_file)

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
            return {
                "open_exposure": self._open_exposure_dollars,
                "realized_pnl": self._realized_pnl,
                "trade_count": self._trade_count,
                "wins": self._wins,
                "losses": self._losses,
            }

    # ------------------------------------------------------------------ #
    # Signal logging
    # ------------------------------------------------------------------ #

    def log_signal(self, market: MarketState, signal_reason: str, accepted: bool) -> None:
        """Log every signal evaluation (accepted or rejected) so gate
        thresholds can be retuned from real paper data."""
        entry = {
            "ts": time.time(),
            "market_id": market.market_id,
            "interval": market.interval_label,
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
        self._append(self.signal_log_path, entry)

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

        if not 0.01 < entry_price < 0.99:
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

        with self._lock:
            self._open_exposure_dollars += dollars

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

        with self._lock:
            self._open_exposure_dollars = max(0.0, self._open_exposure_dollars - pos.size_dollars)
            self._realized_pnl += realized
            self._trade_count += 1
            if realized > 0:
                self._wins += 1
            elif realized < 0:
                self._losses += 1

        market.open_position = None
        market.closed_positions.append(pos)

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

        with self._lock:
            self._open_exposure_dollars = max(0.0, self._open_exposure_dollars - pos.size_dollars)
            self._realized_pnl += realized
            self._trade_count += 1
            if realized > 0:
                self._wins += 1
            elif realized < 0:
                self._losses += 1

        market.open_position = None
        market.closed_positions.append(pos)

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
