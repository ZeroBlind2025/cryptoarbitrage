#!/usr/bin/env python3
"""
WebSocket Market Discovery
===========================
Connects to Polymarket WebSocket endpoints WITHOUT pre-known asset IDs.
Dumps all incoming events to discover what crypto markets are live.

Approach: Subscribe with empty/wildcard and see what flows through.
Also tries the RTDS endpoint which may broadcast all market activity.
"""

import asyncio
import json
import sys
import time
from datetime import datetime

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

# Polymarket WebSocket endpoints
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com/ws"

# Crypto keywords to flag
CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "xrp", "ripple", "crypto", "doge", "dogecoin",
    "15 min", "30 min", "60 min", "minute", "hour",
]

# Collect unique asset IDs we discover
discovered_assets = {}  # asset_id -> {first_seen, event_count, last_price_data}
all_events = []


async def try_clob_ws():
    """Try the CLOB WebSocket with various subscription approaches"""
    print(f"\n{'='*60}")
    print(f"[CLOB] Connecting to {CLOB_WS}")
    print(f"{'='*60}")

    try:
        async with websockets.connect(CLOB_WS, ping_interval=30) as ws:
            print("[CLOB] Connected!")

            # Try 1: Subscribe to "market" channel with no specific assets
            # This might give us all market events
            subscribe_msgs = [
                {"type": "market", "assets_ids": []},
                {"type": "subscribe", "channel": "market"},
                {"type": "market"},
            ]

            for msg in subscribe_msgs:
                print(f"[CLOB] Sending: {json.dumps(msg)}")
                await ws.send(json.dumps(msg))
                await asyncio.sleep(0.5)

            print("[CLOB] Listening for events (30s)...")
            start = time.time()
            count = 0

            while time.time() - start < 30:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=5)
                    count += 1
                    parsed = json.loads(data) if isinstance(data, str) else data

                    # Track asset IDs
                    _track_event("CLOB", parsed)

                    # Print first 20 events in full, then just summaries
                    if count <= 20:
                        print(f"[CLOB #{count}] {json.dumps(parsed, indent=2)[:500]}")
                    elif count % 50 == 0:
                        print(f"[CLOB] {count} events received so far...")

                except asyncio.TimeoutError:
                    print(f"[CLOB] No data for 5s (total events: {count})")
                except Exception as e:
                    print(f"[CLOB] Error: {e}")
                    break

            print(f"[CLOB] Done. Total events: {count}")

    except Exception as e:
        print(f"[CLOB] Connection failed: {e}")


async def try_rtds_ws():
    """Try the RTDS WebSocket which may broadcast all market activity"""
    print(f"\n{'='*60}")
    print(f"[RTDS] Connecting to {RTDS_WS}")
    print(f"{'='*60}")

    # Try multiple RTDS URL patterns
    rtds_urls = [
        "wss://ws-live-data.polymarket.com/ws",
        "wss://ws-live-data.polymarket.com",
        "wss://live-data-ws.polymarket.com/ws",
    ]

    for url in rtds_urls:
        print(f"\n[RTDS] Trying: {url}")
        try:
            async with websockets.connect(url, ping_interval=30, close_timeout=5) as ws:
                print(f"[RTDS] Connected to {url}!")

                # Try subscribing
                subscribe_msgs = [
                    {"type": "subscribe", "channel": "prices"},
                    {"type": "subscribe", "channel": "market_activity"},
                    {"type": "subscribe", "channel": "live-activity"},
                    {"type": "subscribe"},
                ]

                for msg in subscribe_msgs:
                    try:
                        print(f"[RTDS] Sending: {json.dumps(msg)}")
                        await ws.send(json.dumps(msg))
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass

                print("[RTDS] Listening for events (30s)...")
                start = time.time()
                count = 0

                while time.time() - start < 30:
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=5)
                        count += 1
                        parsed = json.loads(data) if isinstance(data, str) else data

                        _track_event("RTDS", parsed)

                        if count <= 20:
                            print(f"[RTDS #{count}] {json.dumps(parsed, indent=2)[:500]}")
                        elif count % 50 == 0:
                            print(f"[RTDS] {count} events received...")

                    except asyncio.TimeoutError:
                        print(f"[RTDS] No data for 5s (total: {count})")
                    except Exception as e:
                        print(f"[RTDS] Error: {e}")
                        break

                print(f"[RTDS] Done with {url}. Total events: {count}")
                if count > 0:
                    return  # Got data, no need to try other URLs

        except Exception as e:
            print(f"[RTDS] Failed {url}: {e}")

    print("[RTDS] All URLs exhausted")


def _track_event(source, parsed):
    """Track discovered assets from events"""
    if isinstance(parsed, list):
        for item in parsed:
            _track_single(source, item)
    elif isinstance(parsed, dict):
        _track_single(source, parsed)


def _track_single(source, data):
    """Track a single event"""
    if not isinstance(data, dict):
        return

    asset_id = data.get("asset_id") or data.get("market") or data.get("condition_id") or data.get("id")
    if asset_id:
        if asset_id not in discovered_assets:
            discovered_assets[asset_id] = {
                "source": source,
                "first_seen": datetime.now().isoformat(),
                "event_count": 0,
                "event_types": set(),
                "sample_data": {},
            }
        discovered_assets[asset_id]["event_count"] += 1

        event_type = data.get("event_type", "unknown")
        discovered_assets[asset_id]["event_types"].add(event_type)

        # Capture price info if available
        for key in ["best_bid", "best_ask", "price", "last_trade_price", "outcome", "question", "title", "description"]:
            if key in data:
                discovered_assets[asset_id]["sample_data"][key] = data[key]

    # Also check nested structures
    for key in ["market", "markets", "events", "data"]:
        nested = data.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    _track_single(source, item)
        elif isinstance(nested, dict):
            _track_single(source, nested)

    # Store full event for analysis
    all_events.append({"source": source, "data": data})


def print_summary():
    """Print summary of discovered assets"""
    print(f"\n{'='*60}")
    print(f"DISCOVERY SUMMARY")
    print(f"{'='*60}")
    print(f"Total unique assets discovered: {len(discovered_assets)}")
    print(f"Total events collected: {len(all_events)}")

    if discovered_assets:
        print(f"\nDiscovered asset IDs:")
        for asset_id, info in sorted(discovered_assets.items(), key=lambda x: -x[1]["event_count"]):
            types = ", ".join(info["event_types"]) if isinstance(info["event_types"], set) else str(info["event_types"])
            print(f"  {asset_id[:40]}...")
            print(f"    source={info['source']} events={info['event_count']} types=[{types}]")
            if info["sample_data"]:
                for k, v in info["sample_data"].items():
                    print(f"    {k}: {str(v)[:80]}")
            print()

    # Check for crypto keywords in any event data
    print(f"\nSearching all events for crypto keywords...")
    crypto_hits = []
    for evt in all_events:
        text = json.dumps(evt["data"]).lower()
        for kw in CRYPTO_KEYWORDS:
            if kw in text:
                crypto_hits.append((kw, evt))
                break

    if crypto_hits:
        print(f"Found {len(crypto_hits)} crypto-related events!")
        for kw, evt in crypto_hits[:10]:
            print(f"  keyword='{kw}': {json.dumps(evt['data'])[:300]}")
    else:
        print("No crypto keywords found in any events")

    # Save raw data for analysis
    outfile = "/home/user/cryptoarbitrage/ws_discovery_dump.json"
    with open(outfile, "w") as f:
        # Convert sets to lists for JSON
        assets_serializable = {}
        for k, v in discovered_assets.items():
            v_copy = dict(v)
            v_copy["event_types"] = list(v_copy["event_types"])
            assets_serializable[k] = v_copy

        json.dump({
            "discovered_assets": assets_serializable,
            "total_events": len(all_events),
            "events_sample": [{"source": e["source"], "data": e["data"]} for e in all_events[:100]],
        }, f, indent=2, default=str)
    print(f"\nRaw data saved to {outfile}")


async def main():
    print("Polymarket WebSocket Discovery")
    print(f"Started: {datetime.now().isoformat()}")
    print("Connecting to both endpoints to discover live markets...\n")

    # Run both WebSocket connections concurrently
    await asyncio.gather(
        try_clob_ws(),
        try_rtds_ws(),
        return_exceptions=True,
    )

    print_summary()


if __name__ == "__main__":
    asyncio.run(main())
