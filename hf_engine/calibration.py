"""
hf_engine.calibration
=====================

Online (post-resolution) calibration of the Hawkes and Glosten-Milgrom
parameters (Section 11 Phase 1, adapted for live paper trading).

Every time a market resolves the engine hands this module the full
trade history plus the true outcome. The calibrator then:

    1. grid-searches ``(alpha, beta)`` to maximize the Hawkes
       log-likelihood on the observed trades;
    2. grid-searches ``pi`` to find the value that drives the final
       Bayesian posterior closest to the true outcome (0 or 1);
    3. EMA-blends those fitted values into the running global prior so
       subsequent markets benefit from what we have learnt.

The grids are intentionally coarse. The goal is continuous drift in a
sensible direction — not a perfect MLE — and the whole replay must fit
comfortably inside the gap between two 5-minute markets.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import List, Tuple

from .config import HFEConfig
from .glosten_milgrom import information_weight, log_odds_to_prob, prob_to_log_odds
from .hawkes import hawkes_log_likelihood
from .market_state import MarketState, Trade


@dataclass
class CalibrationResult:
    market_id: str
    outcome: int
    trade_count: int
    best_alpha: float
    best_beta: float
    best_pi: float
    posterior_at_fitted_pi: float
    posterior_gap: float   # |posterior - outcome| after refit
    log_likelihood: float


class OnlineCalibrator:
    def __init__(self, cfg: HFEConfig) -> None:
        self.cfg = cfg
        self.current_alpha = cfg.hawkes_alpha_prior
        self.current_beta = cfg.hawkes_beta_prior
        self.current_pi = cfg.pi_base
        self.results: List[CalibrationResult] = []

        os.makedirs(cfg.log_dir, exist_ok=True)
        self.log_path = os.path.join(cfg.log_dir, cfg.calibration_log_file)

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    def on_market_resolved(self, market: MarketState) -> CalibrationResult | None:
        """Called by the engine after a market resolves and after any
        open paper position has been settled."""
        if not self.cfg.enable_online_calibration:
            return None
        if market.resolved_outcome is None:
            return None

        # Pull just the trades that carry side information (the median
        # tracker pushes sizeless dummies into ``market.trades``, so we
        # have to look at the true trade log instead).
        trades: List[Trade] = [t for t in market.trades if t.side in ("buy", "sell")]
        if len(trades) < 5:
            return None

        t_start = trades[0].timestamp
        t_end = trades[-1].timestamp
        if t_end <= t_start:
            return None

        buy_ts = [t.timestamp for t in trades if t.side == "buy"]
        sell_ts = [t.timestamp for t in trades if t.side == "sell"]

        # Per-market mu is the observed total rate split between sides.
        total_rate = len(trades) / max(1e-3, t_end - t_start)
        mu_buy = max(1e-4, total_rate * (len(buy_ts) / max(1, len(trades))) / 2.0)
        mu_sell = max(1e-4, total_rate * (len(sell_ts) / max(1, len(trades))) / 2.0)

        best = self._grid_fit_hawkes(
            buy_ts, sell_ts, mu_buy, mu_sell, t_start, t_end
        )

        best_pi, posterior, gap = self._grid_fit_pi(
            trades=trades,
            initial_probability=_infer_initial_prob(market),
            outcome=market.resolved_outcome,
        )

        result = CalibrationResult(
            market_id=market.market_id,
            outcome=market.resolved_outcome,
            trade_count=len(trades),
            best_alpha=best[0],
            best_beta=best[1],
            best_pi=best_pi,
            posterior_at_fitted_pi=posterior,
            posterior_gap=gap,
            log_likelihood=best[2],
        )
        self.results.append(result)

        # EMA blend into the running priors.
        a = self.cfg.calibration_ema_alpha
        self.current_alpha = (1 - a) * self.current_alpha + a * result.best_alpha
        self.current_beta = (1 - a) * self.current_beta + a * result.best_beta
        self.current_pi = (1 - a) * self.current_pi + a * result.best_pi

        self._append_log(
            {
                "ts": time.time(),
                "market_id": market.market_id,
                "interval": market.interval_label,
                "outcome": market.resolved_outcome,
                "trade_count": result.trade_count,
                "fitted_alpha": result.best_alpha,
                "fitted_beta": result.best_beta,
                "fitted_pi": result.best_pi,
                "posterior_at_fitted_pi": result.posterior_at_fitted_pi,
                "posterior_gap": result.posterior_gap,
                "log_likelihood": result.log_likelihood,
                "current_alpha_ema": self.current_alpha,
                "current_beta_ema": self.current_beta,
                "current_pi_ema": self.current_pi,
            }
        )
        return result

    def current_priors(self) -> Tuple[float, float, float]:
        return self.current_alpha, self.current_beta, self.current_pi

    # ------------------------------------------------------------------ #
    # Grids
    # ------------------------------------------------------------------ #

    def _grid_fit_hawkes(
        self,
        buy_ts: List[float],
        sell_ts: List[float],
        mu_buy: float,
        mu_sell: float,
        t_start: float,
        t_end: float,
    ) -> Tuple[float, float, float]:
        best_alpha = self.current_alpha
        best_beta = self.current_beta
        best_ll = float("-inf")

        for alpha in self.cfg.calibration_alpha_grid:
            for beta in self.cfg.calibration_beta_grid:
                if beta <= 0:
                    continue
                # We fit buy and sell sides jointly by summing their
                # independent log-likelihoods. This is the simplest
                # factorization and keeps the grid search cheap.
                ll_buy = hawkes_log_likelihood(
                    buy_ts, mu_buy, alpha, beta, t_start, t_end
                )
                ll_sell = hawkes_log_likelihood(
                    sell_ts, mu_sell, alpha, beta, t_start, t_end
                )
                if not math.isfinite(ll_buy) or not math.isfinite(ll_sell):
                    continue
                ll = ll_buy + ll_sell
                if ll > best_ll:
                    best_ll = ll
                    best_alpha = alpha
                    best_beta = beta
        return best_alpha, best_beta, best_ll

    def _grid_fit_pi(
        self,
        trades: List[Trade],
        initial_probability: float,
        outcome: int,
    ) -> Tuple[float, float, float]:
        """Find the value of pi that drives the final posterior as close
        to the resolved outcome as possible. Returns (pi, posterior, gap).

        ``outcome`` is 1 if Yes won, 0 otherwise. The gap we minimize is
        ``|posterior - outcome|`` after replaying every trade through
        the GM update at the candidate pi.
        """
        target = 1.0 if outcome == 1 else 0.0

        lo = self.cfg.calibration_pi_grid_lo
        hi = self.cfg.calibration_pi_grid_hi
        steps = max(3, self.cfg.calibration_pi_grid_steps)

        best_pi = self.current_pi
        best_gap = float("inf")
        best_posterior = initial_probability

        for i in range(steps):
            t = i / (steps - 1)
            pi = lo + t * (hi - lo)
            w = information_weight(pi)

            log_odds = prob_to_log_odds(initial_probability)
            for tr in trades:
                if tr.side == "buy":
                    log_odds += w
                elif tr.side == "sell":
                    log_odds -= w
                if log_odds > self.cfg.max_log_odds:
                    log_odds = self.cfg.max_log_odds
                elif log_odds < -self.cfg.max_log_odds:
                    log_odds = -self.cfg.max_log_odds

            posterior = log_odds_to_prob(log_odds)
            gap = abs(posterior - target)
            if gap < best_gap:
                best_gap = gap
                best_pi = pi
                best_posterior = posterior

        return best_pi, best_posterior, best_gap

    # ------------------------------------------------------------------ #
    # IO
    # ------------------------------------------------------------------ #

    def _append_log(self, entry: dict) -> None:
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            print(
                f"{self.cfg.log_prefix} calibration log write error: {e}",
                flush=True,
            )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _infer_initial_prob(market: MarketState) -> float:
    mid = market.clob_mid_yes()
    if mid is None:
        return 0.5
    if mid <= 0.0 or mid >= 1.0:
        return 0.5
    return mid
