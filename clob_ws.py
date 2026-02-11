#!/usr/bin/env python3
"""
Polymarket CLOB WebSocket Client
================================

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market for real-time
order book updates. No authentication required for market data.

Events received:
- book: Full order book snapshot
- price_change: Best bid/ask updates
- tick_size_change: Tick size updates
- last_trade_price: Trade executions

This enables sub-100ms signal detection vs 500ms+ REST polling.

References:
- https://docs.polymarket.com/developers/CLOB/websocket/wss-overview
- https://github.com/nevuamarkets/poly-websockets
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
import websocket


@dataclass
class OrderBookState:
    """Real-time order book state for a token"""
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    last_update_ms: int = 0

    # Full book (top N levels)
    bids: list = field(default_factory=list)  # [(price, size), ...]
    asks: list = field(default_factory=list)


class CLOBWebSocket:
    """WebSocket client for Polymarket CLOB order book data"""

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    MAX_TOKENS_PER_CONNECTION = 500
    RECONNECT_BASE_DELAY = 1
    RECONNECT_MAX_DELAY = 60

    def __init__(
        self,
        on_book_update: Optional[Callable[[str, OrderBookState], None]] = None,
        on_price_change: Optional[Callable[[str, float, float], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize CLOB WebSocket client.

        Args:
            on_book_update: Callback(token_id, book_state) for any book update
            on_price_change: Callback(token_id, best_bid, best_ask) for price changes
            on_connect: Callback when connected
            on_disconnect: Callback when disconnected
        """
        self.on_book_update = on_book_update
        self.on_price_change = on_price_change
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.running = False
        self.connected = False
        self.reconnect_delay = self.RECONNECT_BASE_DELAY

        # Token subscriptions
        self._subscribed_tokens: set[str] = set()
        self._pending_subscribe: list[str] = []

        # Order book cache: token_id -> OrderBookState
        self._books: dict[str, OrderBookState] = {}
        self._lock = threading.Lock()

        # Stats
        self.messages_received = 0
        self.book_updates = 0
        self.price_changes = 0
        self.last_message_time: Optional[datetime] = None

    def start(self):
        """Start the WebSocket connection"""
        if self.running:
            return
        self.running = True
        self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self.ws_thread.start()
        print(f"[CLOB WS] Starting connection to {self.WS_URL}", flush=True)

    def stop(self):
        """Stop the WebSocket connection"""
        self.running = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5)
        print("[CLOB WS] Stopped", flush=True)

    def subscribe(self, token_ids: list[str]):
        """
        Subscribe to order book updates for tokens.

        Args:
            token_ids: List of CLOB token IDs to subscribe to

        Note: Max 500 tokens per connection. Duplicates are ignored.
        """
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]

        if len(self._subscribed_tokens) + len(new_tokens) > self.MAX_TOKENS_PER_CONNECTION:
            print(f"[CLOB WS] Warning: Max {self.MAX_TOKENS_PER_CONNECTION} tokens. Truncating.", flush=True)
            new_tokens = new_tokens[:self.MAX_TOKENS_PER_CONNECTION - len(self._subscribed_tokens)]

        if not new_tokens:
            return

        # Initialize book state for new tokens
        with self._lock:
            for token_id in new_tokens:
                if token_id not in self._books:
                    self._books[token_id] = OrderBookState(token_id=token_id)

        if self.connected and self.ws:
            self._send_subscribe(new_tokens)
        else:
            self._pending_subscribe.extend(new_tokens)

    def _send_subscribe(self, token_ids: list[str]):
        """Send subscription message"""
        if not token_ids:
            return

        msg = {
            "assets_ids": token_ids,
            "type": "market"
        }
        try:
            self.ws.send(json.dumps(msg))
            self._subscribed_tokens.update(token_ids)
            print(f"[CLOB WS] Subscribed to {len(token_ids)} tokens (total: {len(self._subscribed_tokens)})", flush=True)
        except Exception as e:
            print(f"[CLOB WS] Subscribe error: {e}", flush=True)

    def get_book(self, token_id: str) -> Optional[OrderBookState]:
        """Get current order book state for a token"""
        with self._lock:
            return self._books.get(token_id)

    def get_best_prices(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        """Get (best_bid, best_ask) for a token"""
        book = self.get_book(token_id)
        if book:
            return book.best_bid, book.best_ask
        return None, None

    def _run_forever(self):
        """Run WebSocket with automatic reconnection"""
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                )
            except Exception as e:
                print(f"[CLOB WS] Error: {e}", flush=True)

            if self.running:
                self.connected = False
                print(f"[CLOB WS] Reconnecting in {self.reconnect_delay}s...", flush=True)
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2,
                    self.RECONNECT_MAX_DELAY
                )

    def _on_open(self, ws):
        """Handle connection open"""
        self.connected = True
        self.reconnect_delay = self.RECONNECT_BASE_DELAY
        print("[CLOB WS] Connected", flush=True)

        # Send pending subscriptions
        if self._pending_subscribe:
            self._send_subscribe(self._pending_subscribe)
            self._pending_subscribe = []

        # Resubscribe to existing tokens
        if self._subscribed_tokens:
            tokens = list(self._subscribed_tokens)
            self._subscribed_tokens.clear()
            self._send_subscribe(tokens)

        if self.on_connect:
            self.on_connect()

    def _on_message(self, ws, message):
        """Handle incoming message"""
        self.messages_received += 1
        self.last_message_time = datetime.now()

        try:
            data = json.loads(message)

            # Handle array of events (batch updates)
            if isinstance(data, list):
                for item in data:
                    self._process_single_event(item)
            else:
                self._process_single_event(data)

        except json.JSONDecodeError:
            print(f"[CLOB WS] Invalid JSON: {message[:100]}", flush=True)
        except Exception as e:
            print(f"[CLOB WS] Message error: {e}", flush=True)

    def _process_single_event(self, data: dict):
        """Process a single event message"""
        if not isinstance(data, dict):
            return

        # Handle different event types
        event_type = data.get("event_type")

        if event_type == "book":
            self._handle_book(data)
        elif event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "last_trade_price":
            self._handle_last_trade(data)
        elif event_type == "tick_size_change":
            pass  # Ignore tick size changes
        else:
            # Unknown event type - might be initial snapshot
            if "bids" in data or "asks" in data:
                self._handle_book(data)

    def _handle_book(self, data: dict):
        """Handle full book snapshot"""
        asset_id = data.get("asset_id") or data.get("market")
        if not asset_id:
            return

        self.book_updates += 1

        with self._lock:
            book = self._books.get(asset_id)
            if not book:
                book = OrderBookState(token_id=asset_id)
                self._books[asset_id] = book

            # Parse bids
            bids = data.get("bids", [])
            if bids:
                book.bids = [(float(b.get("price", 0)), float(b.get("size", 0))) for b in bids[:10]]
                if book.bids:
                    book.best_bid = book.bids[0][0]
                    book.bid_depth = sum(size for _, size in book.bids)

            # Parse asks
            asks = data.get("asks", [])
            if asks:
                book.asks = [(float(a.get("price", 0)), float(a.get("size", 0))) for a in asks[:10]]
                if book.asks:
                    book.best_ask = book.asks[0][0]
                    book.ask_depth = sum(size for _, size in book.asks)

            book.last_update_ms = int(time.time() * 1000)

        # Fire callbacks
        if self.on_book_update:
            self.on_book_update(asset_id, book)

        if self.on_price_change and book.best_bid and book.best_ask:
            self.on_price_change(asset_id, book.best_bid, book.best_ask)

    def _handle_price_change(self, data: dict):
        """Handle price change event"""
        asset_id = data.get("asset_id") or data.get("market")
        if not asset_id:
            return

        self.price_changes += 1

        changes = data.get("price_changes", [data])  # Might be single or array
        if not isinstance(changes, list):
            changes = [changes]

        for change in changes:
            best_bid = change.get("best_bid")
            best_ask = change.get("best_ask")

            if best_bid is not None or best_ask is not None:
                with self._lock:
                    book = self._books.get(asset_id)
                    if not book:
                        book = OrderBookState(token_id=asset_id)
                        self._books[asset_id] = book

                    if best_bid is not None:
                        book.best_bid = float(best_bid)
                    if best_ask is not None:
                        book.best_ask = float(best_ask)
                    book.last_update_ms = int(time.time() * 1000)

                # Fire callback
                if self.on_price_change and book.best_bid and book.best_ask:
                    self.on_price_change(asset_id, book.best_bid, book.best_ask)

    def _handle_last_trade(self, data: dict):
        """Handle last trade price event"""
        # We can use this to infer market activity but don't need to store
        pass

    def _on_error(self, ws, error):
        """Handle WebSocket error"""
        print(f"[CLOB WS] Error: {error}", flush=True)

    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close"""
        self.connected = False
        print(f"[CLOB WS] Closed: {close_status_code} - {close_msg}", flush=True)
        if self.on_disconnect:
            self.on_disconnect()

    def get_stats(self) -> dict:
        """Get WebSocket statistics"""
        return {
            "connected": self.connected,
            "subscribed_tokens": len(self._subscribed_tokens),
            "cached_books": len(self._books),
            "messages_received": self.messages_received,
            "book_updates": self.book_updates,
            "price_changes": self.price_changes,
            "last_message": self.last_message_time.isoformat() if self.last_message_time else None,
        }


# =============================================================================
# DEMO
# =============================================================================

if __name__ == "__main__":
    import sys

    def on_price_change(token_id: str, bid: float, ask: float):
        price_sum = bid + ask if bid and ask else None
        print(f"[PRICE] {token_id[:20]}... bid=${bid:.3f} ask=${ask:.3f} sum=${price_sum:.4f if price_sum else 'N/A'}")

    def on_connect():
        print("[DEMO] Connected! Subscribing to test tokens...")

    # Create client
    client = CLOBWebSocket(
        on_price_change=on_price_change,
        on_connect=on_connect,
    )

    # Start connection
    client.start()

    # Wait for connection
    time.sleep(2)

    # Subscribe to some test tokens (you'd need real token IDs)
    if len(sys.argv) > 1:
        tokens = sys.argv[1:]
        print(f"Subscribing to {len(tokens)} tokens from command line")
        client.subscribe(tokens)
    else:
        print("Usage: python clob_ws.py <token_id_1> <token_id_2> ...")
        print("No tokens provided - waiting for manual subscription")

    # Run forever
    try:
        while True:
            time.sleep(10)
            stats = client.get_stats()
            print(f"[STATS] msgs={stats['messages_received']} books={stats['book_updates']} prices={stats['price_changes']}")
    except KeyboardInterrupt:
        print("\nShutting down...")
        client.stop()
