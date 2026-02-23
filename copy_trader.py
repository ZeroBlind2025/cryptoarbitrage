#!/usr/bin/env python3
"""
COPY TRADER - Follow a specific trader's crypto bets
=====================================================

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
    print("[COPY] Warning: py_clob_client not installed. Install with: pip install py-clob-client")


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
                return json.load(f)
        except Exception as e:
            print(f"[COPY] Error loading positions: {e}")
    return {"open": [], "resolved": [], "stats": {"wins": 0, "losses": 0, "total_pnl": 0.0}}


def save_positions(positions: dict):
    """Save positions to file"""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"[COPY] Error saving positions: {e}")


def get_market_resolution(condition_id: str) -> Optional[dict]:
    """Check if a market has resolved and get the winning outcome"""
    try:
        # Try gamma API for market info
        response = requests.get(
            f"{GAMMA_API}/markets",
            params={"condition_ids": condition_id},
            timeout=10
        )
        response.raise_for_status()
        markets = response.json()

        if markets and len(markets) > 0:
            market = markets[0]
            # Check if resolved
            if market.get("closed") or market.get("resolved"):
                # Get winning outcome
                outcomes = market.get("outcomes", [])
                outcome_prices = market.get("outcomePrices", [])

                # If resolved, one outcome will be $1.00 and others $0.00
                for i, price in enumerate(outcome_prices):
                    try:
                        if float(price) >= 0.99:  # Winner
                            return {
                                "resolved": True,
                                "winning_outcome": outcomes[i] if i < len(outcomes) else f"outcome_{i}",
                                "winning_index": i,
                            }
                    except (ValueError, TypeError):
                        continue

                # Market closed but can't determine winner
                return {"resolved": True, "winning_outcome": None, "winning_index": None}

        return {"resolved": False}

    except Exception as e:
        print(f"[COPY] Error checking resolution for {condition_id[:20]}...: {e}")
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
        print(f"[COPY] Error fetching positions: {e}")
        return []


def get_latest_bets(wallet_address: str, limit: int = 20) -> list:
    """Get recent buy trades for a wallet"""
    try:
        response = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet_address, "limit": limit},
            timeout=10
        )
        response.raise_for_status()

        bets = []
        for activity in response.json():
            if activity.get("type") == "TRADE" and activity.get("side") == "BUY":
                bets.append(activity)
        return bets
    except Exception as e:
        print(f"[COPY] Error fetching activity: {e}")
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


def already_has_position(my_positions: list, condition_id: str, outcome_index: int) -> bool:
    """Check if we already have this position"""
    target_key = f"{condition_id}_{outcome_index}"
    my_keys = {f"{p['conditionId']}_{p['outcomeIndex']}" for p in my_positions}
    return target_key in my_keys


def get_clob_client() -> Optional["ClobClient"]:
    """Initialize CLOB client with credentials"""
    if not HAS_CLOB_CLIENT:
        return None

    if not PRIVATE_KEY:
        print("[COPY] Error: POLYGON_PRIVATE_KEY not set in environment")
        return None

    if not FUNDER_ADDRESS:
        print("[COPY] Error: POLYMARKET_FUNDER_ADDRESS not set in environment")
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
        print(f"[COPY] Error initializing CLOB client: {e}")
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
        print(f"[COPY] Order error: {e}")
        return False


# =============================================================================
# COPY TRADING LOGIC
# =============================================================================

class CopyTrader:
    """Copy trading engine"""

    def __init__(self, dry_run: bool = True, crypto_only: bool = True, on_trade: Optional[callable] = None, on_resolution: Optional[callable] = None):
        self.dry_run = dry_run
        self.crypto_only = crypto_only
        self.client: Optional["ClobClient"] = None
        self.copied_trades: set = set()  # Track copied trade IDs
        self.target_name = get_profile_name(TARGET_ADDRESS)
        self.on_trade = on_trade  # Callback for dashboard integration
        self.on_resolution = on_resolution  # Callback when position resolves

        # Stats
        self.trades_copied = 0
        self.trades_skipped = 0
        self.total_spent = 0.0

        # Trade history (for dashboard)
        self.trade_history: list = []

        # Position tracking (persisted to file)
        self.positions = load_positions()
        self.last_resolution_check = 0
        self.resolution_check_interval = 60  # Check every 60 seconds

    def start(self):
        """Initialize the copy trader"""
        print("\n" + "=" * 60)
        print(f"  COPY TRADER")
        print(f"  Following: {self.target_name}")
        print(f"  Target: {TARGET_ADDRESS[:20]}...")
        print(f"  Bet amount: ${BET_AMOUNT}")
        print(f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"  Filter: {'Crypto only' if self.crypto_only else 'All markets'}")
        print("=" * 60 + "\n")

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[COPY] Failed to initialize client. Running in dry-run mode.")
                self.dry_run = True

        # Snapshot existing trades so we don't copy historical trades
        # Only copy NEW trades that happen AFTER we start monitoring
        print("[COPY] Loading existing trades to avoid duplicates...")
        existing_bets = get_latest_bets(TARGET_ADDRESS, limit=50)
        for bet in existing_bets:
            trade_id = bet.get("id") or f"{bet.get('conditionId')}_{bet.get('timestamp')}"
            self.copied_trades.add(trade_id)
        print(f"[COPY] Marked {len(self.copied_trades)} existing trades as seen. Waiting for NEW trades...")

        # Show position tracking stats
        stats = self.positions.get("stats", {})
        open_count = len(self.positions.get("open", []))
        resolved_count = len(self.positions.get("resolved", []))
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = stats.get("total_pnl", 0.0)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        print(f"[COPY] Positions: {open_count} open, {resolved_count} resolved")
        print(f"[COPY] Record: {wins}W / {losses}L ({win_rate:.1f}% win rate)")
        print(f"[COPY] Total PnL: ${total_pnl:+.2f}")

    def check_and_copy(self) -> int:
        """Check for new trades and copy them. Returns number of trades copied."""
        copied = 0

        # Get target's recent bets
        bets = get_latest_bets(TARGET_ADDRESS)
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
            price = bet.get("price", 0)
            slug = bet.get("slug", "")

            # Filter for crypto markets only
            if self.crypto_only and not is_crypto_market(bet):
                print(f"[COPY] Skip (not crypto): {title}")
                self.trades_skipped += 1
                continue

            # Check if we already have this position
            condition_id = bet.get("conditionId", "")
            outcome_index = bet.get("outcomeIndex", 0)

            if already_has_position(my_positions, condition_id, outcome_index):
                print(f"[COPY] Skip (already own): {title}")
                self.trades_skipped += 1
                continue

            # Copy the trade!
            print(f"\n[COPY] NEW TRADE DETECTED!")
            print(f"       Market: {title}")
            print(f"       Target bought: {size:.1f} {outcome} @ {price*100:.1f}¢")
            print(f"       Copying: ${BET_AMOUNT:.2f} of {outcome}")

            # Build trade record for dashboard
            trade_record = {
                "id": f"copy_{trade_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": slug,
                "outcome": outcome,
                "side": "BUY",
                "amount": BET_AMOUNT,
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
                    success = place_bet(self.client, token_id, BET_AMOUNT)
                    if success:
                        print(f"       EXECUTED!")
                        trade_record["status"] = "filled"
                        copied += 1
                        self.total_spent += BET_AMOUNT
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
                position = {
                    "id": trade_record["id"],
                    "timestamp": trade_record["timestamp"],
                    "condition_id": condition_id,
                    "outcome_index": outcome_index,
                    "outcome": outcome,
                    "market": title,
                    "slug": slug,
                    "entry_price": price,
                    "amount": BET_AMOUNT,
                    "potential_payout": BET_AMOUNT / price if price > 0 else 0,
                    "dry_run": self.dry_run,
                }
                self.positions["open"].append(position)
                save_positions(self.positions)
                print(f"       Position saved. Potential payout: ${position['potential_payout']:.2f}")

            # Call dashboard callback
            if self.on_trade:
                try:
                    self.on_trade(trade_record)
                except Exception as e:
                    print(f"[COPY] Callback error: {e}")

            self.trades_copied += copied

        return copied

    def run_once(self):
        """Single check iteration"""
        self.start()

        print("[COPY] Checking for trades to copy...")
        copied = self.check_and_copy()

        if copied > 0:
            print(f"\n[COPY] Copied {copied} trade(s)")
        else:
            print("[COPY] No new trades to copy")

        self.print_stats()

    def run_loop(self):
        """Continuous monitoring loop"""
        self.start()

        print(f"[COPY] Starting continuous monitoring (every {POLL_INTERVAL}s)...")
        print("[COPY] Press Ctrl+C to stop\n")

        try:
            while True:
                copied = self.check_and_copy()
                if copied > 0:
                    print(f"[COPY] Copied {copied} trade(s) this cycle")

                # Periodically check for resolved positions
                self.check_resolutions()

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n[COPY] Stopping...")
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
            if not condition_id:
                continue

            result = get_market_resolution(condition_id)

            if result.get("resolved"):
                # Position resolved!
                winning_outcome = result.get("winning_outcome")
                winning_index = result.get("winning_index")
                our_outcome = position.get("outcome")
                our_index = position.get("outcome_index")
                entry_price = position.get("entry_price", 0)
                amount = position.get("amount", 0)

                # Debug logging
                print(f"[COPY] Checking resolution for: {position.get('market', '?')[:30]}")
                print(f"       Our outcome: '{our_outcome}' (index {our_index})")
                print(f"       Winning outcome: '{winning_outcome}' (index {winning_index})")

                # Compare by index first (more reliable), then by name
                if winning_index is not None and our_index is not None:
                    won = (winning_index == our_index)
                elif winning_outcome and our_outcome:
                    # Normalize for comparison (case insensitive)
                    won = winning_outcome.lower() == our_outcome.lower()
                else:
                    # Can't determine winner
                    won = None

                if won is True:
                    payout = amount / entry_price if entry_price > 0 else 0
                    pnl = payout - amount
                    position["result"] = "WIN"
                    position["pnl"] = pnl
                    self.positions["stats"]["wins"] = self.positions["stats"].get("wins", 0) + 1
                    print(f"[COPY] WIN: {position['market'][:30]} | +${pnl:.2f}")
                elif won is False:
                    pnl = -amount
                    position["result"] = "LOSS"
                    position["pnl"] = pnl
                    self.positions["stats"]["losses"] = self.positions["stats"].get("losses", 0) + 1
                    print(f"[COPY] LOSS: {position['market'][:30]} | -${amount:.2f}")
                else:
                    # Unknown result
                    pnl = 0
                    position["result"] = "UNKNOWN"
                    position["pnl"] = 0
                    print(f"[COPY] RESOLVED (unknown): {position['market'][:30]}")

                # Update totals
                self.positions["stats"]["total_pnl"] = self.positions["stats"].get("total_pnl", 0) + pnl

                # Move from open to resolved
                position["resolved_at"] = datetime.now(timezone.utc).isoformat()
                position["winning_outcome"] = winning_outcome
                self.positions["open"].remove(position)
                self.positions["resolved"].append(position)
                resolved_this_check += 1

                # Call resolution callback
                if self.on_resolution:
                    try:
                        self.on_resolution(position)
                    except Exception as e:
                        print(f"[COPY] Resolution callback error: {e}")

        if resolved_this_check > 0:
            save_positions(self.positions)
            stats = self.positions["stats"]
            print(f"[COPY] {resolved_this_check} position(s) resolved. Record: {stats['wins']}W/{stats['losses']}L, PnL: ${stats['total_pnl']:+.2f}")

    def get_stats(self) -> dict:
        """Get current statistics for dashboard"""
        stats = self.positions.get("stats", {})
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total_pnl = stats.get("total_pnl", 0.0)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        return {
            "trades_copied": self.trades_copied,
            "trades_skipped": self.trades_skipped,
            "total_spent": self.total_spent,
            "open_positions": len(self.positions.get("open", [])),
            "resolved_positions": len(self.positions.get("resolved", [])),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "dry_run": self.dry_run,
        }

    def print_stats(self):
        """Print trading statistics"""
        stats = self.get_stats()
        print("\n" + "-" * 40)
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

    parser = argparse.ArgumentParser(description="Copy trader for Polymarket crypto markets")
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
        print(f"\n[COPY] Polymarket API Error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"\n[COPY] Error: {e}")
        raise
