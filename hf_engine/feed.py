"""
hf_engine.feed
==============

Self-contained Polymarket CLOB WebSocket feed for the Hawkes-GM engine.

This module deliberately does **not** import ``clob_ws.py`` from the
repository root. ``clob_ws.py`` is consumed by the momentum engine and
its trade-event handler is a no-op (clob_ws.py:389-392). We need a
distinct feed that:

    * captures ``last_trade_price`` events with side / size / timestamp
    * normalizes every trade into the Yes-token frame of reference
    * fires ``on_trade`` / ``on_book`` callbacks the engine can register
    * keeps its own reconnection state so it cannot contaminate anything
      the momentum side of the codebase is doing with its WS session

The feed runs in a background thread using ``websocket-client`` just like
``clob_ws.py``, so there are zero new runtime dependencies.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import websocket


@dataclass
class TradeEvent:
    token_id: str
    timestamp: float       # epoch seconds (server-provided when available)
    side: str              # "BUY" or "SELL" as reported by Polymarket
    price: float
    size: float


@dataclass
class BookUpdate:
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    depth: float
    last_update_ms: int


@dataclass
class TokenRegistration:
    """Mapping from a subscribed Polymarket asset id to its role."""

    market_id: str
    token_id: str
    is_yes: bool


class HFETradeFeed:
    """Trade-aware Polymarket CLOB WebSocket client.

    Call ``register_market`` once the scanner has discovered a new
    5m/15m market and knows both its Yes and No token ids. The feed
    will subscribe to both sides so a cross-side (BUY on No) can be
    mapped back to a SELL on Yes before being forwarded to the engine.
    """

    RECONNECT_BASE_DELAY = 1
    RECONNECT_MAX_DELAY = 60
    SUBSCRIBE_CHUNK_SIZE = 20
    MAX_TOKENS_PER_CONNECTION = 500
    STALE_TIMEOUT_SEC = 120

    def __init__(
        self,
        ws_url: str,
        on_trade: Callable[[str, TradeEvent], None],
        on_book: Callable[[str, BookUpdate], None],
        log_prefix: str = "[HFE-FEED]",
    ) -> None:
        self.ws_url = ws_url
        self.on_trade = on_trade
        self.on_book = on_book
        self.log_prefix = log_prefix

        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.running = False
        self.connected = False
        self.reconnect_delay = self.RECONNECT_BASE_DELAY

        self._subscribed: Set[str] = set()
        self._pending: List[str] = []
        self._lock = threading.Lock()

        # token_id -> TokenRegistration
        self._registry: Dict[str, TokenRegistration] = {}

        self.messages_received = 0
        self.trades_received = 0
        self.book_updates = 0
        self.last_message_time: Optional[float] = None
        # Tracks when ``start()`` was called so the stale-feed guard in
        # the main loop respects a grace period before declaring the
        # feed dead. Without this the main loop would keep tearing the
        # feed down on every tick while waiting for the first message.
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._started_at = time.time()
        self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self.ws_thread.start()
        print(f"{self.log_prefix} starting WS connection to {self.ws_url}", flush=True)

    def stop(self) -> None:
        self.running = False
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5)
        print(f"{self.log_prefix} stopped", flush=True)

    def register_market(
        self,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        """Register both outcome tokens and subscribe to them."""
        with self._lock:
            self._registry[yes_token_id] = TokenRegistration(
                market_id=market_id, token_id=yes_token_id, is_yes=True
            )
            self._registry[no_token_id] = TokenRegistration(
                market_id=market_id, token_id=no_token_id, is_yes=False
            )
        self._subscribe([yes_token_id, no_token_id])

    def unregister_market(self, market_id: str) -> None:
        """Drop a market from the registry. We cannot unsubscribe on
        Polymarket's WS but we can stop routing its events."""
        with self._lock:
            drop = [t for t, r in self._registry.items() if r.market_id == market_id]
            for t in drop:
                self._registry.pop(t, None)

    def is_stale(self) -> bool:
        """Return True if no messages have flowed for ``STALE_TIMEOUT_SEC``.

        A feed that has never received a message is only considered
        stale once ``STALE_TIMEOUT_SEC`` has elapsed since it was
        started, so the main loop cannot flail reconnecting before the
        initial handshake has had a chance to complete.
        """
        reference = self.last_message_time
        if reference is None:
            reference = self._started_at
        if reference is None:
            return False
        return (time.time() - reference) > self.STALE_TIMEOUT_SEC

    # ------------------------------------------------------------------ #
    # WebSocket lifecycle
    # ------------------------------------------------------------------ #

    def _run_forever(self) -> None:
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"{self.log_prefix} run_forever error: {e}", flush=True)

            if self.running:
                self.connected = False
                print(
                    f"{self.log_prefix} reconnecting in {self.reconnect_delay}s",
                    flush=True,
                )
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2, self.RECONNECT_MAX_DELAY
                )

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self.connected = True
        self.reconnect_delay = self.RECONNECT_BASE_DELAY
        print(f"{self.log_prefix} connected", flush=True)

        # Flush anything we queued while disconnected, then re-subscribe
        # everything currently in the registry so we survive reconnects.
        with self._lock:
            queued = list(self._pending)
            self._pending.clear()
            if queued:
                self._send_subscribe(queued)
            registered = list(self._registry.keys())
        if registered:
            # Clear the "known subscribed" cache so we resend after a
            # reconnect — the server lost our prior subscription.
            self._subscribed.clear()
            self._send_subscribe(registered)

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"{self.log_prefix} ws error: {error}", flush=True)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        self.connected = False
        print(
            f"{self.log_prefix} ws closed: code={close_status_code} msg={close_msg}",
            flush=True,
        )

    # ------------------------------------------------------------------ #
    # Subscription helpers
    # ------------------------------------------------------------------ #

    def _subscribe(self, token_ids: List[str]) -> None:
        with self._lock:
            new_tokens = [t for t in token_ids if t not in self._subscribed]
            if not new_tokens:
                return
            if len(self._subscribed) + len(new_tokens) > self.MAX_TOKENS_PER_CONNECTION:
                budget = self.MAX_TOKENS_PER_CONNECTION - len(self._subscribed)
                new_tokens = new_tokens[:max(0, budget)]
                if not new_tokens:
                    print(
                        f"{self.log_prefix} subscription budget exhausted",
                        flush=True,
                    )
                    return
            if self.connected and self.ws is not None:
                self._send_subscribe(new_tokens)
            else:
                self._pending.extend(new_tokens)

    def _send_subscribe(self, token_ids: List[str]) -> None:
        if not token_ids or self.ws is None:
            return
        for i in range(0, len(token_ids), self.SUBSCRIBE_CHUNK_SIZE):
            chunk = token_ids[i : i + self.SUBSCRIBE_CHUNK_SIZE]
            message = {
                "auth": {},
                "markets": [],
                "assets_ids": chunk,
                "type": "market",
            }
            try:
                self.ws.send(json.dumps(message))
                self._subscribed.update(chunk)
            except Exception as e:
                print(f"{self.log_prefix} subscribe error: {e}", flush=True)
                break

    # ------------------------------------------------------------------ #
    # Message parsing
    # ------------------------------------------------------------------ #

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        self.messages_received += 1
        self.last_message_time = time.time()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._dispatch(item)
        elif isinstance(data, dict):
            self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        event_type = data.get("event_type")
        if event_type == "last_trade_price":
            self._handle_trade(data)
        elif event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "book":
            self._handle_book(data)
        elif event_type is None:
            if "price_changes" in data:
                self._handle_price_change(data)
            elif "bids" in data or "asks" in data:
                self._handle_book(data)

    def _handle_trade(self, data: dict) -> None:
        asset_id = data.get("asset_id") or data.get("market")
        if not asset_id:
            return
        with self._lock:
            reg = self._registry.get(asset_id)
        if reg is None:
            return

        try:
            price = float(data.get("price") or 0.0)
            size = float(data.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        if size <= 0:
            return

        side_raw = (data.get("side") or "").upper()
        if side_raw not in ("BUY", "SELL"):
            return

        timestamp_raw = data.get("timestamp")
        if timestamp_raw is not None:
            try:
                ts_float = float(timestamp_raw)
                # Polymarket timestamps are milliseconds.
                timestamp = ts_float / 1000.0 if ts_float > 1e12 else ts_float
            except (TypeError, ValueError):
                timestamp = time.time()
        else:
            timestamp = time.time()

        # Normalize to the Yes-token frame (Section 13.4.3 — the GM update
        # is defined on Yes-side trades, so a BUY of the No token is a
        # SELL of the Yes token).
        if reg.is_yes:
            normalized_side = side_raw
            normalized_price = price
        else:
            normalized_side = "SELL" if side_raw == "BUY" else "BUY"
            normalized_price = 1.0 - price

        trade = TradeEvent(
            token_id=reg.token_id,
            timestamp=timestamp,
            side=normalized_side,
            price=normalized_price,
            size=size,
        )
        self.trades_received += 1
        try:
            self.on_trade(reg.market_id, trade)
        except Exception as e:
            print(f"{self.log_prefix} on_trade callback error: {e}", flush=True)

    def _handle_book(self, data: dict) -> None:
        asset_id = data.get("asset_id") or data.get("market")
        if not asset_id:
            return
        with self._lock:
            reg = self._registry.get(asset_id)
        if reg is None:
            return

        bids = data.get("bids", []) or []
        asks = data.get("asks", []) or []

        best_bid: Optional[float] = None
        best_ask: Optional[float] = None
        depth = 0.0

        if bids:
            try:
                best_bid = float(bids[0].get("price", 0.0))
                depth += sum(float(b.get("size", 0.0)) for b in bids[:10])
            except (TypeError, ValueError):
                best_bid = None
        if asks:
            try:
                best_ask = float(asks[0].get("price", 0.0))
                depth += sum(float(a.get("size", 0.0)) for a in asks[:10])
            except (TypeError, ValueError):
                best_ask = None

        update = BookUpdate(
            token_id=reg.token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            depth=depth,
            last_update_ms=int(time.time() * 1000),
        )
        self.book_updates += 1
        try:
            self.on_book(reg.market_id, update)
        except Exception as e:
            print(f"{self.log_prefix} on_book callback error: {e}", flush=True)

    def _handle_price_change(self, data: dict) -> None:
        changes = data.get("price_changes", [data])
        if not isinstance(changes, list):
            changes = [changes]

        for change in changes:
            if not isinstance(change, dict):
                continue
            asset_id = change.get("asset_id") or data.get("asset_id") or data.get("market")
            if not asset_id:
                continue
            with self._lock:
                reg = self._registry.get(asset_id)
            if reg is None:
                continue

            best_bid = None
            best_ask = None
            try:
                if change.get("best_bid") is not None:
                    best_bid = float(change.get("best_bid"))
                if change.get("best_ask") is not None:
                    best_ask = float(change.get("best_ask"))
            except (TypeError, ValueError):
                continue

            update = BookUpdate(
                token_id=reg.token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                depth=0.0,  # price_change doesn't include depth
                last_update_ms=int(time.time() * 1000),
            )
            try:
                self.on_book(reg.market_id, update)
            except Exception as e:
                print(
                    f"{self.log_prefix} on_book callback error: {e}",
                    flush=True,
                )

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "subscribed_tokens": len(self._subscribed),
            "registered_tokens": len(self._registry),
            "messages_received": self.messages_received,
            "trades_received": self.trades_received,
            "book_updates": self.book_updates,
            "last_message": self.last_message_time,
        }
