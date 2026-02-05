#!/usr/bin/env python3
"""
POLYMARKET ARBITRAGE DASHBOARD SERVER
======================================

Flask server that:
1. Serves the React dashboard
2. Provides REST API endpoints for scan data
3. Runs the scanner in a background thread
4. Streams real-time updates via Server-Sent Events (SSE)

Usage:
    python server.py                    # Start server on port 5000
    python server.py --port 8080        # Custom port
    python server.py --mode paper       # Start in paper trading mode
"""

import argparse
import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Generator

from flask import Flask, jsonify, render_template, Response, request
from flask_cors import CORS

# Import scanner components
from step_c_scanner_v4 import ScanConfig as GammaConfig, MarketScanner as GammaScanner
from step_d_clob_client import CLOBConfig, CLOBClient
from step_e_integrated_scanner import IntegratedConfig, IntegratedScanner

# =============================================================================
# APP CONFIGURATION
# =============================================================================

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Scanner state
scanner_state = {
    "is_running": False,
    "mode": "scan",  # scan, paper, live
    "scan_count": 0,
    "paper_balance": 10000.0,
    "total_exposure": 0.0,
}

# Data buffers (thread-safe with deque)
scan_history = deque(maxlen=100)
exclusion_funnel = {}
price_slip_history = deque(maxlen=50)
current_opportunities = []
closest_misses = deque(maxlen=10)
trade_history = deque(maxlen=50)

# Scanner thread
scanner_thread: Optional[threading.Thread] = None
scanner_instance: Optional[IntegratedScanner] = None
stop_scanner = threading.Event()


# =============================================================================
# SCANNER THREAD
# =============================================================================

def run_scanner_loop(mode: str):
    """Background scanner loop"""
    global scanner_instance, scanner_state, current_opportunities

    # Build config
    gamma_config = GammaConfig(
        max_resolution_hours=24,
        min_volume_24h=1000.0,
        fee_buffer=0.02,
        slippage_buffer=0.005,
        risk_buffer=0.005,
    )

    clob_config = CLOBConfig.from_env()

    config = IntegratedConfig(
        gamma=gamma_config,
        clob=clob_config,
        clob_validation_enabled=True,
        max_position_usd=50.0,
    )

    scanner_instance = IntegratedScanner(
        config,
        paper_mode=(mode != "live"),
    )

    while not stop_scanner.is_set():
        try:
            # Run scan
            result = scanner_instance.scan()

            # Update state
            scanner_state["scan_count"] += 1
            scanner_state["paper_balance"] = scanner_instance.clob_client.get_balance()
            scanner_state["total_exposure"] = scanner_instance.total_exposure

            # Store scan history
            scan_entry = {
                "timestamp": result.timestamp,
                "marketsScanned": result.gamma_markets_scanned,
                "gammaOpportunities": result.gamma_opportunities,
                "clobValidated": result.clob_validations_attempted,
                "executable": result.clob_validations_passed,
            }
            scan_history.append(scan_entry)

            # Store price slip
            if result.avg_price_slip_bps is not None:
                price_slip_entry = {
                    "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "gammaSum": 0.97 - (result.avg_price_slip_bps or 0) / 10000,
                    "clobSum": 0.97,
                    "slipBps": result.avg_price_slip_bps or 0,
                }
                price_slip_history.append(price_slip_entry)

            # Update opportunities
            current_opportunities = []
            for opp in result.executable_opportunities:
                current_opportunities.append({
                    "slug": opp.market.slug,
                    "question": opp.market.question,
                    "gammaSum": opp.gamma_price_sum,
                    "clobSum": opp.clob_price_sum or opp.gamma_price_sum,
                    "edge": opp.clob_edge or opp.gamma_edge,
                    "netProfit": (opp.clob_edge or opp.gamma_edge) - 0.03,  # After friction
                    "minutesUntil": opp.market.minutes_until_resolution or 0,
                    "volume24h": opp.market.volume_24h or 0,
                    "depth": opp.clob_depth or 0,
                    "status": "executable" if opp.is_executable else "validating",
                })

            # Execute trades in paper/live mode
            if mode != "scan" and result.executable_opportunities:
                for opp in result.executable_opportunities:
                    execution = scanner_instance.execute_opportunity(opp)
                    if execution and execution.success:
                        trade_entry = {
                            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "market": opp.market.slug[:30],
                            "side": "ARB",
                            "cost": execution.total_cost,
                            "status": "filled",
                            "pnl": execution.total_cost * (opp.clob_edge or opp.gamma_edge or 0),
                        }
                        trade_history.append(trade_entry)

            # Sleep between scans
            for _ in range(50):  # 5 seconds in 0.1s chunks for responsiveness
                if stop_scanner.is_set():
                    break
                time.sleep(0.1)

        except Exception as e:
            print(f"Scanner error: {e}")
            time.sleep(5)

    scanner_state["is_running"] = False


def start_scanner(mode: str = "scan"):
    """Start the scanner in a background thread"""
    global scanner_thread, stop_scanner

    if scanner_state["is_running"]:
        return False

    stop_scanner.clear()
    scanner_state["is_running"] = True
    scanner_state["mode"] = mode

    scanner_thread = threading.Thread(target=run_scanner_loop, args=(mode,), daemon=True)
    scanner_thread.start()

    return True


def stop_scanner_thread():
    """Stop the scanner thread"""
    global scanner_thread

    if not scanner_state["is_running"]:
        return False

    stop_scanner.set()
    if scanner_thread:
        scanner_thread.join(timeout=5)

    scanner_state["is_running"] = False
    return True


# =============================================================================
# API ROUTES
# =============================================================================

@app.route('/')
def index():
    """Serve the dashboard"""
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get scanner status"""
    return jsonify({
        "isRunning": scanner_state["is_running"],
        "mode": scanner_state["mode"],
        "scanCount": scanner_state["scan_count"],
        "paperBalance": scanner_state["paper_balance"],
        "totalExposure": scanner_state["total_exposure"],
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    """Start the scanner"""
    data = request.get_json() or {}
    mode = data.get('mode', 'scan')

    if mode == 'live':
        return jsonify({"error": "Live mode requires CLI confirmation"}), 403

    if start_scanner(mode):
        return jsonify({"success": True, "mode": mode})
    return jsonify({"error": "Scanner already running"}), 400


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop the scanner"""
    if stop_scanner_thread():
        return jsonify({"success": True})
    return jsonify({"error": "Scanner not running"}), 400


@app.route('/api/data')
def api_data():
    """Get all dashboard data"""
    return jsonify({
        "scanHistory": list(scan_history),
        "exclusionFunnel": get_exclusion_funnel(),
        "priceSlipHistory": list(price_slip_history),
        "opportunities": current_opportunities,
        "closestMisses": list(closest_misses),
        "trades": list(trade_history),
        "status": {
            "isRunning": scanner_state["is_running"],
            "mode": scanner_state["mode"],
            "scanCount": scanner_state["scan_count"],
            "paperBalance": scanner_state["paper_balance"],
        },
    })


@app.route('/api/events')
def api_events():
    """Server-Sent Events for real-time updates"""
    def generate():
        last_scan_count = 0
        while True:
            if scanner_state["scan_count"] > last_scan_count:
                last_scan_count = scanner_state["scan_count"]
                data = {
                    "type": "update",
                    "scanHistory": list(scan_history)[-10:],
                    "opportunities": current_opportunities,
                    "status": {
                        "isRunning": scanner_state["is_running"],
                        "mode": scanner_state["mode"],
                        "scanCount": scanner_state["scan_count"],
                        "paperBalance": scanner_state["paper_balance"],
                    },
                }
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(1)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/logs/scans')
def api_logs_scans():
    """Get scan logs from JSONL file"""
    log_file = Path("logs/integrated_scans.jsonl")
    if not log_file.exists():
        return jsonify([])

    logs = []
    with open(log_file) as f:
        for line in f:
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Return last 100 entries
    return jsonify(logs[-100:])


@app.route('/api/logs/executions')
def api_logs_executions():
    """Get execution logs from JSONL file"""
    log_file = Path("logs/executions.jsonl")
    if not log_file.exists():
        return jsonify([])

    logs = []
    with open(log_file) as f:
        for line in f:
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return jsonify(logs[-100:])


def get_exclusion_funnel():
    """Get exclusion funnel data from recent scans"""
    # This would normally come from the scanner's exclusion tracking
    # For now, return sample data structure
    return [
        {"reason": "not_crypto", "count": 89, "pct": 52.4},
        {"reason": "resolves_too_far", "count": 34, "pct": 20.0},
        {"reason": "volume_24h_missing", "count": 21, "pct": 12.4},
        {"reason": "volume_24h_low", "count": 12, "pct": 7.1},
        {"reason": "price_sum_too_high", "count": 8, "pct": 4.7},
        {"reason": "not_binary_raw", "count": 4, "pct": 2.4},
        {"reason": "missing_token_ids", "count": 2, "pct": 1.2},
    ]


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket Dashboard Server")
    parser.add_argument("--port", type=int, default=5000, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--mode", choices=["scan", "paper"], default="scan",
                       help="Initial scanner mode")
    parser.add_argument("--auto-start", action="store_true",
                       help="Start scanner automatically")
    parser.add_argument("--debug", action="store_true", help="Debug mode")

    args = parser.parse_args()

    # Auto-start scanner if requested
    if args.auto_start:
        print(f"Auto-starting scanner in {args.mode} mode...")
        start_scanner(args.mode)

    print(f"\n{'='*50}")
    print("POLYMARKET ARBITRAGE DASHBOARD")
    print(f"{'='*50}")
    print(f"Server: http://{args.host}:{args.port}")
    print(f"Mode: {args.mode}")
    print(f"Auto-start: {args.auto_start}")
    print(f"{'='*50}\n")

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
