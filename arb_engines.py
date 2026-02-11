"""
ARBITRAGE ENGINES
=================

Pluggable arbitrage detection engines:

1. SUM-TO-ONE: Buy both YES+NO when combined price < $1.00
   - Edge = $1.00 - price_sum
   - Works on any binary market
   - Guaranteed profit on resolution

2. TAIL-END: Buy high-probability outcome near settlement
   - Target: 95-99% probability outcomes
   - Retail sells early to lock in profits
   - Buy at $0.97-$0.99, settle at $1.00
   - Higher risk: outcome must resolve as expected

Both engines can run simultaneously or independently.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal
from enum import Enum
import uuid


# =============================================================================
# ENGINE TYPES
# =============================================================================

class EngineType(Enum):
    SUM_TO_ONE = "sum_to_one"
    TAIL_END = "tail_end"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class MarketState:
    """Current state of a binary market"""
    market_slug: str
    question: str
    token_a_id: str  # YES token
    token_b_id: str  # NO token

    # Prices
    token_a_bid: Optional[float] = None
    token_a_ask: Optional[float] = None
    token_b_bid: Optional[float] = None
    token_b_ask: Optional[float] = None

    # Depth at best price
    token_a_ask_depth: float = 0
    token_b_ask_depth: float = 0

    # Resolution timing
    minutes_until_resolution: Optional[float] = None

    # Volume
    volume_24h: Optional[float] = None

    # Market category for engine routing
    market_category: Optional[str] = None  # "crypto", "sports", "politics", etc.

    timestamp_us: int = 0

    def is_crypto_15min(self) -> bool:
        """Check if this is a 15-min crypto market (BTC, ETH, XRP, SOL)"""
        if self.market_category != "crypto":
            return False
        if self.minutes_until_resolution is None:
            return False
        # 15-minute window markets resolve within ~20 minutes
        if self.minutes_until_resolution > 20:
            return False
        # Check for the 4 main crypto assets
        slug_lower = self.market_slug.lower()
        question_lower = self.question.lower()
        crypto_keywords = ["btc", "bitcoin", "eth", "ethereum", "xrp", "ripple", "sol", "solana"]
        return any(kw in slug_lower or kw in question_lower for kw in crypto_keywords)

    def is_sports(self) -> bool:
        """Check if this is a sports market"""
        return self.market_category == "sports"

    @property
    def price_sum(self) -> Optional[float]:
        """Sum of best asks (cost to lock arb)"""
        if self.token_a_ask and self.token_b_ask:
            return self.token_a_ask + self.token_b_ask
        return None

    @property
    def yes_probability(self) -> Optional[float]:
        """Implied YES probability from mid price"""
        if self.token_a_bid and self.token_a_ask:
            return (self.token_a_bid + self.token_a_ask) / 2
        return self.token_a_ask

    @property
    def no_probability(self) -> Optional[float]:
        """Implied NO probability from mid price"""
        if self.token_b_bid and self.token_b_ask:
            return (self.token_b_bid + self.token_b_ask) / 2
        return self.token_b_ask


@dataclass
class EngineSignal:
    """Signal from an arbitrage engine"""
    signal_id: str
    engine: EngineType
    detected_at_us: int

    # Market info
    market_slug: str
    market_state: MarketState

    # Signal details
    action: Literal["buy_both", "buy_yes", "buy_no"]

    # For sum-to-one
    price_sum: Optional[float] = None
    arb_edge: Optional[float] = None

    # For tail-end
    target_token: Optional[str] = None  # token_id to buy
    target_price: Optional[float] = None
    implied_probability: Optional[float] = None
    expected_payout: float = 1.0

    # Sizing
    max_size: float = 0
    recommended_size: float = 0

    # Risk metrics
    confidence: float = 0  # 0-1 score
    risk_level: Literal["low", "medium", "high"] = "medium"

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "engine": self.engine.value,
            "detected_at_us": self.detected_at_us,
            "market_slug": self.market_slug,
            "action": self.action,
            "price_sum": self.price_sum,
            "arb_edge": self.arb_edge,
            "target_token": self.target_token,
            "target_price": self.target_price,
            "implied_probability": self.implied_probability,
            "max_size": self.max_size,
            "recommended_size": self.recommended_size,
            "confidence": self.confidence,
            "risk_level": self.risk_level,
        }


# =============================================================================
# SUM-TO-ONE ENGINE
# =============================================================================

@dataclass
class SumToOneConfig:
    """Configuration for Sum-to-One engine"""

    # Threshold: max price sum to trigger (< $1.00 = guaranteed profit)
    max_price_sum: float = 1.0  # Execute if total < $1.00

    # Minimum edge after fees (0 = any profit is good)
    min_edge_after_fees: float = 0.0  # No minimum

    # Fee estimate
    fee_rate: float = 0.02  # 2% Polymarket fee

    # Depth requirements
    min_depth_usd: float = 10.0

    # Position sizing
    max_position_usd: float = 100.0

    # Any resolution timing is fine for sum-to-one
    max_resolution_hours: Optional[float] = None

    # Market type filter: only analyze these categories
    # Default: focus on 15-min crypto markets (BTC, ETH, XRP, SOL)
    crypto_15min_only: bool = True


class SumToOneEngine:
    """
    Sum-to-One Arbitrage Engine

    Detects when YES + NO < $1.00 and signals to buy both.
    Guaranteed profit on resolution regardless of outcome.
    """

    def __init__(self, config: SumToOneConfig):
        self.config = config
        self.enabled = True

        # Stats
        self.signals_generated = 0
        self.total_edge_detected = 0.0

        # Debug counters
        self.debug_not_crypto = 0
        self.debug_missing_asks = 0
        self.debug_price_sum_high = 0
        self.debug_low_edge = 0
        self.debug_low_depth = 0
        self.debug_passed = 0
        self.debug_best_price_sum = 2.0  # Track closest to opportunity
        self.debug_markets_checked = 0

    def analyze(self, market: MarketState) -> Optional[EngineSignal]:
        """
        Analyze market for sum-to-one opportunity.

        Returns signal if opportunity detected, None otherwise.
        """
        if not self.enabled:
            return None

        # Market type filter: only 15-min crypto markets (BTC, ETH, XRP, SOL)
        if self.config.crypto_15min_only and not market.is_crypto_15min():
            self.debug_not_crypto += 1
            return None

        self.debug_markets_checked += 1

        # Need both asks
        if market.token_a_ask is None or market.token_b_ask is None:
            self.debug_missing_asks += 1
            return None

        price_sum = market.price_sum
        if price_sum is None:
            return None

        # Track best price_sum seen (closest to opportunity)
        if price_sum < self.debug_best_price_sum:
            self.debug_best_price_sum = price_sum

        # Check threshold
        if price_sum >= self.config.max_price_sum:
            self.debug_price_sum_high += 1
            return None

        # Calculate edge
        gross_edge = 1.0 - price_sum
        net_edge = gross_edge - self.config.fee_rate

        if net_edge < self.config.min_edge_after_fees:
            self.debug_low_edge += 1
            return None

        # Check depth
        min_depth = min(market.token_a_ask_depth, market.token_b_ask_depth)
        depth_usd = min_depth * price_sum

        if depth_usd < self.config.min_depth_usd:
            self.debug_low_depth += 1
            return None

        self.debug_passed += 1

        # Calculate sizing
        max_size = min_depth
        max_position = self.config.max_position_usd / price_sum
        recommended_size = min(max_size, max_position)

        # Create signal
        signal = EngineSignal(
            signal_id=f"s2o_{uuid.uuid4().hex[:8]}",
            engine=EngineType.SUM_TO_ONE,
            detected_at_us=time.perf_counter_ns() // 1000,
            market_slug=market.market_slug,
            market_state=market,
            action="buy_both",
            price_sum=price_sum,
            arb_edge=net_edge,
            max_size=max_size,
            recommended_size=recommended_size,
            confidence=min(1.0, net_edge / 0.03),  # Higher edge = higher confidence
            risk_level="low",  # Sum-to-one is guaranteed profit
        )

        self.signals_generated += 1
        self.total_edge_detected += net_edge

        return signal

    def get_stats(self) -> dict:
        return {
            "engine": "sum_to_one",
            "enabled": self.enabled,
            "signals_generated": self.signals_generated,
            "total_edge_detected": self.total_edge_detected,
            "config": {
                "max_price_sum": self.config.max_price_sum,
                "min_edge_after_fees": self.config.min_edge_after_fees,
            },
            "debug_rejections": {
                "not_crypto_15min": self.debug_not_crypto,
                "missing_asks_orderbook_failed": self.debug_missing_asks,
                "price_sum_too_high": self.debug_price_sum_high,
                "edge_too_low_after_fees": self.debug_low_edge,
                "depth_too_low": self.debug_low_depth,
                "crypto_markets_checked": self.debug_markets_checked,
                "PASSED_ALL_CHECKS": self.debug_passed,
                "best_price_sum_seen": round(self.debug_best_price_sum, 4),
            }
        }


# =============================================================================
# TAIL-END ENGINE
# =============================================================================

@dataclass
class TailEndConfig:
    """Configuration for Tail-End engine"""

    # Probability thresholds
    min_probability: float = 0.95  # 95% minimum
    max_probability: float = 0.995  # 99.5% maximum (avoid certain markets)

    # Price thresholds (what we're willing to pay)
    min_price: float = 0.95  # Won't buy below 95c
    max_price: float = 0.99  # Won't buy above 99c

    # Time to resolution
    max_minutes_until_resolution: float = 60  # 1 hour max
    min_minutes_until_resolution: float = 1   # At least 1 minute

    # Volume requirements (higher = more confident)
    min_volume_24h: float = 5000.0

    # Depth requirements
    min_depth_usd: float = 20.0

    # Position sizing
    max_position_usd: float = 50.0  # Smaller due to directional risk

    # Risk management
    max_concurrent_positions: int = 5

    # Minimum risk-adjusted EV (set to 0 for sports where game state > market price)
    # Default 0.5% EV requirement - but for sports tail-end this should be 0
    # because we're betting on game state (score/time), not market efficiency
    min_risk_adjusted_ev: float = 0.005

    # Market type filter: only analyze these categories
    # Default: focus on sports markets for tail-end
    sports_only: bool = True


class TailEndEngine:
    """
    Tail-End Arbitrage Engine

    Targets markets near resolution where outcome is nearly certain (95-99%).
    Retail traders sell early to lock in profits, creating buying opportunities.

    Strategy:
    - Find markets resolving soon with high-probability outcome
    - Buy the leading token at $0.97-$0.99
    - Wait for settlement at $1.00

    Risk: Outcome must resolve as expected. Black swan events can cause losses.
    """

    def __init__(self, config: TailEndConfig):
        self.config = config
        self.enabled = True

        # Track active positions to limit exposure
        self.active_positions: dict[str, float] = {}

        # Stats
        self.signals_generated = 0
        self.yes_signals = 0
        self.no_signals = 0
        self.avg_probability = 0.0

        # Debug counters for rejection reasons
        self.debug_not_sports = 0
        self.debug_position_limit = 0
        self.debug_already_positioned = 0
        self.debug_no_resolution_time = 0
        self.debug_resolution_too_far = 0
        self.debug_resolution_too_close = 0
        self.debug_low_volume = 0
        self.debug_checked_tokens = 0
        self.debug_low_prob = 0
        self.debug_high_prob = 0
        self.debug_low_price = 0
        self.debug_high_price = 0
        self.debug_low_depth = 0
        self.debug_low_ev = 0
        self.debug_passed_all = 0

    def analyze(self, market: MarketState) -> Optional[EngineSignal]:
        """
        Analyze market for tail-end opportunity.

        Returns signal if opportunity detected, None otherwise.
        """
        if not self.enabled:
            return None

        # Market type filter: only sports markets for tail-end
        if self.config.sports_only and not market.is_sports():
            self.debug_not_sports += 1
            return None

        # Check concurrent position limit
        if len(self.active_positions) >= self.config.max_concurrent_positions:
            self.debug_position_limit += 1
            return None

        # Already have position in this market?
        if market.market_slug in self.active_positions:
            self.debug_already_positioned += 1
            return None

        # Check resolution timing
        if market.minutes_until_resolution is None:
            self.debug_no_resolution_time += 1
            return None

        if market.minutes_until_resolution > self.config.max_minutes_until_resolution:
            self.debug_resolution_too_far += 1
            return None

        if market.minutes_until_resolution < self.config.min_minutes_until_resolution:
            self.debug_resolution_too_close += 1
            return None

        # Check volume
        if market.volume_24h and market.volume_24h < self.config.min_volume_24h:
            self.debug_low_volume += 1
            return None

        # Analyze both sides for tail-end opportunity
        signal = self._check_token(market, "yes")
        if signal is None:
            signal = self._check_token(market, "no")

        return signal

    def _check_token(
        self,
        market: MarketState,
        side: Literal["yes", "no"]
    ) -> Optional[EngineSignal]:
        """Check if a specific token qualifies for tail-end"""
        self.debug_checked_tokens += 1

        if side == "yes":
            ask = market.token_a_ask
            depth = market.token_a_ask_depth
            token_id = market.token_a_id
            prob = market.yes_probability
        else:
            ask = market.token_b_ask
            depth = market.token_b_ask_depth
            token_id = market.token_b_id
            prob = market.no_probability

        if ask is None or prob is None:
            return None

        # Check probability range (this token should be winning)
        if prob < self.config.min_probability:
            self.debug_low_prob += 1
            return None

        if prob > self.config.max_probability:
            self.debug_high_prob += 1
            return None  # Too certain, probably already priced in

        # Check price range
        if ask < self.config.min_price:
            self.debug_low_price += 1
            return None  # Suspiciously cheap

        if ask > self.config.max_price:
            self.debug_high_price += 1
            return None  # Not enough edge

        # Check depth
        depth_usd = depth * ask if depth else 0
        if depth_usd < self.config.min_depth_usd:
            self.debug_low_depth += 1
            return None

        # Calculate expected profit
        # Buy at ask, expect payout of $1.00
        expected_profit = 1.0 - ask

        # Risk-adjusted profit (probability of winning * profit - probability of losing * cost)
        # NOTE: For sports, prob comes from market price, so risk_adjusted ≈ 0 on efficient markets
        # The real edge comes from game state (score/time) which isn't captured here
        # Set min_risk_adjusted_ev=0 in config to disable this filter for sports
        risk_adjusted = (prob * expected_profit) - ((1 - prob) * ask)

        if risk_adjusted < self.config.min_risk_adjusted_ev:
            self.debug_low_ev += 1
            return None

        # PASSED ALL CHECKS!
        self.debug_passed_all += 1

        # Calculate sizing
        max_size = depth
        max_position = self.config.max_position_usd / ask
        recommended_size = min(max_size, max_position)

        # Calculate confidence based on probability and time to resolution
        time_factor = 1.0 - (market.minutes_until_resolution / self.config.max_minutes_until_resolution)
        prob_factor = (prob - self.config.min_probability) / (self.config.max_probability - self.config.min_probability)
        confidence = (time_factor * 0.3 + prob_factor * 0.7)  # Weight probability more

        # Determine risk level
        if prob >= 0.98 and market.minutes_until_resolution <= 10:
            risk_level = "low"
        elif prob >= 0.96:
            risk_level = "medium"
        else:
            risk_level = "high"

        signal = EngineSignal(
            signal_id=f"te_{uuid.uuid4().hex[:8]}",
            engine=EngineType.TAIL_END,
            detected_at_us=time.perf_counter_ns() // 1000,
            market_slug=market.market_slug,
            market_state=market,
            action=f"buy_{side}",
            target_token=token_id,
            target_price=ask,
            implied_probability=prob,
            expected_payout=1.0,
            max_size=max_size,
            recommended_size=recommended_size,
            confidence=confidence,
            risk_level=risk_level,
        )

        # Update stats
        self.signals_generated += 1
        if side == "yes":
            self.yes_signals += 1
        else:
            self.no_signals += 1

        # Running average of probability
        n = self.signals_generated
        self.avg_probability = ((n - 1) * self.avg_probability + prob) / n

        return signal

    def register_position(self, market_slug: str, size: float):
        """Register an active position"""
        self.active_positions[market_slug] = size

    def close_position(self, market_slug: str):
        """Close a position after resolution"""
        self.active_positions.pop(market_slug, None)

    def get_stats(self) -> dict:
        return {
            "engine": "tail_end",
            "enabled": self.enabled,
            "signals_generated": self.signals_generated,
            "yes_signals": self.yes_signals,
            "no_signals": self.no_signals,
            "avg_probability": self.avg_probability,
            "active_positions": len(self.active_positions),
            "config": {
                "min_probability": self.config.min_probability,
                "max_probability": self.config.max_probability,
                "max_minutes": self.config.max_minutes_until_resolution,
                "min_risk_adjusted_ev": self.config.min_risk_adjusted_ev,
            },
            "debug_rejections": {
                "not_sports": self.debug_not_sports,
                "no_resolution_time": self.debug_no_resolution_time,
                "position_limit": self.debug_position_limit,
                "already_positioned": self.debug_already_positioned,
                "resolution_too_far": self.debug_resolution_too_far,
                "resolution_too_close": self.debug_resolution_too_close,
                "low_volume": self.debug_low_volume,
                "tokens_checked": self.debug_checked_tokens,
                "low_probability": self.debug_low_prob,
                "high_probability": self.debug_high_prob,
                "low_price": self.debug_low_price,
                "high_price": self.debug_high_price,
                "low_depth": self.debug_low_depth,
                "low_ev": self.debug_low_ev,
                "passed_all_checks": self.debug_passed_all,
            }
        }


# =============================================================================
# ENGINE MANAGER
# =============================================================================

class EngineManager:
    """
    Manages multiple arbitrage engines.

    Allows enabling/disabling engines and routes market data to active engines.
    """

    def __init__(
        self,
        sum_to_one_config: Optional[SumToOneConfig] = None,
        tail_end_config: Optional[TailEndConfig] = None,
    ):
        # Initialize engines with configs or defaults
        self.sum_to_one = SumToOneEngine(sum_to_one_config or SumToOneConfig())
        self.tail_end = TailEndEngine(tail_end_config or TailEndConfig())

        # Engine state
        self._engines = {
            EngineType.SUM_TO_ONE: self.sum_to_one,
            EngineType.TAIL_END: self.tail_end,
        }

    def enable_engine(self, engine_type: EngineType):
        """Enable an engine"""
        self._engines[engine_type].enabled = True

    def disable_engine(self, engine_type: EngineType):
        """Disable an engine"""
        self._engines[engine_type].enabled = False

    def toggle_engine(self, engine_type: EngineType) -> bool:
        """Toggle engine state, returns new state"""
        engine = self._engines[engine_type]
        engine.enabled = not engine.enabled
        return engine.enabled

    def is_enabled(self, engine_type: EngineType) -> bool:
        """Check if engine is enabled"""
        return self._engines[engine_type].enabled

    def analyze(self, market: MarketState) -> list[EngineSignal]:
        """
        Analyze market with all enabled engines.

        Returns list of signals (can be multiple if multiple engines trigger).
        """
        signals = []

        for engine_type, engine in self._engines.items():
            if engine.enabled:
                signal = engine.analyze(market)
                if signal:
                    signals.append(signal)

        return signals

    def get_stats(self) -> dict:
        """Get stats from all engines"""
        return {
            "sum_to_one": self.sum_to_one.get_stats(),
            "tail_end": self.tail_end.get_stats(),
        }

    def get_enabled_engines(self) -> list[str]:
        """Get list of enabled engine names"""
        return [
            engine_type.value
            for engine_type, engine in self._engines.items()
            if engine.enabled
        ]


# =============================================================================
# DEMO
# =============================================================================

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  ARBITRAGE ENGINES                                                   ║
    ║                                                                      ║
    ║  1. Sum-to-One: Buy YES+NO when combined < $1.00 (guaranteed)        ║
    ║  2. Tail-End: Buy 95-99% outcomes near resolution (directional)      ║
    ╚══════════════════════════════════════════════════════════════════════╝
    """)

    # Create manager with both engines
    manager = EngineManager()

    print("Engine Status:")
    print(f"  Sum-to-One: {'ENABLED' if manager.is_enabled(EngineType.SUM_TO_ONE) else 'DISABLED'}")
    print(f"  Tail-End:   {'ENABLED' if manager.is_enabled(EngineType.TAIL_END) else 'DISABLED'}")

    # Simulate a market state for sum-to-one
    print("\n" + "=" * 60)
    print("TEST: Sum-to-One Detection")
    print("=" * 60)

    market1 = MarketState(
        market_slug="btc-above-100k-jan",
        question="Will BTC be above $100k?",
        token_a_id="0xyes123",
        token_b_id="0xno456",
        token_a_ask=0.55,
        token_b_ask=0.42,  # Sum = 0.97
        token_a_ask_depth=500,
        token_b_ask_depth=500,
        minutes_until_resolution=60,
    )

    print(f"Market: {market1.market_slug}")
    print(f"YES ask: ${market1.token_a_ask}")
    print(f"NO ask:  ${market1.token_b_ask}")
    print(f"Sum:     ${market1.price_sum}")

    signals = manager.analyze(market1)
    for signal in signals:
        print(f"\nSIGNAL: {signal.engine.value}")
        print(f"  Action: {signal.action}")
        print(f"  Edge: {signal.arb_edge*100:.2f}%")
        print(f"  Confidence: {signal.confidence:.2f}")

    # Simulate a market state for tail-end
    print("\n" + "=" * 60)
    print("TEST: Tail-End Detection")
    print("=" * 60)

    market2 = MarketState(
        market_slug="fed-rate-decision",
        question="Will Fed raise rates?",
        token_a_id="0xyes789",
        token_b_id="0xno012",
        token_a_ask=0.97,  # 97% probability
        token_b_ask=0.04,
        token_a_bid=0.96,
        token_b_bid=0.03,
        token_a_ask_depth=1000,
        token_b_ask_depth=200,
        minutes_until_resolution=15,  # 15 minutes to resolution
        volume_24h=50000,
    )

    print(f"Market: {market2.market_slug}")
    print(f"YES ask: ${market2.token_a_ask} (prob: {market2.yes_probability*100:.1f}%)")
    print(f"NO ask:  ${market2.token_b_ask}")
    print(f"Resolves in: {market2.minutes_until_resolution} minutes")

    signals = manager.analyze(market2)
    for signal in signals:
        print(f"\nSIGNAL: {signal.engine.value}")
        print(f"  Action: {signal.action}")
        print(f"  Target price: ${signal.target_price}")
        print(f"  Implied prob: {signal.implied_probability*100:.1f}%")
        print(f"  Confidence: {signal.confidence:.2f}")
        print(f"  Risk: {signal.risk_level}")

    print("\n" + "=" * 60)
    print("ENGINE STATS")
    print("=" * 60)
    import json
    print(json.dumps(manager.get_stats(), indent=2))


if __name__ == "__main__":
    main()
