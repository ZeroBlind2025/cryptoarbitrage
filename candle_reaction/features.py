"""Feature extraction for a just-closed 5m candle.

Everything here is a function of the prior-candle structure plus a
rolling window -- the hypothesis under test is that the next candle is
the reaction to pressure and participation already observable in the
close that just printed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .coindesk import Candle


@dataclass(frozen=True)
class Features:
    # All fields are bounded-ish so the logistic judge behaves sanely.
    close_position: float    # 0..1, (close-low)/(high-low)
    body_signed: float       # -1..+1, body/range, signed
    direction: int           # -1, 0, +1
    volume_z: float          # z-score vs lookback
    range_z: float           # z-score vs lookback
    streak: int              # signed consecutive same-direction count, prior bar


def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    return n / d if d else default


def _zscore(x: float, series: Sequence[float]) -> float:
    if len(series) < 3:
        return 0.0
    mean = sum(series) / len(series)
    var = sum((s - mean) ** 2 for s in series) / len(series)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (x - mean) / sd


def _streak(candles: Sequence[Candle]) -> int:
    """Signed streak ending at the last candle. +N if N up-bars in a row."""
    if not candles:
        return 0
    last_dir = 1 if candles[-1].close > candles[-1].open else (
        -1 if candles[-1].close < candles[-1].open else 0
    )
    if last_dir == 0:
        return 0
    count = 0
    for c in reversed(candles):
        d = 1 if c.close > c.open else (-1 if c.close < c.open else 0)
        if d != last_dir:
            break
        count += 1
    return count * last_dir


def extract(history: Sequence[Candle], lookback: int = 20) -> Features:
    """Build features for the MOST RECENT closed candle in `history`."""
    if not history:
        raise ValueError("history is empty")
    last = history[-1]

    if last.range > 0:
        close_position = (last.close - last.low) / last.range
        body_signed = (last.close - last.open) / last.range
    else:
        close_position = 0.5
        body_signed = 0.0

    direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)

    window = history[-(lookback + 1):-1] if len(history) > 1 else []
    vols = [c.volume for c in window]
    ranges = [c.range for c in window]
    volume_z = _zscore(last.volume, vols)
    range_z = _zscore(last.range, ranges)

    streak = _streak(history[-min(len(history), lookback):])

    return Features(
        close_position=close_position,
        body_signed=body_signed,
        direction=direction,
        volume_z=volume_z,
        range_z=range_z,
        streak=streak,
    )
