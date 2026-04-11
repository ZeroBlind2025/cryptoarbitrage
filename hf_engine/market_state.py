"""
hf_engine.market_state
======================

Per-market state, trade-ingestion pipeline, signal evaluation, position
sizing, and exit logic (Sections 5, 6, 7 of the spec).

Every 5m/15m market the scanner discovers creates one ``MarketState``
object. On every incoming trade event the engine calls ``on_trade`` to
advance the Hawkes intensities and the GM posterior, then
``evaluate_entry_signal`` to gate an entry, and ``evaluate_exit_signal``
on any open paper position.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from .config import HFEConfig
from .glosten_milgrom import GlostenMilgromPosterior, size_weight
from .hawkes import HawkesTracker


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class Trade:
    """Normalized trade event (Yes-token perspective).

    ``side`` is either ``"buy"`` or ``"sell"`` on the Yes token. A buy
    on the No token is flipped into a sell on Yes by the feed layer
    before we get here, so the GM update is always applied in the Yes
    frame of reference.
    """

    timestamp: float
    side: str
    price: float
    size: float


@dataclass
class PaperPosition:
    side: str              # "yes" or "no"
    entry_price: float
    size_dollars: float
    entry_time: float
    reason: str
    posterior_at_entry: float
    flow_imbalance_at_entry: float
    branching_ratio_at_entry: float
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None


@dataclass
class Signal:
    action: str            # "none", "buy_yes", "buy_no", "exit"
    confidence: float = 0.0
    edge: float = 0.0
    reason: str = ""


# --------------------------------------------------------------------------- #
# Market state
# --------------------------------------------------------------------------- #


@dataclass
class MarketState:
    """All per-market state needed by the Hawkes-GM engine.

    The object is cheap to create and destroy; the engine builds one of
    these the moment a new market appears and throws it away as soon as
    the market resolves so there are no long-lived references left
    over (Section 13.4 point 5).
    """

    cfg: HFEConfig
    market_id: str           # Polymarket condition id
    yes_token_id: str
    no_token_id: str
    description: str
    created_at: float
    resolves_at: float
    interval_label: str      # "5m" / "15m" / "30m" / "1h"

    # Hawkes (one per side)
    hawkes_buy: HawkesTracker = field(init=False)
    hawkes_sell: HawkesTracker = field(init=False)

    # GM posterior
    gm: GlostenMilgromPosterior = field(init=False)

    # Running derived state
    flow_imbalance: float = 0.0
    cascade_active: bool = False
    pi_effective: float = 0.0

    # Book snapshot (updated by the feed out of band)
    best_bid_yes: Optional[float] = None
    best_ask_yes: Optional[float] = None
    book_depth_yes: float = 0.0
    best_bid_no: Optional[float] = None
    best_ask_no: Optional[float] = None

    # Trade history (capped deque — used for post-resolution replay).
    trades: Deque[Trade] = field(default_factory=lambda: deque(maxlen=2000))
    # Separate sliding window of sizes so median estimation cannot
    # corrupt the real trade log.
    _recent_sizes: Deque[float] = field(default_factory=lambda: deque(maxlen=64))
    median_size: Optional[float] = None
    trade_count: int = 0

    # Paper position (at most one open at a time per market)
    open_position: Optional[PaperPosition] = None
    closed_positions: List[PaperPosition] = field(default_factory=list)
    # Timestamp (epoch seconds) when the last position on this market
    # was closed. Used to enforce ``reentry_cooldown_sec`` so the
    # engine cannot flap cascade -> enter -> contrary-flow -> exit ->
    # enter every few seconds on the same market.
    last_close_time: Optional[float] = None

    # Per-market baseline informed-fraction. Defaults to ``cfg.pi_base``
    # in ``__post_init__`` but ``HFEngine._ingest_new_market`` overrides
    # it with the calibrator's current EMA value so the online learning
    # loop actually applies to new markets. Previously only the
    # calibrator's ``current_alpha`` / ``current_beta`` were being
    # applied and the learned ``current_pi`` was silently discarded.
    pi_base: Optional[float] = None

    # Baseline-rate observation window
    first_trade_time: Optional[float] = None
    mu_finalized: bool = False

    # Resolution (filled in by the scanner when the market settles)
    resolved_outcome: Optional[int] = None   # 1 = Yes wins, 0 = No wins
    resolution_time: Optional[float] = None

    def __post_init__(self) -> None:
        mu0 = self.cfg.hawkes_mu_fallback / 2.0
        self.hawkes_buy = HawkesTracker(
            mu=mu0,
            alpha=self.cfg.hawkes_alpha_prior,
            beta=self.cfg.hawkes_beta_prior,
            intensity_cap=self.cfg.intensity_cap,
        )
        self.hawkes_sell = HawkesTracker(
            mu=mu0,
            alpha=self.cfg.hawkes_alpha_prior,
            beta=self.cfg.hawkes_beta_prior,
            intensity_cap=self.cfg.intensity_cap,
        )
        # Initialize the GM posterior at 0.5 — the scanner overwrites it
        # with the real mid-price from the order book as soon as it is
        # known. 0.5 is the maximum-entropy prior when we know nothing.
        self.gm = GlostenMilgromPosterior(
            initial_probability=0.5,
            max_log_odds=self.cfg.max_log_odds,
        )
        if self.pi_base is None:
            self.pi_base = self.cfg.pi_base
        self.pi_effective = self.pi_base

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def posterior(self) -> float:
        return self.gm.probability

    @property
    def branching_ratio_max(self) -> float:
        """Static model parameter max(alpha_buy/beta_buy, alpha_sell/beta_sell).

        Kept for compatibility with the snapshot / calibration code.
        Cascade detection no longer uses this value — see
        ``excitation_ratio_max`` for the live cascade signal.
        """
        return max(self.hawkes_buy.branching_ratio, self.hawkes_sell.branching_ratio)

    @property
    def excitation_ratio_max(self) -> float:
        """Live cascade signal across both sides.

        This is ``max(intensity_buy / mu_buy, intensity_sell / mu_sell)``.
        Unlike the branching ratio (which is a static parameter of the
        model), this responds to trade clustering in real time — each
        trade bumps the corresponding side's intensity by ``alpha`` and
        the bump decays exponentially between events. When a burst of
        informed trades hits the book the ratio climbs well above its
        steady-state value, which is the signature of a cascade.
        """
        return max(
            self.hawkes_buy.excitation_ratio, self.hawkes_sell.excitation_ratio
        )

    @property
    def time_remaining_sec(self) -> float:
        return max(0.0, self.resolves_at - time.time())

    @property
    def total_duration_sec(self) -> float:
        """The span between when we first saw the market and its
        resolution time. Used for sqrt-time position sizing."""
        return max(1.0, self.resolves_at - self.created_at)

    @property
    def declared_duration_sec(self) -> float:
        """The nominal trading-window length of the market, parsed
        from ``interval_label`` (e.g. ``5m`` -> 300s). Unlike
        ``total_duration_sec`` this is independent of when we first
        discovered the market and is the correct value to compare
        against ``time_remaining_sec`` when deciding whether the
        trading window has opened yet."""
        label = (self.interval_label or "").strip().lower()
        if not label:
            return 300.0
        try:
            if label.endswith("m"):
                return float(int(label[:-1])) * 60.0
            if label.endswith("h"):
                return float(int(label[:-1])) * 3600.0
        except ValueError:
            return 300.0
        return 300.0

    def clob_mid_yes(self) -> Optional[float]:
        if self.best_bid_yes is not None and self.best_ask_yes is not None:
            return (self.best_bid_yes + self.best_ask_yes) / 2.0
        return self.best_ask_yes or self.best_bid_yes

    # ------------------------------------------------------------------ #
    # Book updates (called by feed on non-trade events)
    # ------------------------------------------------------------------ #

    def update_book(
        self,
        side_token: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        depth: float = 0.0,
    ) -> None:
        if side_token == self.yes_token_id:
            if best_bid is not None:
                self.best_bid_yes = best_bid
            if best_ask is not None:
                self.best_ask_yes = best_ask
            if depth > 0:
                self.book_depth_yes = depth
        elif side_token == self.no_token_id:
            if best_bid is not None:
                self.best_bid_no = best_bid
            if best_ask is not None:
                self.best_ask_no = best_ask

        # On the very first book snapshot, re-seed the GM posterior to
        # the CLOB mid of the Yes token so we do not start our Bayesian
        # update from 50/50 when the market has already published a
        # prior belief.
        if self.gm.updates == 0 and self.gm.log_odds == 0.0:
            mid = self.clob_mid_yes()
            if mid is not None and 0.001 < mid < 0.999:
                self.gm.reset(mid)

    # ------------------------------------------------------------------ #
    # Trade ingestion (Section 5.2)
    # ------------------------------------------------------------------ #

    def _finalize_mu_if_ready(self, now: float) -> None:
        """After the observation window closes, replace the fallback mu
        with the observed trade rate from the first N seconds."""
        if self.mu_finalized or self.first_trade_time is None:
            return
        elapsed = now - self.first_trade_time
        if elapsed < self.cfg.mu_observation_window_sec:
            return

        observed_rate = self.trade_count / max(elapsed, 1.0)   # trades/sec
        # Split between buy and sell roughly by what we have already seen.
        n_buy = self.hawkes_buy.event_count or 1
        n_sell = self.hawkes_sell.event_count or 1
        total = n_buy + n_sell
        mu_buy = observed_rate * (n_buy / total) / 2.0 + self.cfg.hawkes_mu_fallback / 4.0
        mu_sell = observed_rate * (n_sell / total) / 2.0 + self.cfg.hawkes_mu_fallback / 4.0

        # Write the new mu but keep existing excitation intact — we only
        # re-home the baseline floor, not wipe the cascade state.
        self.hawkes_buy.mu = max(mu_buy, 1e-4)
        self.hawkes_sell.mu = max(mu_sell, 1e-4)
        self.mu_finalized = True

    def _update_median_size(self, size: float) -> None:
        """Streaming approximate median over the last 64 sizes.

        An exact rolling median would cost O(log n) per trade with a
        balanced tree, but a sort over the latest 64 entries is trivial
        for our volumes and keeps the deterministic estimate from
        Section 5.2 point 10.
        """
        self._recent_sizes.append(size)
        if self.trade_count < 10:
            return
        recent = sorted(self._recent_sizes)
        self.median_size = recent[len(recent) // 2]

    def on_trade(self, trade: Trade) -> None:
        """Process a trade (Section 5.2). No gating or sizing here — this
        method only advances the probabilistic state."""
        now = trade.timestamp

        if self.first_trade_time is None:
            self.first_trade_time = now
            # Anchor the Hawkes clocks so decays are measured from the
            # very first observed event rather than from t=0.
            self.hawkes_buy.last_time = now
            self.hawkes_sell.last_time = now

        self.trade_count += 1
        # Persist the real trade for post-resolution calibration replay.
        self.trades.append(trade)

        # 1. Hawkes intensity update.
        if trade.side == "buy":
            self.hawkes_buy.update(now)
            # Still decay the opposite side to keep it up to date.
            self.hawkes_sell.decay_to(now)
        elif trade.side == "sell":
            self.hawkes_sell.update(now)
            self.hawkes_buy.decay_to(now)
        else:
            return

        # 2. Flow imbalance.
        lam_buy = self.hawkes_buy.intensity
        lam_sell = self.hawkes_sell.intensity
        total = lam_buy + lam_sell
        self.flow_imbalance = (lam_buy / total - 0.5) if total > 0 else 0.0

        # 3. Cascade detection.
        #
        # Use the live excitation ratio (intensity / mu) rather than
        # the static branching ratio (alpha / beta). The branching
        # ratio is a parameter of the model, fixed by the priors, and
        # therefore never changes during a market's life — it cannot
        # serve as a "cascade is happening right now" signal. The
        # excitation ratio responds directly to trade clustering: each
        # trade bumps the corresponding side's intensity by alpha, the
        # bump decays exponentially between events, and the ratio
        # climbs well above its steady-state value during a burst.
        exc_max = self.excitation_ratio_max
        self.cascade_active = exc_max > self.cfg.cascade_threshold

        # 4. Hawkes-adaptive pi (Section 4.5 Method 3).
        #
        # When a cascade is active, scale the informed-fraction upwards
        # by how far the excitation ratio exceeds the cascade threshold.
        # At the threshold the boost is 0; at 2x the threshold the boost
        # reaches its configured maximum.
        if self.cascade_active and self.cfg.cascade_threshold > 0.0:
            excess = exc_max - self.cfg.cascade_threshold
            boost_frac = min(1.0, max(0.0, excess / self.cfg.cascade_threshold))
            boost = self.cfg.pi_hawkes_boost * boost_frac
            self.pi_effective = min(self.pi_base + boost, self.cfg.pi_cap)
        else:
            self.pi_effective = self.pi_base

        # 5. Size-weighted GM update.
        sw = size_weight(trade.size, self.median_size)
        self.gm.update(trade.side, self.pi_effective, size_w=sw)

        # 6. Maintain running median size.
        self._update_median_size(trade.size)

        # 7. Finalize mu once the observation window has closed.
        self._finalize_mu_if_ready(now)

    # ------------------------------------------------------------------ #
    # Signal evaluation (Section 5.3)
    # ------------------------------------------------------------------ #

    def evaluate_entry_signal(self) -> Signal:
        """Run the six gates from Section 5.3. Returns a Signal whose
        ``action`` is ``"buy_yes"``, ``"buy_no"``, or ``"none"``. Reasons
        for a reject are logged verbatim so paper-trading diagnostics can
        be tracked without running every gate twice."""

        # Observation window must have closed before we even consider a
        # signal — we do not trust the Hawkes/GM numbers during the first
        # 15 seconds of a market.
        if not self.mu_finalized:
            return Signal(action="none", reason="mu-observation-window")

        if self.open_position is not None:
            return Signal(action="none", reason="position-already-open")

        # Re-entry cooldown: after closing a position on this market
        # we impose a quiet period before a new entry is allowed,
        # otherwise the cascade/contrary-flow alternation can open and
        # close several positions per second on the same market.
        if self.last_close_time is not None:
            elapsed = time.time() - self.last_close_time
            if elapsed < self.cfg.reentry_cooldown_sec:
                return Signal(
                    action="none",
                    reason=f"reentry-cooldown({elapsed:.0f}s)",
                )

        # Gate 0a: the trading window must have opened.
        #
        # Polymarket often creates short-duration markets several
        # minutes before the start of their trading window. During
        # that lead-in period the book is typically thin / phantom
        # and any ask on the Yes side can be far off its steady-state
        # value, so an entry against that book is taking on a weird
        # unmodeled risk. Refuse to enter until the market has
        # actually started: time_remaining_sec must be <= the
        # **declared** interval duration (5m → 300s) rather than the
        # ``total_duration_sec`` which is measured from when we first
        # ingested the market.
        declared = self.declared_duration_sec
        if self.time_remaining_sec > declared + 5.0:
            return Signal(action="none", reason="pre-active")

        # Gate 0b: the best Yes-ask must be within a sane interval.
        #
        # The earlier "BUY_NO @ 0.01¢ on a pre-active 15m market"
        # incident came from a phantom/empty book where best_ask_yes
        # was effectively 0.99 (so entry_no = 0.01). Even a more
        # modest 0.05 / 0.95 can produce 20x-to-1 mark-to-market
        # swings against thin book depth, so we clamp the acceptable
        # ask range to [0.10, 0.90] — any ask outside that window
        # reflects a market that is either not really interesting
        # from an information standpoint or has unreliable depth on
        # one side.
        ask_yes = self.best_ask_yes
        if ask_yes is not None and (ask_yes < 0.10 or ask_yes > 0.90):
            return Signal(
                action="none",
                reason=f"extreme-ask({ask_yes:.3f})",
            )

        # Gate 1: cascade active
        if not self.cascade_active:
            return Signal(action="none", reason="no-cascade")

        # Gate 2: directional conviction
        if abs(self.flow_imbalance) < self.cfg.min_flow_imbalance:
            return Signal(
                action="none",
                reason=f"imbalance-too-small({self.flow_imbalance:.3f})",
            )

        # Gate 3: posterior diverges from CLOB
        clob_price = self.clob_mid_yes()
        if clob_price is None:
            return Signal(action="none", reason="no-clob-mid")
        edge = self.posterior - clob_price  # + => Yes underpriced
        if abs(edge) < self.cfg.min_edge:
            return Signal(
                action="none",
                reason=f"edge-too-small({edge:+.3f})",
            )

        # Gate 4: enough time remaining
        if self.time_remaining_sec < self.cfg.min_time_remaining_sec:
            return Signal(action="none", reason="too-late")

        # Gate 5: book depth
        if self.book_depth_yes < self.cfg.min_book_depth:
            return Signal(
                action="none",
                reason=f"thin-book({self.book_depth_yes:.0f})",
            )

        # Gate 6: posterior and flow must agree on direction.
        if edge > 0 and self.flow_imbalance < 0:
            return Signal(action="none", reason="direction-conflict-yes")
        if edge < 0 and self.flow_imbalance > 0:
            return Signal(action="none", reason="direction-conflict-no")

        action = "buy_yes" if edge > 0 else "buy_no"
        # Confidence is the scaled absolute imbalance, bounded to [0,1].
        confidence = min(1.0, abs(self.flow_imbalance) * 2.0)
        reason = (
            f"cascade exc={self.excitation_ratio_max:.2f} "
            f"imbalance={self.flow_imbalance:+.3f} "
            f"posterior={self.posterior:.3f} "
            f"clob={clob_price:.3f} "
            f"edge={edge:+.3f}"
        )
        return Signal(action=action, confidence=confidence, edge=abs(edge), reason=reason)

    def evaluate_exit_signal(self) -> Signal:
        """Section 7 exit logic. Only meaningful when a paper position is
        open."""
        pos = self.open_position
        if pos is None:
            return Signal(action="none", reason="no-position")

        # 7.2: posterior has reverted to the coin-flip zone.
        if abs(self.posterior - 0.5) < self.cfg.edge_collapse_threshold:
            return Signal(action="exit", reason="edge-collapse")

        # 7.3: contrary cascade.
        if pos.side == "yes" and self.flow_imbalance < -self.cfg.contrary_flow_threshold:
            return Signal(action="exit", reason="contrary-flow-vs-yes")
        if pos.side == "no" and self.flow_imbalance > self.cfg.contrary_flow_threshold:
            return Signal(action="exit", reason="contrary-flow-vs-no")

        # 7.4: time buffer — only force exit when the position is
        # currently losing so we are not bailing out of winners.
        if self.time_remaining_sec < self.cfg.exit_time_buffer_sec:
            clob_price = self.clob_mid_yes() or pos.entry_price
            pnl_sign = self._pnl_sign(pos, clob_price)
            if pnl_sign < 0:
                return Signal(action="exit", reason="time-buffer-losing")

        return Signal(action="none", reason="hold")

    @staticmethod
    def _pnl_sign(pos: PaperPosition, mark_price: float) -> float:
        if pos.side == "yes":
            return mark_price - pos.entry_price
        return pos.entry_price - mark_price


# --------------------------------------------------------------------------- #
# Position sizing (Section 6)
# --------------------------------------------------------------------------- #


def kelly_fraction(probability: float, entry_price: float) -> float:
    """Vanilla Kelly for a binary payoff (Section 6.1)."""
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    b_odds = (1.0 / entry_price) - 1.0
    if b_odds <= 0:
        return 0.0
    kelly = (probability * (b_odds + 1.0) - 1.0) / b_odds
    return max(0.0, kelly)


def position_dollars(
    cfg: HFEConfig,
    probability: float,
    entry_price: float,
    time_remaining_sec: float,
    total_market_duration_sec: float,
    current_total_exposure: float,
) -> float:
    """Apply fractional-Kelly, sqrt-time scaling, and the hard caps
    from Sections 6.2 and 6.3."""
    kelly = kelly_fraction(probability, entry_price)
    if kelly <= 0:
        return 0.0

    time_fraction = max(0.01, min(1.0, time_remaining_sec / max(1.0, total_market_duration_sec)))
    adjusted = cfg.kelly_base_fraction * kelly * math.sqrt(time_fraction)

    dollars = adjusted * cfg.max_dollar_risk_per_market
    dollars = min(dollars, cfg.max_dollar_risk_per_market)

    remaining_budget = cfg.max_total_dollar_risk - current_total_exposure
    if remaining_budget <= 0:
        return 0.0
    dollars = min(dollars, remaining_budget)

    return dollars
