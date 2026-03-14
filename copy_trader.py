#!/usr/bin/env python3
"""
POLY ALGO - Algorithmic trader following a specific trader's crypto bets
========================================================================

Monitors a target trader's activity and copies their trades on crypto markets
(15, 30, and 60 minute timeframes).

Target: configured via COPY_TARGET_ADDRESS env var (default: 0xd0d6053c3c37e727402d84c14069780d360993aa)

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
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    print("[ALGO] Warning: py_clob_client not installed. Install with: pip install py-clob-client")

try:
    from clob_ws import CLOBWebSocket
    HAS_CLOB_WS = True
except ImportError:
    HAS_CLOB_WS = False

try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False


# =============================================================================
# CONFIGURATION
# =============================================================================

# Target trader to copy
TARGET_ADDRESS = os.getenv("COPY_TARGET_ADDRESS", "0xd0d6053c3c37e727402d84c14069780d360993aa")

# Your credentials (from environment)
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", os.getenv("POLYGON_ADDRESS", ""))
PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))  # 0=EOA, 1=Poly proxy, 2=Gnosis Safe (most common)

# Relayer API Key (simplest auth — create at polymarket.com/settings?tab=api-keys)
# Just needs RELAYER_API_KEY + your EOA address. No rate limits from shared signer.
RELAYER_API_KEY = os.getenv("POLYMARKET_RELAYER_API_KEY", "")

# Builder credentials (from polymarket.com/settings?tab=builder)
# IMPORTANT: These are BUILDER creds, NOT CLOB API creds. Get them from polymarket.com/settings?tab=builder
# Set POLYMARKET_BUILDER_ENABLED=true to use builder mode (not needed for basic trading)
BUILDER_ENABLED = os.getenv("POLYMARKET_BUILDER_ENABLED", "false").lower() == "true"
BUILDER_API_KEY = os.getenv("POLYMARKET_BUILDER_API_KEY", os.getenv("POLYMARKET_API_KEY", ""))
BUILDER_API_SECRET = os.getenv("POLYMARKET_BUILDER_API_SECRET", os.getenv("POLYMARKET_API_SECRET", ""))
BUILDER_API_PASSPHRASE = os.getenv("POLYMARKET_BUILDER_API_PASSPHRASE", os.getenv("POLYMARKET_API_PASSPHRASE", ""))

# Trading settings
BET_AMOUNT = float(os.getenv("COPY_BET_AMOUNT", "5.0"))  # $ per copied bet
PROBE_AMOUNT = float(os.getenv("PROBE_AMOUNT", "10.0"))   # $ for first (probe) entry into a new market
POLL_INTERVAL = int(os.getenv("COPY_POLL_INTERVAL", "10"))  # seconds between checks
ALGO_STARTING_BALANCE = float(os.getenv("ALGO_STARTING_BALANCE", "2300.0"))  # Starting balance for Poly Algo
PRICE_BUFFER_BPS = int(os.getenv("COPY_PRICE_BUFFER_BPS", "50"))  # Max overbid vs target's price (50 bps = 0.5%)
FOLLOW_UP_COOLDOWN = int(os.getenv("COPY_FOLLOW_UP_COOLDOWN", "30"))  # seconds between re-entries into same market
STOP_LOSS_PCT = float(os.getenv("COPY_STOP_LOSS_PCT", "25"))  # Auto-sell when position drops this % from peak (0 = disabled)

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

# Persistent trade log — one JSON object per line, append-only
TRADE_LOG = Path(__file__).parent / "copy_trades.jsonl"


def _log_copy_trade(event_type: str, data: dict):
    """Append a trade event to copy_trades.jsonl.

    event_type: 'buy', 'sell', 'resolved', 'dry_run_buy', 'dry_run_sell', 'failed'
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    try:
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


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

        # Determine if OUR token won — compare with normalization
        winning_token_id = str(winning_token.get("token_id", "")).strip()
        our_tid = str(token_id).strip() if token_id else ""
        our_token_won = (our_tid == winning_token_id) if our_tid else None

        # Log enough detail to diagnose mismatches
        if our_tid and not our_token_won:
            print(f"[ALGO] CLOB token mismatch: ours={our_tid[:30]}... vs winner={winning_token_id[:30]}...")
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
                        # Price extreme but not redeemable yet — oracle hasn't settled.
                        # Don't mark resolved; keep checking so we redeem when ready.
                        print(f"[ALGO] Price extreme ({cur_price}) but not redeemable yet — waiting for oracle")
                        return None
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
# ON-CHAIN REDEMPTION
# =============================================================================

# Conditional Tokens Framework contract — holds all outcome tokens
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
# Polymarket uses native USDC as collateral
USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
# Bridged USDC.e (legacy — some older markets use this)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
HASH_ZERO = b"\x00" * 32

# Minimal ABI for CTF redeemPositions
CTF_REDEEM_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]""")

# Minimal ABI for NegRiskAdapter redeemPositions + getConditionId
NEG_RISK_REDEEM_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "_questionId", "type": "bytes32"}
        ],
        "name": "getConditionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# Minimal ABI for ERC1155 balanceOf + isApprovedForAll + setApprovalForAll
ERC1155_BALANCE_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"}
        ],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# Gnosis Safe minimal ABI — needed to execute redemptions through the proxy wallet
GNOSIS_SAFE_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"}
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"}
        ],
        "name": "getTransactionHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")


POLYGON_RPC_FALLBACKS = [
    "https://polygon-mainnet.g.alchemy.com/v2/S3PJkQkcYoIJiE9iaUxFc",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]


def _get_web3():
    """Lazy-initialize a Web3 connection to Polygon with fallback RPCs."""
    if not HAS_WEB3:
        return None

    # Try user-configured RPC first, then fallbacks
    rpc_urls = []
    user_rpc = os.getenv("POLYGON_RPC_URL", "")
    if user_rpc:
        rpc_urls.append(user_rpc)
    rpc_urls.extend(POLYGON_RPC_FALLBACKS)

    for rpc_url in rpc_urls:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                print(f"[RPC] Connected via {rpc_url[:40]}...")
                return w3
        except Exception:
            continue

    print("[RPC] All Polygon RPC endpoints failed")
    return None


def _check_neg_risk(condition_id: str, slug: str = "") -> bool:
    """Check if a market is neg_risk by querying the CLOB API.

    Falls back to slug-based detection for crypto updown markets which
    are always neg_risk.  This covers cases where the CLOB API doesn't
    return the neg_risk field (e.g. resolved/closed markets).
    """
    # Fast path: crypto updown markets are always neg_risk
    if slug:
        s = slug.lower()
        if any(pattern in s for pattern in ["-updown-", "updown-5m", "updown-15m", "updown-30m", "updown-60m"]):
            return True

    try:
        resp = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            neg = data.get("neg_risk", None)
            if neg is not None:
                return bool(neg)
    except Exception:
        pass

    # Conservative fallback: treat as neg_risk if condition_id lookup failed
    # Most Polymarket binary markets are neg_risk
    return True


# Polymarket gasless relay — pays gas so the EOA doesn't need MATIC
RELAY_URL = "https://relayer-v2.polymarket.com"

# Rate limiter for relayer /submit — 25 req/min limit, we target 24 req/min (2.5s gap)
_relay_submit_times: list = []  # timestamps of recent /submit calls
_RELAY_MAX_PER_MINUTE = 24  # stay just under the 25/min limit
_RELAY_WINDOW = 60.0  # sliding window in seconds
_RELAY_MIN_GAP = 2.5  # minimum seconds between /submit calls

# Daily transaction cap — Unverified Builder tier = 100 tx/day, Verified = 3000 tx/day
# Set via RELAY_DAILY_LIMIT env var; defaults to 90 (leaves 10 tx buffer for manual use)
_RELAY_DAILY_LIMIT = int(os.getenv("RELAY_DAILY_LIMIT", "90"))
_relay_daily_count = 0       # transactions submitted today
_relay_daily_date = ""       # date string (YYYY-MM-DD UTC) for current count


def _relay_daily_remaining() -> int:
    """Return how many relay transactions remain for today (UTC)."""
    global _relay_daily_count, _relay_daily_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _relay_daily_date != today:
        # New UTC day — reset counter
        _relay_daily_count = 0
        _relay_daily_date = today
    return max(0, _RELAY_DAILY_LIMIT - _relay_daily_count)


def _relay_daily_increment():
    """Record one relay transaction for today's daily counter."""
    global _relay_daily_count, _relay_daily_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _relay_daily_date != today:
        _relay_daily_count = 0
        _relay_daily_date = today
    _relay_daily_count += 1
    remaining = _RELAY_DAILY_LIMIT - _relay_daily_count
    print(f"[RELAY] Daily usage: {_relay_daily_count}/{_RELAY_DAILY_LIMIT} (remaining: {remaining})")


def _relay_rate_limit_wait() -> bool:
    """Block until we can safely send another /submit request.

    Enforces:
      1. Daily transaction cap (default 90/day, configurable via RELAY_DAILY_LIMIT)
      2. Per-minute cap (24/min, under the 25/min hard limit)
      3. Minimum gap between calls (2.5s)

    Returns True if the request can proceed, False if the daily limit is exhausted.
    """
    import time as _t

    # Check daily limit first — no point waiting if we're already at the cap
    if _relay_daily_remaining() <= 0:
        next_reset = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # next_reset is today 00:00 UTC which is in the past; add 1 day
        from datetime import timedelta
        next_reset += timedelta(days=1)
        until_reset = (next_reset - datetime.now(timezone.utc)).total_seconds()
        hours = int(until_reset // 3600)
        mins = int((until_reset % 3600) // 60)
        print(f"[RELAY] DAILY LIMIT REACHED ({_relay_daily_count}/{_RELAY_DAILY_LIMIT}). "
              f"Resets in {hours}h {mins}m. Skipping relay — will queue for retry.")
        return False

    now = _t.time()

    # Purge timestamps outside the sliding window
    while _relay_submit_times and _relay_submit_times[0] < now - _RELAY_WINDOW:
        _relay_submit_times.pop(0)

    # Enforce minimum gap between calls
    if _relay_submit_times:
        gap = _RELAY_MIN_GAP - (now - _relay_submit_times[-1])
        if gap > 0:
            print(f"[RELAY] Rate limit: waiting {gap:.1f}s (min gap)")
            _t.sleep(gap)
            now = _t.time()

    # Enforce per-minute cap
    while len(_relay_submit_times) >= _RELAY_MAX_PER_MINUTE:
        oldest = _relay_submit_times[0]
        wait = (oldest + _RELAY_WINDOW) - now + 0.1  # +0.1s buffer
        if wait > 0:
            print(f"[RELAY] Rate limit: waiting {wait:.1f}s ({len(_relay_submit_times)}/{_RELAY_MAX_PER_MINUTE} in window)")
            _t.sleep(wait)
            now = _t.time()
        # Re-purge after sleeping
        while _relay_submit_times and _relay_submit_times[0] < now - _RELAY_WINDOW:
            _relay_submit_times.pop(0)

    _relay_submit_times.append(now)
    return True
RELAY_SIGN_URL = "https://builder-signing-server.vercel.app/sign"
ADDRESS_ZERO = "0x" + "00" * 20


def _sign_safe_tx(account, safe_contract, to: str, call_data: str, nonce: int, w3):
    """Sign a Gnosis Safe transaction hash using EIP-191 personal sign.

    Returns the hex signature with v-value adjusted for the Polymarket relay
    (v 0x00→0x1f, 0x01→0x20, 0x1b→0x1f, 0x1c→0x20).
    """
    from eth_account.messages import encode_defunct

    tx_hash_bytes = safe_contract.functions.getTransactionHash(
        w3.to_checksum_address(to),
        0,                                    # value
        call_data,                            # data (hex string)
        0,                                    # operation (Call)
        0, 0, 0,                              # safeTxGas, baseGas, gasPrice
        w3.to_checksum_address(ADDRESS_ZERO), # gasToken
        w3.to_checksum_address(ADDRESS_ZERO), # refundReceiver
        nonce,
    ).call()

    # Sign the hash as an EIP-191 personal message (eth_sign style)
    tx_hash_hex = tx_hash_bytes.hex()
    message = encode_defunct(hexstr=tx_hash_hex)
    signed = account.sign_message(message)

    # Adjust v for Gnosis Safe eth_sign verification
    sig_hex = signed.signature.hex()
    v_byte = sig_hex[-2:]
    if v_byte in ("00", "1b"):
        sig_hex = sig_hex[:-2] + "1f"
    elif v_byte in ("01", "1c"):
        sig_hex = sig_hex[:-2] + "20"

    return sig_hex


def _get_relay_headers(body_dict: dict) -> dict:
    """Get signed headers for the Polymarket gasless relay.

    Priority:
    1. Relayer API Key (simplest, from polymarket.com/settings?tab=api-keys)
    2. Builder creds (env vars) + derived L2 creds — own quota, no rate limits
    3. Shared signing server — generates both header sets, but rate-limited
    """
    import json as _json

    # --- Method 0: Relayer API Key (simplest, no HMAC needed) ---
    if RELAYER_API_KEY:
        eoa = os.getenv("POLYMARKET_ADDRESS", "")
        if not eoa and PRIVATE_KEY:
            try:
                from eth_account import Account
                eoa = Account.from_key(PRIVATE_KEY).address
            except Exception:
                pass
        if eoa:
            print(f"[REDEEM] Using Relayer API Key (key={RELAYER_API_KEY[:12]}...)")
            return {
                "RELAYER_API_KEY": RELAYER_API_KEY,
                "RELAYER_API_KEY_ADDRESS": eoa,
                "Content-Type": "application/json",
            }

    # --- Method 1: Builder credentials + L2 creds ---
    if BUILDER_ENABLED and BUILDER_API_KEY and BUILDER_API_SECRET and BUILDER_API_PASSPHRASE:
        try:
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
            from py_clob_client.signer import Signer
            from py_clob_client.headers.headers import create_level_2_headers
            from py_clob_client.clob_types import RequestArgs

            # Generate builder-specific headers (POLY_BUILDER_*)
            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=BUILDER_API_KEY,
                    secret=BUILDER_API_SECRET,
                    passphrase=BUILDER_API_PASSPHRASE,
                ),
            )
            body_str = _json.dumps(body_dict)
            builder_payload = builder_config.generate_builder_headers(
                "POST", "/submit", body_str,
            )
            builder_headers = builder_payload.to_dict() if builder_payload else {}

            # Generate L2 headers from derived CLOB creds
            l2_headers = {}
            if HAS_CLOB_CLIENT and PRIVATE_KEY:
                from py_clob_client.client import ClobClient
                if FUNDER_ADDRESS:
                    client = ClobClient(
                        CLOB_API, key=PRIVATE_KEY, chain_id=137,
                        signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS,
                    )
                else:
                    client = ClobClient(CLOB_API, key=PRIVATE_KEY, chain_id=137)
                creds = client.derive_api_key()
                if creds and creds.api_key:
                    signer = Signer(private_key=PRIVATE_KEY, chain_id=137)
                    l2_headers = create_level_2_headers(
                        signer=signer,
                        creds=creds,
                        request_args=RequestArgs(
                            method="POST",
                            request_path="/submit",
                            body=body_dict,
                        ),
                    )

            headers = {**l2_headers, **builder_headers}
            print(f"[REDEEM] Using builder creds (key={BUILDER_API_KEY[:12]}...)")
            return headers
        except Exception as e:
            print(f"[REDEEM] Builder creds failed ({e}), falling back to shared signer...")

    # --- Method 2: Shared signing server (with retry for 429) ---
    payload = {
        "method": "POST",
        "path": "/submit",
        "body": _json.dumps(body_dict),
    }
    try:
        resp = requests.post(RELAY_SIGN_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            print(f"[REDEEM] Shared signer rate-limited (429), skipping relay — will queue for retry")
        raise
    # --- Method 3: Shared signing server (rate-limited) ---
    payload = {
        "method": "POST",
        "path": "/submit",
        "body": _json.dumps(body_dict),
    }
    resp = requests.post(RELAY_SIGN_URL, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _redeem_via_relay(w3, account, safe_contract, proxy_address: str,
                      target_contract: str, call_data_hex: str) -> str:
    """Submit a redemption through Polymarket's gasless relay.

    The relay pays gas, so the EOA doesn't need MATIC.
    Returns the transaction hash hex string.
    """
    import json as _json

    eoa = account.address

    # Get nonce from relay
    nonce_resp = requests.get(
        f"{RELAY_URL}/nonce",
        params={"address": eoa, "type": "SAFE"},
        timeout=10,
    )
    nonce_resp.raise_for_status()
    safe_nonce = int(nonce_resp.json()["nonce"])
    print(f"[REDEEM] Relay safe nonce: {safe_nonce}")

    # Sign the Safe transaction
    sig_hex = _sign_safe_tx(
        account, safe_contract, target_contract, call_data_hex, safe_nonce, w3
    )

    body = {
        "data": call_data_hex,
        "from": eoa,
        "metadata": "redeem",
        "nonce": str(safe_nonce),
        "proxyWallet": proxy_address,
        "signature": "0x" + sig_hex if not sig_hex.startswith("0x") else sig_hex,
        "signatureParams": {
            "baseGas": "0",
            "gasPrice": "0",
            "gasToken": ADDRESS_ZERO,
            "operation": "0",
            "refundReceiver": ADDRESS_ZERO,
            "safeTxnGas": "0",
        },
        "to": w3.to_checksum_address(target_contract),
        "type": "SAFE",
    }

    headers = _get_relay_headers(body)

    # Enforce rate limit before hitting /submit (25 req/min + daily cap)
    if not _relay_rate_limit_wait():
        raise RuntimeError("Daily relay limit reached (429-prevention)")

    resp = requests.post(
        f"{RELAY_URL}/submit",
        headers=headers,
        data=_json.dumps(body).encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()

    # Count this successful submission against the daily cap
    _relay_daily_increment()

    result = resp.json()

    tx_hash = result.get("transactionHash")
    print(f"[REDEEM] Relay tx: {tx_hash} | state: {result.get('state', 'N/A')}")

    if tx_hash:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"Relay tx reverted: {tx_hash}")
        return tx_hash

    raise RuntimeError(f"No tx hash from relay: {result}")


def _exec_via_safe(w3, safe_address: str, to: str, call_data: bytes, account) -> str:
    """Execute a transaction through a Gnosis Safe proxy wallet (direct, needs MATIC).

    Fallback for when the gasless relay is unavailable.  The EOA must hold
    MATIC to pay gas.

    Returns the transaction hash hex string, or raises on failure.
    """
    from eth_account.messages import encode_defunct

    safe = w3.eth.contract(
        address=w3.to_checksum_address(safe_address),
        abi=GNOSIS_SAFE_ABI,
    )

    safe_nonce = safe.functions.nonce().call()
    print(f"[REDEEM] Direct Safe nonce (on-chain): {safe_nonce}")
    call_data_hex = "0x" + call_data.hex()

    tx_hash_bytes = safe.functions.getTransactionHash(
        w3.to_checksum_address(to),
        0, call_data_hex, 0,
        0, 0, 0,
        w3.to_checksum_address(ADDRESS_ZERO),
        w3.to_checksum_address(ADDRESS_ZERO),
        safe_nonce,
    ).call()

    # EIP-191 personal sign + v adjustment for Gnosis Safe
    message = encode_defunct(hexstr=tx_hash_bytes.hex())
    signed = account.sign_message(message)

    r = signed.r.to_bytes(32, "big")
    s = signed.s.to_bytes(32, "big")
    v = signed.v
    # Adjust v for eth_sign: +4 for 27/28, +31 for 0/1
    if v in (0, 1):
        v += 31
    elif v in (27, 28):
        v += 4
    signature_bytes = r + s + v.to_bytes(1, "big")

    eoa_nonce = w3.eth.get_transaction_count(account.address)
    tx = safe.functions.execTransaction(
        w3.to_checksum_address(to),
        0, call_data_hex,
        0, 0, 0, 0,
        w3.to_checksum_address(ADDRESS_ZERO),
        w3.to_checksum_address(ADDRESS_ZERO),
        signature_bytes,
    ).build_transaction({
        "chainId": 137,
        "from": account.address,
        "nonce": eoa_nonce,
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"[REDEEM] Safe tx sent: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Safe execTransaction reverted (tx={tx_hash.hex()})")
    return tx_hash.hex()


# Queue for redemptions that failed due to relay rate-limit / no gas.
# Each entry: {"condition_id": str, "token_id": str, "slug": str, "attempts": int, "next_retry": float}
_pending_redemptions: list = []
_PENDING_MAX_ATTEMPTS = 10  # give up after this many total attempts (conserve daily quota)


def _queue_pending_redemption(condition_id: str, token_id: str, slug: str, attempts: int = 1):
    """Add a failed redemption to the retry queue.

    Backoff is aggressive to conserve daily relay quota (100 tx/day on Unverified tier).
    First retry after 30 min, then exponential up to 2 hour cap.
    """
    import time as _t
    # Don't duplicate
    for item in _pending_redemptions:
        if item["condition_id"] == condition_id:
            item["attempts"] = attempts
            # Exponential backoff: 30min, 60min, 90min, 120min cap
            item["next_retry"] = _t.time() + min(1800 * attempts, 7200)
            return
    _pending_redemptions.append({
        "condition_id": condition_id,
        "token_id": token_id,
        "slug": slug,
        "attempts": attempts,
        "next_retry": _t.time() + 1800,  # 30 minutes before first retry
    })
    remaining = _relay_daily_remaining()
    print(f"[REDEEM] Queued for retry (attempt {attempts}): condition={condition_id[:20]}... "
          f"(daily relay remaining: {remaining})")


def retry_pending_redemptions(dry_run: bool = False):
    """Retry any queued redemptions whose backoff has elapsed. Call periodically.

    Checks daily relay quota before each retry to avoid wasting transactions.
    """
    import time as _t
    if not _pending_redemptions:
        return
    now = _t.time()
    remaining = _relay_daily_remaining()
    if remaining <= 0:
        print(f"[REDEEM] Daily relay limit reached — skipping {len(_pending_redemptions)} pending redemption(s)")
        return
    still_pending = []
    retried = 0
    for item in _pending_redemptions:
        if now < item["next_retry"]:
            still_pending.append(item)
            continue
        if item["attempts"] >= _PENDING_MAX_ATTEMPTS:
            print(f"[REDEEM] Giving up after {item['attempts']} attempts: {item['condition_id'][:20]}...")
            continue
        # Reserve some daily quota — don't burn all remaining on retries
        if _relay_daily_remaining() <= 5:
            print(f"[REDEEM] Only {_relay_daily_remaining()} relay tx left today — deferring remaining retries")
            still_pending.append(item)
            continue
        print(f"[REDEEM] Retrying queued redemption (attempt {item['attempts']+1}): {item['condition_id'][:20]}...")
        result = redeem_winning_position(
            item["condition_id"], token_id=item["token_id"],
            dry_run=dry_run, slug=item["slug"],
        )
        if result is True:
            print(f"[REDEEM] Queued redemption succeeded!")
            retried += 1
        else:
            item["attempts"] += 1
            item["next_retry"] = now + min(1800 * item["attempts"], 7200)  # 30min increments, 2hr cap
            still_pending.append(item)
    _pending_redemptions[:] = still_pending
    if _pending_redemptions:
        print(f"[REDEEM] {len(_pending_redemptions)} redemption(s) still queued for retry "
              f"(daily relay remaining: {_relay_daily_remaining()})")


def redeem_winning_position(condition_id: str, token_id: str = "", dry_run: bool = False, slug: str = ""):
    """Redeem winning conditional tokens on-chain for USDC.

    After a market resolves, winning shares must be redeemed via the
    Conditional Tokens Framework (CTF) contract to convert them back to USDC.

    For proxy wallets (SIGNATURE_TYPE >= 1), the redemption call is routed
    through the Gnosis Safe's execTransaction so the proxy wallet (which
    holds the tokens) is the msg.sender.

    Args:
        condition_id: The market's condition ID (hex string, 0x-prefixed or not).
        token_id: The specific token_id we hold (used to check balance).
        dry_run: If True, log but don't submit the transaction.

    Returns:
        True if redemption succeeded, None if no-op (balance=0), False on error.
    """
    if not HAS_WEB3:
        print("[REDEEM] web3 not installed — skipping on-chain redemption")
        return False

    if not PRIVATE_KEY:
        print("[REDEEM] No POLYGON_PRIVATE_KEY — skipping redemption")
        return False

    if not condition_id:
        print("[REDEEM] No condition_id — cannot redeem")
        return False

    # Determine which wallet holds the tokens
    use_proxy = SIGNATURE_TYPE >= 1 and FUNDER_ADDRESS
    token_holder = FUNDER_ADDRESS if use_proxy else None

    # Ensure condition_id is 0x-prefixed and 32 bytes
    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    try:
        condition_bytes = bytes.fromhex(cid[2:].zfill(64))
    except ValueError:
        print(f"[REDEEM] Invalid condition_id format: {condition_id[:30]}")
        return False

    w3 = None
    for _rpc_attempt in range(3):
        w3 = _get_web3()
        if w3 and w3.is_connected():
            break
        wait = 2 ** (_rpc_attempt + 1)
        print(f"[REDEEM] RPC connect failed, retrying in {wait}s...")
        import time as _time
        _time.sleep(wait)
        w3 = None

    if not w3:
        print("[REDEEM] Cannot connect to Polygon RPC after retries")
        return False

    account = w3.eth.account.from_key(PRIVATE_KEY)
    eoa_address = account.address
    # Check balance on the wallet that actually holds the tokens
    balance_wallet = w3.to_checksum_address(token_holder) if token_holder else eoa_address
    print(f"[REDEEM] EOA: {eoa_address[:8]}...{eoa_address[-4:]} | "
          f"Token holder: {balance_wallet[:8]}...{balance_wallet[-4:]}"
          f"{' (proxy)' if use_proxy else ' (EOA)'}")

    # Check if we actually hold tokens to redeem
    token_balance = 0
    if token_id:
        try:
            ctf_token = w3.eth.contract(
                address=w3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=ERC1155_BALANCE_ABI,
            )
            tid_int = int(token_id, 16) if token_id.startswith("0x") else int(token_id)
            token_balance = ctf_token.functions.balanceOf(balance_wallet, tid_int).call()
            if token_balance == 0:
                print(f"[REDEEM] No tokens to redeem (balance=0 for token {token_id[:20]}...)")
                return None  # Nothing to redeem — not an error, but not a real redemption
            print(f"[REDEEM] Token balance: {token_balance / 1e6:.2f} shares")
        except Exception as e:
            print(f"[REDEEM] Could not check balance (proceeding anyway): {e}")

    # Determine if neg_risk market
    is_neg_risk = _check_neg_risk(condition_id, slug=slug)

    if dry_run:
        print(f"[REDEEM] DRY RUN: would redeem condition={cid[:20]}... neg_risk={is_neg_risk} proxy={use_proxy}")
        return True

    try:
        # Both NegRisk and standard CLOB tokens are redeemed through the CTF directly.
        # The NegRiskAdapter must NOT be used for CLOB tokens — it uses different
        # internal token IDs, causing "SafeMath: subtraction overflow" errors.
        # For CLOB tokens: CTF.redeemPositions(USDC, bytes32(0), conditionId, [1,2])
        contract_addr = CTF_CONTRACT_ADDRESS
        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_addr),
            abi=CTF_REDEEM_ABI,
        )

        # Check if condition is resolved on-chain before attempting redemption
        try:
            ctf_diag = w3.eth.contract(
                address=w3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=ERC1155_BALANCE_ABI,
            )
            payout_denom = ctf_diag.functions.payoutDenominator(condition_bytes).call()
            print(f"[REDEEM] On-chain payoutDenominator={payout_denom} (0=not resolved)")
            if payout_denom == 0:
                print(f"[REDEEM] Condition NOT resolved on-chain yet — skipping")
                return None
        except Exception as diag_err:
            print(f"[REDEEM] payoutDenominator check failed: {diag_err}")

        market_type = "NegRisk" if is_neg_risk else "Standard"
        # Polymarket uses USDC.e as collateral (per docs). Try USDC.e first.
        collateral = USDC_E_ADDRESS
        print(f"[REDEEM] {market_type} CTF redemption: condition={cid[:20]}... collateral=USDC.e indexSets=[1,2]")
        call_data = contract.encode_abi("redeemPositions", [
            w3.to_checksum_address(collateral),
            HASH_ZERO,
            condition_bytes,
            [1, 2],
        ])

        if use_proxy:
            # Route through Polymarket's gasless relay (no MATIC needed on EOA)
            safe_contract = w3.eth.contract(
                address=w3.to_checksum_address(token_holder),
                abi=GNOSIS_SAFE_ABI,
            )
            # Try gasless relay once — on 429, queue for retry instead of blocking.
            relay_err = None
            try:
                tx_hex = _redeem_via_relay(
                    w3, account, safe_contract, token_holder,
                    contract_addr, call_data,
                )
                print(f"[REDEEM] SUCCESS (gasless relay): condition={cid[:20]}... tx={tx_hex}")
                return True
            except Exception as e:
                relay_err = e
                if "429" in str(e) or "Daily relay limit" in str(e):
                    print(f"[REDEEM] Relay rate-limited ({e}), queuing for later retry (non-blocking)")
                    _queue_pending_redemption(condition_id, token_id, slug)
                    return False

            print(f"[REDEEM] Gasless relay failed ({relay_err}), trying direct Safe tx...")
            try:
                tx_hex = _exec_via_safe(w3, token_holder, contract_addr, bytes.fromhex(call_data[2:]), account)
                print(f"[REDEEM] SUCCESS (direct proxy): condition={cid[:20]}... tx={tx_hex}")
                return True
            except Exception as direct_err:
                if "insufficient" in str(direct_err).lower():
                    print(f"[REDEEM] Direct tx failed: EOA has insufficient MATIC for gas.")
                    _queue_pending_redemption(condition_id, token_id, slug)
                    return False
                raise RuntimeError(f"Both relay ({relay_err}) and direct ({direct_err}) failed") from direct_err
        else:
            # Direct EOA call (USDC.e first per Polymarket docs)
            nonce = w3.eth.get_transaction_count(eoa_address)
            tx = contract.functions.redeemPositions(
                w3.to_checksum_address(collateral),
                HASH_ZERO,
                condition_bytes,
                [1, 2],
            ).build_transaction({
                "chainId": 137,
                "from": eoa_address,
                "nonce": nonce,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"[REDEEM] Tx sent: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                print(f"[REDEEM] SUCCESS: condition={cid[:20]}... tx={tx_hash.hex()}")
                return True

            # Transaction reverted — try native USDC as fallback collateral
            print(f"[REDEEM] USDC.e failed, trying native USDC...")
            nonce = w3.eth.get_transaction_count(eoa_address)
            tx = contract.functions.redeemPositions(
                w3.to_checksum_address(USDC_ADDRESS),
                HASH_ZERO,
                condition_bytes,
                [1, 2],
            ).build_transaction({
                "chainId": 137,
                "from": eoa_address,
                "nonce": nonce,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                print(f"[REDEEM] SUCCESS (native USDC): tx={tx_hash.hex()}")
                return True

            print(f"[REDEEM] FAILED: transaction reverted")
            return False

    except Exception as e:
        # If proxy call reverted (wrong collateral?), try native USDC as fallback
        if use_proxy and "reverted" in str(e).lower():
            try:
                print(f"[REDEEM] USDC.e failed via proxy, trying native USDC...")
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                    abi=CTF_REDEEM_ABI,
                )
                call_data_fb = contract.encode_abi("redeemPositions", [
                    w3.to_checksum_address(USDC_ADDRESS),
                    HASH_ZERO,
                    condition_bytes,
                    [1, 2],
                ])
                safe_contract = w3.eth.contract(
                    address=w3.to_checksum_address(token_holder),
                    abi=GNOSIS_SAFE_ABI,
                )
                try:
                    tx_hex = _redeem_via_relay(
                        w3, account, safe_contract, token_holder,
                        CTF_CONTRACT_ADDRESS, call_data_fb,
                    )
                except Exception:
                    tx_hex = _exec_via_safe(w3, token_holder, CTF_CONTRACT_ADDRESS, bytes.fromhex(call_data_fb[2:]), account)
                print(f"[REDEEM] SUCCESS (native USDC via proxy): tx={tx_hex}")
                return True
            except Exception as e2:
                print(f"[REDEEM] Native USDC proxy also failed: {e2}")
                return False
        print(f"[REDEEM] Error: {e}")
        return False


def sweep_unredeemed(dry_run: bool = False) -> dict:
    """Sweep all unredeemed winning positions from our Polymarket wallet.

    Queries the data API for our positions, finds any marked redeemable,
    and redeems them on-chain.  This catches winnings that the bot missed
    (e.g. from before it was running, or failed redemption attempts).

    Returns dict with counts: {"found": N, "redeemed": N, "failed": N, "skipped": N}
    """
    wallet = FUNDER_ADDRESS
    if not wallet:
        print("[SWEEP] No FUNDER_ADDRESS configured — cannot sweep")
        return {"found": 0, "redeemed": 0, "failed": 0, "skipped": 0, "error": "no wallet"}

    print(f"[SWEEP] Scanning positions for {wallet[:8]}...{wallet[-4:]}", flush=True)

    try:
        response = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": 0, "redeemable": True},
            timeout=15,
        )
        response.raise_for_status()
        positions = response.json()
    except Exception as e:
        print(f"[SWEEP] Error fetching positions: {e}")
        return {"found": 0, "redeemed": 0, "failed": 0, "skipped": 0, "error": str(e)}

    # Filter to redeemable positions
    redeemable = [p for p in positions if p.get("redeemable")]
    print(f"[SWEEP] Found {len(redeemable)} redeemable position(s) out of {len(positions)} total", flush=True)

    if not redeemable:
        return {"found": 0, "redeemed": 0, "failed": 0, "skipped": 0}

    stats = {"found": len(redeemable), "redeemed": 0, "failed": 0, "skipped": 0}

    # Group by condition_id to avoid duplicate redemption calls
    seen_conditions = set()
    for pos in redeemable:
        condition_id = pos.get("conditionId", "") or pos.get("condition_id", "")
        token_id = str(pos.get("asset", "") or pos.get("token_id", ""))
        size = float(pos.get("size", 0))
        title = pos.get("title", pos.get("market", ""))[:40]

        if not condition_id:
            print(f"[SWEEP] Skipping position with no condition_id: {title}")
            stats["skipped"] += 1
            continue

        if condition_id in seen_conditions:
            print(f"[SWEEP] Already redeemed condition {condition_id[:20]}... — skipping duplicate")
            stats["skipped"] += 1
            continue
        seen_conditions.add(condition_id)

        print(f"[SWEEP] Redeeming: {title} | size={size:.2f} | cid={condition_id[:20]}...", flush=True)

        slug = pos.get("slug", "") or pos.get("market_slug", "") or ""
        result = redeem_winning_position(
            condition_id=condition_id,
            token_id=token_id,
            dry_run=dry_run,
            slug=slug,
        )

        if result is True:
            stats["redeemed"] += 1
            print(f"[SWEEP] OK: {title}", flush=True)
            _log_copy_trade("sweep_redeem", {
                "condition_id": condition_id,
                "token_id": token_id,
                "size": size,
                "title": title,
            })
        elif result is None:
            stats["skipped"] += 1  # balance was 0
        else:
            stats["failed"] += 1
            print(f"[SWEEP] FAILED: {title}", flush=True)

    print(f"[SWEEP] Done: {stats['redeemed']} redeemed, {stats['failed']} failed, "
          f"{stats['skipped']} skipped", flush=True)
    return stats


# =============================================================================
# API FUNCTIONS
# =============================================================================

def get_active_crypto_tokens() -> list[str]:
    """Fetch token IDs for currently active crypto updown markets.

    Uses multiple strategies to find crypto markets:
      1. Targeted slug_contains queries for each crypto coin (most reliable)
      2. Broad /markets fetch with client-side filtering (fallback)

    Returns all CLOB token IDs so the WebSocket can subscribe to real-time prices.
    """
    token_ids = []
    seen_cids = set()

    def _extract_tokens(markets_list):
        for m in markets_list:
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if cid in seen_cids:
                continue
            slug = (m.get("slug") or "").lower()
            question = (m.get("question") or "").lower()
            combined = slug + " " + question
            # Must be crypto updown
            is_crypto = any(p in combined for p in CRYPTO_SLUGS)
            is_updown = ("up or down" in combined or "updown" in combined
                         or "up-or-down" in combined)
            if not (is_crypto and is_updown):
                continue
            seen_cids.add(cid)
            clob_ids = m.get("clobTokenIds") or []
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except (json.JSONDecodeError, ValueError):
                    clob_ids = [clob_ids] if clob_ids else []
            token_ids.extend(clob_ids)

    # Strategy 1: Targeted slug_contains queries (finds crypto markets directly)
    slug_terms = [
        "btc-updown", "eth-updown", "sol-updown", "xrp-updown",
        "bitcoin-updown", "ethereum-updown", "solana-updown",
    ]
    for term in slug_terms:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "slug_contains": term,
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    _extract_tokens(data)
        except Exception:
            pass

    # Strategy 2: Broad fetch (catches anything missed)
    if not token_ids:
        try:
            response = requests.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 200},
                timeout=10,
            )
            response.raise_for_status()
            _extract_tokens(response.json())
        except Exception as e:
            print(f"[ALGO] Error fetching active crypto tokens: {e}")

    return token_ids


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


def get_latest_sells(wallet_address: str, limit: int = 20, verbose: bool = False) -> list:
    """Get recent sell trades for a wallet using server-side filtering"""
    try:
        url = f"{DATA_API}/activity"
        params = {
            "user": wallet_address,
            "limit": limit,
            "type": "TRADE",
            "side": "SELL",
        }
        response = requests.get(url, params=params, timeout=10)

        if verbose:
            print(f"[ALGO] GET {url}?user={wallet_address[:12]}...&limit={limit}&type=TRADE&side=SELL => HTTP {response.status_code}", flush=True)

        response.raise_for_status()
        data = response.json()
        if not data:
            return []
        return data if isinstance(data, list) else data.get("data", data.get("results", data.get("activities", [])))
    except Exception as e:
        print(f"[ALGO] Error fetching sell activity: {e}", flush=True)
        return []


def is_crypto_market(bet: dict) -> bool:
    """Check if bet is on a crypto market (5, 15, 30, or 60 min)"""
    slug = bet.get("slug", "").lower()
    title = bet.get("title", "").lower()

    # Check for crypto market indicators
    for pattern in CRYPTO_SLUGS:
        if pattern in slug or pattern in title:
            return True

    # Also check for specific crypto keywords and timeframes (5, 15, 30, 60 min)
    crypto_keywords = [
        "bitcoin", "ethereum", "solana", "xrp", "ripple",
        "5m", "5-min", "5 min",
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
        print("[ALGO] CLOB client FAILED: py_clob_client not installed", flush=True)
        return None

    if not PRIVATE_KEY:
        print("[ALGO] CLOB client FAILED: POLYGON_PRIVATE_KEY not set in environment", flush=True)
        return None

    print(f"[ALGO] Initializing CLOB client: key={PRIVATE_KEY[:6]}...{PRIVATE_KEY[-4:]}", flush=True)
    if FUNDER_ADDRESS:
        print(f"[ALGO]   funder={FUNDER_ADDRESS[:6]}...{FUNDER_ADDRESS[-4:]}, sig_type={SIGNATURE_TYPE}", flush=True)

    try:
        # Build builder_config for order attribution if credentials are set
        builder_config = None
        if BUILDER_ENABLED and BUILDER_API_KEY and BUILDER_API_SECRET and BUILDER_API_PASSPHRASE:
            try:
                from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
                builder_config = BuilderConfig(
                    local_builder_creds=BuilderApiKeyCreds(
                        key=BUILDER_API_KEY,
                        secret=BUILDER_API_SECRET,
                        passphrase=BUILDER_API_PASSPHRASE,
                    ),
                )
                print(f"[ALGO] Builder attribution enabled (key={BUILDER_API_KEY[:12]}...)", flush=True)
            except ImportError:
                print("[ALGO] py_builder_signing_sdk not installed — builder attribution disabled", flush=True)
            except Exception as e:
                print(f"[ALGO] Builder config failed ({e}) — attribution disabled", flush=True)

        if FUNDER_ADDRESS:
            client = ClobClient(
                CLOB_API,
                key=PRIVATE_KEY,
                chain_id=137,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER_ADDRESS,
                builder_config=builder_config,
            )
        else:
            client = ClobClient(
                CLOB_API,
                key=PRIVATE_KEY,
                chain_id=137,
                builder_config=builder_config,
            )

        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"[ALGO] CLOB client initialized successfully (api_key={creds.api_key[:8]}...)", flush=True)
        return client
    except Exception as e:
        print(f"[ALGO] CLOB client FAILED during init: {e}", flush=True)
        import traceback; traceback.print_exc()
        return None


_balance_error_until = 0.0  # Timestamp until which we skip orders due to balance/allowance errors
_balance_error_count = 0  # Consecutive balance errors — escalates pause duration


def check_usdc_balance() -> Optional[float]:
    """Check on-chain USDC balance of the trading wallet. Returns balance in $ or None on error."""
    if not HAS_WEB3 or not PRIVATE_KEY:
        return None
    try:
        w3 = _get_web3()
        if not w3 or not w3.is_connected():
            return None
        # Check the proxy wallet (funder) if using proxy, else EOA
        use_proxy = SIGNATURE_TYPE >= 1 and FUNDER_ADDRESS
        wallet = w3.to_checksum_address(FUNDER_ADDRESS) if use_proxy else w3.eth.account.from_key(PRIVATE_KEY).address
        # ERC20 balanceOf
        erc20_abi = json.loads('[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
        usdc = w3.eth.contract(address=w3.to_checksum_address(USDC_E_ADDRESS), abi=erc20_abi)
        bal = usdc.functions.balanceOf(wallet).call()
        return bal / 1e6  # USDC has 6 decimals
    except Exception:
        return None

def place_bet(client: "ClobClient", token_id: str, amount: float, max_price: float = 0) -> dict:
    """Place a FOK market buy order. Returns fill details or empty dict on failure.

    Uses create_market_order (FOK) which has the correct precision handling:
    maker_amount (USDC) rounded to 2 decimals, taker_amount (shares) to 4.
    The price parameter caps the worst fill price.
    """
    global _balance_error_until, _balance_error_count
    if time.time() < _balance_error_until:
        # Proactively check if balance has recovered (e.g. from a redemption)
        bal = check_usdc_balance()
        if bal is not None and bal >= amount:
            print(f"[ALGO] Balance recovered (${bal:.2f}) — resuming trading!")
            _balance_error_until = 0.0
            _balance_error_count = 0
        else:
            remaining = int(_balance_error_until - time.time())
            bal_str = f" (on-chain: ${bal:.2f})" if bal is not None else ""
            print(f"[ALGO] Skipping order — insufficient balance{bal_str} (retry in {remaining}s)")
            return {}
    try:
        # Use the price cap if provided, otherwise let the library calculate
        price = min(round(max_price, 2), 0.99) if max_price and max_price > 0 else 0
        if max_price and price < 0.01:
            price = 0.01

        print(f"[ALGO] FOK buy: ${amount:.2f} @ max {price:.2f}" if price else f"[ALGO] FOK buy: ${amount:.2f} (market)")
        order = MarketOrderArgs(
            token_id=token_id,
            amount=round(amount, 2),
            price=price,
            side=BUY,
            order_type=OrderType.FAK,
        )
        signed_order = client.create_market_order(order)
        result = client.post_order(signed_order, OrderType.FAK)

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
        error_str = str(e).lower()
        if "balance" in error_str or "allowance" in error_str:
            _balance_error_count += 1
            # Escalating pause: 2min, 4min, 8min, cap at 10min — balance check can resume early
            pause = min(120 * (2 ** (_balance_error_count - 1)), 600)
            _balance_error_until = time.time() + pause
            print(f"[ALGO] Order error (BALANCE/ALLOWANCE): {e}")
            print(f"[ALGO] *** Pausing orders for {pause//60}m{pause%60}s (attempt #{_balance_error_count}) — will auto-resume if balance recovers ***")
        else:
            print(f"[ALGO] Order error: {e}")
            import traceback; traceback.print_exc()
        return {}


def place_sell(client: "ClobClient", token_id: str, size: float, min_price: float = 0) -> dict:
    """Place a FOK market sell order for `size` shares of token_id.

    Uses create_market_order (FOK) which has the correct precision handling.
    The price parameter sets the minimum acceptable sell price.
    """
    try:
        import math
        price = max(round(min_price, 2), 0.01) if min_price and min_price > 0 else 0
        sell_size = math.floor(size * 100) / 100  # floor to 2 decimals

        print(f"[ALGO] FOK sell: {sell_size:.2f} shares @ min {price:.2f}" if price else f"[ALGO] FOK sell: {sell_size:.2f} shares (market)")
        order = MarketOrderArgs(
            token_id=token_id,
            amount=sell_size,
            price=price,
            side=SELL,
            order_type=OrderType.FAK,
        )
        signed_order = client.create_market_order(order)
        result = client.post_order(signed_order, OrderType.FAK)

        fill_info = {"success": True}
        if isinstance(result, dict):
            status = result.get("status", "").lower()
            if status in ("rejected", "failed", "expired"):
                print(f"[ALGO] Sell order {status}: {result}")
                return {}
            shares_filled = float(result.get("size") or result.get("filledSize") or 0)
            usdc_filled = float(result.get("matchedAmount") or result.get("amount") or 0)
            if shares_filled > 0:
                fill_info["fill_price"] = usdc_filled / shares_filled
                fill_info["shares"] = shares_filled
                fill_info["usdc"] = usdc_filled
            print(f"[ALGO] Sell fill details: {result}")
        return fill_info
    except Exception as e:
        print(f"[ALGO] Sell order error: {e}")
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
        self.copied_trades: set = set()  # Track copied buy trade IDs
        self.copied_sells: set = set()   # Track copied sell trade IDs
        self.copied_sizes: set = set()  # Track (condition_id, target_size) to dedup re-scans
        self.entered_markets: dict = {}  # (condition_id, outcome_index) → entry_price
        self.market_entry_count: dict = {}  # (condition_id, outcome_index) → number of entries
        self.last_trade_time: dict = {}  # (condition_id, outcome_index) → epoch timestamp of last trade
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
        self.total_buys = 0

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

        open_positions = [p for p in self.positions.get("open", []) if p.get("source") != "momentum"]
        resolved_positions = [p for p in self.positions.get("resolved", []) if p.get("source") != "momentum"]
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
        print(f"  Stop loss: {STOP_LOSS_PCT}%" if STOP_LOSS_PCT > 0 else "  Stop loss: DISABLED")
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

        # Snapshot existing sells so we don't mirror historical ones on startup
        existing_sells = get_latest_sells(TARGET_ADDRESS, limit=50)
        for sell in existing_sells:
            sell_id = sell.get("id") or f"{sell.get('conditionId')}_{sell.get('timestamp')}_SELL"
            self.copied_sells.add(sell_id)
        print(f"[ALGO] Marked {len(self.copied_sells)} existing sell trades as seen.")

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

        # Check stop losses first — auto-sell positions that dropped too far
        copied += self._check_stop_losses()

        # Check for sells — close positions before potentially re-entering
        copied += self._check_and_copy_sells()

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

            # Price band filter disabled — copy all prices exactly as target trades

            # Check if we already have this position
            # Try multiple field names for condition_id (API may use camelCase or snake_case)
            condition_id = bet.get("conditionId") or bet.get("condition_id") or bet.get("market_condition_id") or ""
            # Use explicit None checks -- outcome_index=0 is valid (not falsy)
            outcome_index = bet.get("outcomeIndex")
            if outcome_index is None:
                outcome_index = bet.get("outcome_index")
            if outcome_index is None:
                outcome_index = 0

            # GUARD 1 disabled — copy re-entries exactly as target trades
            market_key = (condition_id, outcome_index)
            if condition_id and market_key in self.entered_markets:
                print(f"[ALGO] Re-entry: {title} | {outcome} (price {price:.3f})")

                # 30-second cooldown between re-entries — confirms direction before
                # following up with full lot (prevents rapid-fire double entries)
                last_t = self.last_trade_time.get(market_key, 0)
                elapsed = time.time() - last_t
                if elapsed < FOLLOW_UP_COOLDOWN:
                    remaining = FOLLOW_UP_COOLDOWN - elapsed
                    print(f"[ALGO] Skip (cooldown: {remaining:.0f}s remaining of {FOLLOW_UP_COOLDOWN}s): {title} | {outcome}")
                    self.trades_skipped += 1
                    continue

            # GUARD 2 disabled — no re-entry cap
            # GUARD 3 disabled — opposite side block removed

            # Legacy size-based dedup kept as a secondary safety net
            target_size = bet.get("size", 0)
            if condition_id and target_size:
                size_key = (condition_id, str(outcome_index), str(round(float(target_size), 2)))
                if size_key in self.copied_sizes:
                    print(f"[ALGO] Skip (same size already copied): {title} | {outcome} {target_size} shares")
                    self.trades_skipped += 1
                    continue
                self.copied_sizes.add(size_key)

            # Resolve per-coin lot size — probe on first entry, full lot on re-entry
            trade_coin = detect_coin(slug, title)
            full_lot = self.coin_bet_amounts.get(trade_coin, self.bet_amount) if trade_coin else self.bet_amount
            is_first_entry = condition_id and market_key not in self.entered_markets
            trade_amount = PROBE_AMOUNT if is_first_entry else full_lot

            # Copy the trade!
            entry_type = "PROBE" if is_first_entry else "RE-ENTRY"
            print(f"\n[ALGO] NEW TRADE DETECTED! ({entry_type})")
            print(f"       Market: {title}")
            print(f"       Target bought: {size:.1f} {outcome} @ {price*100:.1f}¢")
            print(f"       Copying: ${trade_amount:.2f} of {outcome} ({trade_coin.upper() or '?'} lot | {entry_type})")

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
            _log_copy_trade(
                "dry_run_buy" if trade_record["status"] == "dry_run" else
                "buy" if trade_record["status"] == "filled" else "failed_buy",
                {k: v for k, v in trade_record.items() if k != "id"}
            )

            # Save position for tracking (if trade was successful or dry run)
            if trade_record["status"] in ["filled", "dry_run"]:
                # Record trade time for follow-up cooldown
                if condition_id:
                    self.last_trade_time[market_key] = time.time()
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
            self.total_buys += copied

        return copied

    def _check_stop_losses(self) -> int:
        """Auto-sell positions that have dropped STOP_LOSS_PCT% from entry price.

        Uses live WebSocket bid prices. Returns number of positions stopped out.
        """
        if STOP_LOSS_PCT <= 0:
            return 0

        open_positions = self.positions.get("open", [])
        if not open_positions or not self.ws:
            return 0

        stopped = 0
        threshold = 1 - (STOP_LOSS_PCT / 100)  # e.g. 0.80 for 20% stop loss

        for position in open_positions[:]:  # copy — list mutated during iteration
            entry_price = position.get("entry_price", 0)
            token_id = position.get("token_id", "")
            if entry_price <= 0 or not token_id:
                continue

            # Get live bid (what we'd actually receive if selling now)
            live_bid, _ = self.ws.get_best_prices(token_id)
            if not live_bid or live_bid <= 0:
                continue

            stop_price = entry_price * threshold
            if live_bid >= stop_price:
                continue  # price is fine, no stop needed

            # --- STOP LOSS TRIGGERED ---
            condition_id = position.get("condition_id", "")
            outcome_index = position.get("outcome_index", 0)
            market_key = (condition_id, outcome_index)
            shares = position.get("potential_payout", 0)
            title = position.get("market", "Unknown")[:50]
            outcome = position.get("outcome", "?")
            amount_spent = position.get("amount", 0)
            loss_pct = (1 - live_bid / entry_price) * 100

            print(f"\n[ALGO] STOP LOSS TRIGGERED!")
            print(f"       Market: {title}")
            print(f"       Entry: {entry_price:.4f} | Live bid: {live_bid:.4f} | Loss: {loss_pct:.1f}%")
            print(f"       Selling {shares:.2f} shares of {outcome}")

            trade_record = {
                "id": f"stop_loss_{condition_id}_{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": position.get("slug", ""),
                "outcome": outcome,
                "side": "SELL",
                "shares": shares,
                "coin": detect_coin(position.get("slug", ""), title),
                "target_trader": self.target_name,
                "source": "stop_loss",
            }

            if self.dry_run:
                trade_record["price"] = live_bid
                trade_record["status"] = "dry_run"
            else:
                if token_id and self.client and shares > 0:
                    buffer = PRICE_BUFFER_BPS / 10000
                    min_price = max(live_bid * (1 - buffer), 0.01)
                    print(f"       Sell limit: {min_price:.4f} (live bid {live_bid:.4f} - {PRICE_BUFFER_BPS}bps)")
                    fill = place_sell(self.client, token_id, shares, min_price=min_price)
                    if fill.get("success"):
                        trade_record["status"] = "filled"
                        fill_price = fill.get("fill_price") or live_bid
                        trade_record["price"] = fill_price
                        print(f"       STOP LOSS SELL EXECUTED! Fill: {fill_price:.4f}")
                    else:
                        print(f"       STOP LOSS SELL FAILED!")
                        trade_record["status"] = "failed"
                else:
                    print(f"       No token ID, client, or shares to sell")
                    trade_record["status"] = "error"

            if trade_record["status"] in ["filled", "dry_run"]:
                sell_price = trade_record.get("price", 0)
                if sell_price > 0 and shares > 0:
                    proceeds = sell_price * shares
                    pnl = proceeds - amount_spent
                else:
                    proceeds = 0
                    pnl = -amount_spent

                position["result"] = "STOP_LOSS"
                position["won"] = False
                position["pnl"] = pnl
                position["proceeds"] = proceeds
                position["sold_at"] = trade_record["timestamp"]
                position["sell_price"] = sell_price

                # Use momentum-prefixed event so it appears in momentum balance chart
                pos_source = position.get("source", "copy_trader")
                event_name = "momentum_stop_loss" if pos_source == "momentum" else "stop_loss"

                try:
                    stats = self.positions["stats"]
                    stats["total_pnl"] = stats.get("total_pnl", 0.0) + pnl
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) + proceeds
                    stats["losses"] = stats.get("losses", 0) + 1
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []) if p is not position)
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats["total_pnl"],
                        "equity": stats["balance"] + open_staked,
                        "event": event_name,
                        "detail": f"STOP LOSS {outcome} {title[:30]} loss={loss_pct:.1f}% pnl={pnl:+.2f} (returned ${proceeds:.2f})"
                    })
                except Exception:
                    pass

                if position in self.positions.get("open", []):
                    self.positions["open"].remove(position)
                if market_key in self.entered_markets:
                    del self.entered_markets[market_key]
                if market_key in self.market_entry_count:
                    del self.market_entry_count[market_key]
                self.positions.setdefault("resolved", []).append(position)
                save_positions(self.positions)
                trade_record["pnl"] = pnl
                print(f"       Position stopped out. PnL: ${pnl:+.2f} | Balance: ${self.positions['stats'].get('balance', 0):.2f}")
                stopped += 1

            self.trade_history.append(trade_record)
            _log_copy_trade(
                "dry_run_stop_loss" if trade_record["status"] == "dry_run" else
                "stop_loss" if trade_record["status"] == "filled" else "failed_stop_loss",
                {k: v for k, v in trade_record.items() if k != "id"}
            )
            if self.on_trade:
                try:
                    self.on_trade(trade_record)
                except Exception as e:
                    print(f"[ALGO] Stop loss callback error: {e}")

        return stopped

    def _check_and_copy_sells(self) -> int:
        """Check for target's sell trades and mirror them against our open positions."""
        sells = get_latest_sells(TARGET_ADDRESS, verbose=True)
        if not sells:
            return 0

        copied = 0
        for sell in sells:
            sell_id = sell.get("id") or f"{sell.get('conditionId')}_{sell.get('timestamp')}_SELL"

            if sell_id in self.copied_sells:
                continue
            self.copied_sells.add(sell_id)

            # Filter for crypto markets
            if self.crypto_only and not is_crypto_market(sell):
                continue

            condition_id = sell.get("conditionId") or sell.get("condition_id") or ""
            outcome_index = sell.get("outcomeIndex")
            if outcome_index is None:
                outcome_index = sell.get("outcome_index", 0)
            market_key = (condition_id, outcome_index)

            # Only act if we hold this position
            if market_key not in self.entered_markets:
                continue

            # Find the matching open position
            position = None
            for p in self.positions.get("open", []):
                if p.get("condition_id") == condition_id and p.get("outcome_index") == outcome_index:
                    position = p
                    break

            if not position:
                continue

            token_id = position.get("token_id") or sell.get("asset", "")
            shares = position.get("potential_payout", 0)
            title = position.get("market", "Unknown")[:50]
            outcome = position.get("outcome", "?")

            # Derive target's sell price from the sell event (usdcSize / size)
            target_usdc = float(sell.get("usdcSize") or sell.get("usdc_size") or sell.get("amount") or 0)
            target_shares = float(sell.get("size") or 1)
            target_sell_price = (target_usdc / target_shares) if target_shares > 0 and target_usdc > 0 else None

            # WS live bid as fallback
            ws_bid = None
            if self.ws and token_id:
                ws_bid, _ = self.ws.get_best_prices(token_id)
                if not ws_bid and token_id not in getattr(self.ws, '_subscribed_tokens', set()):
                    # Token not subscribed yet — subscribe and give WS a moment to populate
                    self.ws.subscribe([token_id])
                    time.sleep(1.5)
                    ws_bid, _ = self.ws.get_best_prices(token_id)

            # Best available price: target's sell price → WS bid → None
            best_price = target_sell_price or ws_bid

            print(f"\n[ALGO] TARGET SELL DETECTED!")
            print(f"       Market: {title}")
            print(f"       Selling: {shares:.2f} shares of {outcome}")
            if best_price:
                src = "target price" if target_sell_price else "WS bid"
                print(f"       Price: {best_price*100:.1f}¢ ({src})")

            trade_record = {
                "id": f"sell_{sell_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": title,
                "slug": position.get("slug", ""),
                "outcome": outcome,
                "side": "SELL",
                "shares": shares,
                "coin": detect_coin(position.get("slug", ""), title),
                "target_trader": self.target_name,
                "source": "copy_trader",
            }

            if self.dry_run:
                if best_price:
                    trade_record["price"] = best_price
                trade_record["status"] = "dry_run"
                copied += 1
            else:
                if token_id and self.client and shares > 0:
                    buffer = PRICE_BUFFER_BPS / 10000
                    live_bid = ws_bid  # already fetched above
                    if live_bid:
                        min_price = max(live_bid * (1 - buffer), 0.01)
                        print(f"       Sell limit: {min_price:.4f} (live bid {live_bid:.4f} - {PRICE_BUFFER_BPS}bps)")
                    else:
                        min_price = 0
                    fill = place_sell(self.client, token_id, shares, min_price=min_price)
                    if fill.get("success"):
                        trade_record["status"] = "filled"
                        fill_price = fill.get("fill_price") or best_price
                        if fill_price:
                            trade_record["price"] = fill_price
                            print(f"       SELL EXECUTED! Fill: {fill_price:.4f}")
                        else:
                            print(f"       SELL EXECUTED!")
                        copied += 1
                    else:
                        print(f"       SELL FAILED!")
                        trade_record["status"] = "failed"
                else:
                    print(f"       No token ID, client, or shares to sell")
                    trade_record["status"] = "error"

            # If sold (or dry run), close the position and record PnL
            if trade_record["status"] in ["filled", "dry_run"]:
                sell_price = trade_record.get("price", 0)
                entry_price = position.get("entry_price", 0)
                amount_spent = position.get("amount", 0)
                if sell_price > 0 and shares > 0:
                    proceeds = sell_price * shares
                    pnl = proceeds - amount_spent
                else:
                    # No live bid available — return capital flat so balance/equity don't drop
                    proceeds = amount_spent
                    pnl = 0.0

                position["result"] = "SOLD"
                position["pnl"] = pnl
                position["sold_at"] = trade_record["timestamp"]
                position["sell_price"] = sell_price

                # Update stats
                try:
                    stats = self.positions["stats"]
                    stats["total_pnl"] = stats.get("total_pnl", 0.0) + pnl
                    stats["balance"] = stats.get("balance", ALGO_STARTING_BALANCE) + proceeds
                    if sell_price > 0:
                        # Only count as win/loss when we have an actual price
                        if pnl > 0:
                            stats["wins"] = stats.get("wins", 0) + 1
                        else:
                            stats["losses"] = stats.get("losses", 0) + 1
                    open_staked = sum(p.get("amount", 0) for p in self.positions.get("open", []) if p is not position)
                    stats.setdefault("balance_history", []).append({
                        "timestamp": trade_record["timestamp"],
                        "balance": stats["balance"],
                        "pnl": stats["total_pnl"],
                        "equity": stats["balance"] + open_staked,
                        "event": "sell",
                        "detail": f"SELL {outcome} {title[:30]} pnl={pnl:+.2f}"
                    })
                except Exception:
                    pass

                if position in self.positions.get("open", []):
                    self.positions["open"].remove(position)
                if market_key in self.entered_markets:
                    del self.entered_markets[market_key]
                if market_key in self.market_entry_count:
                    del self.market_entry_count[market_key]
                self.positions.setdefault("resolved", []).append(position)
                save_positions(self.positions)
                trade_record["pnl"] = pnl
                print(f"       Position closed. PnL: ${pnl:+.2f} | Balance: ${self.positions['stats'].get('balance', 0):.2f}")

            self.trade_history.append(trade_record)
            _log_copy_trade(
                "dry_run_sell" if trade_record["status"] == "dry_run" else
                "sell" if trade_record["status"] == "filled" else "failed_sell",
                {k: v for k, v in trade_record.items() if k != "id"}
            )
            if self.on_trade:
                try:
                    self.on_trade(trade_record)
                except Exception as e:
                    print(f"[ALGO] Sell callback error: {e}")

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

                # Check stop losses on every tick (WS prices are live)
                self._check_stop_losses()

                # Periodically check for resolved positions
                self.check_resolutions()

                # Retry any queued redemptions (rate-limited / failed earlier)
                retry_pending_redemptions(dry_run=self.dry_run)

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

        # Only check non-momentum positions (momentum has its own check_resolutions)
        copy_positions = [p for p in open_positions if p.get("source") != "momentum"]
        if not copy_positions:
            return

        resolved_this_check = 0

        # Cache resolution results by condition_id to avoid duplicate API calls
        # when multiple position entries exist for the same market (e.g. re-entries).
        _resolution_cache = {}

        for position in copy_positions[:]:  # Copy list to allow modification
            condition_id = position.get("condition_id", "")
            slug = position.get("slug", "")
            token_id = position.get("token_id", "")
            our_outcome = position.get("outcome")

            # Need either token_id, condition_id, or slug to check resolution
            if not token_id and not condition_id and not slug:
                continue

            # Use cached result if we already checked this condition_id this cycle
            cache_key = condition_id or slug or token_id
            if cache_key in _resolution_cache:
                result = _resolution_cache[cache_key]
            else:
                result = get_market_resolution(
                    condition_id=condition_id,
                    slug=slug,
                    token_id=token_id,
                    our_outcome=our_outcome
                )
                _resolution_cache[cache_key] = result

            if result.get("resolved"):
                # Position resolved!
                our_index = position.get("outcome_index")
                entry_price = position.get("entry_price", 0)
                amount = position.get("amount", 0)

                # Initialize these - may be set by gamma API fallback
                winning_outcome = result.get("winning_outcome")
                winning_index = result.get("winning_index")

                # --- Determine win/loss using multiple signals ---
                won = None

                # Priority 1: Direct token_id comparison from CLOB API
                if "our_token_won" in result and result["our_token_won"] is not None:
                    won = result["our_token_won"]
                    print(f"[ALGO] Token resolution: {position['market'][:30]} | won={won}")

                # Priority 2: Outcome name comparison
                if won is None:
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

                # Safety net: if CLOB token comparison said LOSS but outcome
                # name says WIN, trust the name match (token_id formatting issue)
                if won is False and winning_outcome and our_outcome:
                    our_normalized = our_outcome.lower().strip()
                    winning_normalized = winning_outcome.lower().strip()
                    if (our_normalized == winning_normalized
                            or our_normalized.startswith(winning_normalized)
                            or winning_normalized.startswith(our_normalized)):
                        print(f"[ALGO] OVERRIDE: token_id said LOSS but outcome name matches "
                              f"(ours={our_outcome}, winner={winning_outcome}). Correcting to WIN.")
                        won = True

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

                    # Auto-redeem winning shares on-chain → converts back to USDC
                    try:
                        redeemed = redeem_winning_position(
                            condition_id=condition_id,
                            token_id=token_id,
                            dry_run=self.dry_run,
                            slug=slug,
                        )
                        position["redeemed"] = bool(redeemed)
                        _log_copy_trade("redeem", {
                            "market": position.get("market", ""),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "redeemed": bool(redeemed),
                            "dry_run": self.dry_run,
                            "pnl": position.get("pnl", 0),
                        })
                        if redeemed is True:
                            print(f"[ALGO] Auto-redeemed: {position['market'][:30]}")
                        elif redeemed is None:
                            pass  # No-op: balance was 0, already logged by redeem function
                        else:
                            print(f"[ALGO] Redemption failed for {position['market'][:30]} — queued for retry")
                            _queue_pending_redemption(condition_id, token_id, slug)
                    except Exception as e:
                        position["redeemed"] = False
                        _log_copy_trade("redeem_error", {
                            "market": position.get("market", ""),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "error": str(e),
                        })
                        print(f"[ALGO] Redemption error: {e}")
                        _queue_pending_redemption(condition_id, token_id, slug)

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

                _log_copy_trade("resolved", {
                    "market": position.get("market", ""),
                    "slug": position.get("slug", ""),
                    "outcome": position.get("outcome", ""),
                    "coin": detect_coin(position.get("slug", ""), position.get("market", "")),
                    "amount": position.get("amount", 0),
                    "entry_price": position.get("entry_price", 0),
                    "result": position.get("result", "UNKNOWN"),
                    "won": won,
                    "pnl": position.get("pnl", 0),
                    "winning_outcome": winning_outcome,
                    "condition_id": position.get("condition_id", ""),
                    "token_id": position.get("token_id", ""),
                    "opened_at": position.get("timestamp", ""),
                    "resolved_at": position["resolved_at"],
                    "redeemed": position.get("redeemed"),
                    "dry_run": position.get("dry_run", False),
                })

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
            elif result == "SOLD" and pos.get("sell_price", 0) > 0:
                # Early sell — count as win/loss based on actual PnL
                if pnl > 0:
                    coin_data[coin]["wins"] += 1
                    coin_data[coin]["results"].append("W")
                else:
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
        all_open = list(self.positions.get("open", []))
        # Per-engine counts (copy-only for the detailed section)
        open_positions = [p for p in all_open if p.get("source") != "momentum"]
        resolved_positions = [p for p in self.positions.get("resolved", []) if p.get("source") != "momentum"]
        # Equity uses ALL open positions (copy + momentum) so the hero reflects total exposure
        all_open_staked = sum(p.get("amount", 0) for p in all_open)
        equity = self._safe_float(balance + all_open_staked)

        # Per-coin ROI stats from resolved + open positions
        coin_roi = self._compute_coin_roi(open_positions, resolved_positions)

        # Group open positions by coin for detail drill-down
        open_by_coin: dict = {}
        for pos in open_positions:
            slug = pos.get("slug", "")
            market = pos.get("market", "")
            coin = detect_coin(slug, market) or "other"
            if coin not in open_by_coin:
                open_by_coin[coin] = []
            open_by_coin[coin].append({
                "market": pos.get("market", "")[:60],
                "outcome": pos.get("outcome", ""),
                "entry_price": self._safe_float(pos.get("entry_price", 0)),
                "amount": self._safe_float(pos.get("amount", 0)),
                "timestamp": pos.get("timestamp", ""),
            })

        sells_early = sum(1 for p in resolved_positions if p.get("result") == "SOLD")
        finished = len(resolved_positions) - sells_early

        return {
            "trades_copied": self.trades_copied,
            "trades_skipped": self.trades_skipped,
            "total_spent": self._safe_float(self.total_spent),
            "total_buys": self.total_buys,
            "total_sells_early": sells_early,
            "total_finished": finished,
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
            "open_by_coin": open_by_coin,
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
