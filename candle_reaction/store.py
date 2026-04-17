"""CSV-backed append-only store for signals and paper trades.

Two files:
  * signals.csv -- every judged candle close, whether traded or not.
  * trades.csv  -- resolved paper trades with win/loss + P&L.

Both include the full feature vector so the dashboard export gives the
signal that triggered each position.
"""

from __future__ import annotations

import csv
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .features import Features
from .judge import Judgement


SIGNAL_HEADER = [
    "candle_ts", "close_price",
    "close_position", "body_signed", "direction",
    "volume_z", "range_z", "streak",
    "p_up", "p_down", "side", "confidence",
    "stake", "traded",
]

TRADE_HEADER = [
    "entry_ts", "entry_close",
    "side", "confidence", "stake",
    "market_slug", "token_id", "fill_price", "edge", "kelly_f", "shares",
    "close_position", "body_signed", "direction",
    "volume_z", "range_z", "streak",
    "resolve_ts", "resolve_close", "settled_token_price",
    "result", "pnl", "resolution_source",
]


@dataclass
class SignalRow:
    candle_ts: int
    close_price: float
    features: Features
    judgement: Judgement
    stake: float
    traded: bool


@dataclass
class TradeRow:
    entry_ts: int
    entry_close: float
    side: str
    confidence: float
    stake: float
    features: Features
    # Polymarket execution context. When the trader couldn't find a
    # market these stay None and the trade is a synthetic 1:1 bet.
    market_slug: Optional[str] = None
    token_id: Optional[str] = None         # the specific YES-UP or YES-DOWN CLOB token we "bought"
    fill_price: Optional[float] = None     # 0..1, ask we paid
    edge: Optional[float] = None           # our_q - fill_price
    kelly_f: Optional[float] = None        # full-Kelly fraction
    shares: Optional[float] = None         # stake / fill_price
    resolve_ts: Optional[int] = None
    resolve_close: Optional[float] = None
    settled_token_price: Optional[float] = None  # final price of our token at settlement
    result: Optional[str] = None   # "WIN" / "LOSS" / "VOID"
    pnl: Optional[float] = None
    resolution_source: Optional[str] = None   # "polymarket" | "unresolved"


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()

    # ---- writers ----

    def record_signal(self, row: SignalRow) -> None:
        self._append(self.cfg.signals_path(), SIGNAL_HEADER, self._signal_values(row))

    def record_trade(self, row: TradeRow) -> None:
        self._append(self.cfg.trades_path(), TRADE_HEADER, self._trade_values(row))

    # ---- readers ----

    def read_trades(self) -> list[dict]:
        path = self.cfg.trades_path()
        if not path.exists():
            return []
        with self._lock, path.open("r", newline="") as fh:
            return list(csv.DictReader(fh))

    def read_signals(self, limit: int = 200) -> list[dict]:
        path = self.cfg.signals_path()
        if not path.exists():
            return []
        with self._lock, path.open("r", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]

    def summary(self) -> dict:
        trades = self.read_trades()
        wins = sum(1 for t in trades if t.get("result") == "WIN")
        losses = sum(1 for t in trades if t.get("result") == "LOSS")
        voids = sum(1 for t in trades if t.get("result") == "VOID")
        resolved = wins + losses
        pnl = sum(float(t["pnl"]) for t in trades if t.get("pnl") not in (None, ""))
        open_trades = [t for t in trades if not t.get("result")]
        return {
            "total": len(trades),
            "open": len(open_trades),
            "wins": wins,
            "losses": losses,
            "voids": voids,
            "win_rate": (wins / resolved) if resolved else 0.0,
            "pnl": round(pnl, 4),
            "equity": round(self.cfg.bankroll + pnl, 4),
        }

    # ---- internals ----

    def _append(self, path: Path, header: list[str], values: list) -> None:
        with self._lock:
            # Rotate if the on-disk schema no longer matches (e.g. after
            # a column is added in a deploy). Old rows are preserved at
            # <name>.legacy-<ts>.csv.
            if path.exists():
                try:
                    with path.open("r", newline="") as fh:
                        first = next(csv.reader(fh), [])
                except Exception:
                    first = []
                if first != header:
                    import time as _t
                    legacy = path.with_suffix(f".legacy-{int(_t.time())}.csv")
                    path.rename(legacy)
            new_file = not path.exists()
            with path.open("a", newline="") as fh:
                w = csv.writer(fh)
                if new_file:
                    w.writerow(header)
                w.writerow(values)

    def _signal_values(self, r: SignalRow) -> list:
        f = r.features
        j = r.judgement
        return [
            r.candle_ts, r.close_price,
            round(f.close_position, 6), round(f.body_signed, 6), f.direction,
            round(f.volume_z, 4), round(f.range_z, 4), f.streak,
            round(j.p_up, 6), round(j.p_down, 6), j.side, round(j.confidence, 6),
            round(r.stake, 4), int(r.traded),
        ]

    def _trade_values(self, r: TradeRow) -> list:
        f = r.features
        def _opt(v, digits=None):
            if v is None:
                return ""
            return round(v, digits) if digits is not None else v
        return [
            r.entry_ts, r.entry_close,
            r.side, round(r.confidence, 6), round(r.stake, 4),
            r.market_slug or "",
            r.token_id or "",
            _opt(r.fill_price, 6), _opt(r.edge, 6),
            _opt(r.kelly_f, 6), _opt(r.shares, 6),
            round(f.close_position, 6), round(f.body_signed, 6), f.direction,
            round(f.volume_z, 4), round(f.range_z, 4), f.streak,
            r.resolve_ts if r.resolve_ts is not None else "",
            r.resolve_close if r.resolve_close is not None else "",
            _opt(r.settled_token_price, 6),
            r.result or "",
            _opt(r.pnl, 4),
            r.resolution_source or "",
        ]
