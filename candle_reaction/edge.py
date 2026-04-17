"""Edge-based trade sizing against Polymarket top-of-book prices.

A YES-UP share on Polymarket pays $1 on win, $0 on loss, and costs
``ask`` now. Net odds ``b = (1 - ask) / ask``. Kelly for a 1:1 binary
payoff with probability ``q`` and odds ``b``:

    f* = q - (1 - q) / b
       = q - (1 - q) * ask / (1 - ask)

Positive ``f*`` means there is edge; we stake ``kelly_fraction * f*``
of bankroll, capped at ``max_stake`` (the user's 2%-of-float ceiling).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DEFAULT_KELLY_FRACTION = 0.25   # quarter Kelly -- paper-safe
DEFAULT_MAX_STAKE = 5.0         # $5 cap on a $250 bankroll (2%)
DEFAULT_MIN_EDGE = 0.03         # 3c of edge required to fire a trade


@dataclass(frozen=True)
class EdgeTrade:
    side: str            # "UP" or "DOWN"
    fill_price: float    # what we paid per share (0..1)
    stake: float         # dollars committed
    shares: float        # stake / fill_price
    edge: float          # our_q - fill_price
    kelly_f: float       # full-Kelly fraction (pre-fractional)
    our_q: float         # our model's P(this side wins)


def kelly_fraction(q: float, ask: float) -> float:
    """Full-Kelly fraction for a binary YES purchase at `ask`.
    Clamps negatives to 0 so callers can just compare positives.
    """
    if ask <= 0 or ask >= 1 or q <= ask:
        return 0.0
    b = (1.0 - ask) / ask
    f = q - (1.0 - q) / b
    return max(0.0, min(1.0, f))


def size_edge_trade(
    p_up: float,
    up_ask: Optional[float],
    down_ask: Optional[float],
    bankroll: float,
    kelly_fraction_mult: float = DEFAULT_KELLY_FRACTION,
    max_stake: float = DEFAULT_MAX_STAKE,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> Optional[EdgeTrade]:
    """Pick the side (UP/DOWN) with the better edge, return the trade.

    Returns None if neither side clears `min_edge`.
    """
    candidates: list[EdgeTrade] = []
    if up_ask is not None:
        f = kelly_fraction(p_up, up_ask)
        edge = p_up - up_ask
        if edge >= min_edge and f > 0:
            stake = min(max_stake, kelly_fraction_mult * f * bankroll)
            if stake > 0:
                candidates.append(EdgeTrade(
                    side="UP", fill_price=up_ask, stake=stake,
                    shares=stake / up_ask, edge=edge, kelly_f=f, our_q=p_up,
                ))
    if down_ask is not None:
        q_down = 1.0 - p_up
        f = kelly_fraction(q_down, down_ask)
        edge = q_down - down_ask
        if edge >= min_edge and f > 0:
            stake = min(max_stake, kelly_fraction_mult * f * bankroll)
            if stake > 0:
                candidates.append(EdgeTrade(
                    side="DOWN", fill_price=down_ask, stake=stake,
                    shares=stake / down_ask, edge=edge, kelly_f=f, our_q=q_down,
                ))

    if not candidates:
        return None
    # If both sides have edge (rare; implies the book sums < 1), pick the bigger.
    return max(candidates, key=lambda t: t.edge)
