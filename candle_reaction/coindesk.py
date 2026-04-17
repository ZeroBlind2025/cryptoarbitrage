"""Thin CoinDesk Data API client.

Only the spot historical-minutes endpoint is needed -- the live loop
polls the same endpoint for the most recent closed candle, so a full
WebSocket implementation is unnecessary at a 5-minute cadence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from .config import COINDESK_HISTORICAL_MINUTES, COINDESK_REST_BASE, Config

log = logging.getLogger("candle_reaction.coindesk")


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

    # CoinDesk /spot/v1/historical/minutes serves at most ~2000 raw 1m
    # points per call; each returned row is ``aggregate`` minutes of
    # those, so the usable bar cap is ``2000 // aggregate``. 400 @ 5m,
    # 133 @ 15m, 33 @ 1h. Give ourselves 1 row of headroom to avoid
    # off-by-one server rejections.
    _AGG_BUDGET = 2000

    def _max_per_call(self, aggregate: int) -> int:
        if aggregate <= 0:
            return self._AGG_BUDGET
        return max(1, (self._AGG_BUDGET // aggregate) - 1)

    def fetch_minutes(
        self,
        limit: Optional[int] = None,
        to_ts: Optional[int] = None,
        aggregate: Optional[int] = None,
        instrument: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch historical minute candles, aggregated to `aggregate` minutes.

        The server caps rows at ``floor(2000 / aggregate)``. For deeper
        history, call repeatedly with `to_ts` stepping backwards.
        """
        agg = aggregate or self.cfg.aggregate
        cap = self._max_per_call(agg)
        req_limit = cap if limit is None else min(limit, cap)
        params = {
            "market": market or self.cfg.market,
            "instrument": instrument or self.cfg.instrument,
            "aggregate": agg,
            "limit": req_limit,
        }
        if to_ts is not None:
            params["to_ts"] = to_ts
        if self.cfg.api_key:
            params["api_key"] = self.cfg.api_key

        url = COINDESK_REST_BASE + COINDESK_HISTORICAL_MINUTES
        resp = self.session.get(url, params=params, timeout=15)
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"CoinDesk returned non-JSON body: {resp.text[:200]!r}")

        # CoinDesk Data API returns 4xx/5xx with a structured Err envelope.
        err = payload.get("Err")
        if err and (err.get("message") or err.get("type")):
            raise RuntimeError(f"CoinDesk API error: {err.get('message') or err}")
        if not resp.ok:
            resp.raise_for_status()

        rows = payload.get("Data") or payload.get("data") or []
        candles: list[Candle] = []
        dropped = 0
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
                dropped += 1
                continue
            try:
                o, h, lo, c = float(o), float(h), float(lo), float(c)
            except (TypeError, ValueError):
                dropped += 1
                continue
            # Reject degenerate rows: CoinDesk occasionally ships zero-filled
            # 5m buckets for recent windows before aggregation completes.
            # A BTC bar where open==high==low==close or all are zero is
            # useless; it makes the feature extractor read body==0 /
            # close_position==0.5 which collapses the judge to p=0.5.
            if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
                dropped += 1
                continue
            if h == lo:   # range is zero -> dead bar
                dropped += 1
                continue
            candles.append(
                Candle(ts=int(ts), open=o, high=h, low=lo, close=c, volume=float(v))
            )
        if dropped:
            log.warning("coindesk: dropped %d degenerate bar(s) out of %d",
                        dropped, len(rows))
        candles.sort(key=lambda k: k.ts)
        return candles

    def fetch_history(self, total: int) -> list[Candle]:
        """Paginate backwards until at least `total` candles are collected."""
        out: list[Candle] = []
        to_ts: Optional[int] = None
        per_call = self._max_per_call(self.cfg.aggregate)
        while len(out) < total:
            batch = self.fetch_minutes(limit=per_call, to_ts=to_ts)
            if not batch:
                break
            out = batch + out
            to_ts = batch[0].ts - 1
            if len(batch) < per_call:
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
