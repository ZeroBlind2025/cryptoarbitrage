"""One-off backtest: replay the judge against N historical 5m candles.

Usage (CLI):
    COINDESK_API_KEY=...  python -m candle_reaction.backtest --candles 6000

Or run from the dashboard via POST /api/candle/backtest.

Output: a human-readable summary (CLI only), plus a CSV in
``data/backtest_<ts>.csv`` containing every bar with features,
prediction, bet, outcome.

No look-ahead: features for bar i are built from candles[:i+1]; the
outcome for bar i is determined by comparing candles[i].close to
candles[i+1].close.
"""

from __future__ import annotations

import argparse
import csv
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .coindesk import CoindeskClient
from .config import load
from .features import extract
from .judge import judge
from .sizing import stake_for


def run(
    total_candles: int = 6000,
    warmup: int = 30,
    verbose: bool = True,
    contrarian: Optional[bool] = None,
    aggregate: Optional[int] = None,
) -> dict:
    cfg = load()
    if not cfg.api_key:
        raise RuntimeError("COINDESK_API_KEY is not set")
    if contrarian is None:
        contrarian = cfg.contrarian
    if aggregate is not None:
        cfg.aggregate = int(aggregate)

    client = CoindeskClient(cfg)
    if verbose:
        print(f"Fetching ~{total_candles} x {cfg.aggregate}m candles for "
              f"{cfg.instrument} on {cfg.market}...")
    candles = client.fetch_history(total_candles)
    if len(candles) < warmup + 2:
        raise RuntimeError(f"Not enough candles: got {len(candles)}")
    if verbose:
        print(f"Got {len(candles)} candles from "
              f"{candles[0].ts} to {candles[-1].ts}")

    out_rows: list[dict] = []
    equity = cfg.bankroll
    wins = losses = voids = skips = 0
    bucket_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "w": 0})
    regime_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "w": 0})

    for i in range(warmup, len(candles) - 1):
        hist = candles[: i + 1]
        feat = extract(hist, lookback=cfg.lookback)
        j = judge(feat, contrarian=contrarian)
        # The regime judge can return side="SKIP" for neutral bars.
        # Respect that before applying the confidence ladder.
        if j.side == "SKIP":
            stake = 0.0
        else:
            stake = stake_for(j.confidence, cfg)

        next_close = candles[i + 1].close
        cur_close = candles[i].close
        actual = "UP" if next_close > cur_close else ("DOWN" if next_close < cur_close else "VOID")

        if stake == 0.0:
            skips += 1
            result = ""
            pnl = 0.0
        else:
            if actual == "VOID":
                result = "VOID"
                pnl = 0.0
                voids += 1
            elif actual == j.side:
                result = "WIN"
                pnl = stake
                wins += 1
            else:
                result = "LOSS"
                pnl = -stake
                losses += 1
            equity += pnl

            bucket = _bucket(j.confidence)
            bucket_stats[bucket]["n"] += 1
            if result == "WIN":
                bucket_stats[bucket]["w"] += 1
            regime_stats[j.regime]["n"] += 1
            if result == "WIN":
                regime_stats[j.regime]["w"] += 1

        out_rows.append({
            "ts": candles[i].ts,
            "close": cur_close,
            "next_close": next_close,
            "close_position": round(feat.close_position, 6),
            "body_signed": round(feat.body_signed, 6),
            "volume_z": round(feat.volume_z, 4),
            "range_z": round(feat.range_z, 4),
            "streak": feat.streak,
            "p_up": round(j.p_up, 6),
            "side": j.side,
            "regime": j.regime,
            "confidence": round(j.confidence, 6),
            "stake": stake,
            "actual": actual,
            "result": result,
            "pnl": round(pnl, 4),
            "equity": round(equity, 4),
        })

    out_path = cfg.data_dir / f"backtest_{int(time.time())}.csv"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    resolved = wins + losses
    buckets = {
        name: {
            "n": bucket_stats.get(name, {}).get("n", 0),
            "wins": bucket_stats.get(name, {}).get("w", 0),
            "hit_rate": (
                (bucket_stats[name]["w"] / bucket_stats[name]["n"])
                if bucket_stats.get(name, {}).get("n") else 0.0
            ),
        }
        for name in ("70-80", "80-90", "90-100")
    }
    regimes = {
        name: {
            "n": s["n"],
            "wins": s["w"],
            "hit_rate": (s["w"] / s["n"]) if s["n"] else 0.0,
        }
        for name, s in regime_stats.items()
    }
    result = {
        "path": str(out_path),
        "csv_name": out_path.name,
        "candles": len(candles),
        "considered": len(candles) - warmup - 1,
        "start_ts": candles[0].ts,
        "end_ts": candles[-1].ts,
        "warmup": warmup,
        "mode": "contrarian" if contrarian else "continuation",
        "aggregate": cfg.aggregate,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "skips": skips,
        "hit_rate": (wins / resolved) if resolved else 0.0,
        "bankroll": cfg.bankroll,
        "equity": round(equity, 4),
        "pnl": round(equity - cfg.bankroll, 4),
        "buckets": buckets,
        "regimes": regimes,
    }

    if verbose:
        print("\n==== Candle-Reaction Backtest ====")
        print(f"Candles considered : {result['considered']}")
        print(f"Skipped (below 70%): {skips}")
        print(f"Traded             : {resolved + voids}")
        print(f"  Wins : {wins}")
        print(f"  Loss : {losses}")
        print(f"  Void : {voids}")
        if resolved:
            print(f"  Hit rate : {wins / resolved:.2%}")
        print(f"Start bankroll : ${cfg.bankroll:.2f}")
        print(f"End equity     : ${equity:.2f}")
        print(f"Net P&L        : ${equity - cfg.bankroll:+.2f}")
        print("\nHit rate by confidence bucket:")
        for name in ("70-80", "80-90", "90-100"):
            b = buckets[name]
            print(f"  {name}: {b['n']:4d} trades, hit {b['hit_rate']:.2%}")
        print("\nHit rate by regime:")
        for name in ("momentum", "exhaustion", "neutral"):
            if name in regimes:
                r = regimes[name]
                print(f"  {name:10s}: {r['n']:4d} trades, hit {r['hit_rate']:.2%}")
        print(f"\nWrote {out_path}")
    return result


def _bucket(confidence: float) -> str:
    if confidence < 0.80:
        return "70-80"
    if confidence < 0.90:
        return "80-90"
    return "90-100"


# ---- Background runner for the dashboard ----------------------------------

_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "status": "idle",       # idle | running | done | failed
    "started_at": None,
    "finished_at": None,
    "params": None,
    "result": None,
    "error": None,
}


def get_state() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def start_async(
    total_candles: int = 6000,
    warmup: int = 30,
    contrarian: Optional[bool] = None,
    aggregate: Optional[int] = None,
) -> dict:
    """Kick off a backtest in a daemon thread. Idempotent while running."""
    with _STATE_LOCK:
        if _STATE["status"] == "running":
            return dict(_STATE)
        _STATE.update({
            "status": "running",
            "started_at": int(time.time()),
            "finished_at": None,
            "params": {"candles": total_candles, "warmup": warmup,
                       "contrarian": bool(contrarian), "aggregate": aggregate},
            "result": None,
            "error": None,
        })
    threading.Thread(
        target=_run_and_store,
        args=(total_candles, warmup, contrarian, aggregate),
        daemon=True,
        name="candle-backtest",
    ).start()
    return get_state()


def _run_and_store(
    total_candles: int,
    warmup: int,
    contrarian: Optional[bool],
    aggregate: Optional[int],
) -> None:
    try:
        result = run(total_candles=total_candles, warmup=warmup,
                     verbose=False, contrarian=contrarian,
                     aggregate=aggregate)
        with _STATE_LOCK:
            _STATE["status"] = "done"
            _STATE["finished_at"] = int(time.time())
            _STATE["result"] = result
    except Exception as e:
        with _STATE_LOCK:
            _STATE["status"] = "failed"
            _STATE["finished_at"] = int(time.time())
            _STATE["error"] = str(e)


def latest_csv_path() -> Optional[Path]:
    state = get_state()
    result = state.get("result")
    if not result:
        return None
    path = result.get("path")
    return Path(path) if path else None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--candles", type=int, default=6000,
                   help="Number of 5m candles to replay (default: 6000 ≈ 20 days)")
    p.add_argument("--warmup", type=int, default=30,
                   help="Warmup bars skipped for z-score windows")
    p.add_argument("--contrarian", action="store_true",
                   help="Flip the judge (trade against the signal)")
    p.add_argument("--aggregate", type=int, default=None,
                   help="Candle aggregation in minutes (default: config / 5m). "
                        "e.g. --aggregate 15 for 15m bars")
    args = p.parse_args()
    run(total_candles=args.candles, warmup=args.warmup,
        contrarian=args.contrarian, aggregate=args.aggregate)


if __name__ == "__main__":
    main()
