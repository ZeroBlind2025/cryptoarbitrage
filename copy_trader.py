#!/usr/bin/env python3
"""
POLY ALGO - Algorithmic trader following a specific trader's crypto bets
========================================================================

Monitors a target trader's activity and copies their trades on crypto markets
(15, 30, and 60 minute timeframes).

Target: 0x1979ae6b7e6534de9c4539d0c205e582ca637c9d

Usage:
    python copy_trader.py              # Dry run mode (preview only)
    python copy_trader.py --live       # Live trading mode
    python copy_trader.py --loop       # Continuous monitoring
    python copy_trader.py --live --loop  # Live + continuous
"""

import os
import sys
import time
import json
import argparse
import requests
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    print("[ALGO] Warning: py_clob_client not installed. Install with: pip install py-clob-client")

try:
    from clob_ws import CLOBWebSocket
    HAS_CLOB_WS = True
except ImportError:
    HAS_CLOB_WS = False


# =============================================================================
# CONFIGURATION
# =============================================================================

# Target trader to copy
TARGET_ADDRESS = "0x1979ae6b7e6534de9c4539d0c205e582ca637c9d"

# Your credentials (from environment)
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", os.getenv("POLYGON_ADDRESS", ""))
PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))  # 0=EOA, 1=Email/Magic, 2=Browser

# Trading settings
BET_AMOUNT = float(os.getenv("COPY_BET_AMOUNT", "2.0"))  # $ per copied bet
POLL_INTERVAL = int(os.getenv("COPY_POLL_INTERVAL", "10"))  # seconds between checks
ALGO_STARTING_BALANCE = float(os.getenv("ALGO_STARTING_BALANCE", "2300.0"))  # Starting balance for Poly Algo
PRICE_BUFFER_BPS = int(os.getenv("COPY_PRICE_BUFFER_BPS", "50"))  # Max overbid vs target's price (50 bps = 0.5%)

# Per-coin lot sizes ($ per copied bet, per coin)
# Override via env: COIN_BET_BTC=1.0  COIN_BET_ETH=2.0  COIN_BET_SOL=1.0
COIN_BET_AMOUNTS = {
    "btc": float(os.getenv("COIN_BET_BTC", str(BET_AMOUNT))),
    "eth": float(os.getenv("COIN_BET_ETH", str(BET_AMOUNT))),
    "sol": float(os.getenv("COIN_BET_SOL", str(BET_AMOUNT))),
    "xrp": float(os.getenv("COIN_BET_XRP", str(BET_AMOUNT))),
}

# Crypto market filter - only copy trades on these markets
CRYPTO_SLUGS = ["btc-", "eth-", "sol-", "xrp-", "-updown-"]


def detect_coin(slug: str, title: str) -> str:
    """Detect which coin a trade is for from slug/title. Returns 'btc', 'eth', 'sol', 'xrp', or ''."""
    s = (slug + " " + title).lower()
    if "btc-" in s or "bitcoin" in s:
        return "btc"
    if "eth-" in s or "ethereum" in s:
        return "eth"
    if "sol-" in s or "solana" in s:
        return "sol"
    if "xrp-" in s or "xrp" in s or "ripple" in s:
        return "xrp"
    return ""

# API endpoints
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
PROFILE_API = "https://gamma-api.polymarket.com"

# Position tracking file
POSITIONS_FILE = Path(__file__).parent / "copy_positions.json"


# =============================================================================
# POSITION TRACKING
# =============================================================================

def load_positions() -> dict:
    """Load positions from file"""
    if POSITIONS_FILE.exists():
        try:
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            # Migrate: add balance fields if missing
            stats = data.get("stats", {})
            if "balance" not in stats:
                stats["balance"] = ALGO_STARTING_BALANCE
            if "balance_history" not in stats:
                open_staked = sum(p.get("amount", 0) for p in data.get("open", []))
                stats["balance_history"] = [
                    {"timestamp": datetime.now(timezone.utc).isoformat(), "balance": stats["balance"],
                     "pnl": stats.get("total_pnl", 0.0), "equity": stats["balance"] + open_staked, "event": "init"}
                ]
            data["stats"] = stats
            return data
        except Exception as e:
            print(f"[ALGO] Error loading positions: {e}")
    return {
        "open": [], "resolved": [],
        "stats": {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "balance": ALGO_STARTING_BALANCE,
            "balance_history": [
                {"timestamp": datetime.now(timezone.utc).isoformat(), "balance": ALGO_STARTING_BALANCE,
                 "pnl": 0.0, "equity": ALGO_STARTING_BALANCE, "event": "init"}
            ],
        }
    }


def save_positions(positions: dict):
    """Save positions to file"""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"[ALGO] Error saving positions: {e}")


def check_clob_market_resolution(condition_id: str, token_id: str = "") -> Optional[dict]:
    """Query CLOB API for definitive market resolution.

    GET /markets/{condition_id} returns tokens with a 'winner' boolean.
    This is the most reliable source — it's Polymarket's own resolution record.
    """
    if not condition_id:
        return None
    try:
        response = requests.get(
            f"{CLOB_API}/markets/{condition_id}",
            timeout=10
        )
        if response.status_code != 200:
            return None

        market = response.json()

        # Market must be closed to be resolved
        if not market.get("closed"):
            return None

        tokens = market.get("tokens", [])
        if not tokens:
            return None

        # Find the winning token
        winning_token = None
        for t in tokens:
            if t.get("winner") is True:
                winning_token = t
                break

        if winning_token is None:
            # Market is closed but no winner flag set yet
            return None

        # Determine if OUR token won
        winning_token_id = str(winning_token.get("token_id", ""))
        our_token_won = (str(token_id) == winning_token_id) if token_id else None

        print(f"[ALGO] CLOB resolution: winner={winning_token.get('outcome')} "
              f"(token={winning_token_id[:20]}...), our_token_won={our_token_won}")

        return {
            "resolved": True,
            "our_token_won": our_token_won,
            "winning_outcome": winning_token.get("outcome"),
            "winning_token_id": winning_token_id,
        }

    except Exception as e:
        print(f"[ALGO] CLOB market resolution error: {e}")
        return None


def check_target_position(token_id: str) -> Optional[dict]:
    """Check target trader's position status for this token - ONLY trust redeemable field"""
    if not token_id:
        return None
    try:
        # Check target trader's position to see if it's redeemable
        # Redeemable = WON (can redeem for $1)
        # NOT redeemable + market closed = LOST
        response = requests.get(
            f"{DATA_API}/positions",
            params={"user": TARGET_ADDRESS, "asset": token_id},
            timeout=10
        )
        if response.status_code == 200:
            positions = response.json()
            for pos in positions:
                # Check if this is the right token
                if str(pos.get("asset")) == str(token_id):
                    redeemable = pos.get("redeemable", False)
                    cur_price = float(pos.get("curPrice", 0.5))
                    size = float(pos.get("size", 0))
                    print(f"[ALGO] Target position: redeemable={redeemable}, curPrice={cur_price}, size={size}")

                    if redeemable:
                        # Market resolved — curPrice indicates winning side
                        if cur_price >= 0.50:
                            return {"resolved": True, "won": True}
                        else:
                            return {"resolved": True, "won": False}
                    elif cur_price <= 0.01 or cur_price >= 0.99:
                        # Not redeemable yet but price extreme = effectively resolved
                        return {"resolved": True, "won": cur_price >= 0.99}
        return None
    except Exception as e:
        print(f"[ALGO] Target position check error: {e}")
        return None


def get_market_resolution(condition_id: str = "", slug: str = "", token_id: str = "", our_outcome: str = "") -> Optional[dict]:
    """Check if a market has resolved and get the winning outcome.

    Resolution priority:
    1. CLOB API /markets/{condition_id} — has definitive 'winner' boolean per token
    2. Target trader position check — redeemable field
    3. Gamma API fallback — outcome prices
    """
    try:
        print(f"[ALGO] Checking resolution: token={token_id[:20] if token_id else 'none'}... cid={condition_id[:20] if condition_id else 'none'}...")

        # Method 1: CLOB API — most reliable, returns winner boolean directly
        if condition_id:
            clob_result = check_clob_market_resolution(condition_id, token_id)
            if clob_result and clob_result.get("resolved"):
                return clob_result

        # Method 2: Target trader's position redeemable status
        if token_id:
            target_result = check_target_position(token_id)
            if target_result and target_result.get("resolved"):
                won = target_result.get("won")
                print(f"[ALGO] Target position resolved: won={won}")
                return {
                    "resolved": True,
                    "our_token_won": won,
                    "winning_outcome": our_outcome if won else None,
                    "winning_index": None,
                }

        # Fallback: Try gamma API for market info
        params = {}
        if condition_id:
            params["condition_ids"] = condition_id
        elif slug:
            params["slug_contains"] = slug
        else:
            print(f"[ALGO] No condition_id or slug, can't query gamma API")
            return {"resolved": False}

        response = requests.get(
            f"{GAMMA_API}/markets",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        markets = response.json()
        print(f"[ALGO] Gamma API returned {len(markets)} markets")

        if markets and len(markets) > 0:
            market = markets[0]
            closed = market.get("closed")
            resolved = market.get("resolved")
            print(f"[ALGO] Market closed={closed}, resolved={resolved}")

            # Check if resolved
            if closed or resolved:
                outcomes = market.get("outcomes", [])
                outcome_prices = market.get("outcomePrices", [])
                print(f"[ALGO] outcomes={outcomes}, prices={outcome_prices}")

                # Only use gamma data if it looks valid (2 outcomes for binary market)
                if len(outcomes) == 2 and len(outcome_prices) == 2:
                    try:
                        prices = [float(p) for p in outcome_prices]
                    except (ValueError, TypeError):
                        prices = []

                    if len(prices) == 2:
                        # Pick the outcome with the higher price if it's clearly dominant
                        high_idx = 0 if prices[0] >= prices[1] else 1
                        # Use stricter threshold for merely "closed" markets,
                        # relaxed threshold for explicitly "resolved" markets
                        # (resolved markets often settle at 0.90-0.98 not exactly 1.0)
                        threshold = 0.60 if resolved else 0.99
                        if prices[high_idx] >= threshold:
                            print(f"[ALGO] Winner determined: {outcomes[high_idx]} (price={prices[high_idx]:.4f}, threshold={threshold})")
                            return {
                                "resolved": True,
                                "winning_outcome": outcomes[high_idx],
                                "winning_index": high_idx,
                            }

                # Market closed but can't determine winner from prices.
                # If market is only "closed" (not "resolved"), don't mark as resolved yet
                # so it stays open for retry. If explicitly "resolved", we must accept it.
                if resolved:
                    # Last resort: try matching our_outcome against available outcomes
                    if our_outcome and outcomes:
                        our_norm = our_outcome.lower().strip()
                        for idx, oc in enumerate(outcomes):
                            if oc.lower().strip() == our_norm:
                                # We have a valid outcome name but couldn't determine price winner.
                                # Return resolved with the outcome info so caller can attempt matching.
                                print(f"[ALGO] Market resolved, no clear price winner. Returning outcome names for matching.")
                                return {"resolved": True, "winning_outcome": None, "winning_index": None}
                    print(f"[ALGO] Market resolved but cannot determine winner. Marking resolved.")
                    return {"resolved": True, "winning_outcome": None, "winning_index": None}
                else:
                    # Only closed, not resolved — keep checking
                    print(f"[ALGO] Market closed but not resolved and no clear winner. Will retry.")
                    return {"resolved": False}

        return {"resolved": False}

    except Exception as e:
        identifier = condition_id[:20] if condition_id else slug[:20] if slug else "unknown"
        print(f"[ALGO] Error checking resolution for {identifier}...: {e}")
        return {"resolved": False}


# =============================================================================
# API FUNCTIONS
# =============================================================================

def get_active_crypto_tokens() -> list[str]:
    """Fetch token IDs for currently active crypto updown markets.

    Queries the Gamma API for open crypto markets and returns all CLOB
    token IDs so the WebSocket can subscribe to real-time prices.
    """
    try:
        response = requests.get(
            f"{GAMMA_API}/markets",
            params={"closed": "false", "limit": 50},
            timeout=10,
        )
        response.raise_for_status()
        markets = response.json()
        token_ids = []
        for m in markets:
            slug = (m.get("slug") or m.get("conditionId") or "").lower()
            question = (m.get("question") or "").lower()
            # Only crypto updown markets
            if not any(p in slug or p in question for p in CRYPTO_SLUGS):
                continue
            # Collect both token IDs (Up and Down)
            clob_ids = m.get("clobTokenIds") or []
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids) if clob_ids.startswith("[") else [clob_ids]
            token_ids.extend(clob_ids)
        return token_ids
    except Exception as e:
        print(f"[ALGO] Error fetching active crypto tokens: {e}")
        return []


def get_profile_name(wallet_address: str) -> str:
    """Get trader's profile name"""
    try:
        response = requests.get(
            f"{PROFILE_API}/public-profile",
            params={"address": wallet_address},
            timeout=10
        )
        response.raise_for_status()
        profile = response.json()
        return profile.get("name") or profile.get("pseudonym") or wallet_address[:10] + "..."
    except Exception:
        return wallet_address[:10] + "..."


def get_positions(wallet_address: str) -> list:
    """Get all positions for a wallet"""
    try:
        response = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet_address, "sizeThreshold": 0},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ALGO] Error fetching positions: {e}")
        return []


def get_latest_bets(wallet_address: str, limit: int = 20, verbose: bool = False, offset: int = 0) -> list:
    """Get recent buy trades for a wallet using server-side filtering"""
    try:
        url = f"{DATA_API}/activity"
        # Use server-side type/side filtering so BUY trades aren't pushed out
        # of the result window by SELLs/REDEEMs/etc.
        params = {
            "user": wallet_address,
            "limit": limit,
            "type": "TRADE",
            "side": "BUY",
        }
        if offset > 0:
            params["offset"] = offset
        response = requests.get(url, params=params, timeout=10)

        if verbose:
            print(f"[ALGO] GET {url}?user={wallet_address[:12]}...&limit={limit}&type=TRADE&side=BUY => HTTP {response.status_code}", flush=True)

        response.raise_for_status()

        data = response.json()
        if verbose:
            count = len(data) if isinstance(data, list) else "dict"
            print(f"[ALGO] Got {count} BUY trades from API", flush=True)

        if not data:
            return []

        # Handle both array and dict responses
        bets = data if isinstance(data, list) else data.get("data", data.get("results", data.get("activities", [])))
        return bets
    except Exception as e:
        print(f"[ALGO] Error fetching activity: {e}", flush=True)
        return []


def is_crypto_market(bet: dict) -> bool:
    """Check if bet is on a crypto market (15, 30, or 60 min)"""
    slug = bet.get("slug", "").lower()
    title = bet.get("title", "").lower()

    # Check for crypto market indicators
    for pattern in CRYPTO_SLUGS:
        if pattern in slug or pattern in title:
            return True

    # Also check for specific crypto keywords and timeframes (15, 30, 60 min)
    crypto_keywords = [
        "bitcoin", "ethereum", "solana", "xrp", "ripple",
        "15m", "15-min", "15 min",
        "30m", "30-min", "30 min",
        "60m", "60-min", "60 min", "1h", "1-hour", "1 hour"
    ]
    for kw in crypto_keywords:
        if kw in title:
            return True

    return False


def has_opposite_position(entered_markets: dict, condition_id: str, outcome_index: int) -> bool:
    """Check if we already have the OPPOSITE side of this market.

    Looks through entered_markets dict for any entry on the same condition_id
    but a different outcome_index.  Prevents betting both Up and Down on the
    same market, which just pays the vig and cancels out edge.
    """
    if not condition_id:
        return False
    for (cid, oi) in entered_markets:
        if cid == condition_id and oi != outcome_index:
            return True
    return False


def get_clob_client() -> Optional["ClobClient"]:
    """Initialize CLOB client with credentials"""
    if not HAS_CLOB_CLIENT:
        return None

    if not PRIVATE_KEY:
        print("[ALGO] Error: POLYGON_PRIVATE_KEY not set in environment")
        return None

    if not FUNDER_ADDRESS:
        print("[ALGO] Error: POLYMARKET_FUNDER_ADDRESS not set in environment")
        return None

    try:
        client = ClobClient(
            CLOB_API,
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER_ADDRESS
        )
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        return client
    except Exception as e:
        print(f"[ALGO] Error initializing CLOB client: {e}")
        return None


def place_bet(client: "ClobClient", token_id: str, amount: float, max_price: float = 0) -> dict:
    """Place a price-protected limit order. Returns fill details or empty dict on failure.

    Uses a GTC limit order at max_price (target's entry + buffer) so the order
    won't fill at absurd prices if the market has moved. Falls back to FOK market
    order only if no max_price is provided.
    """
    try:
        if max_price and max_price > 0:
            # Price-protected limit order: won't pay more than max_price
            limit_price = min(round(max_price, 4), 0.99)
            size = amount / limit_price  # shares to buy at this price

            print(f"[ALGO] Limit order: {size:.2f} shares @ {limit_price:.4f} (max ${amount:.2f})")
            order = client.create_order(
                token_id=token_id,
                price=limit_price,
                size=round(size, 2),
                side=BUY,
            )
            result = client.post_order(order)
        else:
            # Fallback: FOK market order (no price protection)
            print(f"[ALGO] WARNING: No max_price, using FOK market order")
            order = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                order_type=OrderType.FOK
            )
            signed_order = client.create_market_order(order)
            result = client.post_order(signed_order, OrderType.FOK)

        # Extract fill details from CLOB response
        fill_info = {"success": True}
        if isinstance(result, dict):
            status = result.get("status", "").lower()
            # Check if order was rejected / not matched
            if status in ("rejected", "failed", "expired"):
                print(f"[ALGO] Order {status}: price moved beyond limit. Result: {result}")
                return {}

            usdc_filled = float(result.get("matchedAmount") or result.get("amount") or 0)
            shares_filled = float(result.get("size") or result.get("filledSize") or 0)
            if shares_filled > 0:
                fill_info["fill_price"] = usdc_filled / shares_filled
                fill_info["shares"] = shares_filled
                fill_info["usdc"] = usdc_filled
            print(f"[ALGO] Fill details: {result}")
        return fill_info
    except Exception as e:
        print(f"[ALGO] Order error: {e}")
        return {}


# =============================================================================
# COPY TRADING LOGIC
# =============================================================================

class CopyTrader:
    """Copy trading engine"""

    def __init__(self, dry_run: bool = True, crypto_only: bool = True, on_trade: Optional[callable] = None, on_resolution: Optional[callable] = None, bet_amount: Optional[float] = None, coin_bet_amounts: Optional[dict] = None):
        self.dry_run = dry_run
        self.crypto_only = crypto_only
        self.client: Optional["ClobClient"] = None
        self.copied_trades: set = set()  # Track copied trade IDs
        self.copied_sizes: set = set()  # Track (condition_id, target_size) to dedup re-scans
        self.entered_markets: dict = {}  # (condition_id, outcome_index) → entry_price
        self.market_entry_count: dict = {}  # (condition_id, outcome_index) → number of entries
        self.max_entries_per_market = int(os.getenv("COPY_MAX_ENTRIES_PER_MARKET", "2"))  # Cap re-entries per market window
        self.target_name = get_profile_name(TARGET_ADDRESS)
        self.on_trade = on_trade  # Callback for dashboard integration
        self.on_resolution = on_resolution  # Callback when position resolves

        # Configurable trade amount (can be changed at runtime via dashboard)
        self.bet_amount = bet_amount if bet_amount is not None else BET_AMOUNT

        # Per-coin lot sizes (e.g. {"btc": 1.0, "eth": 2.0, "sol": 1.0})
        self.coin_bet_amounts = dict(coin_bet_amounts) if coin_bet_amounts else dict(COIN_BET_AMOUNTS)

        # Per-coin pause: set of coin symbols currently paused (e.g. {"sol", "xrp"})
        self.paused_coins: set = set()

        # Dynamic lot sizing: auto-adjust lot sizes based on per-coin win rate
        # When enabled, lot sizes are recalculated every resolution cycle
        self.dynamic_lot_sizing_enabled = False
        self.dynamic_lot_tiers = [
            # (win_rate_threshold, lot_size) — evaluated bottom-up, first match wins
            (60.0, 1.0),   # ≤ 60% win rate → $1
            (70.0, 2.0),   # ≤ 70% win rate → $2
            (75.0, 5.0),   # ≤ 75% win rate → $5
            (80.0, 10.0),  # ≤ 80% win rate → $10
            (90.0, 20.0),  # ≥ 90% win rate → $20
        ]
        self.dynamic_lot_base = 2.0  # Default lot size for coins with no data

        # Stats
        self.trades_copied = 0
        self.trades_skipped = 0
        self.total_spent = 0.0

        # Trade history (for dashboard)
        self.trade_history: list = []

        # Position tracking (persisted to file)
        # Opening balance set via ALGO_STARTING_BALANCE env var on Railway
        self.positions = load_positions()

        self.last_resolution_check = 0
        self.resolution_check_interval = 60  # Check every 60 seconds

        # WebSocket for real-time prices (replaces stale REST prices)
        self.ws: Optional["CLOBWebSocket"] = None
        self.ws_token_refresh_interval = 300  # Refresh subscribed tokens every 5 min
        self.last_ws_token_refresh = 0

    def get_bet_amount(self, slug: str = "", title: str = "") -> float:
        """Get bet amount for a specific coin, falling back to default."""
        coin = detect_coin(slug, title)
        if coin and coin in self.coin_bet_amounts:
            return self.coin_bet_amounts[coin]
        return self.bet_amount

    def compute_dynamic_lot(self, coin: str, win_rate: float) -> float:
        """Compute lot size for a coin based on its win rate using tiered thresholds.

        Tiers (evaluated bottom-up):
          ≤ 60% → $1   |   ≤ 70% → $2   |   ≤ 75% → $5
          ≤ 80% → $10  |   ≥ 90% → $20
          80-90% → $10 (default)
        """
        # Check from lowest tier up
        for threshold, lot in self.dynamic_lot_tiers:
            if threshold == self.dynamic_lot_tiers[-1][0]:
                # Last tier is the "≥ 90%" tier
                if win_rate >= threshold:
                    return lot
            elif win_rate <= threshold:
                return lot
        # 80-90% range: use $10
        return 10.0

    def apply_dynamic_lot_sizing(self):
        """Recalculate per-coin lot sizes based on current win rate.

        Called periodically (after resolutions) when dynamic_lot_sizing_enabled is True.
        Updates self.coin_bet_amounts in-place so the next trade uses the new sizes.
        """
        if not self.dynamic_lot_sizing_enabled:
            return

        open_positions = list(self.positions.get("open", []))
        resolved_positions = list(self.positions.get("resolved", []))
        coin_roi = self._compute_coin_roi(open_positions, resolved_positions)

        changes = []
        for coin in list(self.coin_bet_amounts.keys()):
            stats = coin_roi.get(coin)
            if not stats or (stats["wins"] + stats["losses"]) == 0:
                # No resolved trades yet — use base lot
                new_lot = self.dynamic_lot_base
            else:
                total = stats["wins"] + stats["losses"]
                win_rate = (stats["wins"] / total) * 100.0 if total > 0 else 0
                new_lot = self.compute_dynamic_lot(coin, win_rate)

            old_lot = self.coin_bet_amounts.get(coin, self.dynamic_lot_base)
            if abs(new_lot - old_lot) >= 0.01:
                self.coin_bet_amounts[coin] = new_lot
                if stats:
                    total = stats["wins"] + stats["losses"]
                    wr = (stats["wins"] / total) * 100.0 if total > 0 else 0
                    changes.append(f"{coin.upper()}: ${old_lot:.2f}→${new_lot:.2f} (WR {wr:.0f}%)")
                else:
                    changes.append(f"{coin.upper()}: ${old_lot:.2f}→${new_lot:.2f}")

        if changes:
            print(f"[ALGO] Dynamic lot resize: {', '.join(changes)}", flush=True)

    def start(self):
        """Initialize the algo trader"""
        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        lot_sizes = ", ".join(f"{c.upper()}=${a}" for c, a in sorted(self.coin_bet_amounts.items()))
        print("\n" + "=" * 60)
        print(f"  POLY ALGO")
        print(f"  Following: {self.target_name}")
        print(f"  Target: {TARGET_ADDRESS[:20]}...")
        print(f"  Lot sizes: {lot_sizes} (default: ${self.bet_amount})")
        print(f"  Balance: ${balance:.2f}")
        print(f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Filter: {'Crypto only' if self.crypto_only else 'All markets'}")
        print(f"  Poll interval: {POLL_INTERVAL}s")
        print(f"  Price buffer: {PRICE_BUFFER_BPS} bps")
        print("=" * 60 + "\n")

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[ALGO] Failed to initialize client. Running in dry-run mode.")
                self.dry_run = True

        # Start WebSocket for real-time prices
        self._start_ws()

        # Snapshot existing trades so we don't copy historical trades
        # Only copy NEW trades that happen AFTER we start monitoring
        print("[ALGO] Loading existing trades to avoid duplicates...")
        existing_bets = get_latest_bets(TARGET_ADDRESS, limit=50)
        for bet in existing_bets:
            trade_id = bet.get("id") or f"{bet.get('conditionId')}_{bet.get('timestamp')}"
            self.copied_trades.add(trade_id)
            # Also snapshot target sizes so we don't re-copy existing positions
            cid = bet.get("conditionId") or bet.get("condition_id") or ""
            oi = bet.get("outcomeIndex")
            if oi is None:
                oi = bet.get("outcome_index")
            if oi is None:
                oi = 0
            target_size = bet.get("size", 0)
            if cid and target_size:
                self.copied_sizes.add((cid, str(oi), str(round(float(target_size), 2))))
        print(f"[ALGO] Marked {len(self.copied_trades)} existing trades ({len(self.copied_sizes)} unique sizes) as seen. Waiting for NEW trades...")

        # Seed entered_markets and market_entry_count from existing open
        # positions so we don't chase markets we already have exposure to
        # after a restart.
        for pos in self.positions.get("open", []):
            cid = pos.get("condition_id", "")
            oi = pos.get("outcome_index", 0)
            ep = pos.get("entry_price", 0)
            if cid:
                mk = (cid, oi)
                if mk not in self.entered_markets:
                    self.entered_markets[mk] = ep
                self.market_entry_count[mk] = self.market_entry_count.get(mk, 0) + 1
        if self.entered_markets:
            print(f"[ALGO] Resumed {len(self.entered_markets)} active market entries from open positions")

        # Show position tracking stats
        stats = self.positions.get("stats", {})
        open_count = len(self.positions.get("open", []))
        resolved_count = len(self.positions.get("resolved", []))
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = stats.get("total_pnl", 0.0)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        print(f"[ALGO] Positions: {open_count} open, {resolved_count} resolved")
        print(f"[ALGO] Record: {wins}W / {losses}L ({win_rate:.1f}% win rate)")
        print(f"[ALGO] Total PnL: ${total_pnl:+.2f}")

    def _start_ws(self):
        """Start WebSocket for real-time market prices."""
        if not HAS_CLOB_WS:
            print("[ALGO] WebSocket not available (clob_ws.py not found)")
            return

        try:
            self.ws = CLOBWebSocket(
                on_connect=lambda: print("[ALGO] WebSocket connected — live prices active", flush=True),
                on_disconnect=lambda: print("[ALGO] WebSocket disconnected — will reconnect", flush=True),
            )
            self.ws.start()
            # Give the connection a moment to establish
            time.sleep(1)
            self._refresh_ws_tokens()
        except Exception as e:
            print(f"[ALGO] WebSocket start failed: {e}")
            self.ws = None

    def _refresh_ws_tokens(self):
        """Subscribe to token IDs for active crypto markets."""
        if not self.ws:
            return

        now = time.time()
        if now - self.last_ws_token_refresh < self.ws_token_refresh_interval:
            return
        self.last_ws_token_refresh = now

        token_ids = get_active_crypto_tokens()
        if token_ids:
            self.ws.subscribe(token_ids)
            print(f"[ALGO] WebSocket subscribed to {len(token_ids)} crypto tokens")
        else:
            print("[ALGO] No active crypto tokens found for WebSocket")

    def get_live_ask(self, token_id: str) -> Optional[float]:
        """Get real-time best ask price from WebSocket, or None if unavailable."""
        if not self.ws or not token_id:
            return None
        _, best_ask = self.ws.get_best_prices(token_id)
        return best_ask

    def check_and_copy(self) -> int:
        """Check for new trades and copy them. Returns number of trades copied."""
        copied = 0

        # Periodically refresh WebSocket token subscriptions (new markets open)
        self._refresh_ws_tokens()

        # Get target's recent bets — always verbose to diagnose scanning
        bets = get_latest_bets(TARGET_ADDRESS, verbose=True)
        if not bets:
            return 0

        # Get our positions
        my_positions = get_positions(FUNDER_ADDRESS) if FUNDER_ADDRESS else []

        for bet in bets:
            trade_id = bet.get("id") or f"{bet.get('conditionId')}_{bet.get('timestamp')}"

            # Skip if already copied
            if trade_id in self.copied_trades:
                continue

            # Mark as seen (even if we skip it)
            self.copied_trades.add(trade_id)

            title = bet.get("title", "Unknown")[:50]
            outcome = bet.get("outcome", "?")
            size = bet.get("size", 0)
            slug = bet.get("slug", "")

            # Calculate entry price from usdcSize / size (dollars spent / shares received)
            # This is the TRUE entry price. The 'price' field from activity API is
            # the CURRENT market price, NOT the entry price — using it as fallback
            # causes 200%+ reconciliation diffs.
            price = None
            price_source = "unknown"
            usdc_size = bet.get('usdcSize') or bet.get('usdc_size') or bet.get('amount')
            shares = bet.get('size')

            if usdc_size and shares:
                try:
                    usdc_f = float(usdc_size)
                    shares_f = float(shares)
                    if shares_f > 0:
                        price = usdc_f / shares_f
                        price_source = "calculated"
                except (ValueError, TypeError):
                    pass

            # If calculation failed, DO NOT fall back to bet.get('price') — that's
            # the current market price and would create a phantom entry price.
            # Instead, log the gap and leave price at None so the WS live ask
            # (or 0) is used, which is more honest.
            if not price or price <= 0:
                # Last resort: try cashAmount field (some API versions)
                cash_amt = bet.get("cashAmount") or bet.get("cash_amount")
                if cash_amt and shares:
                    try:
                        price = float(cash_amt) / float(shares)
                        price_source = "cashAmount"
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass

            if not price or price <= 0:
                price = 0
                price_source = "none"
                print(f"[ALGO] WARNING: Could not calculate entry price "
                      f"(usdcSize={usdc_size}, size={shares}). "
                      f"API price field={bet.get('price')} (NOT used — it's current price, not entry)")

            if price_source == "calculated":
                print(f"[ALGO] Entry: ${float(usdc_size):.4f} / {float(shares):.2f} shares = {price:.4f} ({price*100:.1f}¢)")

            # Filter for crypto markets only
            if self.crypto_only and not is_crypto_market(bet):
                print(f"[ALGO] Skip (not crypto): {title}")
                self.trades_skipped += 1
                continue

            # Per-coin pause check: skip if this coin is individually paused
            trade_coin_check = detect_coin(slug, title)
            if trade_coin_check and trade_coin_check in self.paused_coins:
                print(f"[ALGO] Skip (coin paused: {trade_coin_check.upper()}): {title}")
                self.trades_skipped += 1
                continue

            # Price band filter: only trade when entry price is in the profitable range
            MIN_ENTRY_PRICE = 0.60
            MAX_ENTRY_PRICE = 0.95
            if price and (price < MIN_ENTRY_PRICE or price > MAX_ENTRY_PRICE):
                print(f"[ALGO] Skip (price {price:.3f} outside {MIN_ENTRY_PRICE}-{MAX_ENTRY_PRICE}): {title} | {outcome}")
                self.trades_skipped += 1
                continue

            # Check if we already have this position
            # Try multiple field names for condition_id (API may use camelCase or snake_case)
            condition_id = bet.get("conditionId") or bet.get("condition_id") or bet.get("market_condition_id") or ""
            # Use explicit None checks -- outcome_index=0 is valid (not falsy)
            outcome_index = bet.get("outcomeIndex")
            if outcome_index is None:
                outcome_index = bet.get("outcome_index")
            if outcome_index is None:
                outcome_index = 0

            # --- GUARD 1: Block chasing, allow conviction ---
            # If we already hold this side, only re-enter when the current
            # price is ABOVE our initial entry (market moving in our favour).
            # Block when price is at or below entry — that's throwing good
            # money after bad (e.g. 47¢→35¢→20¢ spiral).
            market_key = (condition_id, outcome_index)
            if condition_id and market_key in self.entered_markets:
                initial_entry = self.entered_markets[market_key]
                if initial_entry and price <= initial_entry:
                    print(f"[ALGO] Skip (chasing — price {price:.3f} <= entry {initial_entry:.3f}): {title} | {outcome}")
                    self.trades_skipped += 1
                    continue
                else:
                    print(f"[ALGO] Re-entry OK (conviction — price {price:.3f} > entry {initial_entry:.3f}): {title} | {outcome}")

            # --- GUARD 2: Cap re-entries per market window ---
            # Prevent piling into the same condition_id beyond max_entries_per_market.
            # This reduces EXTRA_COPY duplicates where the target's multiple buys
            # on the same window cause us to copy each one.
            if condition_id and market_key in self.market_entry_count:
                count = self.market_entry_count[market_key]
                if count >= self.max_entries_per_market:
                    print(f"[ALGO] Skip (max {self.max_entries_per_market} entries reached for this market): {title} | {outcome}")
                    self.trades_skipped += 1
                    continue

            # --- GUARD 3: Block opposite side ---
            # Don't bet both Up and Down on the same market. Betting both
            # sides cancels out any edge and just pays the vig.
            if condition_id and has_opposite_position(self.entered_markets, condition_id, outcome_index):
                print(f"[ALGO] Skip (opposite side already entered): {title} | {outcome}")
                self.trades_skipped += 1
                continue

            # Legacy size-based dedup kept as a secondary safety net
            target_size = bet.get("size", 0)
            if condition_id and target_size:
                size_key = (condition_id, str(outcome_index), str(round(float(target_size), 2)))
                if size_key in self.copied_sizes:
                    print(f"[ALGO] Skip (same size already copied): {title} | {outcome} {target_size} shares")
                    self.trades_skipped += 1
                    continue
                self.copied_sizes.add(size_key)

            # Resolve per-coin lot size
            trade_coin = detect_coin(slug, title)
            trade_amount = self.coin_bet_amounts.get(trade_coin, self.bet_amount) if trade_coin else self.bet_amount

            # Copy the trade!
            print(f"\n[ALGO] NEW TRADE DETECTED!")
            print(f"       Market: {title}")
            print(f"       Target bought: {size:.1f} {outcome} @ {price*100:.1f}¢")
            print(f"       Copying: ${trade_amount:.2f} of {outcome} ({trade_coin.upper() or '?'} lot)")

            # Build trade record for dashboard
            trade_record = {
                "id": f"copy_{trade_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": slug,
                "outcome": outcome,
                "side": "BUY",
                "amount": trade_amount,
                "coin": trade_coin,
                "price": price,
                "target_trader": self.target_name,
                "target_size": size,
                "source": "copy_trader",
            }

            if self.dry_run:
                token_id = bet.get("asset", "")
                # In dry run: use WebSocket live price if available for realistic tracking
                live_ask = self.get_live_ask(token_id) if token_id else None
                if live_ask:
                    price = live_ask
                    trade_record["price"] = price
                    print(f"       DRY RUN - would buy @ {price*100:.1f}¢ (live ask)")
                else:
                    print(f"       DRY RUN - would buy @ {price*100:.1f}¢ (target entry)")
                    # Subscribe to this token for next time
                    if token_id and self.ws:
                        self.ws.subscribe([token_id])
                trade_record["status"] = "dry_run"
                copied += 1
            else:
                token_id = bet.get("asset", "")
                if token_id and self.client:
                    # Use live WebSocket best_ask as the limit price (most accurate)
                    # Falls back to target's entry price + buffer if WS unavailable
                    live_ask = self.get_live_ask(token_id)
                    buffer = PRICE_BUFFER_BPS / 10000
                    if live_ask:
                        max_price = min(live_ask * (1 + buffer), 0.99)
                        print(f"       Limit: {max_price:.4f} (live ask {live_ask:.4f} + {PRICE_BUFFER_BPS}bps)")
                    elif price > 0:
                        max_price = price * (1 + buffer)
                        print(f"       Limit: {max_price:.4f} (target entry + {PRICE_BUFFER_BPS}bps, no WS)")
                    else:
                        max_price = 0
                    fill = place_bet(self.client, token_id, trade_amount, max_price=max_price)
                    if fill.get("success"):
                        # Use our actual fill price instead of target's entry price
                        if fill.get("fill_price"):
                            price = fill["fill_price"]
                            trade_record["price"] = price
                            print(f"       EXECUTED! Fill price: {price:.4f} ({price*100:.1f}¢)")
                        else:
                            print(f"       EXECUTED! (fill price unavailable, using target's entry)")
                        trade_record["status"] = "filled"
                        copied += 1
                        self.total_spent += trade_amount
                    else:
                        print(f"       FAILED!")
                        trade_record["status"] = "failed"
                else:
                    print(f"       No token ID or client")
                    trade_record["status"] = "error"

            # Record trade
            self.trade_history.append(trade_record)

            # Save position for tracking (if trade was successful or dry run)
            if trade_record["status"] in ["filled", "dry_run"]:
                # Record market entry — only store the FIRST entry price so
                # subsequent conviction re-entries are compared against the
                # original, not against an already-elevated price.
                if condition_id and market_key not in self.entered_markets:
                    self.entered_markets[market_key] = price
                # Increment per-market entry count (for GUARD 2 cap)
                if condition_id:
                    self.market_entry_count[market_key] = self.market_entry_count.get(market_key, 0) + 1
                token_id = bet.get("asset", "")
                position = {
                    "id": trade_record["id"],
                    "timestamp": trade_record["timestamp"],
                    "condition_id": condition_id,
                    "token_id": token_id,  # Store token_id for direct resolution check
                    "outcome_index": outcome_index,
                    "outcome": outcome,
                    "market": title,
                    "slug": slug,
                    "entry_price": price,
                    "amount": trade_amount,
                    "potential_payout": trade_amount / price if price > 0 else 0,
                    "dry_run": self.dry_run,
                }
                self.positions["open"].append(position)

                # Deduct balance (non-fatal — must never break trading)
                try:
                    stats = self.positions["stats"]
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - trade_amount
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats.get("total_pnl", 0.0),
                        "equity": stats["balance"] + open_staked,
                        "event": "trade",
                        "detail": f"{outcome} {title[:30]}"
                    })
                except Exception:
                    pass

                save_positions(self.positions)
                print(f"       Position saved. Potential payout: ${position['potential_payout']:.2f} | Balance: ${self.positions['stats'].get('balance', 0):.2f}")

            # Call dashboard callback
            if self.on_trade:
                try:
                    self.on_trade(trade_record)
                except Exception as e:
                    print(f"[ALGO] Callback error: {e}")

            self.trades_copied += copied

        return copied

    def run_once(self):
        """Single check iteration"""
        self.start()

        print("[ALGO] Checking for trades to copy...")
        copied = self.check_and_copy()

        if copied > 0:
            print(f"\n[ALGO] Copied {copied} trade(s)")
        else:
            print("[ALGO] No new trades to copy")

        self.print_stats()

    def stop(self):
        """Clean up resources (WebSocket, etc.)"""
        if self.ws:
            try:
                self.ws.stop()
            except Exception:
                pass
            self.ws = None

    def run_loop(self):
        """Continuous monitoring loop"""
        self.start()

        ws_status = "WebSocket active" if self.ws and self.ws.connected else "REST only"
        print(f"[ALGO] Starting continuous monitoring (every {POLL_INTERVAL}s, {ws_status})...")
        print("[ALGO] Press Ctrl+C to stop\n")

        try:
            while True:
                copied = self.check_and_copy()
                if copied > 0:
                    print(f"[ALGO] Copied {copied} trade(s) this cycle")

                # Periodically check for resolved positions
                self.check_resolutions()

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n[ALGO] Stopping...")
            self.print_stats()

    def check_resolutions(self):
        """Check if any open positions have resolved"""
        now = time.time()
        if now - self.last_resolution_check < self.resolution_check_interval:
            return

        self.last_resolution_check = now

        open_positions = self.positions.get("open", [])
        if not open_positions:
            return

        resolved_this_check = 0

        for position in open_positions[:]:  # Copy list to allow modification
            condition_id = position.get("condition_id", "")
            slug = position.get("slug", "")
            token_id = position.get("token_id", "")
            our_outcome = position.get("outcome")

            # Need either token_id, condition_id, or slug to check resolution
            if not token_id and not condition_id and not slug:
                continue

            result = get_market_resolution(
                condition_id=condition_id,
                slug=slug,
                token_id=token_id,
                our_outcome=our_outcome
            )

            if result.get("resolved"):
                # Position resolved!
                our_index = position.get("outcome_index")
                entry_price = position.get("entry_price", 0)
                amount = position.get("amount", 0)

                # Initialize these - may be set by gamma API fallback
                winning_outcome = result.get("winning_outcome")
                winning_index = result.get("winning_index")

                # Check if we got a direct win/loss result from token price check
                won = None
                if "our_token_won" in result:
                    # Direct result from token price - most reliable
                    won = result.get("our_token_won")
                    print(f"[ALGO] Token resolution: {position['market'][:30]} | won={won}")
                else:
                    # Fallback to outcome name comparison
                    winning_outcome = result.get("winning_outcome")
                    winning_index = result.get("winning_index")

                    if winning_outcome and our_outcome:
                        our_normalized = our_outcome.lower().strip()
                        winning_normalized = winning_outcome.lower().strip()

                        if our_normalized == winning_normalized:
                            won = True
                        elif our_normalized.startswith(winning_normalized) or winning_normalized.startswith(our_normalized):
                            won = True
                        else:
                            won = False
                    elif winning_index is not None and our_index is not None:
                        won = (winning_index == our_index)

                if won is True:
                    # Validate entry_price before calculating
                    if entry_price <= 0:
                        # No valid entry price - use placeholder PnL
                        pnl = amount * 3  # Assume ~4x return (typical for 20-25% odds)
                        print(f"[ALGO] WIN (no price): {position['market'][:30]} | entry_price=0, estimating +${pnl:.2f}")
                    else:
                        payout = amount / entry_price
                        pnl = payout - amount
                        print(f"[ALGO] WIN: {position['market'][:30]} | entry={entry_price:.4f}, payout=${payout:.2f}, pnl=+${pnl:.2f}")
                    position["result"] = "WIN"
                    position["pnl"] = pnl
                    self.positions["stats"]["wins"] = self.positions["stats"].get("wins", 0) + 1
                elif won is False:
                    pnl = -amount
                    position["result"] = "LOSS"
                    position["pnl"] = pnl
                    self.positions["stats"]["losses"] = self.positions["stats"].get("losses", 0) + 1
                    print(f"[ALGO] LOSS: {position['market'][:30]} | -${amount:.2f}")
                else:
                    # Could not determine winner — defer resolution so we retry
                    # on the next check cycle instead of permanently marking UNKNOWN.
                    # Track attempts to avoid infinite retries.
                    attempts = position.get("_resolve_attempts", 0) + 1
                    position["_resolve_attempts"] = attempts
                    max_attempts = 5

                    if attempts < max_attempts:
                        print(f"[ALGO] Cannot determine winner for {position['market'][:30]} "
                              f"(attempt {attempts}/{max_attempts}). Will retry.")
                        continue  # Skip — leave position open for next cycle

                    # Exhausted retries — mark UNKNOWN but log loudly
                    pnl = 0
                    position["result"] = "UNKNOWN"
                    position["pnl"] = 0
                    print(f"[ALGO] RESOLVED (unknown after {max_attempts} attempts): {position['market'][:30]}")

                # Update totals
                self.positions["stats"]["total_pnl"] = self.positions["stats"].get("total_pnl", 0) + pnl

                # Update balance (non-fatal — must never break resolution)
                try:
                    stats = self.positions["stats"]
                    bal = stats.get("balance", ALGO_STARTING_BALANCE)
                    if won is True:
                        payout = amount / entry_price if entry_price > 0 else amount
                        bal += payout
                    stats["balance"] = bal
                    event_type = "win" if won is True else "loss" if won is False else "resolved"
                    # Note: position is about to be removed from open, so exclude it from staked calc
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []) if p is not position)
                    stats.setdefault("balance_history", []).append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "balance": bal,
                        "pnl": stats.get("total_pnl", 0.0),
                        "equity": bal + open_staked,
                        "event": event_type,
                        "detail": f"{position.get('outcome', '?')} {position.get('market', '?')[:30]}"
                    })
                except Exception:
                    pass

                # Move from open to resolved
                position["resolved_at"] = datetime.now(timezone.utc).isoformat()
                position["winning_outcome"] = winning_outcome
                position["winning_index"] = winning_index
                position["won"] = won
                self.positions["open"].remove(position)
                self.positions["resolved"].append(position)
                resolved_this_check += 1

                # Call resolution callback
                if self.on_resolution:
                    try:
                        self.on_resolution(position)
                    except Exception as e:
                        print(f"[ALGO] Resolution callback error: {e}")

        if resolved_this_check > 0:
            save_positions(self.positions)
            stats = self.positions["stats"]
            print(f"[ALGO] {resolved_this_check} position(s) resolved. Record: {stats['wins']}W/{stats['losses']}L, PnL: ${stats['total_pnl']:+.2f}")

            # After resolutions, recalculate dynamic lot sizes if enabled
            self.apply_dynamic_lot_sizing()

    @staticmethod
    def _safe_float(v, default=0.0):
        """Ensure a float is JSON-safe (no NaN/Inf)"""
        import math
        if not isinstance(v, (int, float)):
            return default
        if math.isnan(v) or math.isinf(v):
            return default
        return v

    @staticmethod
    def _thin_balance_history(history: list) -> list:
        """Downsample balance history so all timeframes (1H to 1M) have data.

        Strategy: keep full resolution for recent data, progressively thin older data.
        - Last 1 hour:   every point
        - 1h - 6h:       every 5th point
        - 6h - 24h:      every 20th point
        - 1d - 7d:       every 100th point
        - 7d - 30d:      every 500th point
        Result: ~5-6k points max regardless of how long the algo runs.
        """
        if len(history) <= 2000:
            return history

        now_ms = time.time() * 1000
        buckets = [
            (1 * 3600 * 1000, 1),      # last 1h: every point
            (6 * 3600 * 1000, 5),       # 1h-6h: every 5th
            (24 * 3600 * 1000, 20),     # 6h-1d: every 20th
            (7 * 24 * 3600 * 1000, 100),   # 1d-7d: every 100th
            (30 * 24 * 3600 * 1000, 500),  # 7d-30d: every 500th
        ]

        result = []
        for entry in history:
            try:
                ts = entry.get("timestamp", "")
                entry_ms = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                continue
            age_ms = now_ms - entry_ms
            if age_ms < 0:
                age_ms = 0

            # Find which bucket this entry belongs to
            keep = False
            prev_boundary = 0
            for boundary, step in buckets:
                if age_ms < boundary:
                    # Use index within this bucket's portion of the list for thinning
                    keep = True
                    break
                prev_boundary = boundary

            if not keep:
                # Older than 30 days - skip
                continue

            result.append((age_ms, entry))

        # Now thin each bucket
        # Sort by age descending (oldest first) so indices are stable per bucket
        result.sort(key=lambda x: -x[0])

        thinned = []
        bucket_counters = {}
        for age_ms, entry in result:
            # Determine step for this age
            step = 1
            for boundary, s in buckets:
                if age_ms < boundary:
                    step = s
                    break

            bucket_key = step
            bucket_counters[bucket_key] = bucket_counters.get(bucket_key, 0) + 1
            if bucket_counters[bucket_key] % step == 0 or step == 1:
                thinned.append(entry)

        # Sort chronologically
        thinned.sort(key=lambda e: e.get("timestamp", ""))
        return thinned

    def _compute_coin_roi(self, open_positions: list, resolved_positions: list) -> dict:
        """Compute per-coin W/L/PnL/ROI from position data.

        Returns dict keyed by coin symbol, e.g.:
        {"btc": {"wins": 10, "losses": 3, "pnl": 5.2, "deployed": 26.0,
                 "roi": 20.0, "open": 2, "streak": 3, "streak_type": "W"}, ...}
        """
        coin_data: dict = {}
        all_coins = set(self.coin_bet_amounts.keys())

        # Process resolved positions
        for pos in resolved_positions:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            all_coins.add(coin)

            if coin not in coin_data:
                coin_data[coin] = {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []}

            amount = pos.get("amount", 0)
            pnl = pos.get("pnl", 0) or 0
            result = pos.get("result", "")

            coin_data[coin]["deployed"] += amount
            coin_data[coin]["pnl"] += pnl
            if result == "WIN":
                coin_data[coin]["wins"] += 1
                coin_data[coin]["results"].append("W")
            elif result == "LOSS":
                coin_data[coin]["losses"] += 1
                coin_data[coin]["results"].append("L")

        # Process open positions (count + deployed, no pnl yet)
        for pos in open_positions:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            all_coins.add(coin)

            if coin not in coin_data:
                coin_data[coin] = {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []}

            coin_data[coin]["open"] += 1
            coin_data[coin]["deployed"] += pos.get("amount", 0)

        # Compute ROI and current streak for each coin
        result = {}
        for coin in all_coins:
            d = coin_data.get(coin, {"wins": 0, "losses": 0, "pnl": 0.0, "deployed": 0.0, "open": 0, "results": []})
            total = d["wins"] + d["losses"]
            win_rate = (d["wins"] / total * 100) if total > 0 else 0
            roi = (d["pnl"] / d["deployed"] * 100) if d["deployed"] > 0 else 0

            # Current streak (from most recent results)
            streak = 0
            streak_type = ""
            for r in reversed(d["results"]):
                if not streak_type:
                    streak_type = r
                    streak = 1
                elif r == streak_type:
                    streak += 1
                else:
                    break

            result[coin] = {
                "wins": d["wins"],
                "losses": d["losses"],
                "win_rate": self._safe_float(win_rate),
                "pnl": self._safe_float(d["pnl"]),
                "deployed": self._safe_float(d["deployed"]),
                "roi": self._safe_float(roi),
                "open": d["open"],
                "streak": streak,
                "streak_type": streak_type,
            }

        return result

    def get_stats(self) -> dict:
        """Get current statistics for dashboard.

        Called every 1-2s by the Flask request handler on a different thread.
        Uses a cached balance_history snapshot to avoid re-thinning thousands
        of entries on every poll, and snapshots lists to avoid thread-safety
        issues with concurrent mutations.
        """
        stats = self.positions.get("stats", {})
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = self._safe_float(stats.get("total_pnl", 0.0))
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        balance = self._safe_float(stats.get("balance", ALGO_STARTING_BALANCE))

        # Cache thinned balance_history — only recompute when new entries arrive
        raw_history = stats.get("balance_history", [])
        raw_len = len(raw_history)
        cache = getattr(self, "_bh_cache", None)
        if cache and cache[0] == raw_len:
            balance_history = cache[1]
        else:
            balance_history = self._thin_balance_history(raw_history)
            self._bh_cache = (raw_len, balance_history)

        # Snapshot lists to avoid RuntimeError from concurrent mutation
        open_positions = list(self.positions.get("open", []))
        resolved_positions = list(self.positions.get("resolved", []))
        open_staked = sum(p.get("amount", 0) for p in open_positions)
        equity = self._safe_float(balance + open_staked)

        # Per-coin ROI stats from resolved + open positions
        coin_roi = self._compute_coin_roi(open_positions, resolved_positions)

        return {
            "trades_copied": self.trades_copied,
            "trades_skipped": self.trades_skipped,
            "total_spent": self._safe_float(self.total_spent),
            "open_positions": len(open_positions),
            "resolved_positions": len(resolved_positions),
            "wins": wins,
            "losses": losses,
            "win_rate": self._safe_float(win_rate),
            "total_pnl": total_pnl,
            "dry_run": self.dry_run,
            "balance": balance,
            "equity": equity,
            "balance_history": balance_history,
            "bet_amount": self._safe_float(self.bet_amount),
            "coin_bet_amounts": {k: self._safe_float(v) for k, v in self.coin_bet_amounts.items()},
            "coin_roi": coin_roi,
            "paused_coins": list(self.paused_coins),
            "dynamic_lot_sizing_enabled": self.dynamic_lot_sizing_enabled,
            "dynamic_lot_tiers": [{"win_rate": t, "lot": l} for t, l in self.dynamic_lot_tiers],
        }

    def print_stats(self):
        """Print trading statistics"""
        stats = self.get_stats()
        print("\n" + "-" * 40)
        print(f"  Balance: ${stats['balance']:.2f}")
        print(f"  Trades copied: {stats['trades_copied']}")
        print(f"  Trades skipped: {stats['trades_skipped']}")
        print(f"  Open positions: {stats['open_positions']}")
        print(f"  Resolved: {stats['resolved_positions']}")
        print(f"  Record: {stats['wins']}W / {stats['losses']}L ({stats['win_rate']:.1f}%)")
        print(f"  Total PnL: ${stats['total_pnl']:+.2f}")
        if not self.dry_run:
            print(f"  Total spent: ${stats['total_spent']:.2f}")
        print("-" * 40)


# =============================================================================
# TRADE RECONCILIATION
# =============================================================================

def _parse_target_bet(bet: dict) -> Optional[dict]:
    """Parse a single API bet into a normalized trade dict, or None if not crypto."""
    if not is_crypto_market(bet):
        return None

    usdc_size = bet.get("usdcSize") or bet.get("usdc_size") or bet.get("amount") or 0
    shares = bet.get("size") or 0
    try:
        usdc_size = float(usdc_size)
        shares = float(shares)
        entry_price = usdc_size / shares if shares > 0 else 0
    except (ValueError, TypeError, ZeroDivisionError):
        entry_price = 0

    return {
        "id": bet.get("id", ""),
        "timestamp": bet.get("timestamp") or bet.get("createdAt") or "",
        "market": (bet.get("title") or "")[:80],
        "slug": bet.get("slug", ""),
        "outcome": bet.get("outcome", ""),
        "condition_id": bet.get("conditionId") or bet.get("condition_id") or "",
        "token_id": bet.get("asset") or bet.get("token_id") or "",
        "entry_price": round(entry_price, 6),
        "usdc_size": round(usdc_size, 4),
        "shares": round(shares, 4),
    }


def get_target_trade_history(limit: int = 100, max_pages: int = 10) -> list:
    """Fetch target trader's recent BUY trades for reconciliation.

    Paginates through the Polymarket activity API using offset to get ALL
    crypto BUY trades (up to page_size * max_pages).  Without pagination
    the API cap of ~100 rows only covers the most recent window, making
    everything from earlier windows look like EXTRA_COPY.
    """
    all_trades: list = []
    seen_ids: set = set()
    page_size = min(limit, 100)  # API max per page

    for page in range(max_pages):
        offset = page * page_size
        raw = get_latest_bets(TARGET_ADDRESS, limit=page_size, offset=offset)
        if not raw:
            break

        new_this_page = 0
        for bet in raw:
            bet_id = bet.get("id", "")
            if bet_id in seen_ids:
                continue
            seen_ids.add(bet_id)

            trade = _parse_target_bet(bet)
            if trade:
                all_trades.append(trade)
                new_this_page += 1

        # If API returned fewer than page_size, we've hit the end
        if len(raw) < page_size or new_this_page == 0:
            break

    print(f"[RECONCILE] Fetched {len(all_trades)} target trades across {min(page + 1, max_pages)} pages", flush=True)
    return all_trades


def _base_market_name(title: str) -> str:
    """Extract base asset name from a time-windowed market title.

    "Bitcoin Up or Down - February 27, 8:50AM-8:55AM ET" -> "bitcoin up or down"
    "XRP Up or Down - February 27, 8:45AM-8:50AM ET"     -> "xrp up or down"
    """
    import re
    m = re.match(r'^(.+?)\s*-\s*\w+\s+\d+', title)
    if m:
        return m.group(1).strip().lower()
    return title.strip().lower()


def build_reconciliation(positions: dict, target_limit: int = 100, max_pages: int = 10) -> list:
    """Build a side-by-side reconciliation of target trades vs our trades.

    Returns a list of dicts, one per row, ready to be written as CSV.

    Two-tier matching:
      1. Exact: condition_id + outcome (same market window).
      2. Fuzzy: base_market_name + outcome, closest by timestamp.
         This catches copy trades that land on a different 5-min window
         than the target (different condition_id, same underlying asset).

    Unmatched target trades are flagged as MISSED; extra copies we made
    beyond what the target traded are flagged as EXTRA_COPY.  Positions
    that can't be matched even by fuzzy logic are UNMATCHED_WINDOW.
    """
    from datetime import datetime as _dt

    target_trades = get_target_trade_history(limit=target_limit, max_pages=max_pages)

    # Index our trades by condition_id + outcome for exact matching
    our_open = positions.get("open", [])
    our_resolved = positions.get("resolved", [])
    all_ours = our_open + our_resolved

    # Build exact lookup: (condition_id, outcome_normalised) -> list
    our_lookup: dict[tuple, list] = {}
    for pos in all_ours:
        cid = pos.get("condition_id", "")
        outcome = (pos.get("outcome") or "").lower().strip()
        key = (cid, outcome)
        our_lookup.setdefault(key, []).append(pos)

    # Build fuzzy lookup: (base_market_name, outcome_normalised) -> list
    fuzzy_lookup: dict[tuple, list] = {}
    for pos in all_ours:
        market_title = pos.get("market") or pos.get("slug") or ""
        base = _base_market_name(market_title)
        outcome = (pos.get("outcome") or "").lower().strip()
        fkey = (base, outcome)
        fuzzy_lookup.setdefault(fkey, []).append(pos)

    # Sort each bucket by timestamp
    for key in our_lookup:
        our_lookup[key] = sorted(our_lookup[key], key=lambda p: p.get("timestamp", ""))
    for fkey in fuzzy_lookup:
        fuzzy_lookup[fkey] = sorted(fuzzy_lookup[fkey], key=lambda p: p.get("timestamp", ""))

    # Track which of our positions have been claimed by a target trade
    claimed: set = set()

    def _parse_ts(s: str):
        """Parse ISO timestamp for time-proximity comparison."""
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    def _make_row(t: dict, best: dict, status: str) -> dict:
        """Build a reconciliation row from a target trade + our matched position."""
        our_price = best.get("entry_price", 0)
        target_price = t["entry_price"]
        result = best.get("result", "OPEN")
        return {
            "target_timestamp": t["timestamp"],
            "target_market": t["market"],
            "target_outcome": t["outcome"],
            "target_entry_price": target_price,
            "target_usdc": t["usdc_size"],
            "target_shares": t["shares"],
            "our_timestamp": best.get("timestamp", ""),
            "our_outcome": best.get("outcome", ""),
            "our_entry_price": our_price,
            "our_amount": best.get("amount", 0),
            "our_result": result,
            "our_pnl": best.get("pnl", ""),
            "status": status,
            "condition_id": best.get("condition_id") or t.get("condition_id", ""),
        }

    rows = []
    for t in target_trades:
        cid = t["condition_id"]
        outcome_norm = t["outcome"].lower().strip()
        exact_key = (cid, outcome_norm)

        # --- Tier 1: exact condition_id match ---
        best = None
        best_diff = float("inf")
        for m in our_lookup.get(exact_key, []):
            if id(m) in claimed:
                continue
            diff = abs((m.get("entry_price", 0) or 0) - (t["entry_price"] or 0))
            if diff < best_diff:
                best_diff = diff
                best = m

        if best is not None:
            claimed.add(id(best))
            our_price = best.get("entry_price", 0)
            target_price = t["entry_price"]
            price_diff = abs(our_price - target_price) if our_price and target_price else None
            pct_diff = (price_diff / target_price * 100) if target_price and price_diff is not None else None
            status = "MATCHED"
            if pct_diff is not None and pct_diff > 5:
                status = f"PRICE_DIFF ({pct_diff:.1f}%)"
            rows.append(_make_row(t, best, status))
            continue

        # --- Tier 2: fuzzy match on base market name + outcome ---
        t_market = t.get("market") or t.get("slug") or ""
        t_base = _base_market_name(t_market)
        fkey = (t_base, outcome_norm)
        t_ts = _parse_ts(t.get("timestamp", ""))

        best = None
        best_delta = float("inf")
        for m in fuzzy_lookup.get(fkey, []):
            if id(m) in claimed:
                continue
            m_ts = _parse_ts(m.get("timestamp", ""))
            if t_ts and m_ts:
                delta = abs((t_ts - m_ts).total_seconds())
            else:
                # No timestamp — fall back to entry price proximity
                delta = abs((m.get("entry_price", 0) or 0) - (t["entry_price"] or 0)) * 1e6
            if delta < best_delta:
                best_delta = delta
                best = m

        if best is not None:
            claimed.add(id(best))
            our_price = best.get("entry_price", 0)
            target_price = t["entry_price"]
            price_diff = abs(our_price - target_price) if our_price and target_price else None
            pct_diff = (price_diff / target_price * 100) if target_price and price_diff is not None else None
            status = "FUZZY_MATCHED"
            if pct_diff is not None and pct_diff > 5:
                status = f"FUZZY_PRICE_DIFF ({pct_diff:.1f}%)"
            rows.append(_make_row(t, best, status))
            continue

        # No match at all — target traded but we have no copy
        rows.append({
            "target_timestamp": t["timestamp"],
            "target_market": t["market"],
            "target_outcome": t["outcome"],
            "target_entry_price": t["entry_price"],
            "target_usdc": t["usdc_size"],
            "target_shares": t["shares"],
            "our_timestamp": "",
            "our_outcome": "",
            "our_entry_price": "",
            "our_amount": "",
            "our_result": "",
            "our_pnl": "",
            "status": "MISSED",
            "condition_id": cid,
        })

    # --- Leftover unclaimed positions ---
    # Build set of base market names the target traded for fuzzy classification
    target_bases = set()
    for t in target_trades:
        t_market = t.get("market") or t.get("slug") or ""
        target_bases.add(_base_market_name(t_market))
    target_condition_ids = set(t["condition_id"] for t in target_trades)

    for key, positions_list in our_lookup.items():
        cid = key[0]
        for m in positions_list:
            if id(m) not in claimed:
                market_title = m.get("market") or m.get("slug") or ""
                m_base = _base_market_name(market_title)

                if cid in target_condition_ids:
                    status = "EXTRA_COPY"
                elif m_base in target_bases:
                    # Same asset, different time window, but no target trade
                    # was close enough to claim it
                    status = "EXTRA_COPY"
                else:
                    status = "UNMATCHED_WINDOW"

                rows.append({
                    "target_timestamp": "",
                    "target_market": m.get("market", ""),
                    "target_outcome": "",
                    "target_entry_price": "",
                    "target_usdc": "",
                    "target_shares": "",
                    "our_timestamp": m.get("timestamp", ""),
                    "our_outcome": m.get("outcome", ""),
                    "our_entry_price": m.get("entry_price", ""),
                    "our_amount": m.get("amount", 0),
                    "our_result": m.get("result", "OPEN"),
                    "our_pnl": m.get("pnl", ""),
                    "status": status,
                    "condition_id": cid,
                })

    # Summary stats
    from collections import Counter
    status_counts = Counter(r["status"] for r in rows)
    print(f"[RECONCILE] Results: {dict(status_counts)}", flush=True)

    return rows


# =============================================================================
# MAIN
# =============================================================================

def main():
    global BET_AMOUNT

    parser = argparse.ArgumentParser(description="Poly Algo - algorithmic trader for Polymarket crypto markets")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry run)")
    parser.add_argument("--loop", action="store_true", help="Continuous monitoring mode")
    parser.add_argument("--all-markets", action="store_true", help="Copy all markets (not just crypto)")
    parser.add_argument("--amount", type=float, default=BET_AMOUNT, help=f"Bet amount (default: ${BET_AMOUNT})")
    args = parser.parse_args()

    # Update bet amount if specified
    BET_AMOUNT = args.amount

    # Create copy trader
    trader = CopyTrader(
        dry_run=not args.live,
        crypto_only=not args.all_markets
    )

    # Run
    if args.loop:
        trader.run_loop()
    else:
        trader.run_once()


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"\n[ALGO] Polymarket API Error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"\n[ALGO] Error: {e}")
        raise
