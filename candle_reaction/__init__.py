"""BTC 5m candle-reaction paper-trading module.

Hypothesis: the candle that *follows* a closed 5m candle is primarily the
market's reaction to the pressure and participation baked into the prior
close. We score that prior close into P(next up), gate with a confidence
ladder, and paper-trade a synthetic 1:1 binary bet.
"""
