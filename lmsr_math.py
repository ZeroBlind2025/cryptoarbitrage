#!/usr/bin/env python3
"""
LMSR (Logarithmic Market Scoring Rule) math
===========================================

Pure stateless functions for computing LMSR prices, costs, and the shares
needed to move between prices.  All math is done in log-space so the
cost function stays numerically stable for arbitrarily large q values.

Reference:
  Hanson, R. (2003) "Combinatorial Information Market Design"
  Hanson, R. (2007) "Logarithmic Market Scoring Rules for Modular
    Combinatorial Information Aggregation"

Notation used throughout:
  q_yes, q_no : outstanding quantities for each binary outcome
  b           : liquidity parameter (higher b = less price impact per trade)
  p_yes       : implied LMSR probability of YES (always in (0, 1))
"""

from __future__ import annotations

import math
from typing import Optional


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def logsumexp(xs: list[float]) -> float:
    """Numerically stable log(sum(exp(x) for x in xs))."""
    if not xs:
        return float("-inf")
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def logit(p: float) -> float:
    """log(p / (1 - p)). Raises ValueError outside (0, 1)."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"logit undefined for p={p}")
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    """1 / (1 + e^-x), numerically stable."""
    if x >= 0:
        ex = math.exp(-x)
        return 1.0 / (1.0 + ex)
    else:
        ex = math.exp(x)
        return ex / (1.0 + ex)


# ---------------------------------------------------------------------------
# LMSR price (a.k.a. implied probability)
# ---------------------------------------------------------------------------

def lmsr_price(q_yes: float, q_no: float, b: float) -> float:
    """Implied probability of YES under LMSR.

    p_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))

    Equivalent to sigmoid((q_yes - q_no) / b).
    """
    if b <= 0:
        raise ValueError(f"b must be positive, got {b}")
    return sigmoid((q_yes - q_no) / b)


# ---------------------------------------------------------------------------
# LMSR cost function
# ---------------------------------------------------------------------------

def lmsr_cost_state(q_yes: float, q_no: float, b: float) -> float:
    """LMSR cost function value at state (q_yes, q_no).

    C(q) = b * log(sum(e^(q_i/b)))
    """
    return b * logsumexp([q_yes / b, q_no / b])


def lmsr_cost(q_yes_before: float, q_no_before: float,
              q_yes_after: float, q_no_after: float, b: float) -> float:
    """Cost of moving the market from one state to another.

    cost = C(q') - C(q)
    """
    return (lmsr_cost_state(q_yes_after, q_no_after, b)
            - lmsr_cost_state(q_yes_before, q_no_before, b))


def lmsr_cost_to_buy(q_yes: float, q_no: float, b: float,
                     side: str, amount: float) -> float:
    """Cost (in dollars, if b is in dollars) to buy `amount` shares of `side`.

    Always non-negative for positive amount.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if side == "yes":
        return lmsr_cost(q_yes, q_no, q_yes + amount, q_no, b)
    elif side == "no":
        return lmsr_cost(q_yes, q_no, q_yes, q_no + amount, b)
    else:
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")


# ---------------------------------------------------------------------------
# Inverses
# ---------------------------------------------------------------------------

def shares_to_reach_price(q_yes: float, q_no: float, b: float,
                          side: str, target_price: float) -> float:
    """Shares of `side` that must be bought to move LMSR price to `target_price`.

    Uses the closed form:
       Δ = b * (logit(p_target) - logit(p_current))

    Returns negative if target_price is below current price on that side
    (which would correspond to selling).
    """
    if not (0.0 < target_price < 1.0):
        raise ValueError(f"target_price must be in (0, 1), got {target_price}")
    current = lmsr_price(q_yes, q_no, b)
    if side == "yes":
        p_cur, p_tgt = current, target_price
    elif side == "no":
        p_cur, p_tgt = 1.0 - current, 1.0 - target_price
    else:
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
    # Clamp to avoid logit blowup at the exact boundaries
    p_cur = min(max(p_cur, 1e-9), 1.0 - 1e-9)
    p_tgt = min(max(p_tgt, 1e-9), 1.0 - 1e-9)
    return b * (logit(p_tgt) - logit(p_cur))


def q_yes_from_price(price: float, b: float, q_no: float = 0.0) -> float:
    """Solve for q_yes given a desired LMSR price and a fixed q_no (usually 0).

    From p = sigmoid((q_yes - q_no) / b):
        q_yes = q_no + b * logit(p)
    """
    if not (0.0 < price < 1.0):
        raise ValueError(f"price must be in (0, 1), got {price}")
    return q_no + b * logit(price)


# ---------------------------------------------------------------------------
# b (liquidity parameter) estimation
# ---------------------------------------------------------------------------

def estimate_b_from_trade(p_before: float, p_after: float,
                          size: float) -> Optional[float]:
    """Estimate b from a single observed trade.

    The LMSR first-order price impact is:
        Δp ≈ p * (1 - p) * Δ / b

    Rearranging:
        b ≈ p * (1 - p) * Δ / |Δp|

    Returns None if the price didn't move (can't solve) or if either
    price is outside (0, 1).
    """
    if not (0.0 < p_before < 1.0) or not (0.0 < p_after < 1.0):
        return None
    dp = p_after - p_before
    if abs(dp) < 1e-6:
        return None
    if size <= 0:
        return None
    # Use the midpoint for a more stable Jacobian estimate
    p_mid = 0.5 * (p_before + p_after)
    return (p_mid * (1.0 - p_mid) * size) / abs(dp)


def estimate_b_from_book_depth(q_yes: float, q_no: float, b_guess: float,
                               observed_cost: float,
                               price_delta: float = 0.01,
                               max_iter: int = 60,
                               tol: float = 1e-4) -> Optional[float]:
    """Estimate b by matching the cost to move LMSR price by `price_delta`
    against an observed order-book cost to move by the same amount.

    Bisection root-find on b in [b_guess/1000, b_guess*1000]. Returns None
    if convergence fails.
    """
    if observed_cost <= 0 or price_delta <= 0:
        return None

    def residual(b: float) -> float:
        p_cur = lmsr_price(q_yes, q_no, b)
        p_tgt = min(p_cur + price_delta, 1.0 - 1e-6)
        if p_tgt <= p_cur:
            return float("inf")
        try:
            shares = shares_to_reach_price(q_yes, q_no, b, "yes", p_tgt)
        except ValueError:
            return float("inf")
        cost = lmsr_cost_to_buy(q_yes, q_no, b, "yes", shares)
        return cost - observed_cost

    lo, hi = b_guess / 1000.0, b_guess * 1000.0
    r_lo, r_hi = residual(lo), residual(hi)
    if not (math.isfinite(r_lo) and math.isfinite(r_hi)):
        return None
    if r_lo * r_hi > 0:
        # Same sign on both ends — no root in range
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        r_mid = residual(mid)
        if not math.isfinite(r_mid):
            return None
        if abs(r_mid) < tol:
            return mid
        if r_mid * r_lo < 0:
            hi, r_hi = mid, r_mid
        else:
            lo, r_lo = mid, r_mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Small self-test (run: python -m lmsr_math)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    b = 1000.0

    # 1. Prices sum to 1
    q_yes = q_yes_from_price(0.65, b)
    p = lmsr_price(q_yes, 0.0, b)
    assert abs(p - 0.65) < 1e-9, p
    assert abs((1 - p) - lmsr_price(0.0, q_yes, b)) < 1e-9
    print(f"price round-trip ok: seed p=0.65 → q_yes={q_yes:.4f} → p={p:.6f}")

    # 2. Cost to buy 100 shares of yes from p=0.5
    q0_y, q0_n = 0.0, 0.0
    cost = lmsr_cost_to_buy(q0_y, q0_n, b, "yes", 100)
    print(f"cost to buy 100 yes from p=0.5: ${cost:.4f} "
          f"(expected slightly >$50 due to LMSR convexity)")
    assert 50.0 < cost < 52.0, cost

    # 3. Shares to move price from 0.5 to 0.65
    shares = shares_to_reach_price(0.0, 0.0, b, "yes", 0.65)
    cost = lmsr_cost_to_buy(0.0, 0.0, b, "yes", shares)
    print(f"shares to move 0.5 → 0.65: {shares:.2f}, cost ${cost:.2f}")
    # Confirm the resulting price
    p_new = lmsr_price(shares, 0.0, b)
    assert abs(p_new - 0.65) < 1e-6, p_new

    # 4. Estimate b from a trade (synthetic: we know b=1000)
    est = estimate_b_from_trade(0.50, 0.51, 40.0)
    print(f"estimate_b_from_trade(0.50, 0.51, 40) = {est:.1f} "
          f"(ground truth b=1000)")
    assert est is not None and 900 < est < 1100

    # 5. Numerical stability with huge q
    p_huge = lmsr_price(1e8, 1e6, b)
    print(f"lmsr_price(q_yes=1e8, q_no=1e6, b=1000) = {p_huge}")
    assert math.isfinite(p_huge)

    print("\nAll self-tests passed.")
