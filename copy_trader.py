#!/usr/bin/env python3
"""
COPY TRADER - Follow a specific trader's crypto bets
=====================================================

Monitors a target trader's activity and copies their trades on crypto 15-min markets.

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
import argparse
import requests
from datetime import datetime, timezone
from typing import Optional

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
PROFILE_API = "https://gamma-api.polymarket.com"


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
    """Check if bet is on a crypto 15-min market"""
    slug = bet.get("slug", "").lower()
    title = bet.get("title", "").lower()

    # Check for crypto market indicators
    for pattern in CRYPTO_SLUGS:
        if pattern in slug or pattern in title:
            return True

    # Also check for specific crypto keywords
    crypto_keywords = ["bitcoin", "ethereum", "solana", "xrp", "15m", "15-min", "15 min"]
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

    def __init__(self, dry_run: bool = True, crypto_only: bool = True):
        self.dry_run = dry_run
        self.crypto_only = crypto_only
        self.client: Optional["ClobClient"] = None
        self.copied_trades: set = set()  # Track copied trade IDs
        self.target_name = get_profile_name(TARGET_ADDRESS)

        # Stats
        self.trades_copied = 0
        self.trades_skipped = 0
        self.total_spent = 0.0

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

            if self.dry_run:
                print(f"       DRY RUN - would execute")
                copied += 1
            else:
                token_id = bet.get("asset", "")
                if token_id and self.client:
                    success = place_bet(self.client, token_id, BET_AMOUNT)
                    if success:
                        print(f"       EXECUTED!")
                        copied += 1
                        self.total_spent += BET_AMOUNT
                    else:
                        print(f"       FAILED!")
                else:
                    print(f"       No token ID or client")

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

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n[COPY] Stopping...")
            self.print_stats()

    def print_stats(self):
        """Print trading statistics"""
        print("\n" + "-" * 40)
        print(f"  Trades copied: {self.trades_copied}")
        print(f"  Trades skipped: {self.trades_skipped}")
        if not self.dry_run:
            print(f"  Total spent: ${self.total_spent:.2f}")
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
