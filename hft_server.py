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
from datetime import datetime, timezone
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
from arb_engines import (
    EngineType,
    EngineSignal,
    SumToOneConfig,
    TailEndConfig,
)
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
        # Only actual sports (NBA, NFL, MLB, NHL, UFC, etc.) - not crypto/fed/entertainment
        # ============================================================
        print("[HFT] Fetching live sports events...", flush=True)

        # Only these are actual sports leagues
        REAL_SPORTS_KEYWORDS = [
            "nba", "nfl", "mlb", "nhl", "ncaa", "college",
            "ufc", "mma", "boxing", "pfl",
            "soccer", "football", "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "mls",
            "tennis", "atp", "wta",
            "golf", "pga",
            "f1", "formula", "nascar",
            "cricket", "ipl",
            "rugby",
        ]

        def is_real_sports_league(name: str, slug: str) -> bool:
            """Check if this is an actual sports league (not Fed rates, crypto, box office, etc.)"""
            combined = (name + " " + slug).lower()
            return any(kw in combined for kw in REAL_SPORTS_KEYWORDS)

        def is_game_live(event: dict) -> bool:
            """Check if a sports game is actually live (started but not ended)"""
            # REQUIRE startDate to be present and in the past
            start_date_str = event.get("startDate") or event.get("startDateIso")
            if not start_date_str:
                return False  # No start date = can't verify it's live

            try:
                if "T" in str(start_date_str):
                    start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
                    if start_date > now:
                        return False  # Game hasn't started yet
                else:
                    return False  # Can't parse date
            except:
                return False  # Can't parse date

            # Check end time - game shouldn't be over
            end_date_str = event.get("endDate") or event.get("endDateIso")
            if end_date_str:
                try:
                    if "T" in str(end_date_str):
                        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        if end_date < now:
                            return False  # Game already ended
                except:
                    pass  # If can't parse end date, assume still in progress

            # Must be active and not closed
            if event.get("closed") == True:
                return False
            if event.get("active") == False:
                return False

            return True

        try:
            # Step 1: Get list of series from /sports
            resp = requests.get(f"{GAMMA_API}/sports", timeout=30)
            if resp.status_code == 200:
                all_series = resp.json()
                print(f"[HFT] Found {len(all_series)} series total", flush=True)

                # Filter to only real sports leagues
                sports_leagues = []
                for league in all_series:
                    league_name = league.get("name") or league.get("title", "")
                    league_slug = league.get("slug", "")
                    if is_real_sports_league(league_name, league_slug):
                        sports_leagues.append(league)

                print(f"[HFT] Filtered to {len(sports_leagues)} actual sports leagues", flush=True)

                # Step 2: Query each sports league for live events
                for league in sports_leagues:
                    series_id = (
                        league.get("series_id") or
                        league.get("seriesId") or
                        league.get("id")
                    )
                    league_name = league.get("name") or league.get("title", "Unknown")

                    if not series_id:
                        continue

                    try:
                        events_resp = requests.get(
                            f"{GAMMA_API}/events",
                            params={
                                "series_id": series_id,
                                "active": "true",
                                "closed": "false",
                                "limit": 100,
                            },
                            timeout=10
                        )

                        if events_resp.status_code == 200:
                            events = events_resp.json()
                            live_count = 0

                            for event in events:
                                # Only include games that are actually LIVE
                                if not is_game_live(event):
                                    continue

                                live_count += 1
                                event_markets = event.get("markets", [])
                                event_title = event.get("title", "")

                                # Debug live events
                                if sports_found < 10:
                                    start_str = event.get("startDate", "?")
                                    print(f"[LIVE] {league_name}: {event_title[:50]}... (started: {start_str})", flush=True)

                                for mkt in event_markets:
                                    market_data = parse_market_data(mkt)
                                    if market_data:
                                        market_data["category"] = "sports"
                                        market_data["league"] = league_name
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

                            if live_count > 0:
                                print(f"[HFT] {league_name}: {live_count} live games", flush=True)

                    except Exception as e:
                        print(f"[DEBUG] Error querying {league_name}: {e}", flush=True)

                    time_module.sleep(0.05)  # Rate limiting

        except Exception as e:
            import traceback
            print(f"[HFT] Error fetching sports: {e}", flush=True)
            traceback.print_exc()

        if sports_found == 0:
            print("[HFT] No live sports games currently in progress", flush=True)

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
):
    """Start the HFT scanner with configurable engines"""
    global hft_client, hft_scanner, active_markets, server_state

    if server_state["is_running"]:
        return False, "Scanner already running"

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

    # Create scanner
    hft_scanner = HFTScanner(
        client=hft_client,
        markets=active_markets,
        scan_interval_ms=scan_interval_ms,
    )

    # Start scanner
    hft_scanner.start()

    server_state["mode"] = mode
    server_state["is_running"] = True
    server_state["started_at"] = datetime.now(timezone.utc).isoformat()

    enabled = hft_client.get_enabled_engines()
    return True, f"Started in {mode} mode with {len(active_markets)} markets. Engines: {', '.join(enabled)}"


def stop_hft_scanner():
    """Stop the HFT scanner"""
    global hft_scanner, server_state

    if not server_state["is_running"]:
        return False, "Scanner not running"

    if hft_scanner:
        hft_scanner.stop()

    server_state["is_running"] = False

    return True, "Scanner stopped"


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

    # Sort by timestamp
    trades.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    return jsonify(trades[:limit])


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
