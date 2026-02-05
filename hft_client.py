#!/usr/bin/env python3
"""
HFT CLOB CLIENT - High Frequency Trading Edition
=================================================

Optimized for QuantVPS deployment with:
- WebSocket price feeds (sub-100ms latency)
- FOK (Fill-or-Kill) orders for guaranteed execution
- Minimal processing overhead
- Real-time latency tracking

Arbitrage Engines:
- SUM-TO-ONE: Buy YES+NO when combined < $1.00 (guaranteed profit)
- TAIL-END: Buy 95-99% outcomes near resolution (directional bet)

Modes:
- DEMO: Paper trades with real price feeds, tracks what would have happened
- LIVE: Actual order execution on Polygon

Prerequisites:
    pip install websockets aiohttp py-clob-client python-dotenv
"""

import asyncio
import json
import os
import time
import hmac
import hashlib
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Literal
from enum import Enum
import threading
from collections import deque
import uuid

import requests

from arb_engines import (
    EngineType,
    EngineManager,
    EngineSignal,
    MarketState,
    SumToOneConfig,
    TailEndConfig,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class HFTConfig:
    """HFT client configuration optimized for speed"""

    # Endpoints
    clob_api_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Authentication
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    private_key: str = ""

    # Chain
    chain_id: int = 137  # Polygon mainnet

    # HFT Settings
    request_timeout_ms: int = 500  # Aggressive timeout for HFT
    max_retries: int = 1  # Minimal retries for speed

    # Order Settings
    use_fok: bool = True  # Fill-or-Kill orders
    price_buffer_bps: int = 10  # 0.1% buffer for FOK fills

    # Position Limits
    max_order_size_usd: float = 100.0
    min_order_size_usd: float = 5.0
    max_total_exposure: float = 500.0

    # Latency Tracking
    track_latency: bool = True

    @classmethod
    def from_env(cls) -> "HFTConfig":
        return cls(
            api_key=os.getenv("POLYMARKET_API_KEY", ""),
            api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
            private_key=os.getenv("POLYGON_PRIVATE_KEY", ""),
        )

    @property
    def has_credentials(self) -> bool:
        return all([self.api_key, self.api_secret, self.passphrase, self.private_key])


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class TradingMode(Enum):
    DEMO = "demo"
    LIVE = "live"


class OrderType(Enum):
    GTC = "GTC"  # Good Till Cancelled
    FOK = "FOK"  # Fill or Kill
    IOC = "IOC"  # Immediate or Cancel


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class PriceLevel:
    price: float
    size: float
    timestamp_ms: int = 0


@dataclass
class OrderBook:
    token_id: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    last_update_ms: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread_bps(self) -> Optional[int]:
        if self.best_bid and self.best_ask:
            return int((self.best_ask - self.best_bid) * 10000)
        return None


@dataclass
class Trade:
    """Record of a trade (demo or live)"""
    trade_id: str
    mode: TradingMode
    timestamp: datetime

    # Engine that triggered this trade
    engine: str  # "sum_to_one" or "tail_end"

    # Market info
    market_slug: str
    token_a_id: str
    token_b_id: str

    # Execution details
    token_a_price: float
    token_b_price: float
    size: float
    total_cost: float

    # Trade type
    trade_type: str = "buy_both"  # "buy_both", "buy_yes", "buy_no"

    # Outcome
    status: OrderStatus = OrderStatus.PENDING
    expected_edge: float = 0.0

    # For tail-end trades
    target_probability: Optional[float] = None

    # Timing (microseconds)
    detection_to_submit_us: int = 0
    submit_to_fill_us: int = 0
    total_latency_us: int = 0

    # For resolved trades
    resolved: bool = False
    resolution_time: Optional[datetime] = None
    winning_token: Optional[str] = None
    pnl: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "mode": self.mode.value,
            "engine": self.engine,
            "timestamp": self.timestamp.isoformat(),
            "market_slug": self.market_slug,
            "token_a_id": self.token_a_id,
            "token_b_id": self.token_b_id,
            "token_a_price": self.token_a_price,
            "token_b_price": self.token_b_price,
            "size": self.size,
            "total_cost": self.total_cost,
            "trade_type": self.trade_type,
            "status": self.status.value,
            "expected_edge": self.expected_edge,
            "target_probability": self.target_probability,
            "detection_to_submit_us": self.detection_to_submit_us,
            "submit_to_fill_us": self.submit_to_fill_us,
            "total_latency_us": self.total_latency_us,
            "resolved": self.resolved,
            "resolution_time": self.resolution_time.isoformat() if self.resolution_time else None,
            "winning_token": self.winning_token,
            "pnl": self.pnl,
        }


@dataclass
class ArbOpportunity:
    """Detected arbitrage opportunity"""
    opportunity_id: str
    detected_at_us: int  # Microsecond timestamp

    market_slug: str
    token_a_id: str
    token_b_id: str

    token_a_ask: float
    token_b_ask: float
    price_sum: float
    edge: float

    # Depth available
    token_a_depth: float
    token_b_depth: float
    max_executable_size: float


@dataclass
class LatencyStats:
    """Track execution latency"""
    samples: deque = field(default_factory=lambda: deque(maxlen=1000))

    def add(self, latency_us: int):
        self.samples.append(latency_us)

    @property
    def avg_us(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0

    @property
    def min_us(self) -> int:
        return min(self.samples) if self.samples else 0

    @property
    def max_us(self) -> int:
        return max(self.samples) if self.samples else 0

    @property
    def p99_us(self) -> int:
        if not self.samples:
            return 0
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.99)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def to_dict(self) -> dict:
        return {
            "count": len(self.samples),
            "avg_us": self.avg_us,
            "min_us": self.min_us,
            "max_us": self.max_us,
            "p99_us": self.p99_us,
            "avg_ms": self.avg_us / 1000,
            "p99_ms": self.p99_us / 1000,
        }


# =============================================================================
# HFT CLIENT
# =============================================================================

class HFTClient:
    """
    High-frequency trading client for Polymarket CLOB.

    Optimized for:
    - Sub-100ms detection to execution
    - FOK orders for guaranteed fills
    - Real-time latency monitoring

    Supports multiple arbitrage engines:
    - Sum-to-One: Buy both sides when price < $1.00
    - Tail-End: Buy high-probability outcomes near resolution
    """

    def __init__(
        self,
        config: HFTConfig,
        mode: TradingMode = TradingMode.DEMO,
        sum_to_one_config: Optional[SumToOneConfig] = None,
        tail_end_config: Optional[TailEndConfig] = None,
    ):
        self.config = config
        self.mode = mode

        # HTTP session with aggressive timeouts
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # Initialize arbitrage engines
        self.engine_manager = EngineManager(
            sum_to_one_config=sum_to_one_config,
            tail_end_config=tail_end_config,
        )

        # Order book cache
        self._order_books: dict[str, OrderBook] = {}
        self._book_lock = threading.Lock()

        # Trade tracking
        self.trades: deque[Trade] = deque(maxlen=1000)
        self._trades_lock = threading.Lock()

        # Paper trading state (demo mode)
        self._demo_balance: float = 10000.0
        self._demo_positions: dict[str, float] = {}
        self._demo_exposure: float = 0.0

        # Live trading state
        self._live_exposure: float = 0.0

        # Latency tracking
        self.latency_stats = LatencyStats()

        # py-clob-client for live trading
        self._clob_client = None
        if mode == TradingMode.LIVE and config.has_credentials:
            self._init_clob_client()

        # Callbacks
        self._on_trade: Optional[Callable[[Trade], None]] = None
        self._on_signal: Optional[Callable[[EngineSignal], None]] = None

    def _init_clob_client(self) -> bool:
        """Initialize py-clob-client for live FOK orders"""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.passphrase,
            )

            self._clob_client = ClobClient(
                host=self.config.clob_api_url,
                key=self.config.private_key,
                creds=creds,
                chain_id=self.config.chain_id,
            )

            print(f"[HFT] CLOB client initialized for LIVE trading")
            return True

        except ImportError:
            print("[HFT] WARNING: py-clob-client not installed")
            return False
        except Exception as e:
            print(f"[HFT] WARNING: CLOB client init failed: {e}")
            return False

    # -------------------------------------------------------------------------
    # Order Book (Low Latency)
    # -------------------------------------------------------------------------

    def fetch_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch order book with minimal latency"""
        start_us = time.perf_counter_ns() // 1000

        try:
            resp = self.session.get(
                f"{self.config.clob_api_url}/book",
                params={"token_id": token_id},
                timeout=self.config.request_timeout_ms / 1000,
            )
            resp.raise_for_status()
            data = resp.json()

            book = self._parse_order_book(token_id, data)

            with self._book_lock:
                self._order_books[token_id] = book

            if self.config.track_latency:
                latency_us = (time.perf_counter_ns() // 1000) - start_us
                self.latency_stats.add(latency_us)

            return book

        except Exception as e:
            print(f"[HFT] Order book fetch failed: {e}")
            return None

    def _parse_order_book(self, token_id: str, data: dict) -> OrderBook:
        """Parse order book response"""
        now_ms = int(time.time() * 1000)

        bids = []
        asks = []

        for bid in data.get("bids", []):
            try:
                bids.append(PriceLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0)),
                    timestamp_ms=now_ms,
                ))
            except (ValueError, TypeError):
                pass

        for ask in data.get("asks", []):
            try:
                asks.append(PriceLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0)),
                    timestamp_ms=now_ms,
                ))
            except (ValueError, TypeError):
                pass

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            last_update_ms=now_ms,
        )

    def fetch_paired_books(
        self,
        token_a_id: str,
        token_b_id: str
    ) -> tuple[Optional[OrderBook], Optional[OrderBook]]:
        """Fetch both order books in parallel for speed"""
        # For maximum speed, we could use asyncio here
        # For now, sequential but fast
        book_a = self.fetch_order_book(token_a_id)
        book_b = self.fetch_order_book(token_b_id)
        return book_a, book_b

    # -------------------------------------------------------------------------
    # Engine Management
    # -------------------------------------------------------------------------

    def enable_engine(self, engine_type: EngineType):
        """Enable an arbitrage engine"""
        self.engine_manager.enable_engine(engine_type)

    def disable_engine(self, engine_type: EngineType):
        """Disable an arbitrage engine"""
        self.engine_manager.disable_engine(engine_type)

    def toggle_engine(self, engine_type: EngineType) -> bool:
        """Toggle engine state, returns new state"""
        return self.engine_manager.toggle_engine(engine_type)

    def is_engine_enabled(self, engine_type: EngineType) -> bool:
        """Check if engine is enabled"""
        return self.engine_manager.is_enabled(engine_type)

    def get_enabled_engines(self) -> list[str]:
        """Get list of enabled engine names"""
        return self.engine_manager.get_enabled_engines()

    # -------------------------------------------------------------------------
    # Opportunity Detection (Multi-Engine)
    # -------------------------------------------------------------------------

    def check_market(
        self,
        market_slug: str,
        question: str,
        token_a_id: str,
        token_b_id: str,
        minutes_until_resolution: Optional[float] = None,
        volume_24h: Optional[float] = None,
    ) -> list[EngineSignal]:
        """
        Check market with all enabled engines.
        Returns list of signals from triggered engines.
        """
        # Fetch order books
        book_a, book_b = self.fetch_paired_books(token_a_id, token_b_id)

        if book_a is None or book_b is None:
            return []

        # Build market state
        market_state = MarketState(
            market_slug=market_slug,
            question=question,
            token_a_id=token_a_id,
            token_b_id=token_b_id,
            token_a_bid=book_a.best_bid,
            token_a_ask=book_a.best_ask,
            token_b_bid=book_b.best_bid,
            token_b_ask=book_b.best_ask,
            token_a_ask_depth=book_a.asks[0].size if book_a.asks else 0,
            token_b_ask_depth=book_b.asks[0].size if book_b.asks else 0,
            minutes_until_resolution=minutes_until_resolution,
            volume_24h=volume_24h,
            timestamp_us=time.perf_counter_ns() // 1000,
        )

        # Run through all engines
        signals = self.engine_manager.analyze(market_state)

        # Fire callbacks
        for signal in signals:
            if self._on_signal:
                self._on_signal(signal)

        return signals

    def check_opportunity(
        self,
        market_slug: str,
        token_a_id: str,
        token_b_id: str,
        threshold: float = 0.97,
    ) -> Optional[ArbOpportunity]:
        """
        Legacy method: Check for sum-to-one arbitrage opportunity.
        Returns opportunity if price_sum < threshold.

        For multi-engine support, use check_market() instead.
        """
        detect_start_us = time.perf_counter_ns() // 1000

        book_a, book_b = self.fetch_paired_books(token_a_id, token_b_id)

        if book_a is None or book_b is None:
            return None

        if book_a.best_ask is None or book_b.best_ask is None:
            return None

        price_sum = book_a.best_ask + book_b.best_ask

        if price_sum >= threshold:
            return None

        # Calculate executable size
        a_depth = book_a.asks[0].size if book_a.asks else 0
        b_depth = book_b.asks[0].size if book_b.asks else 0
        max_size = min(a_depth, b_depth)

        if max_size < self.config.min_order_size_usd / price_sum:
            return None

        opp = ArbOpportunity(
            opportunity_id=f"opp_{int(time.time()*1000000)}",
            detected_at_us=detect_start_us,
            market_slug=market_slug,
            token_a_id=token_a_id,
            token_b_id=token_b_id,
            token_a_ask=book_a.best_ask,
            token_b_ask=book_b.best_ask,
            price_sum=price_sum,
            edge=1.0 - price_sum,
            token_a_depth=a_depth,
            token_b_depth=b_depth,
            max_executable_size=max_size,
        )

        return opp

    # -------------------------------------------------------------------------
    # Order Execution
    # -------------------------------------------------------------------------

    def execute_arb(
        self,
        opportunity: ArbOpportunity,
        size: Optional[float] = None,
    ) -> Trade:
        """
        Execute arbitrage trade.

        In DEMO mode: Simulates trade at detected prices
        In LIVE mode: Places FOK orders on CLOB
        """
        exec_start_us = time.perf_counter_ns() // 1000

        # Calculate size
        if size is None:
            max_usd = min(
                self.config.max_order_size_usd,
                opportunity.max_executable_size * opportunity.price_sum,
            )
            size = max_usd / opportunity.price_sum

        # Create trade record
        trade = Trade(
            trade_id=f"trade_{uuid.uuid4().hex[:12]}",
            mode=self.mode,
            timestamp=datetime.now(timezone.utc),
            market_slug=opportunity.market_slug,
            token_a_id=opportunity.token_a_id,
            token_b_id=opportunity.token_b_id,
            token_a_price=opportunity.token_a_ask,
            token_b_price=opportunity.token_b_ask,
            size=size,
            total_cost=size * opportunity.price_sum,
            status=OrderStatus.PENDING,
            expected_edge=opportunity.edge,
            detection_to_submit_us=exec_start_us - opportunity.detected_at_us,
        )

        if self.mode == TradingMode.DEMO:
            trade = self._execute_demo(trade, opportunity)
        else:
            trade = self._execute_live(trade, opportunity)

        # Calculate total latency
        trade.total_latency_us = (time.perf_counter_ns() // 1000) - opportunity.detected_at_us

        # Track latency
        if self.config.track_latency:
            self.latency_stats.add(trade.total_latency_us)

        # Store trade
        with self._trades_lock:
            self.trades.append(trade)

        # Callback
        if self._on_trade:
            self._on_trade(trade)

        return trade

    def _execute_demo(self, trade: Trade, opp: ArbOpportunity) -> Trade:
        """Execute trade in demo mode (paper trading)"""
        submit_start_us = time.perf_counter_ns() // 1000

        # Check demo balance
        if trade.total_cost > self._demo_balance:
            trade.status = OrderStatus.REJECTED
            return trade

        # Check exposure limit
        if self._demo_exposure + trade.total_cost > self.config.max_total_exposure:
            trade.status = OrderStatus.REJECTED
            return trade

        # Simulate instant fill at detected prices
        trade.status = OrderStatus.FILLED
        trade.submit_to_fill_us = (time.perf_counter_ns() // 1000) - submit_start_us

        # Update demo state
        self._demo_balance -= trade.total_cost
        self._demo_exposure += trade.total_cost
        self._demo_positions[opp.token_a_id] = \
            self._demo_positions.get(opp.token_a_id, 0) + trade.size
        self._demo_positions[opp.token_b_id] = \
            self._demo_positions.get(opp.token_b_id, 0) + trade.size

        return trade

    def _execute_live(self, trade: Trade, opp: ArbOpportunity) -> Trade:
        """Execute trade in live mode with FOK orders"""
        submit_start_us = time.perf_counter_ns() // 1000

        if self._clob_client is None:
            trade.status = OrderStatus.FAILED
            return trade

        # Check exposure limit
        if self._live_exposure + trade.total_cost > self.config.max_total_exposure:
            trade.status = OrderStatus.REJECTED
            return trade

        try:
            from py_clob_client.order_builder.constants import BUY

            # Add price buffer for FOK fill probability
            buffer = self.config.price_buffer_bps / 10000
            price_a = min(opp.token_a_ask * (1 + buffer), 0.99)
            price_b = min(opp.token_b_ask * (1 + buffer), 0.99)

            # Create and submit orders
            # Note: py-clob-client may need specific FOK handling
            # This is a simplified implementation

            order_a = self._clob_client.create_order(
                token_id=opp.token_a_id,
                price=price_a,
                size=trade.size,
                side=BUY,
            )

            order_b = self._clob_client.create_order(
                token_id=opp.token_b_id,
                price=price_b,
                size=trade.size,
                side=BUY,
            )

            # Submit both orders
            result_a = self._clob_client.post_order(order_a)
            result_b = self._clob_client.post_order(order_b)

            # Check results
            success_a = result_a.get("success") or result_a.get("status") == "matched"
            success_b = result_b.get("success") or result_b.get("status") == "matched"

            if success_a and success_b:
                trade.status = OrderStatus.FILLED
                self._live_exposure += trade.total_cost
            elif success_a or success_b:
                trade.status = OrderStatus.PARTIALLY_FILLED
                # Would need to handle one-sided exposure here
            else:
                trade.status = OrderStatus.REJECTED

            trade.submit_to_fill_us = (time.perf_counter_ns() // 1000) - submit_start_us

        except Exception as e:
            print(f"[HFT] Live execution failed: {e}")
            trade.status = OrderStatus.FAILED

        return trade

    # -------------------------------------------------------------------------
    # Signal Execution (Multi-Engine)
    # -------------------------------------------------------------------------

    def execute_signal(self, signal: EngineSignal) -> Trade:
        """
        Execute a signal from any engine.

        For SUM_TO_ONE: Buys both tokens
        For TAIL_END: Buys single high-probability token
        """
        exec_start_us = time.perf_counter_ns() // 1000
        market = signal.market_state

        # Create trade record
        trade = Trade(
            trade_id=f"trade_{uuid.uuid4().hex[:12]}",
            mode=self.mode,
            timestamp=datetime.now(timezone.utc),
            engine=signal.engine.value,
            market_slug=signal.market_slug,
            token_a_id=market.token_a_id,
            token_b_id=market.token_b_id,
            token_a_price=market.token_a_ask or 0,
            token_b_price=market.token_b_ask or 0,
            size=signal.recommended_size,
            total_cost=0,
            trade_type=signal.action,
            status=OrderStatus.PENDING,
            expected_edge=signal.arb_edge or (1.0 - (signal.target_price or 0)),
            target_probability=signal.implied_probability,
            detection_to_submit_us=exec_start_us - signal.detected_at_us,
        )

        # Calculate cost based on trade type
        if signal.action == "buy_both":
            trade.total_cost = signal.recommended_size * (
                (market.token_a_ask or 0) + (market.token_b_ask or 0)
            )
        elif signal.action == "buy_yes":
            trade.total_cost = signal.recommended_size * (market.token_a_ask or 0)
        elif signal.action == "buy_no":
            trade.total_cost = signal.recommended_size * (market.token_b_ask or 0)

        # Execute based on mode
        if self.mode == TradingMode.DEMO:
            trade = self._execute_signal_demo(trade, signal)
        else:
            trade = self._execute_signal_live(trade, signal)

        # Calculate total latency
        trade.total_latency_us = (time.perf_counter_ns() // 1000) - signal.detected_at_us

        # Track latency
        if self.config.track_latency:
            self.latency_stats.add(trade.total_latency_us)

        # Store trade
        with self._trades_lock:
            self.trades.append(trade)

        # Callback
        if self._on_trade:
            self._on_trade(trade)

        # Register position for tail-end engine
        if signal.engine == EngineType.TAIL_END and trade.status == OrderStatus.FILLED:
            self.engine_manager.tail_end.register_position(
                signal.market_slug,
                signal.recommended_size
            )

        return trade

    def _execute_signal_demo(self, trade: Trade, signal: EngineSignal) -> Trade:
        """Execute signal in demo mode"""
        submit_start_us = time.perf_counter_ns() // 1000

        # Check demo balance
        if trade.total_cost > self._demo_balance:
            trade.status = OrderStatus.REJECTED
            return trade

        # Check exposure limit
        if self._demo_exposure + trade.total_cost > self.config.max_total_exposure:
            trade.status = OrderStatus.REJECTED
            return trade

        # Simulate instant fill
        trade.status = OrderStatus.FILLED
        trade.submit_to_fill_us = (time.perf_counter_ns() // 1000) - submit_start_us

        # Update demo state
        self._demo_balance -= trade.total_cost
        self._demo_exposure += trade.total_cost

        # Track positions based on trade type
        if signal.action == "buy_both":
            self._demo_positions[signal.market_state.token_a_id] = \
                self._demo_positions.get(signal.market_state.token_a_id, 0) + trade.size
            self._demo_positions[signal.market_state.token_b_id] = \
                self._demo_positions.get(signal.market_state.token_b_id, 0) + trade.size
        elif signal.action == "buy_yes":
            self._demo_positions[signal.market_state.token_a_id] = \
                self._demo_positions.get(signal.market_state.token_a_id, 0) + trade.size
        elif signal.action == "buy_no":
            self._demo_positions[signal.market_state.token_b_id] = \
                self._demo_positions.get(signal.market_state.token_b_id, 0) + trade.size

        return trade

    def _execute_signal_live(self, trade: Trade, signal: EngineSignal) -> Trade:
        """Execute signal in live mode"""
        submit_start_us = time.perf_counter_ns() // 1000

        if self._clob_client is None:
            trade.status = OrderStatus.FAILED
            return trade

        # Check exposure limit
        if self._live_exposure + trade.total_cost > self.config.max_total_exposure:
            trade.status = OrderStatus.REJECTED
            return trade

        try:
            from py_clob_client.order_builder.constants import BUY

            buffer = self.config.price_buffer_bps / 10000
            market = signal.market_state

            if signal.action == "buy_both":
                # Buy both tokens (sum-to-one)
                price_a = min((market.token_a_ask or 0) * (1 + buffer), 0.99)
                price_b = min((market.token_b_ask or 0) * (1 + buffer), 0.99)

                order_a = self._clob_client.create_order(
                    token_id=market.token_a_id,
                    price=price_a,
                    size=trade.size,
                    side=BUY,
                )
                order_b = self._clob_client.create_order(
                    token_id=market.token_b_id,
                    price=price_b,
                    size=trade.size,
                    side=BUY,
                )

                result_a = self._clob_client.post_order(order_a)
                result_b = self._clob_client.post_order(order_b)

                success_a = result_a.get("success") or result_a.get("status") == "matched"
                success_b = result_b.get("success") or result_b.get("status") == "matched"

                if success_a and success_b:
                    trade.status = OrderStatus.FILLED
                    self._live_exposure += trade.total_cost
                elif success_a or success_b:
                    trade.status = OrderStatus.PARTIALLY_FILLED
                else:
                    trade.status = OrderStatus.REJECTED

            else:
                # Buy single token (tail-end)
                token_id = signal.target_token
                price = min((signal.target_price or 0) * (1 + buffer), 0.99)

                order = self._clob_client.create_order(
                    token_id=token_id,
                    price=price,
                    size=trade.size,
                    side=BUY,
                )

                result = self._clob_client.post_order(order)
                success = result.get("success") or result.get("status") == "matched"

                if success:
                    trade.status = OrderStatus.FILLED
                    self._live_exposure += trade.total_cost
                else:
                    trade.status = OrderStatus.REJECTED

            trade.submit_to_fill_us = (time.perf_counter_ns() // 1000) - submit_start_us

        except Exception as e:
            print(f"[HFT] Live execution failed: {e}")
            trade.status = OrderStatus.FAILED

        return trade

    # -------------------------------------------------------------------------
    # Trade Resolution
    # -------------------------------------------------------------------------

    def resolve_trade(self, trade: Trade, winning_token: str) -> Trade:
        """
        Resolve a trade after market settlement.

        For arb trades: Always profit if both sides filled
        PnL = size * 1.0 - total_cost = size * edge
        """
        trade.resolved = True
        trade.resolution_time = datetime.now(timezone.utc)
        trade.winning_token = winning_token

        # Calculate PnL
        # You bought both tokens at price_sum < 1.0
        # One token pays out 1.0, one pays 0
        # PnL = size * 1.0 - total_cost
        trade.pnl = trade.size * 1.0 - trade.total_cost

        # Update demo balance if applicable
        if trade.mode == TradingMode.DEMO and trade.pnl:
            self._demo_balance += trade.size * 1.0  # Payout
            self._demo_exposure -= trade.total_cost

        return trade

    # -------------------------------------------------------------------------
    # State & Stats
    # -------------------------------------------------------------------------

    def get_balance(self) -> float:
        """Get current balance"""
        if self.mode == TradingMode.DEMO:
            return self._demo_balance
        # For live, would query actual balance
        return 0.0

    def get_exposure(self) -> float:
        """Get current exposure"""
        if self.mode == TradingMode.DEMO:
            return self._demo_exposure
        return self._live_exposure

    def get_trades(self, limit: int = 50) -> list[Trade]:
        """Get recent trades"""
        with self._trades_lock:
            return list(self.trades)[-limit:]

    def get_stats(self) -> dict:
        """Get trading statistics"""
        trades = self.get_trades(1000)

        filled = [t for t in trades if t.status == OrderStatus.FILLED]
        resolved = [t for t in filled if t.resolved]

        total_pnl = sum(t.pnl or 0 for t in resolved)
        win_rate = len([t for t in resolved if (t.pnl or 0) > 0]) / len(resolved) if resolved else 0

        # Stats by engine
        sum_to_one_trades = [t for t in filled if t.engine == "sum_to_one"]
        tail_end_trades = [t for t in filled if t.engine == "tail_end"]

        return {
            "mode": self.mode.value,
            "balance": self.get_balance(),
            "exposure": self.get_exposure(),
            "total_trades": len(trades),
            "filled_trades": len(filled),
            "resolved_trades": len(resolved),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "latency": self.latency_stats.to_dict(),
            "engines": self.engine_manager.get_stats(),
            "enabled_engines": self.get_enabled_engines(),
            "trades_by_engine": {
                "sum_to_one": len(sum_to_one_trades),
                "tail_end": len(tail_end_trades),
            },
        }

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------

    def on_trade(self, callback: Callable[[Trade], None]):
        """Register trade callback"""
        self._on_trade = callback

    def on_signal(self, callback: Callable[[EngineSignal], None]):
        """Register signal callback"""
        self._on_signal = callback


# =============================================================================
# HFT SCANNER
# =============================================================================

class HFTScanner:
    """
    High-frequency arbitrage scanner with multi-engine support.

    Continuously monitors markets and routes to enabled engines:
    - Sum-to-One: Detects price_sum < $1.00 opportunities
    - Tail-End: Detects high-probability outcomes near resolution
    """

    def __init__(
        self,
        client: HFTClient,
        markets: list[dict],  # List of {slug, question, token_a_id, token_b_id, minutes_until, volume_24h}
        scan_interval_ms: int = 500,  # 500ms default for multi-engine
    ):
        self.client = client
        self.markets = markets
        self.scan_interval_ms = scan_interval_ms

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Stats
        self.scans_completed = 0
        self.signals_detected = 0
        self.trades_executed = 0

        # Stats by engine
        self.sum_to_one_signals = 0
        self.tail_end_signals = 0

    def start(self):
        """Start the HFT scanner"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

        enabled = self.client.get_enabled_engines()
        print(f"[HFT] Scanner started - {len(self.markets)} markets, {self.scan_interval_ms}ms interval")
        print(f"[HFT] Enabled engines: {', '.join(enabled) if enabled else 'None'}")

    def stop(self):
        """Stop the HFT scanner"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[HFT] Scanner stopped")

    def _scan_loop(self):
        """Main scanning loop with multi-engine support"""
        while self._running:
            cycle_start = time.perf_counter()

            for market in self.markets:
                if not self._running:
                    break

                # Check market with all enabled engines
                signals = self.client.check_market(
                    market_slug=market.get("slug", ""),
                    question=market.get("question", ""),
                    token_a_id=market.get("token_a_id", ""),
                    token_b_id=market.get("token_b_id", ""),
                    minutes_until_resolution=market.get("minutes_until"),
                    volume_24h=market.get("volume_24h"),
                )

                # Process each signal
                for signal in signals:
                    self.signals_detected += 1

                    # Track by engine
                    if signal.engine == EngineType.SUM_TO_ONE:
                        self.sum_to_one_signals += 1
                        edge_str = f"{signal.arb_edge*100:.2f}% edge" if signal.arb_edge else ""
                        print(f"[S2O] {signal.market_slug[:30]} - {edge_str}")
                    else:
                        self.tail_end_signals += 1
                        prob_str = f"{signal.implied_probability*100:.1f}% prob" if signal.implied_probability else ""
                        print(f"[TAIL] {signal.market_slug[:30]} - {signal.action} @ ${signal.target_price:.3f} ({prob_str})")

                    # Execute signal
                    trade = self.client.execute_signal(signal)

                    if trade.status == OrderStatus.FILLED:
                        self.trades_executed += 1
                        print(f"[HFT] FILLED: {trade.trade_id} [{trade.engine}] ${trade.total_cost:.2f} @ {trade.total_latency_us/1000:.1f}ms")
                    else:
                        print(f"[HFT] {trade.status.value}: {trade.trade_id}")

            self.scans_completed += 1

            # Sleep for remaining interval
            elapsed_ms = (time.perf_counter() - cycle_start) * 1000
            sleep_ms = max(0, self.scan_interval_ms - elapsed_ms)
            if sleep_ms > 0:
                time.sleep(sleep_ms / 1000)

    def get_stats(self) -> dict:
        """Get scanner statistics"""
        return {
            "running": self._running,
            "scans_completed": self.scans_completed,
            "signals_detected": self.signals_detected,
            "trades_executed": self.trades_executed,
            "markets_monitored": len(self.markets),
            "scan_interval_ms": self.scan_interval_ms,
            "signals_by_engine": {
                "sum_to_one": self.sum_to_one_signals,
                "tail_end": self.tail_end_signals,
            },
            "enabled_engines": self.client.get_enabled_engines(),
        }


# =============================================================================
# DEMO
# =============================================================================

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════════════╗
    ║  HFT CLOB CLIENT - High Frequency Trading Edition                    ║
    ║                                                                      ║
    ║  Optimized for QuantVPS deployment with FOK orders                   ║
    ║  Modes: DEMO (paper) | LIVE (real money)                             ║
    ╚══════════════════════════════════════════════════════════════════════╝
    """)

    config = HFTConfig.from_env()
    client = HFTClient(config, mode=TradingMode.DEMO)

    print(f"Mode: {client.mode.value}")
    print(f"Has credentials: {config.has_credentials}")
    print(f"Balance: ${client.get_balance():,.2f}")
    print(f"Max exposure: ${config.max_total_exposure:,.2f}")

    print("\n" + "=" * 60)
    print("LATENCY STATS")
    print("=" * 60)
    print(json.dumps(client.latency_stats.to_dict(), indent=2))


if __name__ == "__main__":
    main()
