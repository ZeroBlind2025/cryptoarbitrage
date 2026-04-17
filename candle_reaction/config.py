"""Configuration for the candle-reaction module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# CoinDesk Data API.
# Docs: https://developers.coindesk.com/documentation/data-api/spot
COINDESK_REST_BASE = "https://data-api.coindesk.com"
COINDESK_HISTORICAL_MINUTES = "/spot/v1/historical/minutes"

DEFAULT_MARKET = "coinbase"
DEFAULT_INSTRUMENT = "BTC-USD"
DEFAULT_AGGREGATE = 5          # five-minute candles
DEFAULT_POLL_INTERVAL_SEC = 20  # REST polling cadence for live mode


@dataclass
class LadderRung:
    """One tier of the confidence ladder: [p_lo, p_hi) -> dollar stake."""

    p_lo: float
    p_hi: float
    stake: float


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.environ.get("COINDESK_API_KEY", ""))
    market: str = DEFAULT_MARKET
    instrument: str = DEFAULT_INSTRUMENT
    aggregate: int = DEFAULT_AGGREGATE
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC

    # Bankroll in USD (paper).
    bankroll: float = 250.0

    # Confidence ladder -- applied to max(p_up, p_down).
    # <70% -> skip. 70-80 -> $1, 80-90 -> $3, 90+ -> $5.
    ladder: list[LadderRung] = field(
        default_factory=lambda: [
            LadderRung(0.70, 0.80, 1.0),
            LadderRung(0.80, 0.90, 3.0),
            LadderRung(0.90, 1.01, 5.0),
        ]
    )

    # Rolling window for volume/range z-scores.
    lookback: int = 20

    # Where trades + signals are persisted.
    data_dir: Path = field(default_factory=lambda: Path(__file__).parent / "data")
    trades_csv: str = "trades.csv"
    signals_csv: str = "signals.csv"

    def trades_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.trades_csv

    def signals_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.signals_csv


def load() -> Config:
    """Load config from env. Convenience wrapper."""
    cfg = Config()
    bankroll = os.environ.get("CANDLE_BANKROLL")
    if bankroll:
        cfg.bankroll = float(bankroll)
    return cfg
