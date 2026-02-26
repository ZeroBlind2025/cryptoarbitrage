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

# Crypto market filter - only copy trades on these markets
CRYPTO_SLUGS = ["btc-", "eth-", "sol-", "xrp-", "-updown-"]

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
    """Check if a market has resolved and get the winning outcome"""
    try:
        print(f"[ALGO] Checking resolution: token={token_id[:20] if token_id else 'none'}... cid={condition_id[:20] if condition_id else 'none'}...")

        # ONLY method that works: Check target trader's position redeemable status
        # Token prices are unreliable (winners can show 0 due to no liquidity)
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
                        if prices[high_idx] >= 0.99:
                            return {
                                "resolved": True,
                                "winning_outcome": outcomes[high_idx],
                                "winning_index": high_idx,
                            }

                # Market closed but can't determine winner reliably
                return {"resolved": True, "winning_outcome": None, "winning_index": None}

        return {"resolved": False}

    except Exception as e:
        identifier = condition_id[:20] if condition_id else slug[:20] if slug else "unknown"
        print(f"[ALGO] Error checking resolution for {identifier}...: {e}")
        return {"resolved": False}


# =============================================================================
# API FUNCTIONS
# =============================================================================

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


def get_latest_bets(wallet_address: str, limit: int = 20, verbose: bool = False) -> list:
    """Get recent buy trades for a wallet"""
    try:
        url = f"{DATA_API}/activity"
        params = {"user": wallet_address, "limit": limit}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()
        if verbose or not data:
            print(f"[ALGO] API {url}?user={wallet_address[:12]}...&limit={limit} => {len(data)} activities", flush=True)

        bets = []
        for activity in data:
            if activity.get("type") == "TRADE" and activity.get("side") == "BUY":
                bets.append(activity)
        if verbose:
            print(f"[ALGO] Filtered to {len(bets)} BUY trades", flush=True)
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
        "bitcoin", "ethereum", "solana", "xrp",
        "15m", "15-min", "15 min",
        "30m", "30-min", "30 min",
        "60m", "60-min", "60 min", "1h", "1-hour", "1 hour"
    ]
    for kw in crypto_keywords:
        if kw in title:
            return True

    return False


def has_opposite_position(my_positions: list, condition_id: str, outcome_index: int) -> bool:
    """Check if we already have the OPPOSITE side of this market.
    Allows stacking into the same side (UP, UP, UP) but blocks buying DOWN when we hold UP."""
    if not condition_id:
        return False
    for p in my_positions:
        if p.get('conditionId', '') == condition_id and p.get('outcomeIndex') != outcome_index:
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


def place_bet(client: "ClobClient", token_id: str, amount: float) -> bool:
    """Place a market buy order"""
    try:
        order = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
            order_type=OrderType.FOK
        )
        signed_order = client.create_market_order(order)
        result = client.post_order(signed_order, OrderType.FOK)
        return True
    except Exception as e:
        print(f"[ALGO] Order error: {e}")
        return False


# =============================================================================
# COPY TRADING LOGIC
# =============================================================================

class CopyTrader:
    """Copy trading engine"""

    def __init__(self, dry_run: bool = True, crypto_only: bool = True, on_trade: Optional[callable] = None, on_resolution: Optional[callable] = None, bet_amount: Optional[float] = None):
        self.dry_run = dry_run
        self.crypto_only = crypto_only
        self.client: Optional["ClobClient"] = None
        self.copied_trades: set = set()  # Track copied trade IDs
        self.copied_sizes: set = set()  # Track (condition_id, target_size) to dedup re-scans
        self.target_name = get_profile_name(TARGET_ADDRESS)
        self.on_trade = on_trade  # Callback for dashboard integration
        self.on_resolution = on_resolution  # Callback when position resolves

        # Configurable trade amount (can be changed at runtime via dashboard)
        self.bet_amount = bet_amount if bet_amount is not None else BET_AMOUNT

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

    def start(self):
        """Initialize the algo trader"""
        balance = self.positions.get("stats", {}).get("balance", ALGO_STARTING_BALANCE)
        print("\n" + "=" * 60)
        print(f"  POLY ALGO")
        print(f"  Following: {self.target_name}")
        print(f"  Target: {TARGET_ADDRESS[:20]}...")
        print(f"  Bet amount: ${self.bet_amount}")
        print(f"  Balance: ${balance:.2f}")
        print(f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Filter: {'Crypto only' if self.crypto_only else 'All markets'}")
        print("=" * 60 + "\n")

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[ALGO] Failed to initialize client. Running in dry-run mode.")
                self.dry_run = True

        # Snapshot existing trades so we don't copy historical trades
        # Only copy NEW trades that happen AFTER we start monitoring
        print("[ALGO] Loading existing trades to avoid duplicates...")
        existing_bets = get_latest_bets(TARGET_ADDRESS, limit=50)
        for bet in existing_bets:
            trade_id = bet.get("id") or f"{bet.get('conditionId')}_{bet.get('timestamp')}"
            self.copied_trades.add(trade_id)
            # Also snapshot target sizes so we don't re-copy existing positions
            cid = bet.get("conditionId") or bet.get("condition_id") or ""
            target_size = bet.get("size", 0)
            if cid and target_size:
                self.copied_sizes.add((cid, str(round(float(target_size), 2))))
        print(f"[ALGO] Marked {len(self.copied_trades)} existing trades ({len(self.copied_sizes)} unique sizes) as seen. Waiting for NEW trades...")

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

    def check_and_copy(self) -> int:
        """Check for new trades and copy them. Returns number of trades copied."""
        copied = 0

        # Get target's recent bets (verbose on first call)
        first_scan = not hasattr(self, '_first_scan_done')
        bets = get_latest_bets(TARGET_ADDRESS, verbose=first_scan)
        if first_scan:
            self._first_scan_done = True
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
            # This is the TRUE entry price. The 'price' field from activity API is current price, not entry price!
            price = None
            usdc_size = bet.get('usdcSize') or bet.get('usdc_size') or bet.get('amount')
            shares = bet.get('size')

            print(f"[ALGO] Bet fields: {list(bet.keys())}")
            print(f"[ALGO] Bet values: usdcSize={usdc_size}, size={shares}, price_field={bet.get('price')}")

            if usdc_size and shares:
                try:
                    usdc_size = float(usdc_size)
                    shares = float(shares)
                    if shares > 0:
                        price = usdc_size / shares
                        print(f"[ALGO] Entry price calculated: ${usdc_size} / {shares} shares = {price:.4f} ({price*100:.1f}¢)")
                except (ValueError, TypeError):
                    pass

            # Fallback to price field only if calculation failed (shouldn't happen)
            if not price or price <= 0:
                raw_price = bet.get('price')
                if raw_price:
                    try:
                        price = float(raw_price)
                        print(f"[ALGO] WARNING: Using price field as fallback: {price}")
                    except (ValueError, TypeError):
                        pass

            # Final fallback
            if not price or price <= 0:
                price = 0
                print(f"[ALGO] WARNING: Could not determine entry price, defaulting to 0")

            # Filter for crypto markets only
            if self.crypto_only and not is_crypto_market(bet):
                print(f"[ALGO] Skip (not crypto): {title}")
                self.trades_skipped += 1
                continue

            # No price filter - copy all trades to match target trader's strategy
            # Target trades at low prices (20-30¢) for 4-5x returns

            # Check if we already have this position
            # Try multiple field names for condition_id (API may use camelCase or snake_case)
            condition_id = bet.get("conditionId") or bet.get("condition_id") or bet.get("market_condition_id") or ""
            outcome_index = bet.get("outcomeIndex") or bet.get("outcome_index") or 0

            if has_opposite_position(my_positions, condition_id, outcome_index):
                print(f"[ALGO] Skip (opposite side on-chain): {title}")
                self.trades_skipped += 1
                continue

            # Also check our local open positions (covers indexing delay)
            if condition_id and any(
                op.get("condition_id") == condition_id and op.get("outcome_index") != outcome_index
                for op in self.positions.get("open", [])
            ):
                print(f"[ALGO] Skip (opposite side tracked): {title}")
                self.trades_skipped += 1
                continue

            # Dedup by target share count — trader never repeats the same size on a market.
            # If we've already copied this (condition, size) combo, it's our scan loop
            # re-seeing the same position, not a new entry.
            target_size = bet.get("size", 0)
            if condition_id and target_size:
                size_key = (condition_id, str(round(float(target_size), 2)))
                if size_key in self.copied_sizes:
                    print(f"[ALGO] Skip (same size already copied): {title} | {target_size} shares")
                    self.trades_skipped += 1
                    continue
                self.copied_sizes.add(size_key)

            # Copy the trade!
            print(f"\n[ALGO] NEW TRADE DETECTED!")
            print(f"       Market: {title}")
            print(f"       Target bought: {size:.1f} {outcome} @ {price*100:.1f}¢")
            print(f"       Copying: ${self.bet_amount:.2f} of {outcome}")

            # Build trade record for dashboard
            trade_record = {
                "id": f"copy_{trade_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": slug,
                "outcome": outcome,
                "side": "BUY",
                "amount": self.bet_amount,
                "price": price,
                "target_trader": self.target_name,
                "target_size": size,
                "source": "copy_trader",
            }

            if self.dry_run:
                print(f"       DRY RUN - would execute")
                trade_record["status"] = "dry_run"
                copied += 1
            else:
                token_id = bet.get("asset", "")
                if token_id and self.client:
                    success = place_bet(self.client, token_id, self.bet_amount)
                    if success:
                        print(f"       EXECUTED!")
                        trade_record["status"] = "filled"
                        copied += 1
                        self.total_spent += self.bet_amount
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
                    "amount": self.bet_amount,
                    "potential_payout": self.bet_amount / price if price > 0 else 0,
                    "dry_run": self.dry_run,
                }
                self.positions["open"].append(position)

                # Deduct balance (non-fatal — must never break trading)
                try:
                    stats = self.positions["stats"]
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) - self.bet_amount
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

    def run_loop(self):
        """Continuous monitoring loop"""
        self.start()

        print(f"[ALGO] Starting continuous monitoring (every {POLL_INTERVAL}s)...")
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
                    elif entry_price > 0.95:
                        # Suspiciously high price (>95%) - likely bad data
                        pnl = amount * 0.05  # Minimal win
                        print(f"[ALGO] WIN (high price): {position['market'][:30]} | entry={entry_price:.2f}, +${pnl:.2f}")
                    else:
                        # Normal calculation
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
                    # Unknown result
                    pnl = 0
                    position["result"] = "UNKNOWN"
                    position["pnl"] = 0
                    print(f"[ALGO] RESOLVED (unknown): {position['market'][:30]}")

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

    @staticmethod
    def _safe_float(v, default=0.0):
        """Ensure a float is JSON-safe (no NaN/Inf)"""
        import math
        if not isinstance(v, (int, float)):
            return default
        if math.isnan(v) or math.isinf(v):
            return default
        return v

    def get_stats(self) -> dict:
        """Get current statistics for dashboard"""
        stats = self.positions.get("stats", {})
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = self._safe_float(stats.get("total_pnl", 0.0))
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        balance = self._safe_float(stats.get("balance", ALGO_STARTING_BALANCE))
        # Cap balance_history to last 500 entries to keep API responses fast
        balance_history = stats.get("balance_history", [])[-500:]

        open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []))
        equity = self._safe_float(balance + open_staked)

        return {
            "trades_copied": self.trades_copied,
            "trades_skipped": self.trades_skipped,
            "total_spent": self._safe_float(self.total_spent),
            "open_positions": len(self.positions.get("open", [])),
            "resolved_positions": len(self.positions.get("resolved", [])),
            "wins": wins,
            "losses": losses,
            "win_rate": self._safe_float(win_rate),
            "total_pnl": total_pnl,
            "dry_run": self.dry_run,
            "balance": balance,
            "equity": equity,
            "balance_history": balance_history,
            "bet_amount": self._safe_float(self.bet_amount),
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
