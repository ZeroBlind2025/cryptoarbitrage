"""
Market Maker Engine — BTC 5m Up/Down markets (standalone).

Places resting GTC limit BUY orders on the "Up" outcome at a fixed price
when the live Up price is above a threshold.  Orders rest on the book
until filled or the market closes, then resolved positions pay out at $1.

Strategy:
  - Discover active BTC 5m markets
  - If Up price > ENTRY_THRESHOLD (default 50¢), place a GTC limit BUY
    for "Up" at LIMIT_PRICE (default 70¢) with LOT_SIZE (default $1)
  - Track open orders; cancel stale ones when market closes
  - On fill, record the position and let it resolve naturally

Railway env vars:
  MM_LIMIT_PRICE       — limit price for Up bets (default 0.70)
  MM_ENTRY_THRESHOLD   — only place order when Up > this (default 0.50)
  MM_LOT_SIZE          — USDC per order (default 1.0)
  MM_POLL_INTERVAL     — seconds between scans (default 5)
  MM_DRY_RUN           — true/false (default true)
  MM_COINS             — comma-separated coins (default btc)
  MM_INTERVALS         — comma-separated intervals (default 5m)
"""

import os
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LIMIT_PRICE = float(os.getenv("MM_LIMIT_PRICE", "0.70"))
ENTRY_THRESHOLD = float(os.getenv("MM_ENTRY_THRESHOLD", "0.50"))
LOT_SIZE = float(os.getenv("MM_LOT_SIZE", "1.0"))
POLL_INTERVAL = int(os.getenv("MM_POLL_INTERVAL", "5"))
DRY_RUN = os.getenv("MM_DRY_RUN", "true").lower() == "true"
MM_COINS = [c.strip() for c in os.getenv("MM_COINS", "btc").split(",") if c.strip()]
MM_INTERVALS = [i.strip() for i in os.getenv("MM_INTERVALS", "5m").split(",") if i.strip()]

# Reuse infrastructure from existing codebase
from copy_trader import get_clob_client
from momentum_engine import discover_active_markets

# WebSocket for live prices (optional — falls back to gamma)
_ws = None
try:
    from clob_ws import CLOBWebSocket
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    print("[MM] ERROR: py_clob_client not installed")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Order tracking
# ---------------------------------------------------------------------------
open_orders: dict = {}          # condition_id → order info
_ordered_markets: set = set()   # condition_ids we've already ordered on
_ws_price_cache: dict = {}      # token_id → price from WS


def _on_ws_price(asset_id: str, price: float, **kwargs):
    """WS callback — just cache the price."""
    _ws_price_cache[asset_id] = price


def _get_live_price(token_id: str, ws) -> float | None:
    """Get best ask from WS, or from cache."""
    if ws:
        try:
            _, best_ask = ws.get_best_prices(token_id)
            if best_ask is not None:
                return best_ask
        except Exception:
            pass
    return _ws_price_cache.get(token_id)


def place_limit_order(client, token_id: str, price: float, size: float,
                      label: str = "") -> dict:
    """Place a GTC limit BUY order (maker/post-only)."""
    try:
        price_rounded = round(price, 2)
        shares = round(size / price_rounded, 4)

        print(f"[MM] GTC limit BUY: {shares:.4f} shares @ {price_rounded*100:.0f}¢ "
              f"(${size:.2f} USDC) {label}")

        order_args = OrderArgs(
            token_id=token_id,
            price=price_rounded,
            size=shares,
            side=BUY,
        )
        signed_order = client.create_order(
            order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
        )
        result = client.post_order(signed_order, OrderType.GTC, post_only=True)

        if isinstance(result, dict):
            order_id = result.get("orderID") or result.get("id") or result.get("order_id", "")
            status = result.get("status", "").lower()
            if status in ("rejected", "failed"):
                print(f"[MM] Order rejected: {result}")
                return {}
            print(f"[MM] Order placed: id={order_id} status={status}")
            return {"order_id": order_id, "success": True, "result": result}

        print(f"[MM] Unexpected result type: {result}")
        return {}
    except Exception as e:
        print(f"[MM] Order error: {e}")
        import traceback; traceback.print_exc()
        return {}


def cancel_order(client, order_id: str) -> bool:
    try:
        client.cancel(order_id)
        print(f"[MM] Cancelled order {order_id}")
        return True
    except Exception as e:
        print(f"[MM] Cancel error for {order_id}: {e}")
        return False


def cancel_all_orders(client) -> int:
    try:
        client.cancel_all()
        count = len(open_orders)
        open_orders.clear()
        _ordered_markets.clear()
        print(f"[MM] Cancelled all orders ({count} tracked)")
        return count
    except Exception as e:
        print(f"[MM] Cancel all error: {e}")
        return 0


class MarketMaker:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.client = None
        self.ws = None
        self.scans = 0
        self.orders_placed = 0
        self._last_discovery = 0
        self._cached_markets = []
        self._discovery_interval = 30
        self._last_ws_refresh = 0

    def start(self):
        print("\n" + "=" * 50)
        print("  MARKET MAKER ENGINE")
        print(f"  Coins: {', '.join(MM_COINS)}")
        print(f"  Intervals: {', '.join(MM_INTERVALS)}")
        print(f"  Limit price: {LIMIT_PRICE*100:.0f}¢ (Up)")
        print(f"  Entry threshold: >{ENTRY_THRESHOLD*100:.0f}¢")
        print(f"  Lot size: ${LOT_SIZE:.2f}")
        print(f"  Poll: {POLL_INTERVAL}s")
        print(f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print("=" * 50 + "\n")

        if not self.dry_run:
            self.client = get_clob_client()
            if not self.client:
                print("[MM] Failed to init CLOB client — falling back to dry run")
                self.dry_run = True

        # Start WS for live prices (no MomentumEngine needed)
        if _HAS_WS:
            try:
                self.ws = CLOBWebSocket(
                    on_price_change=_on_ws_price,
                    on_connect=lambda: print("[MM] WebSocket connected"),
                    on_disconnect=lambda: print("[MM] WebSocket disconnected"),
                )
                self.ws.start()
                time.sleep(1)
                print("[MM] WebSocket started")
            except Exception as e:
                print(f"[MM] WebSocket failed: {e} — using REST/gamma prices")
                self.ws = None

    def _refresh_ws_tokens(self):
        """Subscribe WS to token IDs from discovered markets."""
        if not self.ws or not self._cached_markets:
            return
        now = time.time()
        if now - self._last_ws_refresh < 30:
            return
        self._last_ws_refresh = now

        token_ids = []
        for m in self._cached_markets:
            token_ids.extend(m.get("token_ids", []))
        if token_ids:
            try:
                self.ws.subscribe_to_assets(token_ids)
                print(f"[MM] WS subscribed to {len(token_ids)} tokens")
            except Exception as e:
                print(f"[MM] WS subscribe error: {e}")

    def _discover(self):
        now = time.time()
        if now - self._last_discovery < self._discovery_interval and self._cached_markets:
            return self._cached_markets

        all_markets = discover_active_markets()
        filtered = [
            m for m in all_markets
            if m["coin"] in MM_COINS and m["interval"] in MM_INTERVALS
        ]
        self._cached_markets = filtered
        self._last_discovery = time.time()

        if filtered:
            labels = [f"{m['coin'].upper()}_{m['interval']}" for m in filtered]
            print(f"[MM] Discovered {len(filtered)} markets: {', '.join(labels)}")

        # Refresh WS subscriptions with new token IDs
        self._refresh_ws_tokens()
        return filtered

    def scan(self) -> int:
        self.scans += 1
        markets = self._discover()
        if not markets:
            return 0

        # Periodic WS refresh
        if self.scans % 5 == 1:
            self._refresh_ws_tokens()

        placed = 0
        for market in markets:
            condition_id = market["condition_id"]
            coin = market["coin"]
            slug = market["slug"]

            if condition_id in _ordered_markets:
                continue

            end_date = market.get("end_date")
            if end_date:
                minutes_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 60
                if minutes_left < 1:
                    continue

            # "Up" is outcome index 0
            up_token_id = market["token_ids"][0]
            up_price = _get_live_price(up_token_id, self.ws)
            if up_price is None:
                up_price = market["prices"][0]

            label = f"{coin.upper()}_{market['interval']} {(market.get('question') or slug)[:40]}"

            if up_price <= ENTRY_THRESHOLD:
                if self.scans % 30 == 1:
                    print(f"[MM] SKIP {label}: Up @ {up_price*100:.1f}¢ <= {ENTRY_THRESHOLD*100:.0f}¢")
                continue

            if up_price < LIMIT_PRICE:
                if self.scans % 30 == 1:
                    print(f"[MM] SKIP {label}: Up @ {up_price*100:.1f}¢ < {LIMIT_PRICE*100:.0f}¢")
                continue

            print(f"[MM] CANDIDATE {label}: Up @ {up_price*100:.1f}¢")

            if self.dry_run:
                print(f"[MM] DRY RUN — would place GTC BUY Up @ {LIMIT_PRICE*100:.0f}¢ ${LOT_SIZE:.2f}")
                _ordered_markets.add(condition_id)
                placed += 1
            else:
                result = place_limit_order(
                    self.client, up_token_id, LIMIT_PRICE, LOT_SIZE, label=label,
                )
                if result.get("success"):
                    order_id = result.get("order_id", "unknown")
                    open_orders[condition_id] = {
                        "order_id": order_id,
                        "token_id": up_token_id,
                        "price": LIMIT_PRICE,
                        "size": LOT_SIZE,
                        "placed_at": datetime.now(timezone.utc).isoformat(),
                        "market": label,
                    }
                    _ordered_markets.add(condition_id)
                    placed += 1
                    self.orders_placed += 1

        return placed

    def cleanup_expired(self):
        if self.dry_run or not self.client:
            return

        to_remove = []
        for cid, order in open_orders.items():
            market = next((m for m in self._cached_markets if m["condition_id"] == cid), None)
            if market:
                end_date = market.get("end_date")
                if end_date:
                    minutes_left = (end_date - datetime.now(timezone.utc)).total_seconds() / 60
                    if minutes_left < 0.5:
                        cancel_order(self.client, order["order_id"])
                        to_remove.append(cid)
            else:
                cancel_order(self.client, order["order_id"])
                to_remove.append(cid)

        for cid in to_remove:
            del open_orders[cid]
            _ordered_markets.discard(cid)

    def run_loop(self):
        self.start()
        print(f"[MM] Starting scan loop (every {POLL_INTERVAL}s) — Ctrl+C to stop\n")

        try:
            while True:
                try:
                    placed = self.scan()
                    if placed:
                        print(f"[MM] Placed {placed} new order(s) this cycle")

                    self.cleanup_expired()

                    # Clear ordered set every 5 minutes (new markets appear)
                    if self.scans % (300 // max(POLL_INTERVAL, 1)) == 0:
                        _ordered_markets.clear()

                    # Status every 60s
                    if self.scans % (60 // max(POLL_INTERVAL, 1)) == 0:
                        print(f"[MM] Status: scans={self.scans} "
                              f"orders_placed={self.orders_placed} "
                              f"open={len(open_orders)} "
                              f"markets={len(self._cached_markets)}")

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"[MM] Scan error: {e}")
                    import traceback; traceback.print_exc()

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n[MM] Shutting down...")
            if self.client and open_orders:
                print(f"[MM] Cancelling {len(open_orders)} open orders...")
                cancel_all_orders(self.client)
            print("[MM] Stopped.")


if __name__ == "__main__":
    dry = "--live" not in sys.argv
    if not dry:
        print("[MM] *** LIVE MODE ***")
    mm = MarketMaker(dry_run=dry)
    mm.run_loop()
