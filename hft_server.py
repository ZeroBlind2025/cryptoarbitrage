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
)
from arb_engines import (
    EngineType,
    EngineSignal,
    SumToOneConfig,
    TailEndConfig,
)
from step_c_scanner_v4 import ScanConfig as GammaConfig, MarketScanner as GammaScanner


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
gamma_scanner: Optional[GammaScanner] = None

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

def discover_markets() -> list[dict]:
    """
    Use Gamma scanner to discover active markets,
    then extract token IDs for HFT monitoring.
    """
    global gamma_scanner

    if gamma_scanner is None:
        config = GammaConfig(
            max_resolution_hours=24,
            min_volume_24h=1000.0,
            fee_buffer=0.02,
            slippage_buffer=0.005,
            risk_buffer=0.005,
        )
        gamma_scanner = GammaScanner(config)

    try:
        result = gamma_scanner.scan()

        markets = []
        for target in result.targets:
            token_a_id = target.get("token_a_id")
            token_b_id = target.get("token_b_id")

            if token_a_id and token_b_id:
                markets.append({
                    "slug": target.get("slug", ""),
                    "question": target.get("question", ""),
                    "token_a_id": token_a_id,
                    "token_b_id": token_b_id,
                    "price_sum_indicative": target.get("price_sum_indicative"),
                    "minutes_until": target.get("minutes_until"),
                    "volume_24h": target.get("volume_24h"),
                })

        return markets

    except Exception as e:
        print(f"[HFT] Market discovery failed: {e}")
        return []


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

    # Configure engines
    sum_to_one_config = SumToOneConfig(
        max_price_sum=0.97,
        min_edge_after_fees=0.005,
        max_position_usd=100.0,
    )

    tail_end_config = TailEndConfig(
        min_probability=0.95,
        max_probability=0.995,
        min_price=0.95,
        max_price=0.99,
        max_minutes_until_resolution=60,
        min_minutes_until_resolution=1,
        min_volume_24h=5000.0,
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
    return render_template('hft_dashboard.html')


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
        "markets": active_markets[:10],  # Top 10 markets
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
            time.sleep(2)
            start_hft_scanner(args.mode, args.scan_interval)

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
