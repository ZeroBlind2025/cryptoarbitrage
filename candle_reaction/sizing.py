"""Confidence-ladder position sizing.

$250 bankroll, 2% max position ($5) at 90%+ confidence.
Ladder is defined in Config and applied to max(p_up, p_down).
"""

from __future__ import annotations

from .config import Config


def stake_for(confidence: float, cfg: Config) -> float:
    """Return the dollar stake for the given confidence, or 0.0 to skip."""
    for rung in cfg.ladder:
        if rung.p_lo <= confidence < rung.p_hi:
            return rung.stake
    return 0.0
