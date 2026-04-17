"""Single-regime judge: bearish-fade.

On the 36h BTC 5m backtest (3,814 resolved trades) exactly one
feature-slice rule held up out-of-slice with a meaningful sample:

  * ``body_signed < -0.1`` AND ``close_position < 0.5``
    (a candle that closed in the lower half of its range with a
    clearly red body) -> **bet UP on the next bar**.

Empirical: 55.2% win rate on 1,707 trades. Adding ``range_z > 0``
(a bar of average or bigger size) tightens it to 55.9% on 776 trades
but costs 55% of the volume. The trade-off between frequency and
hit rate is mild; we take the looser version for more data per unit
time, and let the Polymarket Kelly sizer decide whether the edge
vs the CLOB ask is worth a stake.

Important caveat: this is a regime-dependent rule. BTC drifted up
during the 36h window, so "bearish bar fades up" captured the drift
as much as any micro-structure reversion. In a down-drifting period
the rule will lose money. Don't treat this as universal edge; it's
a hypothesis to keep testing on forward data.

Every bar that doesn't match the rule is classified and labeled in
the ``regime`` field for audit, but the judge returns ``side=SKIP``
so nothing below gets traded:

  * ``bearish_fade``: body < -0.1 AND cp < 0.5 -> TRADE (bet UP)
  * ``bullish``:      body > 0 OR cp >= 0.5    -> SKIP (no edge)
  * ``neutral``:      everything else           -> SKIP
"""

from __future__ import annotations

from dataclasses import dataclass

from .features import Features


# Import back-compat shim; the old logistic coefficients are no
# longer consulted.
@dataclass(frozen=True)
class Weights:
    bias: float = 0.0
    close_position: float = 1.6
    body_signed: float = 1.1
    volume_boost: float = 0.55
    range_damp: float = 0.35
    streak: float = -0.25


@dataclass(frozen=True)
class Judgement:
    p_up: float
    p_down: float
    side: str          # "UP" / "DOWN" / "SKIP"
    confidence: float  # max(p_up, p_down)
    regime: str = "unknown"


# Conservative prior, below the observed 55.2% so we're not
# over-claiming from one backtest. The Polymarket edge-vs-ask is
# what actually drives Kelly-sized stake anyway.
BEARISH_FADE_Q = 0.55


def _classify_regime(f: Features) -> str:
    if f.body_signed < -0.1 and f.close_position < 0.5:
        return "bearish_fade"
    if f.body_signed > 0 or f.close_position >= 0.5:
        return "bullish"
    return "neutral"


def judge(f: Features, w: Weights = Weights(), contrarian: bool = False) -> Judgement:
    regime = _classify_regime(f)

    if regime != "bearish_fade":
        return Judgement(
            p_up=0.5, p_down=0.5, side="SKIP", confidence=0.5, regime=regime,
        )

    p_up = BEARISH_FADE_Q
    if contrarian:
        p_up = 1.0 - p_up

    p_down = 1.0 - p_up
    if p_up >= p_down:
        return Judgement(
            p_up=p_up, p_down=p_down, side="UP", confidence=p_up, regime=regime,
        )
    return Judgement(
        p_up=p_up, p_down=p_down, side="DOWN", confidence=p_down, regime=regime,
    )
