"""
POLYMARKET ARBITRAGE FUNDAMENTALS - Research Edition
=====================================================

Step A: Core concepts for understanding prediction market arbitrage.

How Prediction Markets Work:
----------------------------
A binary prediction market has two outcomes (Yes/No) that are mutually exclusive
and collectively exhaustive. One WILL pay $1, the other pays $0.

The key insight: If you buy BOTH Yes and No tokens, you're guaranteed
to receive $1 when the market resolves (one token pays $1, other pays $0).

Arbitrage Opportunity:
----------------------
If the sum of (Yes price + No price) < $1.00:
    Buying both guarantees a profit of (1.00 - price_sum)

Example:
    Yes token: $0.45
    No token:  $0.50
    Total cost: $0.95
    Guaranteed payout: $1.00
    Gross profit: $0.05 (5.26% return)

However, real-world friction eats into this:
    - Trading fees (typically 0.5-2%)
    - Slippage (price moves as you trade)
    - Time value of money (capital locked until resolution)
"""

from dataclasses import dataclass
from typing import Optional
from enum import Enum


# =============================================================================
# CORE DATA STRUCTURES
# =============================================================================

class Outcome(Enum):
    """Binary market outcomes"""
    YES = "Yes"
    NO = "No"
    UNKNOWN = "Unknown"


@dataclass
class Token:
    """A tradeable outcome token"""
    token_id: str
    outcome: Outcome
    price: float  # Current indicative price (0-1)

    def __post_init__(self):
        if not 0 <= self.price <= 1:
            raise ValueError(f"Token price must be 0-1, got {self.price}")


@dataclass
class BinaryMarket:
    """A binary prediction market with two outcome tokens"""

    # Identifiers
    condition_id: str
    market_id: str
    slug: str
    question: str

    # Structure
    raw_outcome_count: int  # Should be 2 for binary
    priced_token_count: int  # Tokens with valid prices

    # Tokens
    token_a: Optional[Token]  # Typically "Yes"
    token_b: Optional[Token]  # Typically "No"

    # Metadata
    outcome_labels: list[str]
    minutes_until_resolution: Optional[float]
    volume_24h: Optional[float]
    volume_source: str
    active: bool

    @property
    def is_valid_binary(self) -> bool:
        """Check if this is a valid binary market for arb analysis"""
        return (
            self.raw_outcome_count == 2 and
            self.priced_token_count == 2 and
            self.token_a is not None and
            self.token_b is not None
        )

    @property
    def price_sum(self) -> Optional[float]:
        """Sum of both token prices (should be ~1.0 in efficient market)"""
        if not self.is_valid_binary:
            return None
        return self.token_a.price + self.token_b.price

    @property
    def gross_edge(self) -> Optional[float]:
        """Raw arbitrage edge before friction (negative if no arb)"""
        if self.price_sum is None:
            return None
        return 1.0 - self.price_sum

    def net_edge(self, total_friction: float) -> Optional[float]:
        """Net edge after accounting for all friction"""
        if self.gross_edge is None:
            return None
        return max(self.gross_edge - total_friction, 0.0)

    def __str__(self) -> str:
        if self.is_valid_binary:
            return (
                f"Market: {self.slug}\n"
                f"  {self.token_a.outcome.value}: ${self.token_a.price:.4f}\n"
                f"  {self.token_b.outcome.value}: ${self.token_b.price:.4f}\n"
                f"  Sum: ${self.price_sum:.4f}\n"
                f"  Gross Edge: {self.gross_edge*100:.2f}%"
            )
        return f"Market: {self.slug} (invalid binary structure)"


# =============================================================================
# FRICTION CALCULATION
# =============================================================================

@dataclass
class FrictionModel:
    """Models the real-world costs of executing an arb"""

    # Trading fees (Polymarket charges ~2% on winnings)
    fee_rate: float = 0.02  # 2%

    # Slippage buffer (price moves between seeing and executing)
    slippage_buffer: float = 0.005  # 0.5%

    # Risk buffer (model uncertainty, partial fills, etc.)
    risk_buffer: float = 0.005  # 0.5%

    @property
    def total_friction(self) -> float:
        """Total friction to subtract from gross edge"""
        return self.fee_rate + self.slippage_buffer + self.risk_buffer

    def apply_to_edge(self, gross_edge: float) -> float:
        """Apply friction to get net edge"""
        return max(gross_edge - self.total_friction, 0.0)

    def minimum_price_sum(self) -> float:
        """Maximum price_sum that still has positive edge after friction"""
        return 1.0 - self.total_friction


# =============================================================================
# ARBITRAGE ANALYSIS
# =============================================================================

@dataclass
class ArbOpportunity:
    """Represents a potential arbitrage opportunity"""

    market: BinaryMarket
    friction: FrictionModel

    @property
    def is_valid(self) -> bool:
        """Is this a valid arb opportunity?"""
        return (
            self.market.is_valid_binary and
            self.market.gross_edge is not None and
            self.market.gross_edge > self.friction.total_friction
        )

    @property
    def gross_edge(self) -> Optional[float]:
        """Raw edge before friction"""
        return self.market.gross_edge

    @property
    def net_edge(self) -> Optional[float]:
        """Edge after friction"""
        if self.gross_edge is None:
            return None
        return self.friction.apply_to_edge(self.gross_edge)

    def expected_profit(self, position_size: float) -> Optional[float]:
        """Expected profit for a given position size"""
        if self.net_edge is None:
            return None
        return position_size * self.net_edge

    def breakeven_price_sum(self) -> float:
        """Price sum at which net edge becomes zero"""
        return 1.0 - self.friction.total_friction


def analyze_market(market: BinaryMarket, friction: FrictionModel) -> dict:
    """Full analysis of a market for arb potential"""

    if not market.is_valid_binary:
        return {
            "status": "invalid",
            "reason": "not_valid_binary",
            "market_slug": market.slug,
        }

    price_sum = market.price_sum
    gross_edge = market.gross_edge
    net_edge = friction.apply_to_edge(gross_edge)

    return {
        "status": "analyzed",
        "market_slug": market.slug,
        "token_a_price": market.token_a.price,
        "token_b_price": market.token_b.price,
        "price_sum": price_sum,
        "gross_edge": gross_edge,
        "gross_edge_bps": int(gross_edge * 10000),
        "friction_total": friction.total_friction,
        "friction_total_bps": int(friction.total_friction * 10000),
        "net_edge": net_edge,
        "net_edge_bps": int(net_edge * 10000),
        "is_profitable": net_edge > 0,
        "expected_profit_per_100": net_edge * 100,
    }


# =============================================================================
# EDUCATIONAL EXAMPLES
# =============================================================================

def example_profitable_arb():
    """Example of a profitable arbitrage opportunity"""

    print("="*60)
    print("EXAMPLE: Profitable Arbitrage")
    print("="*60)

    # Create tokens
    yes_token = Token(
        token_id="0x123_yes",
        outcome=Outcome.YES,
        price=0.45
    )
    no_token = Token(
        token_id="0x123_no",
        outcome=Outcome.NO,
        price=0.48
    )

    # Create market
    market = BinaryMarket(
        condition_id="0x123",
        market_id="market_123",
        slug="will-btc-hit-100k-2024",
        question="Will BTC hit $100k in 2024?",
        raw_outcome_count=2,
        priced_token_count=2,
        token_a=yes_token,
        token_b=no_token,
        outcome_labels=["Yes", "No"],
        minutes_until_resolution=60 * 24,  # 1 day
        volume_24h=50000.0,
        volume_source="gamma",
        active=True,
    )

    # Standard friction
    friction = FrictionModel()

    # Analyze
    analysis = analyze_market(market, friction)

    print(f"\nMarket: {market.question}")
    print(f"\nPrices:")
    print(f"  Yes: ${yes_token.price:.2f}")
    print(f"  No:  ${no_token.price:.2f}")
    print(f"  Sum: ${market.price_sum:.2f}")

    print(f"\nArbitrage Analysis:")
    print(f"  Gross Edge: {analysis['gross_edge']*100:.2f}% ({analysis['gross_edge_bps']} bps)")
    print(f"  Friction:   {analysis['friction_total']*100:.2f}% ({analysis['friction_total_bps']} bps)")
    print(f"  Net Edge:   {analysis['net_edge']*100:.2f}% ({analysis['net_edge_bps']} bps)")

    print(f"\nProfitability: {'YES' if analysis['is_profitable'] else 'NO'}")
    if analysis['is_profitable']:
        print(f"  Expected profit per $100 invested: ${analysis['expected_profit_per_100']:.2f}")


def example_unprofitable_arb():
    """Example where friction kills the edge"""

    print("\n" + "="*60)
    print("EXAMPLE: Unprofitable (Friction > Edge)")
    print("="*60)

    yes_token = Token(token_id="0x456_yes", outcome=Outcome.YES, price=0.49)
    no_token = Token(token_id="0x456_no", outcome=Outcome.NO, price=0.49)

    market = BinaryMarket(
        condition_id="0x456",
        market_id="market_456",
        slug="will-eth-flip-btc-2024",
        question="Will ETH flip BTC in 2024?",
        raw_outcome_count=2,
        priced_token_count=2,
        token_a=yes_token,
        token_b=no_token,
        outcome_labels=["Yes", "No"],
        minutes_until_resolution=60 * 24 * 7,  # 1 week
        volume_24h=25000.0,
        volume_source="gamma",
        active=True,
    )

    friction = FrictionModel()
    analysis = analyze_market(market, friction)

    print(f"\nMarket: {market.question}")
    print(f"\nPrices:")
    print(f"  Yes: ${yes_token.price:.2f}")
    print(f"  No:  ${no_token.price:.2f}")
    print(f"  Sum: ${market.price_sum:.2f}")

    print(f"\nArbitrage Analysis:")
    print(f"  Gross Edge: {analysis['gross_edge']*100:.2f}% ({analysis['gross_edge_bps']} bps)")
    print(f"  Friction:   {analysis['friction_total']*100:.2f}% ({analysis['friction_total_bps']} bps)")
    print(f"  Net Edge:   {analysis['net_edge']*100:.2f}% ({analysis['net_edge_bps']} bps)")

    print(f"\nProfitability: {'YES' if analysis['is_profitable'] else 'NO'}")
    print("  Reason: Friction (3%) exceeds gross edge (2%)")


def example_efficient_market():
    """Example of an efficient market with no arb"""

    print("\n" + "="*60)
    print("EXAMPLE: Efficient Market (No Arb)")
    print("="*60)

    yes_token = Token(token_id="0x789_yes", outcome=Outcome.YES, price=0.52)
    no_token = Token(token_id="0x789_no", outcome=Outcome.NO, price=0.50)

    market = BinaryMarket(
        condition_id="0x789",
        market_id="market_789",
        slug="efficient-market-example",
        question="Example of efficient market",
        raw_outcome_count=2,
        priced_token_count=2,
        token_a=yes_token,
        token_b=no_token,
        outcome_labels=["Yes", "No"],
        minutes_until_resolution=60 * 24,
        volume_24h=100000.0,
        volume_source="gamma",
        active=True,
    )

    print(f"\nPrices:")
    print(f"  Yes: ${yes_token.price:.2f}")
    print(f"  No:  ${no_token.price:.2f}")
    print(f"  Sum: ${market.price_sum:.2f}")

    print(f"\nAnalysis:")
    print(f"  Price sum > $1.00 means market makers profit by SELLING both sides")
    print(f"  This is the normal efficient state - no free lunch for arb seekers")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run educational examples"""

    print("""
+======================================================================+
|  POLYMARKET ARBITRAGE FUNDAMENTALS                                    |
|                                                                      |
|  Understanding how prediction market arbitrage works                  |
+======================================================================+
    """)

    print("""
THE CORE CONCEPT
----------------
In a binary prediction market:
- "Yes" token pays $1 if outcome happens, $0 otherwise
- "No" token pays $1 if outcome doesn't happen, $0 otherwise
- Exactly ONE of these will pay out

ARBITRAGE OPPORTUNITY
---------------------
If Yes_Price + No_Price < $1.00:
  -> Buy both tokens
  -> Guaranteed to receive $1.00 at resolution
  -> Profit = $1.00 - (Yes_Price + No_Price)

BUT in the real world, friction eats profits:
  -> Trading fees (~2% on winnings at Polymarket)
  -> Slippage (price moves as you trade)
  -> Opportunity cost (capital locked until resolution)
    """)

    example_profitable_arb()
    example_unprofitable_arb()
    example_efficient_market()

    print("\n" + "="*60)
    print("KEY TAKEAWAYS")
    print("="*60)
    print("""
1. Gross edge = 1.0 - price_sum
2. Net edge = gross_edge - friction
3. Only execute if net_edge > 0
4. Real-world friction is typically 2.5-3.5%
5. This means price_sum must be < 0.97 for viable arbs
6. Such opportunities are RARE in liquid markets
    """)


if __name__ == "__main__":
    main()
