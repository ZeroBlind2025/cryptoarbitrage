"""
hf_engine.server
================

Flask HTTP + dashboard server for the Hawkes-GM engine.

Hosts a single Python process that:

    * owns an ``HFEngine`` instance driving the real Polymarket
      scanner, CLOB WebSocket feed, and paper-trading executor
    * serves the vanilla-JS dashboard at ``/``
    * exposes the JSON + CSV API the dashboard polls

This module replaces the previous ``hawkes-gm-railway`` Node/Express
simulation. It runs off the same virtualenv as the rest of the
repository, so it can reuse the existing Polymarket plumbing (Gamma
REST scanner, CLOB WebSocket client, py-clob-client, etc.) without
duplicating any of it.

CLI::

    python -m hf_engine.server --port 8080

Environment variables: see ``hf_engine.config.HFEConfig`` (all
``HFE_*``). No ``MOMENTUM_*`` variables are read.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, send_from_directory
from flask_cors import CORS

from .config import HFEConfig
from .engine import HFEngine
from .snapshot import (
    export_resolved_trades_json,
    export_snapshot_csv,
    export_trades_csv,
    snapshot,
)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #


def create_app(engine: HFEngine) -> Flask:
    """Build a Flask app wired to a live ``HFEngine`` instance.

    The engine is expected to have been started via
    ``engine.start_background()`` *before* the app begins handling
    requests.
    """
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(
        __name__,
        static_folder=static_dir,
        static_url_path="/static",
    )
    CORS(app)

    # Cache snapshot computation so a flood of dashboard polls cannot
    # swamp the main thread. The snapshot walks every market, calls
    # ``evaluate_entry_signal`` per market, and formats trade history,
    # so we memoize it for ~500 ms which is fast enough for a live
    # dashboard and cheap on CPU.
    snapshot_lock = threading.Lock()
    snapshot_cache: dict = {"ts": 0.0, "data": None}
    SNAPSHOT_TTL_SEC = 0.5

    def get_snapshot() -> dict:
        now = time.time()
        with snapshot_lock:
            if snapshot_cache["data"] is not None and (now - snapshot_cache["ts"]) < SNAPSHOT_TTL_SEC:
                return snapshot_cache["data"]
            data = snapshot(engine)
            snapshot_cache["ts"] = now
            snapshot_cache["data"] = data
            return data

    # ------------------------------------------------------------------ #
    # HTML
    # ------------------------------------------------------------------ #

    @app.route("/")
    def index() -> Response:
        return send_from_directory(static_dir, "index.html")

    # ------------------------------------------------------------------ #
    # Health + stats
    # ------------------------------------------------------------------ #

    @app.route("/health")
    def health() -> Response:
        s = engine.stats()
        return jsonify(
            {
                "status": "ok" if s.get("running") else "starting",
                "uptime": s.get("uptime_sec", 0.0),
                "markets": s.get("feed", {}).get("registered_tokens", 0) // 2,
                "tick": s.get("tick_count", 0),
                "markets_seen": s.get("markets_seen_total", 0),
                "markets_resolved": s.get("markets_resolved_total", 0),
                "feed_connected": s.get("feed", {}).get("connected", False),
                "trades_received": s.get("feed", {}).get("trades_received", 0),
            }
        )

    @app.route("/api/stats")
    def api_stats() -> Response:
        return jsonify(engine.stats())

    # ------------------------------------------------------------------ #
    # State + trades
    # ------------------------------------------------------------------ #

    @app.route("/api/state")
    def api_state() -> Response:
        return jsonify(get_snapshot())

    @app.route("/api/trades")
    def api_trades() -> Response:
        return jsonify(export_resolved_trades_json(engine))

    # ------------------------------------------------------------------ #
    # CSV exports
    # ------------------------------------------------------------------ #

    @app.route("/api/export/trades")
    def api_export_trades() -> Response:
        csv = export_trades_csv(engine)
        day = time.strftime("%Y-%m-%d")
        return Response(
            csv,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=hgm-trades-{day}.csv",
            },
        )

    @app.route("/api/export/snapshot")
    def api_export_snapshot() -> Response:
        csv = export_snapshot_csv(engine)
        day = time.strftime("%Y-%m-%d")
        return Response(
            csv,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=hgm-snapshot-{day}.csv",
            },
        )

    # ------------------------------------------------------------------ #
    # SPA fallback — any unknown GET returns the dashboard HTML. ──
    # ------------------------------------------------------------------ #

    @app.errorhandler(404)
    def not_found(_err) -> Response:
        # Only fall back for GET requests to unknown paths. API 404s
        # should still surface as JSON.
        from flask import request

        if request.method == "GET" and not request.path.startswith("/api/"):
            return send_from_directory(static_dir, "index.html")
        return jsonify({"error": "not_found", "path": request.path}), 404

    return app


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hawkes-GM HFE engine + dashboard server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="HTTP port (default: $PORT or 8080)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HFE_BIND_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0)",
    )
    args = parser.parse_args(argv)

    cfg = HFEConfig.from_env()

    if not cfg.paper_trading:
        print(
            f"{cfg.log_prefix} HFE_PAPER_TRADING=false is not supported in v1",
            flush=True,
        )
        return 3

    engine = HFEngine(cfg=cfg)
    engine.start_background()

    app = create_app(engine)

    # Graceful shutdown: when Railway sends SIGTERM, flush the feed and
    # stop the engine cleanly before exiting so paper P&L isn't lost.
    def _handle_signal(signum, _frame):
        print(f"{cfg.log_prefix} received signal {signum}; shutting down", flush=True)
        try:
            engine.stop()
        except Exception as e:
            print(f"{cfg.log_prefix} engine.stop error: {e}", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print(
        f"{cfg.log_prefix} HGM server listening on http://{args.host}:{args.port}",
        flush=True,
    )
    print(f"{cfg.log_prefix} dashboard:  http://{args.host}:{args.port}/", flush=True)
    print(f"{cfg.log_prefix} health:     http://{args.host}:{args.port}/health", flush=True)
    print(f"{cfg.log_prefix} api state:  http://{args.host}:{args.port}/api/state", flush=True)

    # Use the bundled Werkzeug server. This is perfectly fine for a
    # low-traffic paper-trading dashboard; if we ever outgrow it we can
    # swap in gunicorn with the ``hf_engine.server:create_app_prod``
    # factory.
    try:
        app.run(
            host=args.host,
            port=args.port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
