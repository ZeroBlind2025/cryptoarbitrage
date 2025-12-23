"""
POLYMARKET CAPITAL VELOCITY - Research Edition
===============================================

Step B: Why short-window markets matter for arbitrage economics.

The Problem with Long-Duration Markets:
---------------------------------------
Even if you find a 3% edge, locking capital for 30 days gives you:
    Annualized return = (1 + 0.03) ^ (365/30) - 1 = 42.5%

But if you find the same 3% edge with 1-day resolution:
    Annualized return = (1 + 0.03) ^ 365 - 1 = 4848.6%

Of course, you won't find such opportunities every day.
But the math is clear: CAPITAL VELOCITY matters enormously.

This module models the economics of capital deployment for arb strategies.
"""

from dataclasses import dataclass
from typing import Optional
import math


# =============================================================================
# CAPITAL VELOCITY MODELS
# =============================================================================

@dataclass
class ArbEconomics:
    """Models the economics of an arbitrage opportunity"""

    # Market parameters
    net_edge: float  # After friction (e.g., 0.03 = 3%)
    hours_until_resolution: float  # When capital unlocks

    # Capital parameters
    position_size_usd: float = 100.0  # How much to deploy

    @property
    def absolute_profit(self) -> float:
        """Dollar profit on this trade"""
        return self.position_size_usd * self.net_edge

    @property
    def days_locked(self) -> float:
        """Days capital is locked"""
        return self.hours_until_resolution / 24.0

    @property
    def annualized_return(self) -> Optional[float]:
        """
        Annualized return if you could repeat this trade continuously.

        This is theoretical - you won't find identical opps daily.
        But it's the right metric for comparing opportunities.
        """
        if self.days_locked <= 0:
            return None

        # Compound annual return
        periods_per_year = 365 / self.days_locked
        return (1 + self.net_edge) ** periods_per_year - 1

    @property
    def capital_efficiency_score(self) -> float:
        """
        Score that balances edge and time.
        Higher = better use of capital.

        Formula: net_edge / sqrt(days_locked)
        This penalizes long lockups but not as harshly as linear.
        """
        if self.days_locked <= 0:
            return 0.0
        return self.net_edge / math.sqrt(self.days_locked)


@dataclass
class PortfolioSimulation:
    """Simulates portfolio returns over time with different arb strategies"""

    initial_capital: float = 10000.0
    simulation_days: int = 30

    def simulate_single_opportunity(
        self,
        net_edge: float,
        days_locked: float,
        reinvest: bool = True,
    ) -> dict:
        """
        Simulate repeatedly taking the same opportunity.

        This is unrealistic (you won't find identical opps) but shows
        the THEORETICAL power of capital velocity.
        """
        if days_locked <= 0:
            return {"error": "Invalid days_locked"}

        capital = self.initial_capital
        trades = 0
        total_profit = 0.0

        elapsed = 0.0
        while elapsed < self.simulation_days:
            # Execute trade
            profit = capital * net_edge
            total_profit += profit
            trades += 1

            if reinvest:
                capital += profit

            elapsed += days_locked

        return {
            "initial_capital": self.initial_capital,
            "final_capital": capital,
            "total_profit": total_profit,
            "return_pct": (capital - self.initial_capital) / self.initial_capital * 100,
            "trades_executed": trades,
            "days_simulated": self.simulation_days,
            "avg_daily_return": total_profit / self.simulation_days / self.initial_capital * 100,
        }

    def compare_strategies(self) -> list[dict]:
        """Compare different edge/duration combinations"""

        scenarios = [
            # (name, net_edge, days_locked)
            ("3% edge, 1 day", 0.03, 1),
            ("3% edge, 7 days", 0.03, 7),
            ("3% edge, 30 days", 0.03, 30),
            ("1% edge, 4 hours", 0.01, 0.167),
            ("5% edge, 14 days", 0.05, 14),
        ]

        results = []
        for name, edge, days in scenarios:
            result = self.simulate_single_opportunity(edge, days)
            result["scenario"] = name
            result["edge"] = edge
            result["days_locked"] = days
            results.append(result)

        return results


# =============================================================================
# MARKET WINDOW ANALYSIS
# =============================================================================

@dataclass
class MarketWindowConfig:
    """Configuration for filtering markets by resolution window"""

    # Core filters
    max_resolution_hours: float = 24.0  # Only consider markets resolving within this window
    min_resolution_hours: float = 0.5  # Avoid markets about to close (might have issues)

    # Quality filters
    min_volume_24h: float = 1000.0  # Minimum daily volume in USD
    min_liquidity_depth: float = 100.0  # Minimum order book depth

    @property
    def max_resolution_days(self) -> float:
        return self.max_resolution_hours / 24.0

    def is_in_window(self, hours_until_resolution: float) -> bool:
        """Check if market is within our target window"""
        return (
            self.min_resolution_hours <= hours_until_resolution <= self.max_resolution_hours
        )


def calculate_opportunity_score(
    net_edge: float,
    hours_until_resolution: float,
    volume_24h: float,
    min_volume: float = 1000.0,
) -> float:
    """
    Score an opportunity considering edge, time, and liquidity.

    Components:
    - Edge score: Higher edge = better
    - Velocity score: Shorter time = better
    - Liquidity score: Higher volume = better (but diminishing returns)

    Returns 0-100 score.
    """
    if net_edge <= 0 or hours_until_resolution <= 0:
        return 0.0

    # Edge component (0-40 points)
    # 3% edge = 30 points, 5% edge = 40 points (capped)
    edge_score = min(net_edge * 1000, 40)

    # Velocity component (0-40 points)
    # 1 hour = 40 points, 24 hours = 10 points, 168 hours (7 days) = ~5 points
    days = hours_until_resolution / 24.0
    velocity_score = 40 / (1 + days)

    # Liquidity component (0-20 points)
    # Log scale: $1k = 10 points, $10k = 15 points, $100k = 20 points
    if volume_24h < min_volume:
        liquidity_score = 0
    else:
        liquidity_score = min(10 + 5 * math.log10(volume_24h / min_volume), 20)

    return edge_score + velocity_score + liquidity_score


# =============================================================================
# EDUCATIONAL EXAMPLES
# =============================================================================

def example_capital_velocity():
    """Demonstrate why capital velocity matters"""

    print("="*60)
    print("EXAMPLE: Same Edge, Different Lock Times")
    print("="*60)

    scenarios = [
        ArbEconomics(net_edge=0.03, hours_until_resolution=4),
        ArbEconomics(net_edge=0.03, hours_until_resolution=24),
        ArbEconomics(net_edge=0.03, hours_until_resolution=168),  # 1 week
        ArbEconomics(net_edge=0.03, hours_until_resolution=720),  # 30 days
    ]

    print("\nAll scenarios: 3% net edge, $100 position")
    print("-" * 60)
    print(f"{'Lock Time':<20} {'Profit':<10} {'Annualized':<15} {'Efficiency':<10}")
    print("-" * 60)

    for s in scenarios:
        lock_str = f"{s.hours_until_resolution:.0f} hours" if s.hours_until_resolution < 48 else f"{s.days_locked:.0f} days"
        ann_return = s.annualized_return
        if ann_return and ann_return > 100:
            ann_str = f"{ann_return:.0f}%"
        else:
            ann_str = f"{ann_return*100:.1f}%" if ann_return else "N/A"

        print(f"{lock_str:<20} ${s.absolute_profit:.2f}      {ann_str:<15} {s.capital_efficiency_score:.4f}")


def example_portfolio_simulation():
    """Show compound returns with different strategies"""

    print("\n" + "="*60)
    print("EXAMPLE: 30-Day Portfolio Simulation")
    print("="*60)
    print("(Assuming you can find identical opportunities repeatedly)")

    sim = PortfolioSimulation(initial_capital=1000, simulation_days=30)
    results = sim.compare_strategies()

    print("\n" + "-" * 80)
    print(f"{'Scenario':<25} {'Trades':<8} {'Final $':<12} {'Return %':<12} {'Daily %':<10}")
    print("-" * 80)

    for r in results:
        print(
            f"{r['scenario']:<25} "
            f"{r['trades_executed']:<8} "
            f"${r['final_capital']:,.0f}      "
            f"{r['return_pct']:.1f}%        "
            f"{r['avg_daily_return']:.2f}%"
        )


def example_opportunity_scoring():
    """Show how to score and rank opportunities"""

    print("\n" + "="*60)
    print("EXAMPLE: Opportunity Scoring")
    print("="*60)

    opportunities = [
        # (name, net_edge, hours_until, volume_24h)
        ("High edge, slow", 0.05, 168, 50000),
        ("Med edge, fast", 0.025, 12, 30000),
        ("Low edge, very fast", 0.01, 2, 10000),
        ("High edge, med speed, low vol", 0.04, 24, 500),
        ("Best overall", 0.035, 8, 75000),
    ]

    print("\n" + "-" * 80)
    print(f"{'Opportunity':<30} {'Edge':<8} {'Hours':<8} {'Volume':<12} {'Score':<8}")
    print("-" * 80)

    scored = []
    for name, edge, hours, vol in opportunities:
        score = calculate_opportunity_score(edge, hours, vol)
        scored.append((name, edge, hours, vol, score))

    # Sort by score
    scored.sort(key=lambda x: x[4], reverse=True)

    for name, edge, hours, vol, score in scored:
        print(
            f"{name:<30} "
            f"{edge*100:.1f}%     "
            f"{hours:<8} "
            f"${vol:,}      "
            f"{score:.1f}"
        )


def example_why_short_windows():
    """Explain the strategic importance of short windows"""

    print("\n" + "="*60)
    print("WHY SHORT RESOLUTION WINDOWS?")
    print("="*60)

    print("""
1. CAPITAL VELOCITY
   - Same edge with faster turnover = higher returns
   - $1000 at 3% edge:
     - 30-day lock: $30 profit (36% annualized)
     - 1-day lock: Potentially $30 x 30 = $900 (reinvested)

2. REDUCED RISK
   - Less time for unexpected events
   - Market can't move against you for long
   - Outcome becomes certain faster

3. INFORMATION ADVANTAGE
   - Short-window markets often have clearer outcomes
   - Crypto events (unlocks, earnings) have known dates
   - Weather, sports, known outcomes

4. COMPETITION
   - Long-duration markets are well-arbitraged
   - Short windows have more pricing inefficiency
   - Less time for market to correct

5. PSYCHOLOGICAL
   - Faster feedback loop
   - Better for research (more data points)
   - Lower anxiety from open positions
    """)


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run capital velocity analysis"""

    print("""
+======================================================================+
|  POLYMARKET CAPITAL VELOCITY ANALYSIS                                 |
|                                                                      |
|  Understanding why SHORT resolution windows matter                    |
+======================================================================+
    """)

    print("""
THE KEY INSIGHT
---------------
A 3% edge means nothing without knowing HOW LONG your capital is locked.

Consider:
- 3% edge, locked for 30 days = 36% annualized
- 3% edge, locked for 1 day = 4,848% annualized (theoretical)

Of course you won't find 3% edges every day. But the math shows why
SHORT RESOLUTION WINDOWS dramatically improve capital efficiency.

This is why our scanner focuses on:
- Markets resolving within 24 hours
- Crypto events with known timing
- High-velocity capital deployment
    """)

    example_capital_velocity()
    example_portfolio_simulation()
    example_opportunity_scoring()
    example_why_short_windows()

    print("\n" + "="*60)
    print("IMPLICATIONS FOR SCANNER DESIGN")
    print("="*60)
    print("""
1. Filter for short resolution windows (< 24 hours ideal)
2. Score opportunities using edge/time/liquidity composite
3. Prioritize capital efficiency over raw edge
4. Track opportunity frequency for realistic return estimates
5. Log time-to-resolution for white paper analysis
    """)


if __name__ == "__main__":
    main()
