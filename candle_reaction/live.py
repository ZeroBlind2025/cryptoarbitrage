"""Live paper-trading loop.

Polls CoinDesk every `poll_interval_sec` for the latest 5m candles.
When a new CLOSED candle appears (ts strictly newer than the last one
we judged), we:

    1. Resolve the previous open paper trade (if any) against the
       newly-closed candle's close.
    2. Run the judge on the newly-closed candle.
    3. Size via the ladder.
    4. Record signal + (if staked > 0) open a new trade.

All state is in the Store's CSV files, so the Flask dashboard can read
the same files without coupling.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .coindesk import Candle, CoindeskClient
from .config import Config, load
from .features import extract
from .judge import judge
from .sizing import stake_for
from .store import SignalRow, Store, TradeRow

log = logging.getLogger("candle_reaction")


def _window_label(ts: int) -> str:
    """Format a 5m candle window like '14:55-15:00 UTC'."""
    start = datetime.fromtimestamp(ts, tz=timezone.utc)
    end = start + timedelta(minutes=5)
    return f"{start:%H:%M}-{end:%H:%M} UTC"


@dataclass
class EngineState:
    running: bool = False
    last_judged_ts: Optional[int] = None
    open_trade: Optional[TradeRow] = None
    last_signal: Optional[SignalRow] = None
    last_poll_ts: Optional[int] = None
    last_error: Optional[str] = None
    history: list[Candle] = field(default_factory=list)  # rolling, ~100 bars


class CandleReactionEngine:
    """Thread-safe live engine. Start via `start()` / stop via `stop()`."""

    MAX_HISTORY = 200

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load()
        self.client = CoindeskClient(self.cfg)
        self.store = Store(self.cfg)
        self.state = EngineState()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self.state.running:
                return
            if not self.cfg.api_key:
                self.state.last_error = "COINDESK_API_KEY not set"
                return
            self._stop.clear()
            self.state.running = True
            self._thread = threading.Thread(
                target=self._run, name="candle-reaction-loop", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self.state.running:
                return
            self._stop.set()
            self.state.running = False

    # ---- status for the dashboard ----

    def status(self) -> dict:
        s = self.state
        last = s.last_signal
        open_t = s.open_trade
        summary = self.store.summary()
        return {
            "running": s.running,
            "last_poll_ts": s.last_poll_ts,
            "last_judged_ts": s.last_judged_ts,
            "last_error": s.last_error,
            "bankroll": self.cfg.bankroll,
            "mode": "contrarian" if self.cfg.contrarian else "continuation",
            **summary,
            "last_signal": self._signal_snapshot(last) if last else None,
            "open_trade": self._trade_snapshot(open_t) if open_t else None,
        }

    def set_contrarian(self, contrarian: bool) -> None:
        """Flip live-engine polarity. Only affects signals after the flip."""
        self.cfg.contrarian = bool(contrarian)

    # ---- main loop ----

    def _run(self) -> None:
        # Prime history so z-scores are meaningful from the first live bar.
        try:
            self.state.history = self.client.fetch_minutes(
                limit=max(self.cfg.lookback * 4, 80)
            )
            if self.state.history:
                self.state.last_judged_ts = self.state.history[-1].ts
                log.info("primed %d history bars", len(self.state.history))
        except Exception as e:  # pragma: no cover - network
            self.state.last_error = f"prime failed: {e}"
            log.exception("prime failed")

        while not self._stop.is_set():
            try:
                self._tick()
                self.state.last_error = None
            except Exception as e:  # pragma: no cover - network
                self.state.last_error = str(e)
                log.exception("tick failed")
            self.state.last_poll_ts = int(time.time())
            self._stop.wait(self.cfg.poll_interval_sec)

    def _tick(self) -> None:
        fresh = self.client.fetch_minutes(limit=30)
        if not fresh:
            return

        # Merge into the rolling history, dedup by ts.
        seen = {c.ts for c in self.state.history}
        for c in fresh:
            if c.ts not in seen:
                self.state.history.append(c)
                seen.add(c.ts)
        self.state.history.sort(key=lambda c: c.ts)
        if len(self.state.history) > self.MAX_HISTORY:
            self.state.history = self.state.history[-self.MAX_HISTORY:]

        # The most recent candle from the API is still forming for the
        # current bucket; we want only CLOSED bars. Assume anything
        # whose ts is older than (now - aggregate*60 + 5s slack) is safe.
        now = int(time.time())
        closed = [c for c in self.state.history
                  if c.ts <= now - self.cfg.aggregate * 60 + 5]
        if not closed:
            return

        newest = closed[-1]
        if self.state.last_judged_ts == newest.ts:
            return  # already processed

        self._process_new_close(newest, closed)
        self.state.last_judged_ts = newest.ts

    def _process_new_close(self, candle: Candle, closed_hist: list[Candle]) -> None:
        # 1. Resolve any open trade against this just-closed candle.
        self._resolve_open_trade(candle)

        # 2. Judge the newly-closed candle.
        feat = extract(closed_hist, lookback=self.cfg.lookback)
        j = judge(feat, contrarian=self.cfg.contrarian)
        stake = stake_for(j.confidence, self.cfg)

        # 3. Record the signal.
        sig = SignalRow(
            candle_ts=candle.ts,
            close_price=candle.close,
            features=feat,
            judgement=j,
            stake=stake,
            traded=stake > 0.0,
        )
        self.store.record_signal(sig)
        self.state.last_signal = sig

        if stake <= 0.0:
            log.info(
                "BTC [%s] SKIP side=%s conf=%.1f%% (below ladder) close=$%.2f",
                _window_label(candle.ts), j.side, j.confidence * 100.0, candle.close,
            )
            return

        # 4. Open a new paper trade; resolution happens on the NEXT close.
        self.state.open_trade = TradeRow(
            entry_ts=candle.ts,
            entry_close=candle.close,
            side=j.side,
            confidence=j.confidence,
            stake=stake,
            features=feat,
        )
        log.info(
            "BTC [%s] ENTER %s conf=%.1f%% stake=$%.2f close=$%.2f",
            _window_label(candle.ts), j.side, j.confidence * 100.0, stake, candle.close,
        )

    def _resolve_open_trade(self, new_candle: Candle) -> None:
        t = self.state.open_trade
        if not t:
            return
        if new_candle.close > t.entry_close:
            actual = "UP"
        elif new_candle.close < t.entry_close:
            actual = "DOWN"
        else:
            actual = "VOID"

        if actual == "VOID":
            t.result = "VOID"
            t.pnl = 0.0
        elif actual == t.side:
            t.result = "WIN"
            t.pnl = t.stake
        else:
            t.result = "LOSS"
            t.pnl = -t.stake

        t.resolve_ts = new_candle.ts
        t.resolve_close = new_candle.close
        self.store.record_trade(t)
        log.info(
            "BTC [%s] RESOLVE %s -> %s pnl=%s$%.2f (%.2f -> %.2f)",
            _window_label(t.entry_ts), t.side, t.result,
            "+" if (t.pnl or 0) >= 0 else "-",
            abs(t.pnl or 0.0),
            t.entry_close, new_candle.close,
        )
        self.state.open_trade = None

    # ---- snapshots ----

    def _signal_snapshot(self, s: SignalRow) -> dict:
        return {
            "candle_ts": s.candle_ts,
            "close_price": s.close_price,
            "p_up": round(s.judgement.p_up, 4),
            "p_down": round(s.judgement.p_down, 4),
            "side": s.judgement.side,
            "confidence": round(s.judgement.confidence, 4),
            "stake": s.stake,
            "traded": s.traded,
            "features": {
                "close_position": round(s.features.close_position, 4),
                "body_signed": round(s.features.body_signed, 4),
                "volume_z": round(s.features.volume_z, 3),
                "range_z": round(s.features.range_z, 3),
                "streak": s.features.streak,
            },
        }

    def _trade_snapshot(self, t: TradeRow) -> dict:
        return {
            "entry_ts": t.entry_ts,
            "entry_close": t.entry_close,
            "side": t.side,
            "confidence": round(t.confidence, 4),
            "stake": t.stake,
        }


# Module-level singleton so hft_server can grab a shared instance.
_engine: Optional[CandleReactionEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> CandleReactionEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = CandleReactionEngine()
        return _engine
