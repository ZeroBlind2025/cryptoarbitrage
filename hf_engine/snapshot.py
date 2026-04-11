"""
hf_engine.snapshot
==================

Translate the live ``HFEngine`` state into the JSON shape the dashboard
expects. Keeping this in a dedicated module means the engine internals
can evolve freely without touching the Flask routes or the JS renderer.

The target shape is intentionally identical to the one the previous
simulation-mode Node engine emitted, so the existing vanilla-JS
dashboard (``hf_engine/static/index.html``) can consume it unchanged:

    {
      "markets":        [ { ...market summary... }, ... ],
      "totalPnl":       float,
      "totalTrades":    int,
      "totalWins":      int,
      "balance":        float,
      "activeCount":    int,
      "cascadeCount":   int,
      "signalCount":    int,
      "positionCount":  int,
      "tickCount":      int,
      "uptime":         float,
      "running":        bool,
      "markets_seen":   int,
      "markets_resolved": int,
      "priors":         [alpha, beta, pi],
    }

Per-market shape:

    {
      "id":             str,
      "question":       str,
      "duration":       int (minutes),
      "remaining":      int (milliseconds until resolution),
      "progress":       float in [0, 1],
      "hawkesBuy":      {"intensity": float, "n": float},
      "hawkesSell":     {"intensity": float, "n": float},
      "posterior":      float,
      "logOdds":        float,
      "piBase":         float,
      "piEffective":    float,
      "clobPrice":      float,
      "flowImbalance":  float,
      "cascadeActive":  bool,
      "tradeCount":     int,
      "recentTrades":   [ {"time": ms, "side": "BUY"|"SELL", "size": int} ],
      "resolved":       bool,
      "outcome":        "YES"|"NO"|None,
      "signal":         "BUY_YES"|"BUY_NO"|None,
      "position":       {"side": "YES"|"NO", "entry": float, "size": float,
                         "entryTime": ms, "edge": float} | None
    }
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .market_state import MarketState, PaperPosition


PAPER_STARTING_BALANCE = 200.0


def snapshot(engine) -> Dict[str, Any]:
    """Full dashboard snapshot. Safe to call from any thread."""
    markets = engine.list_markets()
    ex_stats = engine.executor.stats()

    markets_json: List[Dict[str, Any]] = [market_snapshot(m) for m in markets]

    active = [m for m in markets_json if not m["resolved"]]
    cascade_count = sum(1 for m in active if m["cascadeActive"])
    signal_count = sum(1 for m in active if m["signal"] is not None)
    position_count = sum(1 for m in active if m["position"] is not None)

    # Dashboard stats use **resolved-only** numbers. Mark-to-market
    # closes from the optional early-exit path are tracked separately
    # in ``mark_pnl`` and ``wins`` / ``losses``, and never contaminate
    # BALANCE / WIN RATE on the dashboard — because in paper mode
    # there's no counterparty and mark P/L is fictional.
    total_pnl = float(ex_stats.get("realized_pnl", 0.0))
    total_trades = int(ex_stats.get("resolved_count", 0))
    total_wins = int(ex_stats.get("resolved_wins", 0))

    priors = list(engine.calibrator.current_priors())

    # Top-level collections for the dashboard's PAPER POSITIONS panel.
    # Open positions are collected from the still-tracked markets, with
    # a live P&L computed against the current CLOB mid so the dashboard
    # does not have to rewrite the formula. Closed positions come from
    # the executor's history buffer, which survives market eviction.
    open_positions: List[Dict[str, Any]] = []
    for m_json in markets_json:
        if m_json.get("resolved"):
            continue
        pos = m_json.get("position")
        if pos is None:
            continue
        open_positions.append(
            {
                "market_id": m_json["marketIdFull"],
                "market_short": m_json["id"],
                "description": m_json["question"],
                "interval": m_json["interval"],
                "side": pos["side"],
                "entry": pos["entry"],
                "size_dollars": pos["size"],
                "entry_time": pos["entryTime"],
                "edge": pos["edge"],
                "clob_price": m_json["clobPrice"],
                "live_pnl": pos.get("live_pnl", 0.0),
                "posterior": m_json["posterior"],
                "time_remaining_sec": int(round(m_json["remaining"] / 1000)),
            }
        )

    closed_positions = engine.executor.recent_closed_positions(limit=100)

    return {
        "markets": markets_json,
        "totalPnl": total_pnl,
        "totalTrades": total_trades,
        "totalWins": total_wins,
        "totalLosses": int(ex_stats.get("resolved_losses", 0)),
        "mark_pnl": float(ex_stats.get("mark_pnl", 0.0)),
        "balance": PAPER_STARTING_BALANCE + total_pnl,
        "early_exit_enabled": engine.cfg.early_exit_enabled,
        "activeCount": len(active),
        "cascadeCount": cascade_count,
        "signalCount": signal_count,
        "positionCount": position_count,
        "tickCount": engine.tick_count,
        "uptime": engine.uptime_seconds,
        "running": engine.is_running,
        "markets_seen": engine._markets_seen_total,
        "markets_resolved": engine._markets_resolved_total,
        "priors": {
            "alpha": priors[0] if len(priors) > 0 else None,
            "beta": priors[1] if len(priors) > 1 else None,
            "pi": priors[2] if len(priors) > 2 else None,
        },
        "signals_accepted_total": engine.executor.signals_accepted_total,
        "signals_rejected_total": engine.executor.signals_rejected_total,
        "reject_histogram": engine.executor.reject_reason_histogram(),
        "recent_signals": engine.executor.recent_signals(limit=20),
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "mode": "live-paper",
    }


def market_snapshot(m: MarketState) -> Dict[str, Any]:
    remaining_ms = int(max(0, m.time_remaining_sec * 1000))
    duration_min = _parse_interval_minutes(m.interval_label)
    total_ms = duration_min * 60 * 1000
    progress = 1.0 - (remaining_ms / total_ms if total_ms > 0 else 0.0)
    progress = max(0.0, min(1.0, progress))

    clob_mid = m.clob_mid_yes()
    if clob_mid is None:
        clob_mid = 0.5

    resolved = m.resolved_outcome is not None or m.time_remaining_sec <= 0
    outcome: Optional[str] = None
    if m.resolved_outcome is not None:
        outcome = "UP" if m.resolved_outcome == 1 else "DOWN"

    signal = _current_signal(m)
    position = _position_json(m.open_position, clob_mid)

    # Last 10 trades in dashboard shape.
    recent: List[Dict[str, Any]] = []
    for t in list(m.trades)[-10:]:
        if t.side not in ("buy", "sell"):
            continue
        recent.append(
            {
                "time": int(t.timestamp * 1000),
                "side": t.side.upper(),
                "size": int(round(t.size)),
            }
        )

    return {
        "id": _short_id(m.market_id),
        "question": m.description or m.market_id,
        "duration": duration_min,
        "remaining": remaining_ms,
        "progress": progress,
        "hawkesBuy": {
            "intensity": float(m.hawkes_buy.intensity),
            # ``n`` is the live excitation ratio (intensity / mu), not
            # the static branching ratio. This is what the dashboard's
            # cascade gauge reads.
            "n": float(min(m.hawkes_buy.excitation_ratio, 10.0)),
            "branching_ratio": float(m.hawkes_buy.branching_ratio),
        },
        "hawkesSell": {
            "intensity": float(m.hawkes_sell.intensity),
            "n": float(min(m.hawkes_sell.excitation_ratio, 10.0)),
            "branching_ratio": float(m.hawkes_sell.branching_ratio),
        },
        "posterior": float(m.posterior),
        "logOdds": float(m.gm.log_odds),
        "piBase": float(m.cfg.pi_base),
        "piEffective": float(m.pi_effective),
        "clobPrice": float(clob_mid),
        "flowImbalance": float(m.flow_imbalance),
        "cascadeActive": bool(m.cascade_active),
        "tradeCount": int(m.trade_count),
        "recentTrades": recent,
        "resolved": bool(resolved),
        "outcome": outcome,
        "signal": signal,
        "position": position,
        "interval": m.interval_label,
        "marketIdFull": m.market_id,
    }


# --------------------------------------------------------------------------- #
# CSV exports (mirror the previous JS engine's format exactly)
# --------------------------------------------------------------------------- #


def export_trades_csv(engine) -> str:
    headers = [
        "Market",
        "Question",
        "Duration",
        "Side",
        "Entry",
        "Outcome",
        "Won",
        "P/L",
        "Edge",
        "Trades",
        "Resolved At",
    ]
    rows = [",".join(headers)]

    # Pull from the executor's history buffer, not the live markets
    # dict — resolved markets are evicted immediately so iterating
    # ``list_markets()`` would only return still-active markets with
    # any still-uncleared closed positions, which is almost never the
    # data the user actually wants.
    for entry in engine.executor.recent_closed_positions(limit=400):
        row = [
            _short_id(entry.get("market_id", "")),
            f'"{(entry.get("description") or "").replace(chr(34), chr(39))}"',
            str(_parse_interval_minutes(entry.get("interval", ""))),
            entry.get("side", ""),
            f"{(entry.get('entry') or 0.0):.4f}",
            entry.get("outcome") or "",
            "Y" if entry.get("won") else "N",
            f"{(entry.get('realized_pnl') or 0.0):.4f}",
            f"{abs(entry.get('posterior_at_entry', 0.5) - (entry.get('entry') or 0.0)) * 100:.1f}",
            "",  # trade_count no longer available post-eviction
            _iso(entry.get("ts")),
        ]
        rows.append(",".join(row))
    return "\n".join(rows)


def export_snapshot_csv(engine) -> str:
    headers = [
        "Market",
        "Remaining(s)",
        "BuyIntensity",
        "SellIntensity",
        "BuyN",
        "SellN",
        "Posterior",
        "CLOB",
        "Edge(c)",
        "FlowImbalance",
        "Cascade",
        "Signal",
        "Position",
    ]
    rows = [",".join(headers)]
    markets = [m for m in engine.list_markets() if m.resolved_outcome is None and m.time_remaining_sec > 0]
    for m in markets:
        clob = m.clob_mid_yes() or 0.0
        edge_c = (m.posterior - clob) * 100.0
        row = [
            _short_id(m.market_id),
            str(int(m.time_remaining_sec)),
            f"{m.hawkes_buy.intensity:.3f}",
            f"{m.hawkes_sell.intensity:.3f}",
            f"{m.hawkes_buy.branching_ratio:.3f}",
            f"{m.hawkes_sell.branching_ratio:.3f}",
            f"{m.posterior:.4f}",
            f"{clob:.4f}",
            f"{edge_c:.2f}",
            f"{m.flow_imbalance:.3f}",
            "Y" if m.cascade_active else "N",
            _current_signal(m) or "",
            m.open_position.side.upper() if m.open_position else "",
        ]
        rows.append(",".join(row))
    return "\n".join(rows)


def export_resolved_trades_json(engine) -> List[Dict[str, Any]]:
    """Return a flat list of resolved / closed positions for the
    ``/api/trades`` endpoint. Sourced from the executor's history
    buffer so the data survives market eviction."""
    out: List[Dict[str, Any]] = []
    for entry in engine.executor.recent_closed_positions(limit=400):
        out.append(
            {
                "id": _short_id(entry.get("market_id", "")),
                "question": entry.get("description") or entry.get("market_id", ""),
                "duration": _parse_interval_minutes(entry.get("interval", "")),
                "side": entry.get("side", ""),
                "entry": entry.get("entry"),
                "exit": entry.get("exit"),
                "outcome": entry.get("outcome"),
                "won": bool(entry.get("won")),
                "pnl": entry.get("realized_pnl"),
                "size_dollars": entry.get("size_dollars"),
                "posterior_at_entry": entry.get("posterior_at_entry"),
                "entry_reason": entry.get("entry_reason"),
                "exit_reason": entry.get("exit_reason"),
                "resolvedAt": int((entry.get("ts") or time.time()) * 1000),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_interval_minutes(label: str) -> int:
    if not label:
        return 5
    label = label.strip().lower()
    try:
        if label.endswith("m"):
            return int(label[:-1])
        if label.endswith("h"):
            return int(label[:-1]) * 60
    except ValueError:
        return 5
    return 5


def _current_signal(m: MarketState) -> Optional[str]:
    """Return a BUY_UP / BUY_DOWN badge for the dashboard.

    When a paper position is already open we report that position's
    side (so the badge stays visible throughout the hold period);
    otherwise we evaluate the entry gates and report whatever the
    engine would fire on the next trade. Labels are translated from
    the internal ``yes/no`` representation to the domain-friendly
    ``UP/DOWN`` shown on the dashboard.
    """
    if m.open_position is not None:
        return "BUY_UP" if m.open_position.side == "yes" else "BUY_DOWN"

    try:
        sig = m.evaluate_entry_signal()
    except Exception:
        return None
    if sig.action == "buy_yes":
        return "BUY_UP"
    if sig.action == "buy_no":
        return "BUY_DOWN"
    return None


def _position_json(
    pos: Optional[PaperPosition],
    clob_mid_yes: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Serialize an open position for the dashboard.

    * ``side`` is translated from ``yes/no`` to ``UP/DOWN``.
    * ``edge`` is computed in the **position's own frame**. For a
      NO/DOWN position, ``entry_price = 1 - ask_yes`` and the relevant
      posterior is ``1 - posterior_yes``, so ``edge = (1 - posterior) -
      entry`` — comparing ``posterior_yes`` against ``entry_no``
      directly produces a number in a totally different frame and
      wildly understates the edge. (This used to display "8.5¢" for a
      position whose real entry edge was 94¢.)
    * ``live_pnl`` is computed here so the dashboard does not have to
      replicate the dollars-at-risk formula and risk the same bug the
      old JS ``(clobPrice - entry) * size * sign`` version had.
    """
    if pos is None:
        return None

    if pos.side == "yes":
        side_label = "UP"
        effective_posterior = pos.posterior_at_entry
    else:
        side_label = "DOWN"
        effective_posterior = 1.0 - pos.posterior_at_entry

    entry_edge = max(0.0, effective_posterior - pos.entry_price)

    live_pnl = 0.0
    if clob_mid_yes is not None and pos.entry_price > 0:
        if pos.side == "yes":
            current_price = clob_mid_yes
        else:
            current_price = 1.0 - clob_mid_yes
        pnl_frac = (current_price - pos.entry_price) / pos.entry_price
        live_pnl = pos.size_dollars * pnl_frac

    return {
        "side": side_label,
        "entry": pos.entry_price,
        "size": pos.size_dollars,
        "entryTime": int(pos.entry_time * 1000),
        "edge": entry_edge,
        "live_pnl": live_pnl,
        "reason": pos.reason,
    }


def _short_id(market_id: str) -> str:
    if not market_id:
        return "MKT-????"
    # Prefer a short, monospace-friendly identifier for the dashboard.
    if market_id.startswith("0x") and len(market_id) > 10:
        return f"MKT-{market_id[2:8].upper()}"
    return f"MKT-{market_id[:8]}"


def _iso(epoch_seconds: Optional[float]) -> str:
    if not epoch_seconds:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
