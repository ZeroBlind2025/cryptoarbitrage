#!/usr/bin/env python3
"""
HFT ARBITRAGE SERVER
====================

Flask server for HFT arbitrage trading with:
- Demo mode: Paper trading with real price feeds
- Live mode: Actual order execution on Polygon CLOB

Dashboard features:
- Real-time trade tracking for both modes
- Latency metrics
- PnL tracking
- Trade outcome visualization

Optimized for QuantVPS deployment.

Usage:
    python hft_server.py --mode demo --port 5000
    python hft_server.py --mode live --port 5000  # Requires confirmation
"""

import argparse
import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Generator

from flask import Flask, jsonify, render_template, Response, request
from flask_cors import CORS

from hft_client import (
    HFTConfig,
    HFTClient,
    HFTScanner,
    TradingMode,
    Trade,
    OrderStatus,
    ArbOpportunity,
)

# Import WebSocket scanner (optional - falls back to polling if not available)
try:
    from hft_client import HFTWebSocketScanner
    HAS_WS_SCANNER = True
except ImportError:
    HAS_WS_SCANNER = False
    print("[HFT] WebSocket scanner not available, will use polling mode")
from arb_engines import (
    EngineType,
    EngineSignal,
    SumToOneConfig,
    TailEndConfig,
)
from sports_ws import SportsWebSocket

# Note: Using direct API calls for market discovery instead of GammaScanner
# to be more targeted (only crypto 15-min and live sports markets)


# =============================================================================
# APP CONFIGURATION
# =============================================================================

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Server state
server_state = {
    "mode": "demo",  # demo or live
    "is_running": False,
    "started_at": None,
}

# HFT components
hft_client: Optional[HFTClient] = None
hft_scanner: Optional[HFTScanner] = None

# Trade tracking (shared between demo and live)
demo_trades: deque[Trade] = deque(maxlen=500)
live_trades: deque[Trade] = deque(maxlen=500)

# Opportunity history
opportunity_history: deque[ArbOpportunity] = deque(maxlen=100)

# Active markets being monitored
active_markets: list[dict] = []

# Signal history (by engine)
signal_history: deque[dict] = deque(maxlen=200)

# Threading
scanner_thread: Optional[threading.Thread] = None
stop_scanner = threading.Event()

# Market refresh settings
MARKET_REFRESH_INTERVAL_MINUTES = 15
refresh_thread: Optional[threading.Thread] = None
stop_refresh = threading.Event()

# Copy trader
try:
    from copy_trader import CopyTrader, TARGET_ADDRESS
    HAS_COPY_TRADER = True
except ImportError:
    HAS_COPY_TRADER = False
    TARGET_ADDRESS = ""

copy_trader: Optional["CopyTrader"] = None
copy_trader_thread: Optional[threading.Thread] = None
stop_copy_trader = threading.Event()
copy_trades: deque = deque(maxlen=100)  # Copy trader trade history


def on_copy_trade(trade_record: dict):
    """Callback when copy trader executes a trade"""
    copy_trades.append(trade_record)
    print(f"[COPY] Trade recorded: {trade_record.get('market', '?')[:30]} - {trade_record.get('status', '?')}", flush=True)


# Sports WebSocket for real-time game data
sports_ws: Optional[SportsWebSocket] = None
live_sports_data: dict[str, dict] = {}  # event_id -> data from WebSocket


# =============================================================================
# CALLBACKS
# =============================================================================

def on_trade_callback(trade: Trade):
    """Handle new trade"""
    if trade.mode == TradingMode.DEMO:
        demo_trades.append(trade)
    else:
        live_trades.append(trade)

    # Log to file
    log_trade(trade)


def on_signal_callback(signal: EngineSignal):
    """Handle detected signal from any engine"""
    signal_history.append(signal.to_dict())


def log_trade(trade: Trade):
    """Log trade to JSONL file"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    log_file = log_dir / f"trades_{trade.mode.value}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(trade.to_dict()) + "\n")


def on_sports_update(data: dict):
    """Handle real-time sports update from WebSocket"""
    global live_sports_data
    event_id = data.get("eventId") or data.get("event_id") or data.get("id") or data.get("gameId")
    if event_id:
        live_sports_data[event_id] = {
            "data": data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Log significant updates (score changes, period changes)
        if data.get("type") in ["score", "period_change", "game_end"]:
            print(f"[SPORTS WS] {data.get('type')}: {data}", flush=True)


def on_sports_connect():
    """Handle sports WebSocket connection"""
    print("[SPORTS WS] Connected to real-time sports feed!", flush=True)


def on_sports_disconnect():
    """Handle sports WebSocket disconnection"""
    print("[SPORTS WS] Disconnected from sports feed", flush=True)


# =============================================================================
# MARKET DISCOVERY
# =============================================================================

def detect_market_category(slug: str, question: str) -> str:
    """
    Detect market category from slug and question text.

    Categories:
    - "crypto": BTC, ETH, XRP, SOL price markets
    - "sports": Sports/game outcome markets
    - "politics": Election/political markets
    - "other": Everything else
    """
    slug_lower = slug.lower()
    question_lower = question.lower()
    combined = slug_lower + " " + question_lower

    # Crypto keywords (15-min price windows)
    crypto_keywords = [
        "btc", "bitcoin", "eth", "ethereum", "xrp", "ripple", "sol", "solana",
        "crypto", "price above", "price below", "price at"
    ]
    if any(kw in combined for kw in crypto_keywords):
        return "crypto"

    # Sports keywords
    sports_keywords = [
        "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "baseball",
        "hockey", "tennis", "golf", "ufc", "mma", "boxing", "f1", "formula",
        "game", "match", "win", "score", "playoff", "championship", "super bowl",
        "world series", "finals", "league", "team", "player"
    ]
    if any(kw in combined for kw in sports_keywords):
        return "sports"

    # Politics keywords
    politics_keywords = [
        "election", "president", "congress", "senate", "vote", "poll",
        "republican", "democrat", "biden", "trump", "governor", "mayor"
    ]
    if any(kw in combined for kw in politics_keywords):
        return "politics"

    return "other"


def discover_markets() -> list[dict]:
    """
    Discover target markets for HFT monitoring.

    - Crypto 15-min: Search for btc/eth/sol/xrp-updown-15m-{timestamp} markets
    - Sports live: tag_id=100639 filtered for games ending soon
    """
    import requests
    from datetime import datetime, timezone
    import time as time_module

    GAMMA_API = "https://gamma-api.polymarket.com"
    SPORTS_TAG_ID = 100639

    # 15-minute market cryptos
    CRYPTO_SYMBOLS = ["btc", "eth", "sol", "xrp"]

    markets = []
    crypto_found = 0
    sports_found = 0

    try:
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        # ============================================================
        # FETCH CRYPTO 15-MIN MARKETS
        # ============================================================
        print("[HFT] Searching for 15-min crypto markets...", flush=True)

        # Generate timestamps for current and upcoming 15-min windows
        # Round down to nearest 15 minutes
        base_ts = (current_ts // 900) * 900  # 900 seconds = 15 minutes

        # Try multiple time offsets to catch active markets
        timestamps_to_try = [
            base_ts,          # Current window
            base_ts + 900,    # Next window
            base_ts + 1800,   # +30 min
            base_ts - 900,    # Previous (might still be active)
        ]

        # Search for each crypto symbol
        for symbol in CRYPTO_SYMBOLS:
            for ts in timestamps_to_try:
                slug_pattern = f"{symbol}-updown-15m-{ts}"

                try:
                    resp = requests.get(
                        f"{GAMMA_API}/markets",
                        params={
                            "slug": slug_pattern,
                            "active": "true",
                            "closed": "false",
                        },
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list) and data:
                            for raw in data:
                                market_data = parse_market_data(raw)
                                if market_data:
                                    # Calculate minutes until resolution
                                    end_ts = ts + 900  # 15 min after start
                                    minutes_until = (end_ts - current_ts) / 60
                                    if minutes_until > 0:
                                        market_data["category"] = "crypto"
                                        market_data["minutes_until"] = minutes_until
                                        markets.append(market_data)
                                        crypto_found += 1
                                        print(f"[HFT] Found crypto: {slug_pattern} ({minutes_until:.1f}m)", flush=True)
                except:
                    pass

                # Small delay to avoid rate limiting
                time_module.sleep(0.05)

        # Also try a general search for "updown-15m" patterns
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                },
                timeout=30
            )
            if resp.status_code == 200:
                all_markets = resp.json()
                for raw in all_markets:
                    slug = raw.get("slug", "")
                    if "updown-15m" in slug.lower() or "-15m-" in slug.lower():
                        # Check if we already have this market
                        if not any(m["slug"] == slug for m in markets):
                            market_data = parse_market_data(raw)
                            if market_data:
                                end_date_str = raw.get("endDate") or raw.get("endDateIso")
                                minutes_until = None
                                if end_date_str:
                                    try:
                                        if "T" in str(end_date_str):
                                            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                                            minutes_until = (end_date - now).total_seconds() / 60
                                    except:
                                        pass
                                if minutes_until and 0 < minutes_until <= 20:
                                    market_data["category"] = "crypto"
                                    market_data["minutes_until"] = minutes_until
                                    markets.append(market_data)
                                    crypto_found += 1
                                    print(f"[HFT] Found crypto (scan): {slug[:40]}... ({minutes_until:.1f}m)", flush=True)
        except Exception as e:
            print(f"[HFT] General scan error: {e}", flush=True)

        print(f"[HFT] Crypto 15m markets found: {crypto_found}", flush=True)

        # ============================================================
        # FETCH LIVE SPORTS MARKETS
        # Query ALL events, filter by sports slug prefix + today's date + not ended
        # ============================================================
        print("[HFT] Fetching live sports events...", flush=True)

        # Check if we have real-time data from Sports WebSocket
        if live_sports_data:
            print(f"[HFT] Sports WebSocket has {len(live_sports_data)} live events tracked", flush=True)

        import re
        from zoneinfo import ZoneInfo

        # Use PST/PDT for determining "today"
        pst = ZoneInfo("America/Los_Angeles")
        now_pst = datetime.now(pst)
        today_str = now_pst.strftime("%Y-%m-%d")  # Today in PST
        yesterday_str = (now_pst - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_str = (now_pst + timedelta(days=1)).strftime("%Y-%m-%d")
        valid_dates = {today_str, yesterday_str, tomorrow_str}  # Allow all 3 for timezone flexibility
        print(f"[HFT] Valid dates (PST): {today_str}, {yesterday_str}, {tomorrow_str}", flush=True)

        # Sports slug prefixes - comprehensive list
        SPORTS_PREFIXES = [
            "nba-", "nfl-", "nhl-", "mlb-", "ncaa-", "cbb-", "cfb-", "wnba-",
            "ufc-", "mma-", "boxing-", "pfl-",
            "epl-", "laliga-", "bundesliga-", "seriea-", "ligue1-", "mls-", "ucl-",
            "scop-", "bra-", "arg-", "liga-",  # Scottish, Brazilian, Argentine leagues
            "ligamx-", "mexico-", "mex-", "clausura-", "apertura-",  # Liga MX / Mexican soccer
            "atp-", "wta-", "tennis-",
            "pga-", "lpga-", "golf-",
            "f1-", "nascar-", "indycar-",
            "cricket-", "ipl-", "rugby-",
        ]

        # Sports title keywords for fallback matching
        SPORTS_KEYWORDS = [
            "nba", "nfl", "nhl", "mlb", "ncaa", "wnba", "cbb", "cfb",
            "ufc", "mma", "boxing",
            "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "mls", "champions league",
            # Liga MX / Mexican soccer teams and keywords
            "liga mx", "ligamx", "clausura", "apertura",
            "america", "chivas", "cruz azul", "pumas", "tigres", "monterrey", "santos laguna",
            "toluca", "leon", "pachuca", "atlas", "guadalajara", "necaxa", "queretaro", "puebla",
            "mazatlan", "juarez", "tijuana", "san luis", "atletico san luis",
            "atp", "wta", "tennis",
            "pga", "lpga", "golf",
            "formula 1", "f1", "nascar", "indycar",
            # NBA teams
            "clippers", "lakers", "celtics", "warriors", "nets", "bulls", "heat", "knicks", "76ers", "bucks",
            "kings", "grizzlies", "blazers", "trail blazers", "suns", "mavericks", "nuggets", "spurs", "rockets",
            "timberwolves", "pelicans", "thunder", "jazz", "pistons", "pacers", "hornets", "magic", "hawks", "cavaliers", "raptors", "wizards",
            # NFL teams
            "patriots", "cowboys", "chiefs", "eagles", "49ers", "bills", "ravens", "dolphins", "jets", "giants",
            "bruins", "rangers", "maple leafs", "penguins", "blackhawks", "flyers", "capitals", "avalanche",
            "vs.", " v ", " vs "
        ]

        def is_sports_slug(slug: str) -> bool:
            """Check if slug starts with a known sports prefix"""
            slug_lower = slug.lower()
            return any(slug_lower.startswith(prefix) for prefix in SPORTS_PREFIXES)

        def is_sports_event(slug: str, title: str) -> bool:
            """Check if event is sports-related by slug prefix OR title keywords"""
            # First check slug prefix
            if is_sports_slug(slug):
                return True
            # Fallback: check title for sports keywords
            title_lower = title.lower()
            return any(kw in title_lower for kw in SPORTS_KEYWORDS)

        def extract_date_from_slug(slug: str) -> str | None:
            """Extract YYYY-MM-DD date from slug if present"""
            match = re.search(r'(\d{4}-\d{2}-\d{2})', slug)
            if match:
                return match.group(1)
            return None

        def is_event_live_or_upcoming(event: dict, max_hours_ahead: int = 24) -> bool:
            """Check if event is live or starting within max_hours_ahead hours"""
            now_utc = datetime.now(timezone.utc)

            # Check endDate - if ended more than 30 min ago, skip
            end_str = event.get("endDate") or event.get("endDateIso")
            if end_str:
                try:
                    if "T" in str(end_str):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if (end_dt - now_utc).total_seconds() < -1800:  # Ended 30+ min ago
                            return False
                except:
                    pass

            # Check startDate - if more than max_hours_ahead in future, skip
            start_str = event.get("startDate") or event.get("startDateIso") or event.get("createdAt")
            if start_str:
                try:
                    if "T" in str(start_str):
                        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        hours_until_start = (start_dt - now_utc).total_seconds() / 3600
                        if hours_until_start > max_hours_ahead:
                            return False  # Too far in future
                except:
                    pass

            return True

        def is_short_term_game(event: dict) -> bool:
            """Check if event is a short-term game (resolves within 48 hours) vs season-long future"""
            now_utc = datetime.now(timezone.utc)
            end_str = event.get("endDate") or event.get("endDateIso")
            if end_str:
                try:
                    if "T" in str(end_str):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        hours_until_end = (end_dt - now_utc).total_seconds() / 3600
                        # Short-term = resolves within 48 hours
                        return hours_until_end < 48 and hours_until_end > -0.5
                except:
                    pass
            # If no endDate, check if slug has today's date (indicates daily game)
            slug = event.get("slug", "")
            slug_date = extract_date_from_slug(slug)
            if slug_date and slug_date in valid_dates:
                return True
            return False

        # First try querying sports tag directly (tag_id 100639 is sports)
        sports_tag_events = []
        try:
            resp_tag = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "tag_id": 100639,  # Sports tag
                    "active": "true",
                    "closed": "false",
                    "limit": 500,
                },
                timeout=30
            )
            if resp_tag.status_code == 200:
                sports_tag_events = resp_tag.json()
                print(f"[HFT] Sports tag query returned: {len(sports_tag_events)} events", flush=True)
        except Exception as e:
            print(f"[HFT] Sports tag query error: {e}", flush=True)

        try:
            # Query ALL active events (no tag filter) - to catch any we might have missed
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 1000,  # Get more to find all sports
                },
                timeout=30
            )
            # Combine with sports tag events
            all_tag_slugs = {e.get("slug") for e in sports_tag_events}

            if resp.status_code == 200:
                events = resp.json()
                print(f"[HFT] Total active events: {len(events)}", flush=True)

                # Merge sports tag events that aren't already in the general events
                for tag_event in sports_tag_events:
                    if tag_event.get("slug") not in all_tag_slugs:
                        events.append(tag_event)

                # Add sports tag events to beginning (they're more likely to be sports)
                events = sports_tag_events + [e for e in events if e.get("slug") not in all_tag_slugs]
                print(f"[HFT] Combined events to check: {len(events)}", flush=True)

                # Debug: show sports events we're finding (by slug or title)
                sports_sample = []
                for e in events:
                    slug = e.get("slug", "")
                    title = e.get("title", "")
                    if is_sports_event(slug, title):
                        sports_sample.append(f"{slug[:40]}|{title[:30]}")
                        if len(sports_sample) >= 15:
                            break
                print(f"[DEBUG] Sports sample (slug|title): {sports_sample}", flush=True)

                live_events = 0
                short_term_games = 0
                long_term_futures = 0
                sports_checked = 0
                skipped_not_live = 0
                skipped_ended = 0

                for event in events:
                    event_slug = event.get("slug", "")
                    event_title = event.get("title", "")

                    # Filter 1: Must be a sports event (by slug OR title)
                    if not is_sports_event(event_slug, event_title):
                        continue
                    sports_checked += 1

                    # Filter 2: Check if not closed
                    if event.get("closed") == True:
                        continue

                    # Filter 3: Check if live or upcoming (within 24 hours)
                    # This replaces the strict date-in-slug check
                    if not is_event_live_or_upcoming(event, max_hours_ahead=24):
                        skipped_not_live += 1
                        continue

                    # Optional: If there IS a date in slug, verify it's valid
                    slug_date = extract_date_from_slug(event_slug)
                    if slug_date and slug_date not in valid_dates:
                        # Date in slug but it's old - check endDate as fallback
                        event_end_str = event.get("endDate") or event.get("endDateIso")
                        if event_end_str:
                            try:
                                if "T" in str(event_end_str):
                                    event_end = datetime.fromisoformat(event_end_str.replace("Z", "+00:00"))
                                    minutes_until_end = (event_end - now).total_seconds() / 60
                                    if minutes_until_end < -30:
                                        skipped_ended += 1
                                        continue  # Game is definitely over
                            except:
                                pass

                    # Categorize: short-term game vs long-term future
                    is_short = is_short_term_game(event)
                    if is_short:
                        short_term_games += 1

                    # This is a live sports event!
                    live_events += 1
                    # Log short-term games (live games) more prominently
                    if is_short and short_term_games <= 15:
                        print(f"[LIVE GAME] {event_slug[:55]} ({event_title[:25]})", flush=True)
                    elif not is_short and long_term_futures <= 5:
                        long_term_futures += 1
                        print(f"[FUTURE] {event_slug[:55]} ({event_title[:25]})", flush=True)

                    event_markets = event.get("markets", [])
                    for mkt in event_markets:
                        market_data = parse_market_data(mkt)
                        if market_data:
                            market_data["category"] = "sports"
                            market_data["event_title"] = event_title

                            # Calculate time until resolution
                            end_date_str = mkt.get("endDate") or event.get("endDate")
                            if end_date_str:
                                try:
                                    if "T" in str(end_date_str):
                                        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                                        market_data["minutes_until"] = (end_date - now).total_seconds() / 60
                                except:
                                    market_data["minutes_until"] = None

                            markets.append(market_data)
                            sports_found += 1

                print(f"[HFT] Sports: {sports_checked} total, {short_term_games} live games, {live_events - short_term_games} futures", flush=True)
                print(f"[HFT] Found {live_events} events with {sports_found} markets ({skipped_not_live} skipped not live, {skipped_ended} ended)", flush=True)

        except Exception as e:
            print(f"[HFT] Sports /events error: {e}", flush=True)

        # ADDITIONAL: Search for today's games by date in slug
        # This specifically targets "nba-xxx-xxx-2026-02-07" style slugs
        for date_to_search in [today_str, yesterday_str]:
            try:
                resp_date = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "slug_contains": date_to_search,
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                    },
                    timeout=15
                )
                if resp_date.status_code == 200:
                    date_markets = resp_date.json()
                    date_found = 0
                    for mkt in date_markets:
                        slug = mkt.get("slug", "")
                        if any(m["slug"] == slug for m in markets):
                            continue
                        if is_sports_slug(slug):
                            market_data = parse_market_data(mkt)
                            if market_data:
                                market_data["category"] = "sports"
                                markets.append(market_data)
                                sports_found += 1
                                date_found += 1
                    if date_found > 0:
                        print(f"[HFT] Date search '{date_to_search}' found {date_found} additional markets", flush=True)
            except Exception as e:
                pass  # Date search is optional

        # ADDITIONAL: Direct search for NBA games
        # NBA games might use different slug patterns
        NBA_VALIDATORS = ["nba", "basketball", "lakers", "celtics", "warriors", "nets", "bulls", "heat",
                          "knicks", "76ers", "bucks", "clippers", "suns", "mavericks", "nuggets", "grizzlies",
                          "timberwolves", "pelicans", "thunder", "jazz", "kings", "blazers", "rockets", "spurs"]
        for nba_search in ["nba-", "basketball-"]:
            try:
                resp_nba = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "slug_contains": nba_search,
                        "active": "true",
                        "closed": "false",
                        "limit": 50,
                    },
                    timeout=15
                )
                if resp_nba.status_code == 200:
                    nba_markets = resp_nba.json()
                    nba_found = 0
                    for mkt in nba_markets:
                        slug = mkt.get("slug", "").lower()
                        question = mkt.get("question", "").lower()
                        # VALIDATE: Must actually be NBA-related (API returns garbage sometimes)
                        is_nba = any(v in slug or v in question for v in NBA_VALIDATORS)
                        if not is_nba:
                            continue
                        # Exclude NFL/Super Bowl false positives
                        if "super-bowl" in slug or "super bowl" in question or "patriots" in slug or "seahawks" in slug:
                            continue
                        if any(m["slug"] == mkt.get("slug", "") for m in markets):
                            continue
                        # Check if it's a short-term game
                        end_str = mkt.get("endDate") or mkt.get("endDateIso")
                        if end_str:
                            try:
                                if "T" in str(end_str):
                                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                    hours_until = (end_dt - now).total_seconds() / 3600
                                    if hours_until < -0.5 or hours_until > 48:
                                        continue  # Not a current game
                            except:
                                pass
                        market_data = parse_market_data(mkt)
                        if market_data:
                            market_data["category"] = "sports"
                            markets.append(market_data)
                            sports_found += 1
                            nba_found += 1
                            if nba_found <= 5:
                                print(f"[NBA] Found: {mkt.get('slug', '')[:50]}", flush=True)
                    if nba_found > 0:
                        print(f"[HFT] NBA search '{nba_search}' found {nba_found} markets", flush=True)
            except Exception as e:
                pass  # NBA search is optional

        # ADDITIONAL: Direct search for Liga MX (Mexican soccer) games
        LIGAMX_VALIDATORS = ["liga mx", "ligamx", "clausura", "apertura", "mexico soccer",
                             "chivas", "tigres", "america", "cruz azul", "pumas", "monterrey",
                             "santos laguna", "toluca", "leon", "pachuca", "atlas", "necaxa",
                             "queretaro", "puebla", "mazatlan", "juarez", "tijuana", "san luis"]
        for ligamx_search in ["ligamx-", "clausura-", "apertura-", "chivas", "tigres", "cruz-azul", "pumas-"]:
            try:
                resp_mx = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "slug_contains": ligamx_search,
                        "active": "true",
                        "closed": "false",
                        "limit": 50,
                    },
                    timeout=15
                )
                if resp_mx.status_code == 200:
                    mx_markets = resp_mx.json()
                    mx_found = 0
                    for mkt in mx_markets:
                        slug = mkt.get("slug", "").lower()
                        question = mkt.get("question", "").lower()
                        # VALIDATE: Must actually be Liga MX-related (API returns garbage sometimes)
                        is_ligamx = any(v in slug or v in question for v in LIGAMX_VALIDATORS)
                        if not is_ligamx:
                            continue
                        # Exclude non-soccer false positives (e.g., "america" could match US politics)
                        if "president" in slug or "election" in question or "trump" in slug or "biden" in slug:
                            continue
                        if any(m["slug"] == mkt.get("slug", "") for m in markets):
                            continue
                        # Check if it's a short-term game
                        end_str = mkt.get("endDate") or mkt.get("endDateIso")
                        if end_str:
                            try:
                                if "T" in str(end_str):
                                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                    hours_until = (end_dt - now).total_seconds() / 3600
                                    if hours_until < -0.5 or hours_until > 48:
                                        continue  # Not a current game
                            except:
                                pass
                        market_data = parse_market_data(mkt)
                        if market_data:
                            market_data["category"] = "sports"
                            markets.append(market_data)
                            sports_found += 1
                            mx_found += 1
                            if mx_found <= 5:
                                print(f"[LIGA MX] Found: {mkt.get('slug', '')[:50]}", flush=True)
                    if mx_found > 0:
                        print(f"[HFT] Liga MX search '{ligamx_search}' found {mx_found} markets", flush=True)
            except Exception as e:
                pass  # Liga MX search is optional

        # FALLBACK: Also try /markets directly if /events didn't find much
        if sports_found < 10:
            print(f"[HFT] Trying /markets fallback...", flush=True)
            try:
                resp2 = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 500,
                    },
                    timeout=30
                )
                if resp2.status_code == 200:
                    all_markets = resp2.json()
                    fallback_found = 0
                    for mkt in all_markets:
                        slug = mkt.get("slug", "")
                        question = mkt.get("question", "")
                        # Check by slug prefix OR question keywords
                        if not is_sports_event(slug, question):
                            continue
                        # Check if already added
                        if any(m["slug"] == slug for m in markets):
                            continue
                        # Check if market appears active (not ended)
                        end_str = mkt.get("endDate") or mkt.get("endDateIso")
                        if end_str:
                            try:
                                if "T" in str(end_str):
                                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                    if (end_dt - now).total_seconds() < -1800:  # Ended 30+ min ago
                                        continue
                            except:
                                pass
                        market_data = parse_market_data(mkt)
                        if market_data:
                            market_data["category"] = "sports"
                            markets.append(market_data)
                            sports_found += 1
                            fallback_found += 1
                    print(f"[HFT] Fallback found {fallback_found} additional sports markets", flush=True)
            except Exception as e:
                print(f"[HFT] Fallback error: {e}", flush=True)

        if sports_found == 0:
            print("[HFT] No live sports events found", flush=True)

        print(f"[HFT] Total: {crypto_found} crypto, {sports_found} sports = {len(markets)} markets", flush=True)
        return markets

    except Exception as e:
        import traceback
        print(f"[HFT] Market discovery failed: {e}", flush=True)
        traceback.print_exc()
        return []


def parse_market_data(raw: dict) -> dict | None:
    """Parse raw market into standard format"""
    clob_ids_raw = raw.get("clobTokenIds", "[]")
    prices_raw = raw.get("outcomePrices", "[]")

    # Handle JSON strings
    if isinstance(clob_ids_raw, str):
        try:
            clob_ids = json.loads(clob_ids_raw)
        except:
            return None
    else:
        clob_ids = clob_ids_raw or []

    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except:
            return None
    else:
        prices = prices_raw or []

    if len(clob_ids) != 2 or len(prices) != 2:
        return None

    try:
        return {
            "slug": raw.get("slug", ""),
            "question": raw.get("question", ""),
            "token_a_id": str(clob_ids[0]),
            "token_b_id": str(clob_ids[1]),
            "price_sum_indicative": float(prices[0]) + float(prices[1]),
            "volume_24h": float(raw.get("volume24hr") or raw.get("volume", 0) or 0),
        }
    except (ValueError, TypeError):
        return None


# =============================================================================
# SCANNER MANAGEMENT
# =============================================================================

def start_hft_scanner(
    mode: str,
    scan_interval_ms: int = 500,
    enable_sum_to_one: bool = True,
    enable_tail_end: bool = True,
    use_websocket: bool = True,  # Default to WebSocket for real-time detection
):
    """Start the HFT scanner with configurable engines

    Args:
        mode: Trading mode ('demo' or 'live')
        scan_interval_ms: Polling interval (ignored in WebSocket mode)
        enable_sum_to_one: Enable sum-to-one arbitrage engine
        enable_tail_end: Enable tail-end arbitrage engine
        use_websocket: Use WebSocket for real-time price feeds (recommended)
    """
    global hft_client, hft_scanner, active_markets, server_state, sports_ws

    if server_state["is_running"]:
        return False, "Scanner already running"

    # Start Sports WebSocket for real-time game data
    print("[HFT] Starting Sports WebSocket...", flush=True)
    try:
        sports_ws = SportsWebSocket(
            on_update=on_sports_update,
            on_connect=on_sports_connect,
            on_disconnect=on_sports_disconnect,
        )
        sports_ws.start()
        # Give it a moment to connect and receive initial data
        time.sleep(2)
    except Exception as e:
        print(f"[HFT] Sports WebSocket error (continuing without): {e}", flush=True)

    # Discover markets first
    print("[HFT] Discovering markets...")
    active_markets = discover_markets()

    if not active_markets:
        return False, "No markets found to monitor"

    print(f"[HFT] Found {len(active_markets)} markets to monitor")

    # Configure engines - execute on ANY price_sum < $1.00
    sum_to_one_config = SumToOneConfig(
        max_price_sum=1.0,        # Execute if total < $1.00
        min_edge_after_fees=0.0,  # No minimum edge required
        max_position_usd=100.0,
    )

    tail_end_config = TailEndConfig(
        min_probability=0.90,       # 90% = execute
        max_probability=1.0,        # No max
        min_price=0.90,             # 90¢ = execute
        max_price=1.0,              # No max
        max_minutes_until_resolution=10000,  # Ignore time
        min_minutes_until_resolution=0,      # Ignore time
        min_volume_24h=0.0,         # No volume requirement for demo
        max_position_usd=50.0,
        min_risk_adjusted_ev=0.0,   # Disable EV filter - sports edge comes from game state, not market price
        min_depth_usd=5.0,          # Lower depth requirement to catch more opportunities
    )

    # Create HFT client
    config = HFTConfig.from_env()
    trading_mode = TradingMode.LIVE if mode == "live" else TradingMode.DEMO

    hft_client = HFTClient(
        config,
        mode=trading_mode,
        sum_to_one_config=sum_to_one_config,
        tail_end_config=tail_end_config,
    )
    hft_client.on_trade(on_trade_callback)
    hft_client.on_signal(on_signal_callback)

    # Enable/disable engines based on config
    if not enable_sum_to_one:
        hft_client.disable_engine(EngineType.SUM_TO_ONE)
    if not enable_tail_end:
        hft_client.disable_engine(EngineType.TAIL_END)

    # Create scanner - WebSocket (real-time) or Polling mode
    scanner_mode = "websocket" if use_websocket and HAS_WS_SCANNER else "polling"

    if use_websocket and HAS_WS_SCANNER:
        # WebSocket mode: Real-time price feeds, instant signal detection
        print(f"[HFT] Starting WebSocket scanner (real-time mode)...", flush=True)
        hft_scanner = HFTWebSocketScanner(
            client=hft_client,
            markets=active_markets,
        )
    else:
        # Polling mode: Scan every N milliseconds
        if use_websocket and not HAS_WS_SCANNER:
            print(f"[HFT] WebSocket not available, falling back to polling mode", flush=True)
        print(f"[HFT] Starting polling scanner ({scan_interval_ms}ms interval)...", flush=True)
        hft_scanner = HFTScanner(
            client=hft_client,
            markets=active_markets,
            scan_interval_ms=scan_interval_ms,
        )

    # Start scanner
    hft_scanner.start()

    # Start market refresh thread (to pick up new live events)
    global refresh_thread, stop_refresh
    stop_refresh.clear()
    refresh_thread = threading.Thread(target=market_refresh_loop, daemon=True)
    refresh_thread.start()

    server_state["mode"] = mode
    server_state["scanner_mode"] = scanner_mode
    server_state["is_running"] = True
    server_state["started_at"] = datetime.now(timezone.utc).isoformat()

    enabled = hft_client.get_enabled_engines()
    return True, f"Started in {mode} mode ({scanner_mode}) with {len(active_markets)} markets. Engines: {', '.join(enabled)}. Market refresh every {MARKET_REFRESH_INTERVAL_MINUTES}min."


def stop_hft_scanner():
    """Stop the HFT scanner"""
    global hft_scanner, server_state, refresh_thread, stop_refresh, sports_ws

    if not server_state["is_running"]:
        return False, "Scanner not running"

    # Stop refresh thread
    stop_refresh.set()
    if refresh_thread and refresh_thread.is_alive():
        refresh_thread.join(timeout=5)

    # Stop sports WebSocket
    if sports_ws:
        try:
            sports_ws.stop()
        except:
            pass

    if hft_scanner:
        hft_scanner.stop()

    server_state["is_running"] = False

    return True, "Scanner stopped"


def market_refresh_loop():
    """Periodically refresh markets to pick up new live events"""
    global active_markets, hft_scanner

    print(f"[HFT] Market refresh thread started (every {MARKET_REFRESH_INTERVAL_MINUTES} minutes)")

    while not stop_refresh.is_set():
        # Wait for the interval (checking stop flag every second)
        for _ in range(MARKET_REFRESH_INTERVAL_MINUTES * 60):
            if stop_refresh.is_set():
                break
            time.sleep(1)

        if stop_refresh.is_set():
            break

        # Refresh markets
        try:
            print(f"[HFT] Refreshing markets...", flush=True)
            new_markets = discover_markets()

            if new_markets and hft_scanner:
                active_markets = new_markets
                hft_scanner.update_markets(new_markets)
            else:
                print(f"[HFT] No markets found during refresh, keeping existing", flush=True)

        except Exception as e:
            print(f"[HFT] Market refresh error: {e}", flush=True)

    print("[HFT] Market refresh thread stopped")


# =============================================================================
# API ROUTES
# =============================================================================

@app.route('/')
def index():
    """Serve the HFT dashboard"""
    # Serve from static to avoid Jinja2 interpreting React's {{ }} syntax
    return app.send_static_file('hft_dashboard.html')


@app.route('/api/status')
def api_status():
    """Get server status"""
    stats = {}
    if hft_client:
        stats = hft_client.get_stats()

    scanner_stats = {}
    if hft_scanner:
        scanner_stats = hft_scanner.get_stats()

    return jsonify({
        "mode": server_state["mode"],
        "scanner_mode": server_state.get("scanner_mode", "polling"),
        "is_running": server_state["is_running"],
        "started_at": server_state["started_at"],
        "markets_monitored": len(active_markets),
        "client_stats": stats,
        "scanner_stats": scanner_stats,
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    """Start the HFT scanner"""
    data = request.get_json() or {}
    mode = data.get('mode', 'demo')
    scan_interval_ms = data.get('scan_interval_ms', 500)
    enable_sum_to_one = data.get('enable_sum_to_one', True)
    enable_tail_end = data.get('enable_tail_end', True)
    use_websocket = data.get('use_websocket', True)  # Default to WebSocket mode

    if mode == 'live':
        # Require explicit confirmation for live mode
        if not data.get('confirm_live'):
            return jsonify({
                "error": "Live mode requires confirmation",
                "message": "Set confirm_live=true to enable live trading"
            }), 403

    success, message = start_hft_scanner(
        mode,
        scan_interval_ms,
        enable_sum_to_one=enable_sum_to_one,
        enable_tail_end=enable_tail_end,
        use_websocket=use_websocket,
    )

    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"error": message}), 400


@app.route('/api/engines', methods=['GET'])
def api_engines():
    """Get engine status"""
    if hft_client is None:
        return jsonify({
            "sum_to_one": {"enabled": True, "stats": {}},
            "tail_end": {"enabled": True, "stats": {}},
        })

    stats = hft_client.engine_manager.get_stats()
    return jsonify(stats)


@app.route('/api/debug/sum-to-one')
def api_debug_sum_to_one():
    """Debug endpoint showing why sum-to-one signals aren't triggering"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    s2o = hft_client.engine_manager.sum_to_one
    return jsonify({
        "sum_to_one_debug": {
            "enabled": s2o.enabled,
            "signals_generated": s2o.signals_generated,
            "config": {
                "max_price_sum": s2o.config.max_price_sum,
                "fee_rate": s2o.config.fee_rate,
                "min_edge_after_fees": s2o.config.min_edge_after_fees,
                "min_depth_usd": s2o.config.min_depth_usd,
                "crypto_15min_only": s2o.config.crypto_15min_only,
            },
            "rejection_counts": {
                "not_crypto_15min": s2o.debug_not_crypto,
                "missing_asks_ORDERBOOK_FAILED": s2o.debug_missing_asks,
                "price_sum_too_high_gte_1": s2o.debug_price_sum_high,
                "edge_too_low_after_fees": s2o.debug_low_edge,
                "depth_too_low": s2o.debug_low_depth,
                "crypto_markets_checked": s2o.debug_markets_checked,
                "PASSED_ALL_CHECKS": s2o.debug_passed,
            },
            "best_price_sum_seen": round(s2o.debug_best_price_sum, 4),
            "interpretation": _interpret_s2o_stats(s2o),
        }
    })


def _interpret_s2o_stats(s2o) -> str:
    """Interpret sum-to-one debug stats"""
    if s2o.debug_markets_checked == 0:
        return f"PROBLEM: No crypto markets being checked! {s2o.debug_not_crypto} markets rejected as not crypto 15-min."
    if s2o.debug_missing_asks > s2o.debug_markets_checked * 0.5:
        return f"PROBLEM: {s2o.debug_missing_asks} order book fetches failed! Check CLOB API connectivity."
    if s2o.debug_best_price_sum >= 1.0:
        return f"Market is efficient. Best price_sum seen: {s2o.debug_best_price_sum:.4f} (need < 1.00 for opportunity)"
    if s2o.debug_best_price_sum < 1.0 and s2o.debug_low_edge > 0:
        return f"Opportunities exist but filtered by fees. Best: {s2o.debug_best_price_sum:.4f}, need < {1.0 - s2o.config.fee_rate:.2f} after {s2o.config.fee_rate*100}% fee"
    if s2o.debug_passed > 0:
        return f"SUCCESS: {s2o.debug_passed} opportunities passed all checks!"
    return "Check individual rejection counts."


@app.route('/api/debug/tail-end')
def api_debug_tail_end():
    """Debug endpoint showing why tail-end signals aren't triggering"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    te = hft_client.engine_manager.tail_end
    return jsonify({
        "tail_end_debug": {
            "enabled": te.enabled,
            "signals_generated": te.signals_generated,
            "config": {
                "min_probability": te.config.min_probability,
                "max_probability": te.config.max_probability,
                "min_price": te.config.min_price,
                "max_price": te.config.max_price,
                "min_depth_usd": te.config.min_depth_usd,
                "min_risk_adjusted_ev": te.config.min_risk_adjusted_ev,
                "sports_only": te.config.sports_only,
            },
            "rejection_counts": {
                "not_sports_category": te.debug_not_sports,
                "no_resolution_time": te.debug_no_resolution_time,
                "position_limit_hit": te.debug_position_limit,
                "already_have_position": te.debug_already_positioned,
                "resolution_too_far": te.debug_resolution_too_far,
                "resolution_too_close": te.debug_resolution_too_close,
                "volume_too_low": te.debug_low_volume,
                "tokens_checked": te.debug_checked_tokens,
                "probability_too_low": te.debug_low_prob,
                "probability_too_high": te.debug_high_prob,
                "price_too_low": te.debug_low_price,
                "price_too_high": te.debug_high_price,
                "depth_too_low": te.debug_low_depth,
                "risk_adjusted_ev_too_low": te.debug_low_ev,
                "PASSED_ALL_CHECKS": te.debug_passed_all,
            },
            "interpretation": _interpret_debug_stats(te),
        }
    })


def _interpret_debug_stats(te) -> str:
    """Provide human-readable interpretation of debug stats"""
    if te.debug_not_sports > 0 and te.debug_checked_tokens == 0:
        return f"PROBLEM: {te.debug_not_sports} markets rejected because market_category != 'sports'. Check if markets are being labeled correctly."
    if te.debug_no_resolution_time > 0 and te.debug_checked_tokens == 0:
        return f"PROBLEM: {te.debug_no_resolution_time} markets have no resolution time set. Check minutes_until_resolution in market data."
    if te.debug_checked_tokens == 0:
        return "PROBLEM: No tokens are being checked at all. Markets are failing pre-checks before token analysis."
    if te.debug_low_prob > te.debug_checked_tokens * 0.9:
        return f"Most rejections ({te.debug_low_prob}) due to probability < {te.config.min_probability*100}%. Markets don't have high-confidence outcomes."
    if te.debug_low_depth > 0:
        return f"{te.debug_low_depth} rejections due to low depth (< ${te.config.min_depth_usd}). Check order book depth."
    if te.debug_low_ev > 0:
        return f"{te.debug_low_ev} rejections due to risk-adjusted EV < {te.config.min_risk_adjusted_ev}. This filter may be too strict."
    if te.debug_passed_all > 0:
        return f"SUCCESS: {te.debug_passed_all} opportunities passed all checks! Check why signals aren't executing."
    return "Unknown issue - check individual rejection counts."


@app.route('/api/debug/books')
def api_debug_books():
    """Debug endpoint showing order book fetch stats"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    book_stats = getattr(hft_client, '_book_stats', {'success': 0, 'fail_404': 0, 'fail_other': 0})
    book_errors = getattr(hft_client, '_book_errors', {})

    return jsonify({
        "order_book_stats": {
            "successful_fetches": book_stats.get('success', 0),
            "failed_404_no_liquidity": book_stats.get('fail_404', 0),
            "failed_other_errors": book_stats.get('fail_other', 0),
            "unique_tokens_with_errors": len(book_errors),
            "success_rate_pct": round(
                book_stats.get('success', 0) / max(1, sum(book_stats.values())) * 100, 1
            ),
        },
        "interpretation": _interpret_book_stats(book_stats),
    })


def _interpret_book_stats(stats: dict) -> str:
    """Interpret book stats"""
    total = sum(stats.values())
    if total == 0:
        return "No order book fetches attempted yet."
    success_rate = stats.get('success', 0) / total
    if stats.get('fail_404', 0) > stats.get('success', 0):
        return f"PROBLEM: More 404s than successes. Most markets don't have CLOB liquidity. Focus on markets with active trading."
    if success_rate < 0.1:
        return f"PROBLEM: Only {success_rate*100:.1f}% of book fetches succeed. API may be down or rate limited."
    if stats.get('success', 0) > 0:
        return f"Order books working: {stats.get('success', 0)} successful fetches at {success_rate*100:.1f}% success rate."
    return "Check individual stats for details."


@app.route('/api/debug/crypto-raw')
def api_debug_crypto_raw():
    """Show ALL crypto markets in scanner with raw data - no filtering"""
    if hft_scanner is None:
        return jsonify({"error": "Scanner not running"}), 400

    crypto_markets = [m for m in hft_scanner.markets if m.get("category") == "crypto"]

    results = []
    for m in crypto_markets:
        token_a = m.get("token_a_id", "")
        token_b = m.get("token_b_id", "")
        book_a = hft_client.fetch_order_book(token_a) if token_a else None
        book_b = hft_client.fetch_order_book(token_b) if token_b else None

        results.append({
            "slug": m.get("slug"),
            "category": m.get("category"),
            "minutes_until": m.get("minutes_until"),
            "passes_20min_filter": m.get("minutes_until") is not None and m.get("minutes_until") <= 20,
            "YES_ask": book_a.best_ask if book_a else "FAILED",
            "YES_bid": book_a.best_bid if book_a else "FAILED",
            "NO_ask": book_b.best_ask if book_b else "FAILED",
            "NO_bid": book_b.best_bid if book_b else "FAILED",
            "price_sum": round((book_a.best_ask or 0) + (book_b.best_ask or 0), 4) if book_a and book_b else "N/A",
        })

    return jsonify({
        "total_crypto_in_scanner": len(crypto_markets),
        "pass_20min_filter": len([r for r in results if r.get("passes_20min_filter")]),
        "fail_20min_filter": len([r for r in results if not r.get("passes_20min_filter")]),
        "crypto_markets": results,
    })


@app.route('/api/debug/scanner-view')
def api_debug_scanner_view():
    """Show exactly what the scanner sees - markets, prices, why rejected"""
    if hft_scanner is None:
        return jsonify({"error": "Scanner not running"}), 400

    # Get current markets being scanned
    markets = hft_scanner.markets[:20]  # First 20

    results = []
    for m in markets:
        # Fetch live order book
        token_a = m.get("token_a_id", "")
        token_b = m.get("token_b_id", "")

        book_a = hft_client.fetch_order_book(token_a) if token_a else None
        book_b = hft_client.fetch_order_book(token_b) if token_b else None

        yes_ask = book_a.best_ask if book_a else None
        no_ask = book_b.best_ask if book_b else None
        yes_bid = book_a.best_bid if book_a else None
        no_bid = book_b.best_bid if book_b else None

        # Calculate what scanner sees
        yes_prob = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask
        no_prob = (no_bid + no_ask) / 2 if no_bid and no_ask else no_ask

        results.append({
            "slug": m.get("slug", "")[:50],
            "category": m.get("category"),
            "minutes_until": m.get("minutes_until"),
            "YES_ask": yes_ask,
            "YES_bid": yes_bid,
            "YES_prob_calc": round(yes_prob, 4) if yes_prob else None,
            "NO_ask": no_ask,
            "NO_bid": no_bid,
            "NO_prob_calc": round(no_prob, 4) if no_prob else None,
            "would_trigger_90pct": (yes_prob and yes_prob >= 0.9) or (no_prob and no_prob >= 0.9),
            "book_a_ok": book_a is not None,
            "book_b_ok": book_b is not None,
        })

    # Count categories
    sports_count = len([m for m in hft_scanner.markets if m.get("category") == "sports"])
    crypto_count = len([m for m in hft_scanner.markets if m.get("category") == "crypto"])

    return jsonify({
        "total_markets_in_scanner": len(hft_scanner.markets),
        "sports_markets": sports_count,
        "crypto_markets": crypto_count,
        "sample_markets": results,
    })


@app.route('/api/debug/find-game/<search>')
def api_debug_find_game(search):
    """Search for a specific game in scanner's market list"""
    if hft_scanner is None:
        return jsonify({"error": "Scanner not running"}), 400

    search_lower = search.lower()
    matches = []

    for m in hft_scanner.markets:
        slug = m.get("slug", "").lower()
        question = m.get("question", "").lower()

        if search_lower in slug or search_lower in question:
            # Fetch live prices
            token_a = m.get("token_a_id", "")
            token_b = m.get("token_b_id", "")

            book_a = hft_client.fetch_order_book(token_a) if token_a else None
            book_b = hft_client.fetch_order_book(token_b) if token_b else None

            matches.append({
                "slug": m.get("slug"),
                "question": m.get("question", "")[:80],
                "category": m.get("category"),
                "minutes_until": m.get("minutes_until"),
                "token_a_id": token_a[:20] + "..." if token_a else None,
                "token_b_id": token_b[:20] + "..." if token_b else None,
                "YES_ask": book_a.best_ask if book_a else "BOOK_FAILED",
                "YES_bid": book_a.best_bid if book_a else "BOOK_FAILED",
                "NO_ask": book_b.best_ask if book_b else "BOOK_FAILED",
                "NO_bid": book_b.best_bid if book_b else "BOOK_FAILED",
            })

    return jsonify({
        "search": search,
        "matches_found": len(matches),
        "matches": matches[:10],
        "hint": "If 0 matches, the game isn't in scanner's market list - that's the bug"
    })


@app.route('/api/debug/test-book/<slug>')
def api_debug_test_book(slug):
    """Test fetching order book for a specific market by slug"""
    import requests as req

    # Find market by slug
    try:
        resp = req.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 1},
            timeout=10
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Gamma API returned {resp.status_code}"}), 400

        markets = resp.json()
        if not markets:
            return jsonify({"error": f"Market not found: {slug}"}), 404

        market = markets[0] if isinstance(markets, list) else markets

        # Extract token IDs
        clob_ids_raw = market.get("clobTokenIds", "[]")
        if isinstance(clob_ids_raw, str):
            clob_ids = json.loads(clob_ids_raw)
        else:
            clob_ids = clob_ids_raw or []

        result = {
            "slug": slug,
            "question": market.get("question", "")[:100],
            "clobTokenIds_raw": clob_ids_raw,
            "clobTokenIds_parsed": clob_ids,
            "outcomes": market.get("outcomes"),
            "token_tests": []
        }

        # Test fetching each token's order book
        for i, token_id in enumerate(clob_ids):
            token_result = {"token_id": str(token_id), "index": i}
            try:
                book_resp = req.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": str(token_id)},
                    timeout=5
                )
                token_result["status_code"] = book_resp.status_code
                if book_resp.status_code == 200:
                    book_data = book_resp.json()
                    token_result["success"] = True
                    token_result["bids_count"] = len(book_data.get("bids", []))
                    token_result["asks_count"] = len(book_data.get("asks", []))
                    if book_data.get("bids"):
                        token_result["best_bid"] = book_data["bids"][0]
                    if book_data.get("asks"):
                        token_result["best_ask"] = book_data["asks"][0]
                else:
                    token_result["success"] = False
                    token_result["error"] = book_resp.text[:200]
            except Exception as e:
                token_result["success"] = False
                token_result["error"] = str(e)

            result["token_tests"].append(token_result)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/engines/toggle', methods=['POST'])
def api_toggle_engine():
    """Toggle an engine on/off"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    data = request.get_json() or {}
    engine_name = data.get('engine')

    if engine_name == 'sum_to_one':
        new_state = hft_client.toggle_engine(EngineType.SUM_TO_ONE)
        return jsonify({"engine": "sum_to_one", "enabled": new_state})
    elif engine_name == 'tail_end':
        new_state = hft_client.toggle_engine(EngineType.TAIL_END)
        return jsonify({"engine": "tail_end", "enabled": new_state})
    else:
        return jsonify({"error": f"Unknown engine: {engine_name}"}), 400


@app.route('/api/engines/<engine_name>/enable', methods=['POST'])
def api_enable_engine(engine_name):
    """Enable a specific engine"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    if engine_name == 'sum_to_one':
        hft_client.enable_engine(EngineType.SUM_TO_ONE)
        return jsonify({"engine": "sum_to_one", "enabled": True})
    elif engine_name == 'tail_end':
        hft_client.enable_engine(EngineType.TAIL_END)
        return jsonify({"engine": "tail_end", "enabled": True})
    else:
        return jsonify({"error": f"Unknown engine: {engine_name}"}), 400


@app.route('/api/engines/<engine_name>/disable', methods=['POST'])
def api_disable_engine(engine_name):
    """Disable a specific engine"""
    if hft_client is None:
        return jsonify({"error": "Scanner not initialized"}), 400

    if engine_name == 'sum_to_one':
        hft_client.disable_engine(EngineType.SUM_TO_ONE)
        return jsonify({"engine": "sum_to_one", "enabled": False})
    elif engine_name == 'tail_end':
        hft_client.disable_engine(EngineType.TAIL_END)
        return jsonify({"engine": "tail_end", "enabled": False})
    else:
        return jsonify({"error": f"Unknown engine: {engine_name}"}), 400


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop the HFT scanner"""
    success, message = stop_hft_scanner()

    if success:
        return jsonify({"success": True, "message": message})
    return jsonify({"error": message}), 400


# =============================================================================
# COPY TRADER ENDPOINTS
# =============================================================================

def copy_trader_loop():
    """Background loop for copy trader"""
    global copy_trader
    if not copy_trader:
        return

    print(f"[COPY] Background monitoring started (every 10s)...", flush=True)

    while not stop_copy_trader.is_set():
        try:
            copied = copy_trader.check_and_copy()
            if copied > 0:
                print(f"[COPY] Copied {copied} trade(s)", flush=True)
        except Exception as e:
            print(f"[COPY] Error in loop: {e}", flush=True)

        stop_copy_trader.wait(timeout=10)  # Check every 10 seconds

    print("[COPY] Background monitoring stopped", flush=True)


@app.route('/api/copy-trader/start', methods=['POST'])
def api_copy_trader_start():
    """Start copy trading"""
    global copy_trader, copy_trader_thread, stop_copy_trader

    if not HAS_COPY_TRADER:
        return jsonify({"error": "Copy trader module not available"}), 400

    if copy_trader_thread and copy_trader_thread.is_alive():
        return jsonify({"error": "Copy trader already running"}), 400

    data = request.get_json() or {}
    live_mode = data.get('live', False)
    crypto_only = data.get('crypto_only', True)

    # Require confirmation for live mode
    if live_mode and not data.get('confirm_live'):
        return jsonify({
            "error": "Live mode requires confirmation",
            "message": "Set confirm_live=true to enable live copy trading"
        }), 403

    # Create copy trader with dashboard callback
    copy_trader = CopyTrader(
        dry_run=not live_mode,
        crypto_only=crypto_only,
        on_trade=on_copy_trade
    )
    copy_trader.start()

    # Start background thread
    stop_copy_trader.clear()
    copy_trader_thread = threading.Thread(target=copy_trader_loop, daemon=True)
    copy_trader_thread.start()

    mode_str = "LIVE" if live_mode else "DRY RUN"
    return jsonify({
        "success": True,
        "message": f"Copy trader started in {mode_str} mode, following {copy_trader.target_name}",
        "target": TARGET_ADDRESS,
    })


@app.route('/api/copy-trader/stop', methods=['POST'])
def api_copy_trader_stop():
    """Stop copy trading"""
    global copy_trader, copy_trader_thread, stop_copy_trader

    if not copy_trader_thread or not copy_trader_thread.is_alive():
        return jsonify({"error": "Copy trader not running"}), 400

    stop_copy_trader.set()
    copy_trader_thread.join(timeout=5)

    stats = {
        "trades_copied": copy_trader.trades_copied if copy_trader else 0,
        "trades_skipped": copy_trader.trades_skipped if copy_trader else 0,
        "total_spent": copy_trader.total_spent if copy_trader else 0,
    }

    copy_trader = None

    return jsonify({
        "success": True,
        "message": "Copy trader stopped",
        "stats": stats
    })


@app.route('/api/copy-trader/status')
def api_copy_trader_status():
    """Get copy trader status"""
    if not HAS_COPY_TRADER:
        return jsonify({
            "available": False,
            "running": False,
            "error": "Copy trader module not available"
        })

    running = copy_trader_thread and copy_trader_thread.is_alive()

    status = {
        "available": True,
        "running": running,
        "target_address": TARGET_ADDRESS,
    }

    if copy_trader:
        status.update({
            "target_name": copy_trader.target_name,
            "dry_run": copy_trader.dry_run,
            "crypto_only": copy_trader.crypto_only,
            "trades_copied": copy_trader.trades_copied,
            "trades_skipped": copy_trader.trades_skipped,
            "total_spent": copy_trader.total_spent,
        })

    return jsonify(status)


@app.route('/api/trades')
def api_trades():
    """Get trade history"""
    mode = request.args.get('mode', 'all')
    limit = int(request.args.get('limit', 50))

    trades = []

    if mode in ['all', 'demo']:
        trades.extend([t.to_dict() for t in list(demo_trades)[-limit:]])

    if mode in ['all', 'live']:
        trades.extend([t.to_dict() for t in list(live_trades)[-limit:]])

    if mode in ['all', 'copy']:
        trades.extend(list(copy_trades)[-limit:])

    # Sort by timestamp
    trades.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    return jsonify(trades[:limit])


@app.route('/api/copy-trader/trades')
def api_copy_trades():
    """Get copy trader trade history"""
    limit = int(request.args.get('limit', 50))
    trades = list(copy_trades)[-limit:]
    trades.reverse()  # Most recent first
    return jsonify(trades)


@app.route('/api/trades/demo')
def api_trades_demo():
    """Get demo trades"""
    limit = int(request.args.get('limit', 50))
    trades = [t.to_dict() for t in list(demo_trades)[-limit:]]
    return jsonify(trades)


@app.route('/api/trades/live')
def api_trades_live():
    """Get live trades"""
    limit = int(request.args.get('limit', 50))
    trades = [t.to_dict() for t in list(live_trades)[-limit:]]
    return jsonify(trades)


@app.route('/api/opportunities')
def api_opportunities():
    """Get opportunity history"""
    limit = int(request.args.get('limit', 50))

    opps = []
    for opp in list(opportunity_history)[-limit:]:
        opps.append({
            "opportunity_id": opp.opportunity_id,
            "market_slug": opp.market_slug,
            "price_sum": opp.price_sum,
            "edge": opp.edge,
            "max_executable_size": opp.max_executable_size,
        })

    return jsonify(opps)


@app.route('/api/markets')
def api_markets():
    """Get monitored markets"""
    return jsonify(active_markets)


@app.route('/api/sports/live')
def api_sports_live():
    """Get live sports data from WebSocket"""
    return jsonify({
        "connected": sports_ws.connected if sports_ws else False,
        "events_tracked": len(live_sports_data),
        "events": list(live_sports_data.values())[:50],  # Last 50 events
    })


@app.route('/api/latency')
def api_latency():
    """Get latency statistics"""
    if hft_client:
        return jsonify(hft_client.latency_stats.to_dict())
    return jsonify({})


@app.route('/api/pnl')
def api_pnl():
    """Get PnL summary"""
    demo_pnl = {
        "total_trades": len(demo_trades),
        "filled_trades": len([t for t in demo_trades if t.status == OrderStatus.FILLED]),
        "resolved_trades": len([t for t in demo_trades if t.resolved]),
        "total_pnl": sum(t.pnl or 0 for t in demo_trades if t.resolved),
        "total_cost": sum(t.total_cost for t in demo_trades if t.status == OrderStatus.FILLED),
        "balance": hft_client.get_balance() if hft_client and hft_client.mode == TradingMode.DEMO else 10000,
    }

    live_pnl = {
        "total_trades": len(live_trades),
        "filled_trades": len([t for t in live_trades if t.status == OrderStatus.FILLED]),
        "resolved_trades": len([t for t in live_trades if t.resolved]),
        "total_pnl": sum(t.pnl or 0 for t in live_trades if t.resolved),
        "total_cost": sum(t.total_cost for t in live_trades if t.status == OrderStatus.FILLED),
    }

    return jsonify({
        "demo": demo_pnl,
        "live": live_pnl,
    })


@app.route('/api/data')
def api_data():
    """Get all dashboard data in one call"""
    stats = {}
    scanner_stats = {}
    latency = {}
    engine_stats = {}

    if hft_client:
        stats = hft_client.get_stats()
        latency = hft_client.latency_stats.to_dict()
        engine_stats = hft_client.engine_manager.get_stats()

    if hft_scanner:
        scanner_stats = hft_scanner.get_stats()

    # Recent trades
    recent_demo = [t.to_dict() for t in list(demo_trades)[-20:]]
    recent_live = [t.to_dict() for t in list(live_trades)[-20:]]

    # PnL calculations
    demo_filled = [t for t in demo_trades if t.status == OrderStatus.FILLED]
    demo_resolved = [t for t in demo_trades if t.resolved]
    live_filled = [t for t in live_trades if t.status == OrderStatus.FILLED]
    live_resolved = [t for t in live_trades if t.resolved]

    # Trades by engine
    demo_s2o = [t for t in demo_filled if t.engine == "sum_to_one"]
    demo_tail = [t for t in demo_filled if t.engine == "tail_end"]
    live_s2o = [t for t in live_filled if t.engine == "sum_to_one"]
    live_tail = [t for t in live_filled if t.engine == "tail_end"]

    return jsonify({
        "status": {
            "mode": server_state["mode"],
            "is_running": server_state["is_running"],
            "started_at": server_state["started_at"],
        },
        "scanner": {
            "markets_monitored": len(active_markets),
            "scans_completed": scanner_stats.get("scans_completed", 0),
            "signals_detected": scanner_stats.get("signals_detected", 0),
            "trades_executed": scanner_stats.get("trades_executed", 0),
            "signals_by_engine": scanner_stats.get("signals_by_engine", {}),
        },
        "engines": {
            "sum_to_one": {
                "enabled": engine_stats.get("sum_to_one", {}).get("enabled", True),
                "signals": engine_stats.get("sum_to_one", {}).get("signals_generated", 0),
            },
            "tail_end": {
                "enabled": engine_stats.get("tail_end", {}).get("enabled", True),
                "signals": engine_stats.get("tail_end", {}).get("signals_generated", 0),
                "avg_probability": engine_stats.get("tail_end", {}).get("avg_probability", 0),
            },
            "enabled_list": stats.get("enabled_engines", []),
        },
        "latency": latency,
        "trades": {
            "demo": recent_demo,
            "live": recent_live,
            "copy": list(copy_trades)[-20:],
        },
        "copy_trader": {
            "running": copy_trader_thread and copy_trader_thread.is_alive(),
            "target": TARGET_ADDRESS[:20] + "..." if TARGET_ADDRESS else "",
            "target_name": copy_trader.target_name if copy_trader else "",
            "dry_run": copy_trader.dry_run if copy_trader else True,
            "trades_copied": copy_trader.trades_copied if copy_trader else 0,
            "trades_skipped": copy_trader.trades_skipped if copy_trader else 0,
            "total_spent": copy_trader.total_spent if copy_trader else 0,
        },
        "trades_by_engine": {
            "demo": {
                "sum_to_one": len(demo_s2o),
                "tail_end": len(demo_tail),
            },
            "live": {
                "sum_to_one": len(live_s2o),
                "tail_end": len(live_tail),
            },
        },
        "pnl": {
            "demo": {
                "filled": len(demo_filled),
                "resolved": len(demo_resolved),
                "total_cost": sum(t.total_cost for t in demo_filled),
                "total_pnl": sum(t.pnl or 0 for t in demo_resolved),
                "balance": stats.get("balance", 10000),
            },
            "live": {
                "filled": len(live_filled),
                "resolved": len(live_resolved),
                "total_cost": sum(t.total_cost for t in live_filled),
                "total_pnl": sum(t.pnl or 0 for t in live_resolved),
            },
        },
        "signals": list(signal_history)[-20:],
        "markets": active_markets,  # All markets
    })


@app.route('/api/events')
def api_events():
    """Server-Sent Events for real-time updates"""
    def generate():
        last_demo_count = len(demo_trades)
        last_live_count = len(live_trades)

        while True:
            current_demo = len(demo_trades)
            current_live = len(live_trades)

            if current_demo > last_demo_count or current_live > last_live_count:
                last_demo_count = current_demo
                last_live_count = current_live

                # Send update
                stats = hft_client.get_stats() if hft_client else {}

                data = {
                    "type": "update",
                    "demo_trades": [t.to_dict() for t in list(demo_trades)[-5:]],
                    "live_trades": [t.to_dict() for t in list(live_trades)[-5:]],
                    "stats": stats,
                    "is_running": server_state["is_running"],
                }
                yield f"data: {json.dumps(data)}\n\n"

            time.sleep(0.5)  # 500ms update interval

    return Response(generate(), mimetype='text/event-stream')


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="HFT Arbitrage Server")
    parser.add_argument("--port", type=int, default=5000, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo",
                       help="Trading mode")
    parser.add_argument("--auto-start", action="store_true",
                       help="Start scanner automatically")
    parser.add_argument("--scan-interval", type=int, default=500,
                       help="Scan interval in milliseconds")
    parser.add_argument("--debug", action="store_true", help="Debug mode")

    args = parser.parse_args()

    # Safety check for live mode
    if args.mode == "live" and args.auto_start:
        print("\n" + "=" * 60)
        print("WARNING: LIVE MODE WITH AUTO-START")
        print("This will execute REAL trades with REAL money!")
        print("=" * 60)
        confirm = input("Type 'CONFIRM LIVE' to continue: ")
        if confirm != "CONFIRM LIVE":
            print("Aborted.")
            return

    # Auto-start if requested
    if args.auto_start:
        print(f"Auto-starting HFT scanner in {args.mode} mode...")
        # Delay to allow Flask to start
        def delayed_start():
            import sys
            time.sleep(2)
            try:
                print("[AUTO-START] Starting scanner...", flush=True)
                success, message = start_hft_scanner(args.mode, args.scan_interval)
                print(f"[AUTO-START] Result: success={success}, message={message}", flush=True)
            except Exception as e:
                import traceback
                print(f"[AUTO-START] ERROR: {e}", flush=True)
                traceback.print_exc()
                sys.stdout.flush()

        threading.Thread(target=delayed_start, daemon=True).start()

    print(f"\n{'=' * 60}")
    print("HFT ARBITRAGE SERVER")
    print(f"{'=' * 60}")
    print(f"Server: http://{args.host}:{args.port}")
    print(f"Mode: {args.mode.upper()}")
    print(f"Scan interval: {args.scan_interval}ms")
    print(f"Auto-start: {args.auto_start}")
    print(f"{'=' * 60}")
    print("\nEndpoints:")
    print("  GET  /              - Dashboard")
    print("  GET  /api/status    - Server status")
    print("  POST /api/start     - Start scanner")
    print("  POST /api/stop      - Stop scanner")
    print("  GET  /api/trades    - Trade history")
    print("  GET  /api/data      - All dashboard data")
    print("  GET  /api/events    - Real-time SSE stream")
    print(f"{'=' * 60}\n")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
