"""Standalone Flask app for the BTC 5m candle-reaction paper trader.

Runs as its own Railway service, fully decoupled from the HFT /
Hawkes-GM dashboards. Single page at ``/`` showing W/L, the last
signal, the open paper trade, the backtest panel, and CSV export
buttons.

CLI::

    python -m candle_reaction.server --port 8080

Env::

    COINDESK_API_KEY   CoinDesk Data API key (required)
    CANDLE_BANKROLL    Paper starting bankroll in USD (default 250)
    PORT               Served port (Railway sets this)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

from . import backtest as bt
from .live import get_engine


STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    CORS(app)

    # ---- UI -------------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "service": "candle_reaction"})

    # ---- Live engine ----------------------------------------------------

    @app.route("/api/candle/status")
    def api_status():
        return jsonify(get_engine().status())

    @app.route("/api/candle/start", methods=["POST"])
    def api_start():
        eng = get_engine()
        eng.start()
        return jsonify({"success": True, **eng.status()})

    @app.route("/api/candle/stop", methods=["POST"])
    def api_stop():
        eng = get_engine()
        eng.stop()
        return jsonify({"success": True, **eng.status()})

    @app.route("/api/candle/mode", methods=["POST"])
    def api_mode():
        data = request.get_json(silent=True) or {}
        if "contrarian" not in data:
            return jsonify({"error": "missing 'contrarian' boolean"}), 400
        eng = get_engine()
        eng.set_contrarian(bool(data["contrarian"]))
        return jsonify({"success": True, **eng.status()})

    @app.route("/api/candle/trades")
    def api_trades():
        eng = get_engine()
        return jsonify({"trades": eng.store.read_trades(), "summary": eng.store.summary()})

    @app.route("/api/candle/signals")
    def api_signals():
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        return jsonify({"signals": get_engine().store.read_signals(limit=limit)})

    @app.route("/api/candle/export")
    def api_export():
        kind = (request.args.get("kind") or "trades").lower()
        cfg = get_engine().store.cfg
        path = cfg.trades_path() if kind == "trades" else cfg.signals_path()
        if not path.exists():
            return Response("", mimetype="text/csv")
        with path.open("r") as fh:
            body = fh.read()
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="candle_{kind}.csv"'},
        )

    # ---- Backtest -------------------------------------------------------

    @app.route("/api/candle/backtest", methods=["POST"])
    def api_backtest_start():
        data = request.get_json(silent=True) or {}
        try:
            candles = int(data.get("candles", 6000))
            warmup = int(data.get("warmup", 30))
        except (TypeError, ValueError):
            return jsonify({"error": "candles/warmup must be integers"}), 400
        candles = max(100, min(candles, 20000))
        warmup = max(5, min(warmup, 200))
        contrarian = data.get("contrarian")
        if contrarian is not None:
            contrarian = bool(contrarian)
        return jsonify(bt.start_async(total_candles=candles, warmup=warmup,
                                      contrarian=contrarian))

    @app.route("/api/candle/backtest/status")
    def api_backtest_status():
        return jsonify(bt.get_state())

    @app.route("/api/candle/backtest/export")
    def api_backtest_export():
        path = bt.latest_csv_path()
        if not path or not path.exists():
            return jsonify({"error": "no backtest result available"}), 404
        with path.open("r") as fh:
            body = fh.read()
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--auto-start", action="store_true",
                   help="Start the live engine on boot")
    args = p.parse_args()

    app = create_app()
    if args.auto_start and os.environ.get("COINDESK_API_KEY"):
        get_engine().start()

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
