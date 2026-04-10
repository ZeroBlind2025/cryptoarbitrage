"""
hf_engine.glosten_milgrom
=========================

Glosten-Milgrom posterior updates (Section 4 of the spec).

The GM (1985) model classifies every incoming trade as coming from either
an informed trader (with probability ``pi``) or a noise trader (with
probability ``1 - pi``). The informed trader buys if and only if the
outcome is ``V = 1``; the noise trader buys with probability 0.5 either
way. Applying Bayes' rule per trade yields a posterior ``delta = P(V=1)``.

Working in log-odds space is numerically clean and reduces the per-trade
update to a single addition / subtraction:

    L = log(delta / (1 - delta))
    w = log((1 + pi) / (1 - pi))
    After buy : L <- L + w
    After sell: L <- L - w

A size-weighting factor is applied per Section 4.6:

    size_weight(s) = log(1 + s / median_size)

so that small fills barely move the posterior while outsized fills carry
their full Bayesian weight.
"""

from __future__ import annotations

import math


def information_weight(pi: float) -> float:
    """Per-trade log-odds increment implied by informed-fraction ``pi``.

    Returns ``log((1 + pi) / (1 - pi))``. For pi=0 this is 0 (no info);
    for pi=0.5 it is ``log 3 ~= 1.10``; asymptotes at pi -> 1.
    """
    pi = max(0.0, min(pi, 0.999))  # clamp away from the singular endpoint
    return math.log((1.0 + pi) / (1.0 - pi))


def size_weight(trade_size: float, median_size: float | None) -> float:
    """Trade-size weight (Section 4.6).

    Returns 1.0 when we do not yet have a median estimate so the engine
    still behaves correctly during the first few trades of a market.
    """
    if median_size is None or median_size <= 0.0 or trade_size <= 0.0:
        return 1.0
    return math.log1p(trade_size / median_size)


def prob_to_log_odds(p: float, eps: float = 1e-9) -> float:
    """Sigmoid inverse with clamping to avoid log(0)."""
    p = max(eps, min(1.0 - eps, p))
    return math.log(p / (1.0 - p))


def log_odds_to_prob(log_odds: float) -> float:
    """Numerically stable sigmoid."""
    if log_odds >= 0:
        z = math.exp(-log_odds)
        return 1.0 / (1.0 + z)
    z = math.exp(log_odds)
    return z / (1.0 + z)


class GlostenMilgromPosterior:
    """Running log-odds posterior over ``P(Yes)`` for a single market."""

    def __init__(self, initial_probability: float, max_log_odds: float = 10.0) -> None:
        self.log_odds = prob_to_log_odds(initial_probability)
        self.max_log_odds = max_log_odds
        self.updates = 0

    @property
    def probability(self) -> float:
        return log_odds_to_prob(self.log_odds)

    def update(self, side: str, pi_effective: float, size_w: float = 1.0) -> float:
        """Apply one trade update.

        ``side`` is ``"buy"`` (taker lifted an ask on Yes, or hit the bid on
        No — the engine normalizes to the Yes-token perspective before
        calling this). ``pi_effective`` is the cascade-adjusted informed
        fraction from the Hawkes branch.
        """
        w = information_weight(pi_effective) * max(0.0, size_w)
        if side == "buy":
            self.log_odds += w
        elif side == "sell":
            self.log_odds -= w
        else:
            return self.probability

        # Clamp for numerical sanity (Section 13.4).
        if self.log_odds > self.max_log_odds:
            self.log_odds = self.max_log_odds
        elif self.log_odds < -self.max_log_odds:
            self.log_odds = -self.max_log_odds

        self.updates += 1
        return self.probability

    def reset(self, initial_probability: float) -> None:
        self.log_odds = prob_to_log_odds(initial_probability)
        self.updates = 0

    def snapshot(self) -> dict:
        return {
            "log_odds": self.log_odds,
            "probability": self.probability,
            "updates": self.updates,
            "max_log_odds": self.max_log_odds,
        }
