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

from .coindesk import Candle
from .config import Config, load, make_live_client
from .edge import size_edge_trade
from .features import extract
from .judge import judge
from .polymarket import (
    PolyMarket,
    discover_btc_updown_5m,
    get_market_settled_prices,
    get_resolution,
    get_top_of_book,
)
from .store import SignalRow, Store, TradeRow

log = logging.getLogger("candle_reaction")


def _window_label(ts: int) -> str:
    """Format a 5m candle window like '14:55-15:00 UTC'."""
    start = datetime.fromtimestamp(ts, tz=timezone.utc)
    end = start + timedelta(minutes=5)
    return f"{start:%H:%M}-{end:%H:%M} UTC"


def _c(price: Optional[float]) -> str:
    """Format a 0..1 price as cents, e.g. 0.75 -> '75c'."""
    if price is None:
        return "n/a"
    return f"{price * 100:.0f}c"


@dataclass
class EngineState:
    running: bool = False
    last_judged_ts: Optional[int] = None
    pending_trades: list[TradeRow] = field(default_factory=list)
    last_signal: Optional[SignalRow] = None
    last_poll_ts: Optional[int] = None
    last_error: Optional[str] = None
    history: list[Candle] = field(default_factory=list)  # rolling, ~100 bars


class CandleReactionEngine:
    """Thread-safe live engine. Start via `start()` / stop via `stop()`."""

    MAX_HISTORY = 200
    # Cap concurrent pending trades so a Polymarket-wide outage can't
    # build up an unbounded queue.
    MAX_PENDING = 10
    # If Polymarket hasn't set ``closed=true`` this long after the
    # expected 5m resolve time, void the trade and move on. 1 hour is
    # far beyond normal settlement (seconds) but short enough that a
    # stuck market can't sit in the queue forever.
    STALE_AFTER_SEC = 60 * 60

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load()
        self.client = make_live_client(self.cfg)
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
        summary = self.store.summary()
        pending = [self._trade_snapshot(t) for t in s.pending_trades]
        return {
            "running": s.running,
            "last_poll_ts": s.last_poll_ts,
            "last_judged_ts": s.last_judged_ts,
            "last_error": s.last_error,
            "bankroll": self.cfg.bankroll,
            "mode": "contrarian" if self.cfg.contrarian else "continuation",
            "source": self.cfg.source,
            **summary,
            "last_signal": self._signal_snapshot(last) if last else None,
            "pending_trades": pending,
            # Back-compat: the most recent pending trade as the
            # "single open trade" the original card reads.
            "open_trade": pending[-1] if pending else None,
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
        # Opportunistic Polymarket resolution check on every tick,
        # across all pending trades. Most resolve within ~60s of the
        # 5m boundary.
        self._try_poly_resolution_all()
        self._void_stale_trades()

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

    def _try_poly_resolution_all(self) -> None:
        """Poll Polymarket for resolution of every pending trade.

        A market goes through several states after its 5m window ends:

          1. ``closed=true`` but oracle hasn't posted yet: prices are
             the last-traded book (e.g. [0.98, 0.02]).
          2. Oracle settled: prices are [1.0, 0.0] -- sum = 1.0.
          3. Post-redemption: winning token drops to 0 on the book
             while the loser can show a 1c residual ([0, 0.01]).

        Only state (2) is the definitive oracle outcome. We require
        ``our_price + other_price`` to sum to ~1.0 before trusting the
        numbers; any other sum means 'not yet oracle-settled, poll again'.
        """
        now = int(time.time())
        for t in list(self.state.pending_trades):
            if not t.market_slug or not t.token_id:
                continue
            expected = t.entry_ts + self.cfg.aggregate * 60
            if now < expected:
                continue
            try:
                settled = get_market_settled_prices(
                    t.market_slug, session=self.client.session,
                )
            except Exception as e:  # pragma: no cover - network
                log.debug("poly settled-prices check failed for %s: %s",
                          t.market_slug, e)
                continue
            if not settled:
                continue

            our_price = settled.get(str(t.token_id))
            others = [p for tid, p in settled.items() if tid != str(t.token_id)]
            other_price = others[0] if others else None
            if our_price is None or other_price is None:
                continue

            total = our_price + other_price
            # Only state (2) above: oracle-settled clean 1/0.
            if abs(total - 1.0) > 0.02:
                log.info(
                    "BTC [%s] awaiting oracle settle: our=%.4f other=%.4f sum=%.4f",
                    _window_label(t.entry_ts), our_price, other_price, total,
                )
                continue

            self._finalise_trade_by_token(
                t, our_price=our_price, other_price=other_price,
            )

    def _void_stale_trades(self) -> None:
        """Safety valve: if Polymarket never settles, mark UNRESOLVED.

        This should be extremely rare (Polymarket BTC markets settle in
        seconds from Chainlink), but without a cap a stuck market
        could keep a trade pending forever.
        """
        now = int(time.time())
        for t in list(self.state.pending_trades):
            expected = t.entry_ts + self.cfg.aggregate * 60
            if now - expected < self.STALE_AFTER_SEC:
                continue
            log.warning(
                "BTC [%s] STALE after %ds past expected resolve; marking UNRESOLVED",
                _window_label(t.entry_ts), now - expected,
            )
            self._finalise_trade(t, outcome="VOID", source="unresolved")

    def _process_new_close(self, candle: Candle, closed_hist: list[Candle]) -> None:
        # Resolution of prior trades is handled entirely by the per-tick
        # Polymarket poll; nothing to do against CoinDesk/Coinbase closes
        # here -- using a different price source would drift from
        # Chainlink settlement.

        # Judge the newly-closed candle.
        feat = extract(closed_hist, lookback=self.cfg.lookback)
        j = judge(feat, contrarian=self.cfg.contrarian)

        # Degenerate bar: the CoinDesk row had zero range so features
        # collapsed to the neutral midpoint. Judge output is meaningless;
        # record the signal but never trade on it.
        degenerate = (
            feat.body_signed == 0.0
            and abs(feat.close_position - 0.5) < 1e-9
            and feat.volume_z == 0.0
            and feat.range_z == 0.0
        )

        # 3. Ask Polymarket for the next-bucket BTC updown-5m market
        #    and compute the edge trade. If nothing tradable, signal only.
        if degenerate:
            edge_trade, market, up_ask, down_ask = None, None, None, None
        else:
            edge_trade, market, up_ask, down_ask = self._price_polymarket(candle.ts, j.p_up)
        stake = edge_trade.stake if edge_trade else 0.0

        # 4. Record the signal (always).
        sig = SignalRow(
            candle_ts=candle.ts,
            close_price=candle.close,
            features=feat,
            judgement=j,
            stake=stake,
            traded=edge_trade is not None,
        )
        self.store.record_signal(sig)
        self.state.last_signal = sig

        window = _window_label(candle.ts)
        if edge_trade is None:
            if degenerate:
                log.warning(
                    "BTC [%s] SKIP degenerate candle (flat OHLC from data source); "
                    "judge uninformative, no trade",
                    window,
                )
                return
            if market is None:
                log.info(
                    "BTC [%s] SKIP side=%s conf=%.1f%% (no Polymarket market) close=$%.2f",
                    window, j.side, j.confidence * 100.0, candle.close,
                )
            else:
                log.info(
                    "BTC [%s] SKIP side=%s conf=%.1f%% (no edge; up_ask=%s down_ask=%s) close=$%.2f",
                    window, j.side, j.confidence * 100.0,
                    _c(up_ask), _c(down_ask), candle.close,
                )
            return

        # Cap the pending queue to avoid unbounded build-up if
        # Polymarket is ever having an outage.
        if len(self.state.pending_trades) >= self.MAX_PENDING:
            log.warning(
                "BTC [%s] SKIP side=%s (pending queue full: %d)",
                window, edge_trade.side, len(self.state.pending_trades),
            )
            return

        # Record the exact CLOB token we "bought" so resolution can
        # look up that one token's settled price directly (no outcome
        # label interpretation).
        token_id = (
            market.yes_up_token_id if edge_trade.side == "UP"
            else market.yes_down_token_id
        ) if market else None

        # Append a new paper trade; it'll resolve via Polymarket polling.
        trade = TradeRow(
            entry_ts=candle.ts,
            entry_close=candle.close,
            side=edge_trade.side,
            confidence=edge_trade.our_q,
            stake=edge_trade.stake,
            features=feat,
            market_slug=market.slug if market else None,
            token_id=token_id,
            fill_price=edge_trade.fill_price,
            edge=edge_trade.edge,
            kelly_f=edge_trade.kelly_f,
            shares=edge_trade.shares,
        )
        self.state.pending_trades.append(trade)
        log.info(
            "BTC [%s] ENTER %s @ %s (edge %s, q=%.1f%%) stake=$%.2f close=$%.2f pending=%d",
            window, edge_trade.side, _c(edge_trade.fill_price), _c(edge_trade.edge),
            edge_trade.our_q * 100.0, edge_trade.stake, candle.close,
            len(self.state.pending_trades),
        )

    def _price_polymarket(self, candle_ts: int, p_up: float):
        """Return (EdgeTrade|None, PolyMarket|None, up_ask, down_ask)."""
        try:
            market = discover_btc_updown_5m(candle_ts, session=self.client.session)
        except Exception as e:  # pragma: no cover - network
            log.warning("polymarket discovery failed: %s", e)
            return None, None, None, None
        if market is None:
            return None, None, None, None

        up_tob = get_top_of_book(market.yes_up_token_id, session=self.client.session)
        down_tob = get_top_of_book(market.yes_down_token_id, session=self.client.session)
        up_ask = up_tob.ask if up_tob else None
        down_ask = down_tob.ask if down_tob else None

        trade = size_edge_trade(
            p_up=p_up,
            up_ask=up_ask,
            down_ask=down_ask,
            bankroll=self.cfg.bankroll,
        )
        return trade, market, up_ask, down_ask

    def _finalise_trade_by_token(
        self, t: TradeRow, our_price: float, other_price: float,
    ) -> None:
        """Close out ``t`` using the oracle-settled token prices.

        Caller has already verified ``our_price + other_price`` ≈ 1.0
        so the prices are the oracle-settled outcome, not a transient
        book state. Our token at ~1.0 means we won; at ~0.0 means we
        lost; ~0.5 means the market voided.
        """
        t.settled_token_price = our_price
        if our_price >= 0.98:
            t.result = "WIN"
            if t.fill_price and t.fill_price > 0:
                t.pnl = t.stake * (1.0 - t.fill_price) / t.fill_price
            else:
                t.pnl = t.stake
        elif our_price <= 0.02:
            t.result = "LOSS"
            t.pnl = -t.stake
        elif 0.45 <= our_price <= 0.55:
            t.result = "VOID"
            t.pnl = 0.0
        else:
            log.warning(
                "BTC [%s] settlement within sum=1 band but our_price=%.4f is ambiguous; "
                "leaving open",
                _window_label(t.entry_ts), our_price,
            )
            return

        t.resolve_ts = int(time.time())
        t.resolution_source = "polymarket"
        self.store.record_trade(t)
        log.info(
            "BTC [%s] RESOLVE %s -> %s pnl=%s$%.2f (our=%.3f other=%.3f)",
            _window_label(t.entry_ts), t.side, t.result,
            "+" if (t.pnl or 0) >= 0 else "-",
            abs(t.pnl or 0.0),
            our_price, other_price,
        )
        if t in self.state.pending_trades:
            self.state.pending_trades.remove(t)

    def _finalise_trade(
        self,
        t: TradeRow,
        outcome: str,
        source: str,
        resolve_close: Optional[float] = None,
        resolve_ts: Optional[int] = None,
    ) -> None:
        """Close out ``t`` with the given outcome and persist."""
        if outcome == "VOID":
            t.result = "VOID"
            t.pnl = 0.0
        elif outcome == t.side:
            t.result = "WIN"
            # Polymarket binary pays $1/share on win. Profit = shares - stake
            # = stake * (1 - fill_price) / fill_price. Fall back to 1:1 if
            # fill_price is missing (synthetic, pre-Polymarket).
            if t.fill_price and t.fill_price > 0:
                t.pnl = t.stake * (1.0 - t.fill_price) / t.fill_price
            else:
                t.pnl = t.stake
        else:
            t.result = "LOSS"
            t.pnl = -t.stake

        t.resolve_ts = resolve_ts if resolve_ts is not None else int(time.time())
        t.resolve_close = resolve_close
        t.resolution_source = source
        self.store.record_trade(t)
        log.info(
            "BTC [%s] RESOLVE %s -> %s pnl=%s$%.2f (source=%s, entry=%.2f)",
            _window_label(t.entry_ts), t.side, t.result,
            "+" if (t.pnl or 0) >= 0 else "-",
            abs(t.pnl or 0.0),
            source,
            t.entry_close,
        )
        if t in self.state.pending_trades:
            self.state.pending_trades.remove(t)

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
            "fill_price": round(t.fill_price, 4) if t.fill_price is not None else None,
            "edge": round(t.edge, 4) if t.edge is not None else None,
            "market_slug": t.market_slug,
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
