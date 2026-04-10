"""
hf_engine.config
================

All tunable parameters for the Hawkes-Glosten-Milgrom High-Frequency Binary
Engine. Every environment variable consumed by this engine is prefixed
``HFE_`` (Hawkes-Flow Engine). This module deliberately reads **no**
``MOMENTUM_*`` variables and does **not** import from ``momentum_engine.py``
so that this file cannot trigger parent-momentum behavior.

All parameters are drawn from sections 10.1-10.5 of the theoretical spec.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


def _getenv_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _getenv_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw


@dataclass
class HFEConfig:
    """Complete Hawkes-GM engine configuration.

    All fields are populated from HFE_* environment variables with sensible
    defaults sourced from the theoretical spec. Call ``HFEConfig.from_env()``
    at startup and keep a single instance for the run.
    """

    # ------------------------------------------------------------------
    # 10.1 — Hawkes parameters
    # ------------------------------------------------------------------
    # Excitation amplitude and decay rate. Starting priors; the online
    # calibrator will EMA-blend these after every resolved market.
    hawkes_alpha_prior: float = 0.5
    hawkes_beta_prior: float = 1.0

    # Baseline rate used as a fallback if a brand-new market has no early
    # observation window data yet. Expressed in trades / second.
    hawkes_mu_fallback: float = 0.2

    # Duration of the early-observation window used to estimate mu fresh
    # for each market before signal gating is enabled.
    mu_observation_window_sec: float = 15.0

    # Cascade threshold on the branching ratio n = alpha / beta.
    cascade_threshold: float = 0.5

    # Hard cap on the per-side intensity to keep the recursion stable if
    # a pathological burst of trades arrives.
    intensity_cap: float = 50.0

    # ------------------------------------------------------------------
    # 10.2 — Glosten-Milgrom parameters
    # ------------------------------------------------------------------
    pi_base: float = 0.15
    pi_hawkes_boost: float = 0.30
    pi_cap: float = 0.95

    # Clamp on the log-odds to prevent numerical overflow. log_odds = 10
    # corresponds to a probability of ~0.99995.
    max_log_odds: float = 10.0

    # ------------------------------------------------------------------
    # 10.3 — Signal gate thresholds
    # ------------------------------------------------------------------
    min_flow_imbalance: float = 0.15
    min_edge: float = 0.05                 # dollars (posterior vs CLOB)
    min_time_remaining_sec: float = 30.0
    min_book_depth: float = 50.0           # contracts

    # ------------------------------------------------------------------
    # 10.4 — Position sizing
    # ------------------------------------------------------------------
    kelly_base_fraction: float = 0.25
    max_dollar_risk_per_market: float = 10.0
    max_total_dollar_risk: float = 50.0

    # ------------------------------------------------------------------
    # 10.5 — Exit parameters
    # ------------------------------------------------------------------
    edge_collapse_threshold: float = 0.10
    contrary_flow_threshold: float = 0.20
    exit_time_buffer_sec: float = 15.0

    # ------------------------------------------------------------------
    # Scanner / market selection (Section 9)
    # ------------------------------------------------------------------
    scan_interval_sec: float = 10.0
    min_market_duration_min: float = 4.0
    max_market_duration_min: float = 65.0
    min_pre_volume_usd: float = 1000.0
    min_unique_traders: int = 10
    tracked_coin_slugs: Tuple[str, ...] = field(
        default_factory=lambda: ("bitcoin", "ethereum", "solana", "xrp", "btc", "eth", "sol")
    )

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------
    enable_online_calibration: bool = True
    calibration_ema_alpha: float = 0.10     # weight given to each new market
    calibration_pi_grid_lo: float = 0.05
    calibration_pi_grid_hi: float = 0.45
    calibration_pi_grid_steps: int = 9
    calibration_alpha_grid: Tuple[float, ...] = field(
        default_factory=lambda: (0.2, 0.35, 0.5, 0.7, 1.0)
    )
    calibration_beta_grid: Tuple[float, ...] = field(
        default_factory=lambda: (0.5, 0.75, 1.0, 1.5, 2.0)
    )

    # ------------------------------------------------------------------
    # Runtime / logging
    # ------------------------------------------------------------------
    paper_trading: bool = True              # locked on for v1
    log_dir: str = "hf_engine_logs"
    trade_log_file: str = "hf_trades.jsonl"
    signal_log_file: str = "hf_signals.jsonl"
    calibration_log_file: str = "hf_calibration.jsonl"
    report_every_n_markets: int = 10
    log_prefix: str = "[HFE]"
    loop_sleep_sec: float = 0.25

    # Polymarket endpoints (kept here so they are trivially swappable).
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "HFEConfig":
        """Build a config purely from HFE_* environment variables.

        Any variable not set falls back to the dataclass default. No
        MOMENTUM_* variables are read — this is the sole configuration
        entry point for the HF engine.
        """
        cfg = cls()

        # Hawkes
        cfg.hawkes_alpha_prior = _getenv_float("HFE_HAWKES_ALPHA", cfg.hawkes_alpha_prior)
        cfg.hawkes_beta_prior = _getenv_float("HFE_HAWKES_BETA", cfg.hawkes_beta_prior)
        cfg.hawkes_mu_fallback = _getenv_float("HFE_HAWKES_MU_FALLBACK", cfg.hawkes_mu_fallback)
        cfg.mu_observation_window_sec = _getenv_float(
            "HFE_MU_OBSERVATION_WINDOW_SEC", cfg.mu_observation_window_sec
        )
        cfg.cascade_threshold = _getenv_float("HFE_CASCADE_THRESHOLD", cfg.cascade_threshold)
        cfg.intensity_cap = _getenv_float("HFE_INTENSITY_CAP", cfg.intensity_cap)

        # GM
        cfg.pi_base = _getenv_float("HFE_PI_BASE", cfg.pi_base)
        cfg.pi_hawkes_boost = _getenv_float("HFE_PI_HAWKES_BOOST", cfg.pi_hawkes_boost)
        cfg.pi_cap = _getenv_float("HFE_PI_CAP", cfg.pi_cap)
        cfg.max_log_odds = _getenv_float("HFE_MAX_LOG_ODDS", cfg.max_log_odds)

        # Signal gates
        cfg.min_flow_imbalance = _getenv_float("HFE_MIN_FLOW_IMBALANCE", cfg.min_flow_imbalance)
        cfg.min_edge = _getenv_float("HFE_MIN_EDGE", cfg.min_edge)
        cfg.min_time_remaining_sec = _getenv_float(
            "HFE_MIN_TIME_REMAINING_SEC", cfg.min_time_remaining_sec
        )
        cfg.min_book_depth = _getenv_float("HFE_MIN_BOOK_DEPTH", cfg.min_book_depth)

        # Sizing
        cfg.kelly_base_fraction = _getenv_float("HFE_KELLY_BASE_FRACTION", cfg.kelly_base_fraction)
        cfg.max_dollar_risk_per_market = _getenv_float(
            "HFE_MAX_DOLLAR_RISK_PER_MARKET", cfg.max_dollar_risk_per_market
        )
        cfg.max_total_dollar_risk = _getenv_float(
            "HFE_MAX_TOTAL_DOLLAR_RISK", cfg.max_total_dollar_risk
        )

        # Exits
        cfg.edge_collapse_threshold = _getenv_float(
            "HFE_EDGE_COLLAPSE_THRESHOLD", cfg.edge_collapse_threshold
        )
        cfg.contrary_flow_threshold = _getenv_float(
            "HFE_CONTRARY_FLOW_THRESHOLD", cfg.contrary_flow_threshold
        )
        cfg.exit_time_buffer_sec = _getenv_float(
            "HFE_EXIT_TIME_BUFFER_SEC", cfg.exit_time_buffer_sec
        )

        # Scanner
        cfg.scan_interval_sec = _getenv_float("HFE_SCAN_INTERVAL_SEC", cfg.scan_interval_sec)
        cfg.min_market_duration_min = _getenv_float(
            "HFE_MIN_MARKET_DURATION_MIN", cfg.min_market_duration_min
        )
        cfg.max_market_duration_min = _getenv_float(
            "HFE_MAX_MARKET_DURATION_MIN", cfg.max_market_duration_min
        )
        cfg.min_pre_volume_usd = _getenv_float("HFE_MIN_PRE_VOLUME_USD", cfg.min_pre_volume_usd)
        cfg.min_unique_traders = _getenv_int("HFE_MIN_UNIQUE_TRADERS", cfg.min_unique_traders)

        slug_raw = os.getenv("HFE_TRACKED_COINS")
        if slug_raw:
            cfg.tracked_coin_slugs = tuple(
                s.strip().lower() for s in slug_raw.split(",") if s.strip()
            )

        # Calibration
        cfg.enable_online_calibration = _getenv_bool(
            "HFE_ENABLE_ONLINE_CALIBRATION", cfg.enable_online_calibration
        )
        cfg.calibration_ema_alpha = _getenv_float(
            "HFE_CALIBRATION_EMA_ALPHA", cfg.calibration_ema_alpha
        )

        # Logging
        cfg.paper_trading = _getenv_bool("HFE_PAPER_TRADING", cfg.paper_trading)
        cfg.log_dir = _getenv_str("HFE_LOG_DIR", cfg.log_dir)
        cfg.report_every_n_markets = _getenv_int(
            "HFE_REPORT_EVERY_N_MARKETS", cfg.report_every_n_markets
        )
        cfg.log_prefix = _getenv_str("HFE_LOG_PREFIX", cfg.log_prefix)
        cfg.loop_sleep_sec = _getenv_float("HFE_LOOP_SLEEP_SEC", cfg.loop_sleep_sec)

        cfg.gamma_api_base = _getenv_str("HFE_GAMMA_API_BASE", cfg.gamma_api_base)
        cfg.clob_api_base = _getenv_str("HFE_CLOB_API_BASE", cfg.clob_api_base)
        cfg.clob_ws_url = _getenv_str("HFE_CLOB_WS_URL", cfg.clob_ws_url)

        return cfg

    def summary(self) -> str:
        lines = [
            f"{self.log_prefix} config:",
            f"  hawkes: alpha={self.hawkes_alpha_prior} beta={self.hawkes_beta_prior} "
            f"mu_fallback={self.hawkes_mu_fallback} cascade_thr={self.cascade_threshold}",
            f"  gm:     pi_base={self.pi_base} pi_boost={self.pi_hawkes_boost} "
            f"pi_cap={self.pi_cap}",
            f"  gates:  imbalance>={self.min_flow_imbalance} edge>={self.min_edge} "
            f"time_left>={self.min_time_remaining_sec}s depth>={self.min_book_depth}",
            f"  sizing: kelly_frac={self.kelly_base_fraction} "
            f"max_market=${self.max_dollar_risk_per_market} "
            f"max_total=${self.max_total_dollar_risk}",
            f"  exits:  collapse={self.edge_collapse_threshold} "
            f"contrary={self.contrary_flow_threshold} "
            f"buffer={self.exit_time_buffer_sec}s",
            f"  mode:   paper_trading={self.paper_trading} "
            f"online_calibration={self.enable_online_calibration}",
        ]
        return "\n".join(lines)
