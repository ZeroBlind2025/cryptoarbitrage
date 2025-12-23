"""
POLYMARKET CLOB CLIENT - Research Edition
==========================================

Step D: Central Limit Order Book integration.

This is where "indicative" becomes "executable":
- Gamma marks → CLOB best ask (real cost to buy)
- Simulated trades → Signed orders on Polygon

CLOB gives you:
1. Real order book depth (not just last price)
2. Best bid/ask for each token
3. Ability to place/cancel orders
4. Your position and balance info

Prerequisites:
    pip install py-clob-client python-dotenv requests

Credentials needed (.env file):
    POLYMARKET_API_KEY=xxx
    POLYMARKET_API_SECRET=xxx
    POLYMARKET_PASSPHRASE=xxx
    POLYGON_PRIVATE_KEY=0x...
"""

import os
import json
import time
import hmac
import hashlib
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal
from enum import Enum
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class CLOBConfig:
    """CLOB client configuration"""
    
    # Endpoints
    clob_api_url: str = "https://clob.polymarket.com"
    
    # Authentication
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    private_key: str = ""  # Polygon wallet private key
    
    # Chain
    chain_id: int = 137  # Polygon mainnet
    
    # Trading limits
    max_order_size_usd: float = 50.0
    min_order_size_usd: float = 1.0
    
    # Timeouts
    request_timeout: int = 10
    order_timeout: int = 30  # How long to wait for fill
    
    # Safety
    require_confirmation: bool = True  # Prompt before live orders
    
    @classmethod
    def from_env(cls) -> "CLOBConfig":
        """Load config from environment variables"""
        return cls(
            api_key=os.getenv("POLYMARKET_API_KEY", ""),
            api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
            passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
            private_key=os.getenv("POLYGON_PRIVATE_KEY", ""),
        )
    
    @property
    def has_credentials(self) -> bool:
        """Check if all credentials are present"""
        return all([
            self.api_key,
            self.api_secret,
            self.passphrase,
            self.private_key,
        ])


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class OrderBookLevel:
    """Single price level in order book"""
    price: float
    size: float  # Quantity available at this price


@dataclass
class OrderBook:
    """Order book for a single token"""
    token_id: str
    timestamp: datetime
    
    # Bids (buy orders) - sorted high to low
    bids: list[OrderBookLevel] = field(default_factory=list)
    
    # Asks (sell orders) - sorted low to high
    asks: list[OrderBookLevel] = field(default_factory=list)
    
    @property
    def best_bid(self) -> Optional[float]:
        """Highest price someone will pay"""
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        """Lowest price someone will sell at - THIS IS YOUR COST TO BUY"""
        return self.asks[0].price if self.asks else None
    
    @property
    def mid_price(self) -> Optional[float]:
        """Midpoint between best bid and ask"""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def bid_ask_spread(self) -> Optional[float]:
        """Spread between best bid and ask"""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    def depth_at_price(self, side: Literal["bid", "ask"], max_price: float) -> float:
        """Total size available up to a price level"""
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        
        for level in levels:
            if side == "bid" and level.price < max_price:
                break
            if side == "ask" and level.price > max_price:
                break
            total += level.size
        
        return total
    
    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "timestamp": self.timestamp.isoformat(),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "spread": self.bid_ask_spread,
            "bid_depth": sum(b.size for b in self.bids),
            "ask_depth": sum(a.size for a in self.asks),
            "bid_levels": len(self.bids),
            "ask_levels": len(self.asks),
        }


@dataclass
class PairedOrderBook:
    """Order books for both sides of a binary market"""
    token_a_book: OrderBook
    token_b_book: OrderBook
    
    @property
    def executable_price_sum(self) -> Optional[float]:
        """
        REAL cost to lock arb: best_ask_a + best_ask_b
        
        This is what you actually pay, not the indicative Gamma mark.
        """
        if self.token_a_book.best_ask and self.token_b_book.best_ask:
            return self.token_a_book.best_ask + self.token_b_book.best_ask
        return None
    
    @property
    def executable_edge(self) -> Optional[float]:
        """Real edge based on executable prices"""
        if self.executable_price_sum:
            return 1.0 - self.executable_price_sum
        return None
    
    def executable_net_profit(self, total_friction: float) -> Optional[float]:
        """Net profit after friction, using real executable prices"""
        if self.executable_edge:
            return max(self.executable_edge - total_friction, 0.0)
        return None
    
    def max_executable_size(self) -> float:
        """
        Maximum size you can execute at current best asks.
        Limited by the smaller of the two order book depths.
        """
        if not self.token_a_book.asks or not self.token_b_book.asks:
            return 0.0
        
        # Size available at best ask
        a_size = self.token_a_book.asks[0].size
        b_size = self.token_b_book.asks[0].size
        
        return min(a_size, b_size)
    
    def to_dict(self) -> dict:
        return {
            "token_a": self.token_a_book.to_dict(),
            "token_b": self.token_b_book.to_dict(),
            "executable_price_sum": self.executable_price_sum,
            "executable_edge": self.executable_edge,
            "max_executable_size": self.max_executable_size(),
        }


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    """An order to place or track"""
    token_id: str
    side: OrderSide
    price: float
    size: float
    
    # Filled after submission
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: Optional[float] = None
    
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "side": self.side.value,
            "price": self.price,
            "size": self.size,
            "order_id": self.order_id,
            "status": self.status.value,
            "filled_size": self.filled_size,
            "avg_fill_price": self.avg_fill_price,
        }


@dataclass
class ArbExecution:
    """Record of an arb execution attempt"""
    execution_id: str
    timestamp: datetime
    
    # Market info
    market_slug: str
    token_a_id: str
    token_b_id: str
    
    # Prices at execution time
    token_a_ask: float
    token_b_ask: float
    executable_price_sum: float
    expected_edge: float
    
    # Orders
    order_a: Order
    order_b: Order
    
    # Result
    success: bool = False
    both_filled: bool = False
    total_cost: float = 0.0
    actual_edge: Optional[float] = None
    notes: str = ""
    
    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "timestamp": self.timestamp.isoformat(),
            "market_slug": self.market_slug,
            "token_a_ask": self.token_a_ask,
            "token_b_ask": self.token_b_ask,
            "executable_price_sum": self.executable_price_sum,
            "expected_edge": self.expected_edge,
            "order_a": self.order_a.to_dict(),
            "order_b": self.order_b.to_dict(),
            "success": self.success,
            "both_filled": self.both_filled,
            "total_cost": self.total_cost,
            "actual_edge": self.actual_edge,
            "notes": self.notes,
        }


# =============================================================================
# CLOB API CLIENT
# =============================================================================

class CLOBClient:
    """
    Client for Polymarket CLOB API.
    
    Handles authentication, order book fetching, and order placement.
    Paper trading mode works without credentials.
    """
    
    def __init__(self, config: CLOBConfig, paper_mode: bool = True):
        self.config = config
        self.paper_mode = paper_mode
        self.base_url = config.clob_api_url.rstrip("/")
        
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        
        # Track paper positions
        self._paper_positions: dict[str, float] = {}
        self._paper_balance: float = 10000.0  # Simulated USDC
        
        # py-clob-client integration (optional)
        self._clob_client = None
        
        if not paper_mode and config.has_credentials:
            self._init_clob_client()
    
    def _init_clob_client(self) -> bool:
        """Initialize py-clob-client for live trading"""
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
            
            print("✓ CLOB client initialized for live trading")
            return True
            
        except ImportError:
            print("⚠ py-clob-client not installed - paper trading only")
            print("  Install with: pip install py-clob-client")
            return False
        except Exception as e:
            print(f"⚠ CLOB client init failed: {e}")
            return False
    
    # -------------------------------------------------------------------------
    # Order Book (works without auth)
    # -------------------------------------------------------------------------
    
    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """
        Fetch order book for a token.
        
        This is READ-ONLY and doesn't require authentication.
        """
        try:
            resp = self.session.get(
                f"{self.base_url}/book",
                params={"token_id": token_id},
                timeout=self.config.request_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            
            return self._parse_order_book(token_id, data)
            
        except requests.RequestException as e:
            print(f"Order book fetch failed: {e}")
            return None
    
    def _parse_order_book(self, token_id: str, data: dict) -> OrderBook:
        """Parse CLOB order book response"""
        
        bids = []
        asks = []
        
        for bid in data.get("bids", []):
            try:
                bids.append(OrderBookLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0)),
                ))
            except (ValueError, TypeError):
                pass
        
        for ask in data.get("asks", []):
            try:
                asks.append(OrderBookLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0)),
                ))
            except (ValueError, TypeError):
                pass
        
        # Sort: bids high→low, asks low→high
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        
        return OrderBook(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=bids,
            asks=asks,
        )
    
    def get_paired_order_book(
        self, 
        token_a_id: str, 
        token_b_id: str
    ) -> Optional[PairedOrderBook]:
        """
        Fetch order books for both tokens in a binary market.
        
        This gives you the REAL executable prices, not Gamma marks.
        """
        book_a = self.get_order_book(token_a_id)
        book_b = self.get_order_book(token_b_id)
        
        if book_a is None or book_b is None:
            return None
        
        return PairedOrderBook(
            token_a_book=book_a,
            token_b_book=book_b,
        )
    
    # -------------------------------------------------------------------------
    # Order Placement
    # -------------------------------------------------------------------------
    
    def place_order(self, order: Order) -> Order:
        """
        Place a single order.
        
        In paper mode: simulates fill at requested price
        In live mode: submits to CLOB
        """
        order.created_at = datetime.now(timezone.utc)
        
        if self.paper_mode:
            return self._paper_fill(order)
        else:
            return self._live_place_order(order)
    
    def _paper_fill(self, order: Order) -> Order:
        """Simulate order fill for paper trading"""
        
        # Check balance
        cost = order.price * order.size
        if order.side == OrderSide.BUY and cost > self._paper_balance:
            order.status = OrderStatus.FAILED
            order.notes = f"Insufficient balance: need ${cost:.2f}, have ${self._paper_balance:.2f}"
            return order
        
        # Simulate instant fill at requested price
        order.order_id = f"paper_{int(time.time()*1000)}"
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.avg_fill_price = order.price
        order.updated_at = datetime.now(timezone.utc)
        
        # Update paper state
        if order.side == OrderSide.BUY:
            self._paper_balance -= cost
            self._paper_positions[order.token_id] = \
                self._paper_positions.get(order.token_id, 0) + order.size
        else:
            self._paper_balance += cost
            self._paper_positions[order.token_id] = \
                self._paper_positions.get(order.token_id, 0) - order.size
        
        return order
    
    def _live_place_order(self, order: Order) -> Order:
        """Place order via CLOB API"""
        
        if self._clob_client is None:
            order.status = OrderStatus.FAILED
            order.notes = "CLOB client not initialized"
            return order
        
        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            
            side = BUY if order.side == OrderSide.BUY else SELL
            
            # Create and sign order
            clob_order = self._clob_client.create_order(
                token_id=order.token_id,
                price=order.price,
                size=order.size,
                side=side,
            )
            
            # Submit
            result = self._clob_client.post_order(clob_order)
            
            # Parse result
            order.order_id = result.get("orderID") or result.get("id")
            
            if result.get("success") or result.get("status") == "matched":
                order.status = OrderStatus.FILLED
                order.filled_size = order.size
                order.avg_fill_price = order.price
            else:
                order.status = OrderStatus.OPEN
            
            order.updated_at = datetime.now(timezone.utc)
            return order
            
        except Exception as e:
            order.status = OrderStatus.FAILED
            order.notes = str(e)
            return order
    
    # -------------------------------------------------------------------------
    # Arb Execution
    # -------------------------------------------------------------------------
    
    def execute_arb(
        self,
        market_slug: str,
        token_a_id: str,
        token_b_id: str,
        size: float,
        max_price_sum: float,
    ) -> ArbExecution:
        """
        Execute arbitrage: buy both tokens.
        
        CRITICAL: This tries to fill both sides. If one fills and the other
        doesn't, you have directional exposure.
        
        Strategy used here:
        1. Fetch fresh order books
        2. Verify price_sum still below threshold
        3. Place both orders at best ask + small buffer
        4. Track results
        
        More sophisticated strategies:
        - Check depth before sizing
        - Use FOK (fill-or-kill) orders if available
        - Place as maker to save fees (but risk not filling)
        """
        
        execution_id = f"arb_{int(time.time()*1000)}"
        timestamp = datetime.now(timezone.utc)
        
        # Get fresh order books
        paired_book = self.get_paired_order_book(token_a_id, token_b_id)
        
        if paired_book is None:
            return ArbExecution(
                execution_id=execution_id,
                timestamp=timestamp,
                market_slug=market_slug,
                token_a_id=token_a_id,
                token_b_id=token_b_id,
                token_a_ask=0,
                token_b_ask=0,
                executable_price_sum=0,
                expected_edge=0,
                order_a=Order(token_a_id, OrderSide.BUY, 0, size),
                order_b=Order(token_b_id, OrderSide.BUY, 0, size),
                success=False,
                notes="Failed to fetch order books",
            )
        
        token_a_ask = paired_book.token_a_book.best_ask or 0
        token_b_ask = paired_book.token_b_book.best_ask or 0
        price_sum = paired_book.executable_price_sum or 999
        
        # Verify still profitable
        if price_sum >= max_price_sum:
            return ArbExecution(
                execution_id=execution_id,
                timestamp=timestamp,
                market_slug=market_slug,
                token_a_id=token_a_id,
                token_b_id=token_b_id,
                token_a_ask=token_a_ask,
                token_b_ask=token_b_ask,
                executable_price_sum=price_sum,
                expected_edge=1.0 - price_sum,
                order_a=Order(token_a_id, OrderSide.BUY, token_a_ask, size),
                order_b=Order(token_b_id, OrderSide.BUY, token_b_ask, size),
                success=False,
                notes=f"Price moved: sum={price_sum:.4f} >= max={max_price_sum:.4f}",
            )
        
        # Check available depth
        max_size = paired_book.max_executable_size()
        actual_size = min(size, max_size)
        
        if actual_size < self.config.min_order_size_usd / price_sum:
            return ArbExecution(
                execution_id=execution_id,
                timestamp=timestamp,
                market_slug=market_slug,
                token_a_id=token_a_id,
                token_b_id=token_b_id,
                token_a_ask=token_a_ask,
                token_b_ask=token_b_ask,
                executable_price_sum=price_sum,
                expected_edge=1.0 - price_sum,
                order_a=Order(token_a_id, OrderSide.BUY, token_a_ask, actual_size),
                order_b=Order(token_b_id, OrderSide.BUY, token_b_ask, actual_size),
                success=False,
                notes=f"Insufficient depth: {max_size:.2f} available, need {size:.2f}",
            )
        
        # Add small buffer to price to improve fill probability
        # Crossing the spread slightly to act as taker
        price_buffer = 0.001  # 0.1%
        order_a = Order(
            token_id=token_a_id,
            side=OrderSide.BUY,
            price=min(token_a_ask * (1 + price_buffer), 0.99),  # Cap at 99¢
            size=actual_size,
        )
        order_b = Order(
            token_id=token_b_id,
            side=OrderSide.BUY,
            price=min(token_b_ask * (1 + price_buffer), 0.99),
            size=actual_size,
        )
        
        # Execute both orders
        # NOTE: In production, you'd want more sophisticated handling:
        # - Atomic execution if possible
        # - Cancel second if first fails
        # - Handle partial fills
        
        filled_a = self.place_order(order_a)
        filled_b = self.place_order(order_b)
        
        both_filled = (
            filled_a.status == OrderStatus.FILLED and
            filled_b.status == OrderStatus.FILLED
        )
        
        total_cost = 0.0
        if filled_a.status == OrderStatus.FILLED:
            total_cost += filled_a.avg_fill_price * filled_a.filled_size
        if filled_b.status == OrderStatus.FILLED:
            total_cost += filled_b.avg_fill_price * filled_b.filled_size
        
        actual_edge = None
        if both_filled and filled_a.filled_size > 0:
            actual_price_sum = (
                (filled_a.avg_fill_price or 0) + 
                (filled_b.avg_fill_price or 0)
            )
            actual_edge = 1.0 - actual_price_sum
        
        return ArbExecution(
            execution_id=execution_id,
            timestamp=timestamp,
            market_slug=market_slug,
            token_a_id=token_a_id,
            token_b_id=token_b_id,
            token_a_ask=token_a_ask,
            token_b_ask=token_b_ask,
            executable_price_sum=price_sum,
            expected_edge=1.0 - price_sum,
            order_a=filled_a,
            order_b=filled_b,
            success=both_filled,
            both_filled=both_filled,
            total_cost=total_cost,
            actual_edge=actual_edge,
            notes="" if both_filled else f"a={filled_a.status.value}, b={filled_b.status.value}",
        )
    
    # -------------------------------------------------------------------------
    # Account Info
    # -------------------------------------------------------------------------
    
    def get_balance(self) -> float:
        """Get USDC balance"""
        if self.paper_mode:
            return self._paper_balance
        
        if self._clob_client is None:
            return 0.0
        
        try:
            # TODO: Implement actual balance fetch
            # balance = self._clob_client.get_balance()
            return 0.0
        except Exception:
            return 0.0
    
    def get_positions(self) -> dict[str, float]:
        """Get current positions (token_id -> size)"""
        if self.paper_mode:
            return self._paper_positions.copy()
        
        if self._clob_client is None:
            return {}
        
        try:
            # TODO: Implement actual position fetch
            return {}
        except Exception:
            return {}


# =============================================================================
# COMPARISON: GAMMA vs CLOB
# =============================================================================

def compare_gamma_vs_clob(
    gamma_price_a: float,
    gamma_price_b: float,
    clob_client: CLOBClient,
    token_a_id: str,
    token_b_id: str,
) -> dict:
    """
    Compare indicative Gamma prices vs executable CLOB prices.
    
    This is the key insight for your white paper:
    Gamma marks are NOT what you pay. CLOB best ask is.
    """
    
    paired_book = clob_client.get_paired_order_book(token_a_id, token_b_id)
    
    if paired_book is None:
        return {"error": "Could not fetch CLOB data"}
    
    gamma_sum = gamma_price_a + gamma_price_b
    clob_sum = paired_book.executable_price_sum or 0
    
    return {
        "gamma": {
            "token_a": gamma_price_a,
            "token_b": gamma_price_b,
            "sum": gamma_sum,
            "edge": 1.0 - gamma_sum,
        },
        "clob": {
            "token_a_best_ask": paired_book.token_a_book.best_ask,
            "token_b_best_ask": paired_book.token_b_book.best_ask,
            "sum": clob_sum,
            "edge": 1.0 - clob_sum if clob_sum else None,
        },
        "difference": {
            "sum_diff": clob_sum - gamma_sum if clob_sum else None,
            "sum_diff_bps": int((clob_sum - gamma_sum) * 10000) if clob_sum else None,
            "gamma_optimistic": clob_sum > gamma_sum if clob_sum else None,
        },
        "depth": {
            "token_a_ask_depth": paired_book.token_a_book.asks[0].size if paired_book.token_a_book.asks else 0,
            "token_b_ask_depth": paired_book.token_b_book.asks[0].size if paired_book.token_b_book.asks else 0,
            "max_executable": paired_book.max_executable_size(),
        },
    }


# =============================================================================
# DEMO / TEST
# =============================================================================

def main():
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║  POLYMARKET CLOB CLIENT - Research Edition                           ║
║                                                                      ║
║  This shows REAL executable prices vs indicative Gamma marks.        ║
║  Paper trading mode - no real money at risk.                         ║
╚══════════════════════════════════════════════════════════════════════╝
    """)
    
    # Initialize in paper mode
    config = CLOBConfig.from_env()
    client = CLOBClient(config, paper_mode=True)
    
    print(f"Paper mode: {client.paper_mode}")
    print(f"Has credentials: {config.has_credentials}")
    print(f"Paper balance: ${client.get_balance():,.2f}")
    
    print("\n" + "="*60)
    print("To use with real CLOB data, run on your local machine")
    print("with network access to clob.polymarket.com")
    print("="*60)
    
    # Demo paper trade
    print("\n" + "="*60)
    print("PAPER TRADE DEMO")
    print("="*60)
    
    # Simulate an arb execution
    demo_execution = client.execute_arb(
        market_slug="demo-btc-100k",
        token_a_id="0xdemo_yes_token",
        token_b_id="0xdemo_no_token",
        size=100,  # 100 shares
        max_price_sum=0.97,
    )
    
    # In paper mode without real order books, this will fail
    # But it shows the structure
    print(f"\nExecution ID: {demo_execution.execution_id}")
    print(f"Success: {demo_execution.success}")
    print(f"Notes: {demo_execution.notes}")
    
    print("\n" + "="*60)
    print("NEXT STEPS")
    print("="*60)
    print("""
1. Run this on a machine with network access to clob.polymarket.com
2. Use get_paired_order_book() to see real executable prices
3. Compare Gamma marks vs CLOB best asks
4. When ready for live trading:
   - Set up .env with credentials
   - Use paper_mode=False
   - Start with tiny positions ($1-5)
""")


if __name__ == "__main__":
    main()
