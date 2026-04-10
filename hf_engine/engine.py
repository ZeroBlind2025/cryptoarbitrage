"""
hf_engine.engine
================

Main orchestrator for the Hawkes-Glosten-Milgrom high-frequency engine.

Owns:

    * the global configuration
    * the market scanner (discovery thread)
    * the WebSocket trade feed (background thread owned by HFETradeFeed)
    * the per-market state store
    * the paper executor
    * the online calibrator

This class exposes a single ``run()`` method that drives the whole
event loop until interrupted. It is deliberately synchronous — the
Hawkes / GM update path is O(1) per trade and bounded well under 1 ms,
so ``asyncio`` buys us nothing and complicates callback semantics with
the thread-based WebSocket.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from .calibration import OnlineCalibrator
from .config import HFEConfig
from .feed import BookUpdate, HFETradeFeed, TradeEvent
from .market_state import MarketState, Trade
from .paper_executor import PaperExecutor
from .scanner import MarketMeta, MarketScanner


class HFEngine:
    def __init__(self, cfg: Optional[HFEConfig] = None) -> None:
        self.cfg = cfg or HFEConfig.from_env()

        self.markets: Dict[str, MarketState] = {}
        self._markets_lock = threading.Lock()

        self.scanner = MarketScanner(self.cfg)
        self.executor = PaperExecutor(self.cfg)
        self.calibrator = OnlineCalibrator(self.cfg)

        self.feed = HFETradeFeed(
            ws_url=self.cfg.clob_ws_url,
            on_trade=self._on_trade,
            on_book=self._on_book,
            log_prefix=f"{self.cfg.log_prefix}-FEED",
        )

        self._running = False
        self._scan_thread: Optional[threading.Thread] = None
        self._main_thread: Optional[threading.Thread] = None
        self._last_report_at = 0
        self._markets_seen_total = 0
        self._markets_resolved_total = 0
        self._tick_count = 0
        self._started_at: Optional[float] = None
        # Rate limiter for the "recreate the feed when stale" path so
        # we cannot spiral into a tight recreate loop while waiting on
        # the first WS message.
        self._last_feed_reset_at = 0.0
        self._feed_reset_min_interval = 60.0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Start the engine and block until interrupted."""
        self.start_background()
        try:
            # The main-loop thread is doing the work; this call just
            # waits on it so ``run()`` keeps its blocking semantics.
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._log("shutdown requested")
        finally:
            self.stop()

    def start_background(self) -> None:
        """Start the engine in background threads and return immediately.

        This is the entry point used by the Flask dashboard server: the
        Flask process owns the main thread and hosts HTTP handlers,
        while the engine runs the scanner, feed, and main-loop threads
        underneath it.
        """
        if self._running:
            return
        self._log(self.cfg.summary())
        self._log("paper trading mode — NO live orders will be placed")

        self.feed.start()
        self._running = True
        self._started_at = time.time()
        self._scan_thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="hfe-scanner"
        )
        self._scan_thread.start()
        self._main_thread = threading.Thread(
            target=self._main_loop, daemon=True, name="hfe-main"
        )
        self._main_thread.start()

    def stop(self) -> None:
        self._running = False
        self.feed.stop()
        self._log(f"final stats: {self.executor.stats()}")

    # ------------------------------------------------------------------ #
    # Accessors (used by hf_engine.server and hf_engine.snapshot)
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    def list_markets(self) -> list:
        with self._markets_lock:
            return list(self.markets.values())

    def stats(self) -> dict:
        return {
            "markets_seen_total": self._markets_seen_total,
            "markets_resolved_total": self._markets_resolved_total,
            "tick_count": self._tick_count,
            "uptime_sec": self.uptime_seconds,
            "running": self._running,
            "feed": self.feed.stats(),
            "executor": self.executor.stats(),
            "priors": list(self.calibrator.current_priors()),
        }

    # ------------------------------------------------------------------ #
    # Scanner thread
    # ------------------------------------------------------------------ #

    def _scan_loop(self) -> None:
        while self._running:
            try:
                found = self.scanner.scan()
                for meta in found:
                    self._ingest_new_market(meta)

                # Check for resolved markets and settle them.
                with self._markets_lock:
                    market_ids = list(self.markets.keys())
                for mid in market_ids:
                    self._maybe_resolve_market(mid)

            except Exception as e:
                self._log(f"scan loop error: {e}")
            time.sleep(self.cfg.scan_interval_sec)

    def _ingest_new_market(self, meta: MarketMeta) -> None:
        with self._markets_lock:
            if meta.market_id in self.markets:
                return

            market = MarketState(
                cfg=self.cfg,
                market_id=meta.market_id,
                yes_token_id=meta.yes_token_id,
                no_token_id=meta.no_token_id,
                description=meta.description,
                created_at=time.time(),
                resolves_at=meta.resolves_at,
                interval_label=meta.interval_label,
            )
            # Seed the GM posterior with the known last trade price so
            # we do not start from 50/50 if the market has pre-existing
            # book activity.
            if 0.01 < meta.initial_yes_price < 0.99:
                market.gm.reset(meta.initial_yes_price)

            # Apply current calibrator priors. Every new market benefits
            # from whatever the online calibrator has learned so far.
            current_alpha, current_beta, current_pi = self.calibrator.current_priors()
            market.hawkes_buy.alpha = current_alpha
            market.hawkes_buy.beta = current_beta
            market.hawkes_sell.alpha = current_alpha
            market.hawkes_sell.beta = current_beta
            # pi_base stays in the cfg; pi_effective will track cascade.
            self.markets[meta.market_id] = market

        # Register with the feed (outside the lock to avoid contention).
        self.feed.register_market(
            market_id=meta.market_id,
            yes_token_id=meta.yes_token_id,
            no_token_id=meta.no_token_id,
        )

        self._markets_seen_total += 1
        self._log(
            f"new market {meta.interval_label} {meta.description[:60]} "
            f"(id={meta.market_id[:12]}... "
            f"resolves_in={meta.resolves_at - time.time():.0f}s)"
        )

    def _maybe_resolve_market(self, market_id: str) -> None:
        with self._markets_lock:
            market = self.markets.get(market_id)
        if market is None:
            return

        # Once the market is past its end time, settle and evict.
        if market.time_remaining_sec > 0:
            return

        # Give the outcome endpoint a few seconds of grace then look up.
        outcome = self.scanner.lookup_resolution(market_id)
        if outcome is None:
            # Not yet reported; if we are more than 2 minutes past end
            # time, give up and treat as unresolved (still clean up).
            if (time.time() - market.resolves_at) < 120:
                return

        market.resolved_outcome = outcome
        market.resolution_time = time.time()

        if outcome is not None and market.open_position is not None:
            self.executor.settle_at_resolution(market)
        elif market.open_position is not None:
            # Fallback: close at the last known mid.
            self.executor.close_position(market, reason="unresolved-timeout")

        if outcome is not None:
            self.calibrator.on_market_resolved(market)

        self._markets_resolved_total += 1
        self.feed.unregister_market(market_id)
        with self._markets_lock:
            self.markets.pop(market_id, None)

    # ------------------------------------------------------------------ #
    # Feed callbacks
    # ------------------------------------------------------------------ #

    def _on_trade(self, market_id: str, ev: TradeEvent) -> None:
        with self._markets_lock:
            market = self.markets.get(market_id)
        if market is None:
            return

        trade = Trade(
            timestamp=ev.timestamp,
            side="buy" if ev.side == "BUY" else "sell",
            price=ev.price,
            size=ev.size,
        )
        market.on_trade(trade)

        # Evaluate entry.
        if market.open_position is None:
            sig = market.evaluate_entry_signal()
            self.executor.log_signal(market, sig.reason, accepted=(sig.action != "none"))
            if sig.action in ("buy_yes", "buy_no"):
                self.executor.open_position(market, action=sig.action, reason=sig.reason)
        else:
            exit_sig = market.evaluate_exit_signal()
            if exit_sig.action == "exit":
                self.executor.close_position(market, reason=exit_sig.reason)

    def _on_book(self, market_id: str, update: BookUpdate) -> None:
        with self._markets_lock:
            market = self.markets.get(market_id)
        if market is None:
            return
        market.update_book(
            side_token=update.token_id,
            best_bid=update.best_bid,
            best_ask=update.best_ask,
            depth=update.depth,
        )

    # ------------------------------------------------------------------ #
    # Main loop (reporting + housekeeping)
    # ------------------------------------------------------------------ #

    def _main_loop(self) -> None:
        while self._running:
            self._tick_count += 1
            now = time.time()
            if (
                self._markets_resolved_total > 0
                and (self._markets_resolved_total % self.cfg.report_every_n_markets == 0)
                and (now - self._last_report_at) > self.cfg.scan_interval_sec
            ):
                self._report()
                self._last_report_at = now

            if (
                self.feed.is_stale()
                and (now - self._last_feed_reset_at) > self._feed_reset_min_interval
            ):
                self._log("feed appears stale — forcing reconnect")
                self._last_feed_reset_at = now
                try:
                    self.feed.stop()
                except Exception:
                    pass
                self.feed = HFETradeFeed(
                    ws_url=self.cfg.clob_ws_url,
                    on_trade=self._on_trade,
                    on_book=self._on_book,
                    log_prefix=f"{self.cfg.log_prefix}-FEED",
                )
                self.feed.start()
                # Re-register everything we are tracking.
                with self._markets_lock:
                    to_reg = list(self.markets.values())
                for m in to_reg:
                    self.feed.register_market(m.market_id, m.yes_token_id, m.no_token_id)

            time.sleep(max(self.cfg.loop_sleep_sec, 0.05))

    def _report(self) -> None:
        stats = self.executor.stats()
        priors = self.calibrator.current_priors()
        active = len(self.markets)
        self._log(
            f"report: markets_seen={self._markets_seen_total} "
            f"resolved={self._markets_resolved_total} "
            f"active={active} "
            f"pnl=${stats['realized_pnl']:+.3f} "
            f"wins={stats['wins']} losses={stats['losses']} "
            f"priors alpha={priors[0]:.3f} beta={priors[1]:.3f} pi={priors[2]:.3f}"
        )

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #

    def _log(self, msg: str) -> None:
        print(f"{self.cfg.log_prefix} {msg}", flush=True)
