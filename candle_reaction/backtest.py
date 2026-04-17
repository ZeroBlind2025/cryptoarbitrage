"""One-off backtest: replay the judge against N historical 5m candles.

Usage:
    COINDESK_API_KEY=...  python -m candle_reaction.backtest --candles 6000

Output: a human-readable summary, plus a CSV in data/backtest_<ts>.csv
containing every bar with features, prediction, bet, outcome.

No look-ahead: features for bar i are built from candles[:i+1]; the
outcome for bar i is determined by comparing candles[i].close to
candles[i+1].close.
"""

from __future__ import annotations

import argparse
import csv
import time
from collections import defaultdict
from pathlib import Path

from .coindesk import CoindeskClient
from .config import load
from .features import extract
from .judge import judge
from .sizing import stake_for


def run(total_candles: int = 6000, warmup: int = 30) -> dict:
    cfg = load()
    if not cfg.api_key:
        raise SystemExit("COINDESK_API_KEY is not set")

    client = CoindeskClient(cfg)
    print(f"Fetching ~{total_candles} x {cfg.aggregate}m candles for "
          f"{cfg.instrument} on {cfg.market}...")
    candles = client.fetch_history(total_candles)
    if len(candles) < warmup + 2:
        raise SystemExit(f"Not enough candles: got {len(candles)}")
    print(f"Got {len(candles)} candles from "
          f"{candles[0].ts} to {candles[-1].ts}")

    out_rows: list[dict] = []
    equity = cfg.bankroll
    wins = losses = voids = skips = 0
    bucket_stats = defaultdict(lambda: {"n": 0, "w": 0})

    for i in range(warmup, len(candles) - 1):
        hist = candles[: i + 1]
        feat = extract(hist, lookback=cfg.lookback)
        j = judge(feat)
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

            # Bucketise by confidence.
            bucket = _bucket(j.confidence)
            bucket_stats[bucket]["n"] += 1
            if result == "WIN":
                bucket_stats[bucket]["w"] += 1

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
    print("\n==== Candle-Reaction Backtest ====")
    print(f"Candles considered : {len(candles) - warmup - 1}")
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
        s = bucket_stats.get(name, {"n": 0, "w": 0})
        rate = (s["w"] / s["n"]) if s["n"] else 0.0
        print(f"  {name}: {s['n']:4d} trades, hit {rate:.2%}")
    print(f"\nWrote {out_path}")
    return {
        "path": str(out_path),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "skips": skips,
        "equity": equity,
    }


def _bucket(confidence: float) -> str:
    if confidence < 0.80:
        return "70-80"
    if confidence < 0.90:
        return "80-90"
    return "90-100"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--candles", type=int, default=6000,
                   help="Number of 5m candles to replay (default: 6000 ≈ 20 days)")
    p.add_argument("--warmup", type=int, default=30,
                   help="Warmup bars skipped for z-score windows")
    args = p.parse_args()
    run(total_candles=args.candles, warmup=args.warmup)


if __name__ == "__main__":
    main()
