"""Coinbase Exchange public candles client.

Swapped in as the default live-feed source because CoinDesk's spot
endpoint was returning zero-range rows for recent 5m buckets that
collapsed the judge to p=0.5. Coinbase's /products/<id>/candles is
auth-less, returns clean BTC-USD OHLCV direct from their book, and
is the same feed most market makers use.

Endpoint:
  GET https://api.exchange.coinbase.com/products/BTC-USD/candles
      ?granularity=300[&start=ISO&end=ISO]

Response: a list of arrays ``[time, low, high, open, close, volume]``,
newest-first, up to 300 rows per call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from .coindesk import Candle
from .config import Config

log = logging.getLogger("candle_reaction.coinbase")

COINBASE_BASE = "https://api.exchange.coinbase.com"
COINBASE_CANDLES = "/products/{product_id}/candles"

MAX_PER_REQUEST = 300  # Coinbase cap


class CoinbaseClient:
    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def fetch_minutes(
        self,
        limit: int = MAX_PER_REQUEST,
        to_ts: Optional[int] = None,
        aggregate: Optional[int] = None,
        instrument: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch aggregated Coinbase candles.

        ``aggregate`` is in minutes (default from cfg); it maps to the
        API's ``granularity`` in seconds. Valid: 1, 5, 15, 60, 360, 1440.
        """
        product_id = instrument or self.cfg.instrument
        granularity = (aggregate or self.cfg.aggregate) * 60

        params: dict = {"granularity": granularity}
        if to_ts is not None:
            span = min(limit, MAX_PER_REQUEST) * granularity
            start_iso = datetime.fromtimestamp(to_ts - span, tz=timezone.utc).isoformat()
            end_iso = datetime.fromtimestamp(to_ts, tz=timezone.utc).isoformat()
            params["start"] = start_iso
            params["end"] = end_iso

        url = COINBASE_BASE + COINBASE_CANDLES.format(product_id=product_id)
        resp = self.session.get(url, params=params, timeout=15)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"coinbase: non-JSON body {resp.text[:200]!r}")

        # Error envelope is a dict with a "message" field; success is a list.
        if isinstance(data, dict):
            raise RuntimeError(f"coinbase API error: {data.get('message') or data}")
        if not resp.ok or not isinstance(data, list):
            raise RuntimeError(f"coinbase unexpected response (status={resp.status_code})")

        candles: list[Candle] = []
        dropped = 0
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                dropped += 1
                continue
            try:
                ts = int(row[0])
                low = float(row[1]); high = float(row[2])
                open_p = float(row[3]); close_p = float(row[4])
                vol = float(row[5])
            except (TypeError, ValueError):
                dropped += 1
                continue
            # Same sanity guard as the hardened CoinDesk parser.
            if open_p <= 0 or high <= 0 or low <= 0 or close_p <= 0 or high == low:
                dropped += 1
                continue
            candles.append(Candle(
                ts=ts, open=open_p, high=high, low=low, close=close_p, volume=vol,
            ))
        if dropped:
            log.warning("coinbase: dropped %d degenerate bar(s) out of %d",
                        dropped, len(data))
        candles.sort(key=lambda k: k.ts)
        if limit and len(candles) > limit:
            candles = candles[-limit:]
        return candles

    def fetch_history(self, total: int) -> list[Candle]:
        """Paginate backwards until at least ``total`` bars are collected."""
        out: list[Candle] = []
        to_ts: Optional[int] = None
        while len(out) < total:
            batch = self.fetch_minutes(limit=MAX_PER_REQUEST, to_ts=to_ts)
            if not batch:
                break
            out = batch + out
            to_ts = batch[0].ts - 1
            if len(batch) < MAX_PER_REQUEST:
                break
        seen: set[int] = set()
        unique: list[Candle] = []
        for c in out:
            if c.ts in seen:
                continue
            seen.add(c.ts)
            unique.append(c)
        return unique[-total:] if total else unique
