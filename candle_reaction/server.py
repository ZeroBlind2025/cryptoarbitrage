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
import logging
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS


# Make sure log.info() calls in live.py / polymarket.py reach stdout so
# Railway surfaces them. Flask/gunicorn don't always set up a root handler
# for non-werkzeug loggers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Drop the per-request access log; the dashboard polls 3 endpoints every
# 5s and drowns out real ENTER/RESOLVE/SKIP lines. Real errors still
# surface at WARNING.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

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
        aggregate = data.get("aggregate")
        if aggregate is not None:
            try:
                aggregate = int(aggregate)
            except (TypeError, ValueError):
                return jsonify({"error": "aggregate must be an integer"}), 400
            if aggregate not in (1, 5, 15, 60):
                return jsonify({"error": "aggregate must be one of 1, 5, 15, 60"}), 400
        return jsonify(bt.start_async(total_candles=candles, warmup=warmup,
                                      contrarian=contrarian,
                                      aggregate=aggregate))

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

    @app.route("/api/candle/raw")
    def api_raw_candles():
        """Raw OHLCV dump from the live data source (no judge, no trades)."""
        from .config import load as load_cfg, make_live_client
        try:
            count = int(request.args.get("count", 400))
        except (TypeError, ValueError):
            return jsonify({"error": "count must be an integer"}), 400
        try:
            aggregate = int(request.args.get("aggregate", 5))
        except (TypeError, ValueError):
            return jsonify({"error": "aggregate must be an integer"}), 400
        if aggregate not in (1, 5, 15, 60):
            return jsonify({"error": "aggregate must be one of 1, 5, 15, 60"}), 400
        count = max(1, min(count, 200000))

        cfg = load_cfg()
        cfg.aggregate = aggregate
        client = make_live_client(cfg)
        try:
            candles = client.fetch_history(count) if count > 300 \
                else client.fetch_minutes(limit=count)
        except Exception as e:
            return jsonify({"error": f"fetch failed: {e}"}), 502

        from datetime import datetime, timezone
        import io, csv as _csv
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["ts", "datetime_utc", "open", "high", "low", "close", "volume"])
        for c in candles:
            iso = datetime.fromtimestamp(c.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([c.ts, iso, c.open, c.high, c.low, c.close, c.volume])

        src = cfg.source
        filename = f"raw_{src}_{aggregate}m_{count}bars.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
