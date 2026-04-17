"""Probabilistic judge: features -> P(next candle up).

Hand-tuned logistic. Weights encode two intuitions:

  1. Close position + signed body ("pressure"): the side the candle
     closed on tends to carry into the next bar.
  2. Volume / range ("participation"): pressure with real participation
     is more likely to follow through; pressure on a thin, wide bar
     (news spike) is less reliable -- dampen the signal.

With no backtest, these are priors. The backtest script surfaces hit
rate by confidence bucket so they can be tuned later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .features import Features


@dataclass(frozen=True)
class Weights:
    bias: float = 0.0
    close_position: float = 1.6     # close-at-high -> strong up bias
    body_signed: float = 1.1        # large signed body reinforces direction
    volume_boost: float = 0.55      # amplifies pressure when vol is high
    range_damp: float = 0.35        # dampens when range is abnormally wide
    streak: float = -0.25           # mild mean-reversion after long streaks


@dataclass(frozen=True)
class Judgement:
    p_up: float
    p_down: float
    side: str          # "UP" / "DOWN" / "SKIP"
    confidence: float  # max(p_up, p_down)


def _sigmoid(x: float) -> float:
    # Numerically stable.
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def judge(f: Features, w: Weights = Weights()) -> Judgement:
    # Centre close_position at 0 and scale to [-1, +1].
    cp = (f.close_position - 0.5) * 2.0

    # Pressure term: both cp and body agree when the candle closes
    # decisively on one side of its range.
    pressure = w.close_position * cp + w.body_signed * f.body_signed

    # Participation: high volume amplifies pressure; abnormal range (>2 sd)
    # dampens it (news spikes revert more than trend).
    vol_mult = max(-1.0, min(1.0, f.volume_z / 2.0))
    range_penalty = max(0.0, (f.range_z - 1.5))  # only punish big ranges
    amplified = pressure * (1.0 + w.volume_boost * vol_mult) - w.range_damp * range_penalty * (1 if pressure > 0 else -1)

    # Streak: tanh squashes to (-1, 1).
    streak_term = w.streak * math.tanh(f.streak / 3.0)

    z = w.bias + amplified + streak_term
    p_up = _sigmoid(z)
    p_down = 1.0 - p_up
    if p_up >= p_down:
        return Judgement(p_up=p_up, p_down=p_down, side="UP", confidence=p_up)
    return Judgement(p_up=p_up, p_down=p_down, side="DOWN", confidence=p_down)
