"""Thin CoinDesk Data API client.

Only the spot historical-minutes endpoint is needed -- the live loop
polls the same endpoint for the most recent closed candle, so a full
WebSocket implementation is unnecessary at a 5-minute cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from .config import COINDESK_HISTORICAL_MINUTES, COINDESK_REST_BASE, Config


@dataclass(frozen=True)
class Candle:
    ts: int          # unix seconds, candle close time
    open: float
    high: float
    low: float
    close: float
    volume: float    # base-asset volume

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def range(self) -> float:
        return self.high - self.low


class CoindeskClient:
    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def fetch_minutes(
        self,
        limit: int = 2000,
        to_ts: Optional[int] = None,
        aggregate: Optional[int] = None,
        instrument: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch historical minute candles, aggregated to `aggregate` minutes.

        `limit` is capped at 2000 by the API. For deeper history, call
        repeatedly with `to_ts` stepping backwards.
        """
        params = {
            "market": market or self.cfg.market,
            "instrument": instrument or self.cfg.instrument,
            "aggregate": aggregate or self.cfg.aggregate,
            "limit": min(limit, 2000),
        }
        if to_ts is not None:
            params["to_ts"] = to_ts
        if self.cfg.api_key:
            params["api_key"] = self.cfg.api_key

        url = COINDESK_REST_BASE + COINDESK_HISTORICAL_MINUTES
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        rows = payload.get("Data") or payload.get("data") or []
        candles: list[Candle] = []
        for r in rows:
            # Field names follow the CCData / CoinDesk Data convention.
            ts = r.get("TIMESTAMP") or r.get("time") or r.get("TS")
            o = r.get("OPEN") or r.get("open")
            h = r.get("HIGH") or r.get("high")
            lo = r.get("LOW") or r.get("low")
            c = r.get("CLOSE") or r.get("close")
            v = (
                r.get("VOLUME")
                or r.get("volumefrom")
                or r.get("VOLUME_FROM")
                or 0.0
            )
            if None in (ts, o, h, lo, c):
                continue
            candles.append(
                Candle(
                    ts=int(ts),
                    open=float(o),
                    high=float(h),
                    low=float(lo),
                    close=float(c),
                    volume=float(v),
                )
            )
        candles.sort(key=lambda k: k.ts)
        return candles

    def fetch_history(self, total: int) -> list[Candle]:
        """Paginate backwards until at least `total` candles are collected."""
        out: list[Candle] = []
        to_ts: Optional[int] = None
        while len(out) < total:
            batch = self.fetch_minutes(limit=2000, to_ts=to_ts)
            if not batch:
                break
            out = batch + out
            to_ts = batch[0].ts - 1
            if len(batch) < 2000:
                break
        # Dedupe by timestamp, keep chronological order.
        seen: set[int] = set()
        unique: list[Candle] = []
        for c in out:
            if c.ts in seen:
                continue
            seen.add(c.ts)
            unique.append(c)
        return unique[-total:] if total else unique
