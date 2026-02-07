#!/usr/bin/env python3
"""
Polymarket Sports WebSocket Client
==================================

Connects to wss://sports-api.polymarket.com/ws for real-time sports data.
No authentication required - public broadcast channel.

Handles:
- Automatic PING/PONG heartbeat (5s ping, 10s timeout)
- Reconnection with exponential backoff
- Live match data: scores, periods, game status
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
import websocket


class SportsWebSocket:
    """WebSocket client for Polymarket sports data"""

    WS_URL = "wss://sports-api.polymarket.com/ws"
    PING_INTERVAL = 5  # Server pings every 5 seconds
    RECONNECT_BASE_DELAY = 1
    RECONNECT_MAX_DELAY = 60

    def __init__(
        self,
        on_update: Optional[Callable[[dict], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        self.on_update = on_update
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.running = False
        self.connected = False
        self.reconnect_delay = self.RECONNECT_BASE_DELAY

        # Track active sports events
        self.active_events: dict[str, dict] = {}  # event_id -> event data
        self._lock = threading.Lock()

    def start(self):
        """Start the WebSocket connection"""
        if self.running:
            return
        self.running = True
        self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self.ws_thread.start()
        print(f"[SPORTS WS] Starting connection to {self.WS_URL}", flush=True)

    def stop(self):
        """Stop the WebSocket connection"""
        self.running = False
        if self.ws:
            self.ws.close()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5)
        print("[SPORTS WS] Stopped", flush=True)

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
                    on_ping=self._on_ping,
                )
                # Run with ping handling
                self.ws.run_forever(
                    ping_interval=0,  # We handle pings manually
                    ping_timeout=None,
                )
            except Exception as e:
                print(f"[SPORTS WS] Error: {e}", flush=True)

            if self.running:
                # Reconnect with exponential backoff
                print(f"[SPORTS WS] Reconnecting in {self.reconnect_delay}s...", flush=True)
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2,
                    self.RECONNECT_MAX_DELAY
                )

    def _on_open(self, ws):
        """Handle connection open"""
        self.connected = True
        self.reconnect_delay = self.RECONNECT_BASE_DELAY  # Reset backoff
        print("[SPORTS WS] Connected!", flush=True)
        if self.on_connect:
            self.on_connect()

    def _on_message(self, ws, message):
        """Handle incoming message"""
        try:
            # Handle PING/PONG
            if message == "PING":
                ws.send("PONG")
                return

            # Parse JSON message
            data = json.loads(message)

            # Update active events
            self._process_update(data)

            # Call user callback
            if self.on_update:
                self.on_update(data)

        except json.JSONDecodeError:
            # Not JSON, might be control message
            pass
        except Exception as e:
            print(f"[SPORTS WS] Message error: {e}", flush=True)

    def _on_ping(self, ws, message):
        """Handle ping from server"""
        ws.send("PONG")

    def _on_error(self, ws, error):
        """Handle WebSocket error"""
        print(f"[SPORTS WS] Error: {error}", flush=True)

    def _on_close(self, ws, close_status_code, close_msg):
        """Handle connection close"""
        self.connected = False
        print(f"[SPORTS WS] Disconnected: {close_status_code} {close_msg}", flush=True)
        if self.on_disconnect:
            self.on_disconnect()

    def _process_update(self, data: dict):
        """Process and store sports update"""
        event_id = data.get("eventId") or data.get("event_id") or data.get("id")
        if not event_id:
            return

        with self._lock:
            self.active_events[event_id] = {
                "event_id": event_id,
                "data": data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def get_active_events(self) -> list[dict]:
        """Get list of all active sports events"""
        with self._lock:
            return list(self.active_events.values())

    def get_live_games(self) -> list[dict]:
        """Get list of games currently in progress"""
        with self._lock:
            live = []
            for event in self.active_events.values():
                data = event.get("data", {})
                # Check if game is live (in progress)
                status = data.get("status", "").lower()
                if status in ["live", "in_progress", "playing", "active"]:
                    live.append(event)
                # Also check period info
                period = data.get("period") or data.get("currentPeriod")
                if period and period not in ["pre", "final", "end", "finished"]:
                    live.append(event)
            return live


# Standalone test
if __name__ == "__main__":
    def on_update(data):
        print(f"[UPDATE] {json.dumps(data, indent=2)[:500]}", flush=True)

    def on_connect():
        print("[CONNECTED] Ready to receive sports updates", flush=True)

    ws = SportsWebSocket(
        on_update=on_update,
        on_connect=on_connect,
    )

    try:
        ws.start()
        print("Listening for sports updates... Press Ctrl+C to stop")
        while True:
            time.sleep(10)
            events = ws.get_active_events()
            print(f"[STATUS] {len(events)} active events tracked", flush=True)
    except KeyboardInterrupt:
        print("\nStopping...")
        ws.stop()
