"""
hf_engine.hawkes
================

Recursive Hawkes process intensity tracker (Section 3 of the spec).

A Hawkes process models point events where past events increase the
probability of near-future events. For a short-duration binary prediction
market the arrival of trades is our point process, and a self-exciting
cascade is the signature of informed flow hitting the book.

Intensity function (continuous time):

    lambda(t) = mu + sum_{t_i < t} alpha * exp(-beta * (t - t_i))

The naive O(n) sum can be replaced with an O(1) recursive update by
observing that between events the excitation decays purely exponentially:

    lambda(t + dt) = mu + (lambda(t) - mu) * exp(-beta * dt)

and every new event simply adds ``alpha`` to the running intensity. This
is the formulation in Section 3.4 of the spec and it is what we use here
so the per-trade cost is constant and there is no trade-history scan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class HawkesTracker:
    """Single-side recursive Hawkes intensity tracker.

    ``mu``     — baseline arrival rate (events per second)
    ``alpha``  — self-excitation amplitude (intensity bump per event)
    ``beta``   — exponential decay rate of the bump (per second)

    The branching ratio ``n = alpha / beta`` is the fraction of future
    events each event directly produces on average. ``n < 1`` is stable;
    ``n -> 1`` is near-critical (cascade regime); ``n > 1`` is explosive.
    """

    mu: float
    alpha: float
    beta: float
    intensity: float = 0.0
    last_time: float = 0.0
    event_count: int = 0

    # Upper clamp on intensity so a pathological spray of trades cannot
    # drive the posterior update weights out of range.
    intensity_cap: float = 1.0e6

    def __post_init__(self) -> None:
        if self.intensity == 0.0:
            self.intensity = self.mu

    def reset(self, mu: float | None = None) -> None:
        """Hard reset — used when the baseline rate is re-estimated after
        the early observation window closes."""
        if mu is not None:
            self.mu = mu
        self.intensity = self.mu
        self.last_time = 0.0
        self.event_count = 0

    def decay_to(self, t: float) -> float:
        """Advance the intensity clock to time ``t`` without adding a
        new excitation. Useful for querying the current intensity between
        trades (e.g. during signal evaluation on a non-trade tick)."""
        if self.last_time <= 0.0:
            self.last_time = t
            return self.intensity

        dt = t - self.last_time
        if dt < 0.0:
            # Out-of-order timestamp — clamp to zero to stay monotone.
            dt = 0.0
        self.intensity = self.mu + (self.intensity - self.mu) * math.exp(-self.beta * dt)
        self.last_time = t
        return self.intensity

    def update(self, t: float) -> float:
        """Register an event at time ``t`` (epoch seconds, can be float).

        Returns the post-event intensity.
        """
        self.decay_to(t)
        self.intensity = min(self.intensity + self.alpha, self.intensity_cap)
        self.event_count += 1
        return self.intensity

    @property
    def branching_ratio(self) -> float:
        if self.beta <= 0.0:
            return float("inf")
        return self.alpha / self.beta

    def snapshot(self) -> dict:
        return {
            "mu": self.mu,
            "alpha": self.alpha,
            "beta": self.beta,
            "intensity": self.intensity,
            "branching_ratio": self.branching_ratio,
            "event_count": self.event_count,
            "last_time": self.last_time,
        }


def hawkes_log_likelihood(
    timestamps: list[float],
    mu: float,
    alpha: float,
    beta: float,
    t_start: float,
    t_end: float,
) -> float:
    """Log-likelihood of a univariate Hawkes process on ``[t_start, t_end]``.

    Formula (spec 3.5):

        log L = sum_i log lambda(t_i) - integral_{t_start}^{t_end} lambda(s) ds

    The integral term is computed in closed form using the exponential
    kernel identity:

        integral_{t_start}^{t_end} lambda(s) ds
            = mu * (t_end - t_start)
              + (alpha / beta) * sum_i (1 - exp(-beta * (t_end - t_i)))

    This is used by the offline-style calibration replay in
    ``hf_engine.calibration``. It is not needed on the trade hot path.
    """
    if beta <= 0.0 or alpha < 0.0 or mu <= 0.0:
        return float("-inf")
    if t_end <= t_start:
        return float("-inf")

    # Sum of log intensities via one-pass recursion.
    log_lik = 0.0
    running_excitation = 0.0
    prev_t = t_start
    for t in timestamps:
        if t < t_start or t > t_end:
            continue
        dt = t - prev_t
        running_excitation = running_excitation * math.exp(-beta * dt)
        lam = mu + alpha * running_excitation
        if lam <= 0.0:
            return float("-inf")
        log_lik += math.log(lam)
        running_excitation += 1.0  # contribution of this new event to the kernel sum
        prev_t = t

    # Integrated intensity.
    integral = mu * (t_end - t_start)
    if alpha > 0.0 and timestamps:
        for t in timestamps:
            if t < t_start or t > t_end:
                continue
            integral += (alpha / beta) * (1.0 - math.exp(-beta * (t_end - t)))

    return log_lik - integral
