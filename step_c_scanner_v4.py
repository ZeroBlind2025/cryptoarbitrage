"""
POLYMARKET GAMMA SCANNER v4 - Research Edition
===============================================

Step C: Market discovery using the Gamma API.

The Gamma API provides:
- All active markets on Polymarket
- Indicative prices (marks, not executable)
- Market metadata (resolution time, volume, etc.)

IMPORTANT: Gamma prices are INDICATIVE, not EXECUTABLE.
These are marks/last trades, not what you actually pay.
Always validate against CLOB for real execution prices.

This scanner:
1. Fetches all active markets from Gamma
2. Filters for binary crypto markets in our target window
3. Calculates indicative price sums
4. Tracks exclusion funnel for white paper
5. Outputs candidate opportunities for CLOB validation
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from pathlib import Path
import requests


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class ScanConfig:
    """Scanner configuration"""

    # API endpoints
    gamma_api_url: str = "https://gamma-api.polymarket.com"

    # Resolution window (capital velocity optimization)
    max_resolution_hours: float = 24.0
    min_resolution_hours: float = 0.5

    # Volume filters
    min_volume_24h: float = 1000.0  # Minimum 24h volume in USD

    # Friction model
    fee_buffer: float = 0.02  # 2% Polymarket fee on winnings
    slippage_buffer: float = 0.005  # Expected slippage
    risk_buffer: float = 0.005  # Risk/uncertainty buffer

    # Arb thresholds
    @property
    def total_friction(self) -> float:
        return self.fee_buffer + self.slippage_buffer + self.risk_buffer

    @property
    def max_price_sum_threshold(self) -> float:
        """Price sum must be below this for viable arb"""
        return 1.0 - self.total_friction

    # API settings
    request_timeout: int = 30
    markets_per_page: int = 100

    # Logging
    log_dir: str = "logs"


# =============================================================================
# DATA STRUCTURES (compatible with step_a_fundamentals.py)
# =============================================================================

@dataclass
class Token:
    """A tradeable outcome token"""
    token_id: str
    outcome: str
    price: float


@dataclass
class BinaryMarket:
    """A binary prediction market"""

    condition_id: str
    market_id: str
    slug: str
    question: str
    raw_outcome_count: int
    priced_token_count: int
    token_a: Optional[Token]
    token_b: Optional[Token]
    outcome_labels: list[str]
    minutes_until_resolution: Optional[float]
    volume_24h: Optional[float]
    volume_source: str
    active: bool

    @property
    def is_valid_binary(self) -> bool:
        return (
            self.raw_outcome_count == 2 and
            self.priced_token_count == 2 and
            self.token_a is not None and
            self.token_b is not None
        )

    @property
    def price_sum(self) -> Optional[float]:
        if not self.is_valid_binary:
            return None
        return self.token_a.price + self.token_b.price

    @property
    def gross_edge(self) -> Optional[float]:
        if self.price_sum is None:
            return None
        return 1.0 - self.price_sum


# =============================================================================
# EXCLUSION FUNNEL
# =============================================================================

@dataclass
class ExclusionFunnel:
    """
    Tracks why markets are excluded at each stage.
    Essential for white paper analysis.
    """

    # Stage counts
    total_fetched: int = 0
    passed_active: int = 0
    passed_binary: int = 0
    passed_priced: int = 0
    passed_window: int = 0
    passed_volume: int = 0
    passed_threshold: int = 0  # Final candidates

    # Exclusion reasons
    excluded_inactive: int = 0
    excluded_not_binary: int = 0
    excluded_missing_prices: int = 0
    excluded_outside_window: int = 0
    excluded_low_volume: int = 0
    excluded_above_threshold: int = 0

    # Near misses (for analysis)
    near_misses: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_fetched": self.total_fetched,
            "funnel": {
                "active": self.passed_active,
                "binary": self.passed_binary,
                "priced": self.passed_priced,
                "in_window": self.passed_window,
                "volume_ok": self.passed_volume,
                "below_threshold": self.passed_threshold,
            },
            "exclusions": {
                "inactive": self.excluded_inactive,
                "not_binary": self.excluded_not_binary,
                "missing_prices": self.excluded_missing_prices,
                "outside_window": self.excluded_outside_window,
                "low_volume": self.excluded_low_volume,
                "above_threshold": self.excluded_above_threshold,
            },
            "near_miss_count": len(self.near_misses),
        }


# =============================================================================
# SCAN RESULT
# =============================================================================

@dataclass
class ScanResult:
    """Result of a Gamma scan"""

    scan_id: str
    timestamp: str
    config: dict

    # Funnel
    funnel: ExclusionFunnel
    total_markets_fetched: int

    # Candidates
    targets: list[dict]  # Markets passing all filters

    # Near misses (price_sum between threshold and 1.0)
    near_misses: list[dict]

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "timestamp": self.timestamp,
            "config": self.config,
            "funnel": self.funnel.to_dict(),
            "total_markets_fetched": self.total_markets_fetched,
            "target_count": len(self.targets),
            "targets": self.targets,
            "near_miss_count": len(self.near_misses),
            "near_misses": self.near_misses[:10],  # Top 10 only
        }


# =============================================================================
# MARKET SCANNER
# =============================================================================

class MarketScanner:
    """
    Scans Polymarket via Gamma API for arbitrage candidates.

    Flow:
    1. Fetch all active markets
    2. Filter through exclusion funnel
    3. Calculate indicative price sums
    4. Return candidates for CLOB validation
    """

    def __init__(self, config: Optional[ScanConfig] = None):
        self.config = config or ScanConfig()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })

        # Ensure log directory exists
        self.log_dir = Path(self.config.log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def fetch_markets(self) -> list[dict]:
        """
        Fetch all active markets from Gamma API.

        Returns raw API response data.
        """
        all_markets = []
        offset = 0

        while True:
            try:
                resp = self.session.get(
                    f"{self.config.gamma_api_url}/markets",
                    params={
                        "limit": self.config.markets_per_page,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                    },
                    timeout=self.config.request_timeout,
                )
                resp.raise_for_status()
                data = resp.json()

                # Debug first request
                if offset == 0:
                    print(f"[DEBUG] API response type: {type(data)}, len: {len(data) if isinstance(data, list) else 'N/A'}", flush=True)
                    if isinstance(data, list) and data:
                        first_market = data[0]
                        print(f"[DEBUG] First market ALL keys: {list(first_market.keys())}", flush=True)
                        # Check for token-related fields
                        print(f"[DEBUG] tokens: {first_market.get('tokens', 'NOT FOUND')}", flush=True)
                        print(f"[DEBUG] outcomes: {first_market.get('outcomes', 'NOT FOUND')}", flush=True)
                        print(f"[DEBUG] clobTokenIds: {first_market.get('clobTokenIds', 'NOT FOUND')}", flush=True)
                        print(f"[DEBUG] outcomePrices: {first_market.get('outcomePrices', 'NOT FOUND')}", flush=True)
                    elif isinstance(data, dict):
                        print(f"[DEBUG] Response is dict with keys: {list(data.keys())}", flush=True)

                # Handle both list and dict responses
                if isinstance(data, dict):
                    # API might return {"data": [...]} or similar
                    data = data.get("data", data.get("markets", []))

                if not data:
                    break

                all_markets.extend(data)
                offset += self.config.markets_per_page

                # Rate limiting
                time.sleep(0.1)

                # Safety limit
                if offset > 5000:
                    print("Warning: Hit safety limit on market fetch")
                    break

            except requests.RequestException as e:
                print(f"Error fetching markets: {e}")
                break

        return all_markets

    def parse_market(self, raw: dict) -> Optional[BinaryMarket]:
        """
        Parse raw API response into BinaryMarket.

        Returns None if market is invalid/unparseable.
        """
        try:
            # Extract basic fields
            condition_id = raw.get("conditionId", "")
            market_id = raw.get("id", "")
            slug = raw.get("slug", "")
            question = raw.get("question", "")
            active = raw.get("active", False)

            # Parse outcomes
            outcomes = raw.get("outcomes", [])
            outcome_labels = outcomes if isinstance(outcomes, list) else []

            # Parse tokens
            tokens = raw.get("tokens", [])
            raw_outcome_count = len(outcome_labels)

            token_a = None
            token_b = None
            priced_count = 0

            for tok in tokens:
                if not isinstance(tok, dict):
                    continue

                token_id = tok.get("token_id", "")
                outcome = tok.get("outcome", "")
                price = tok.get("price")

                if price is None:
                    continue

                try:
                    price_float = float(price)
                except (ValueError, TypeError):
                    continue

                if not 0 <= price_float <= 1:
                    continue

                priced_count += 1

                t = Token(token_id=token_id, outcome=outcome, price=price_float)

                if token_a is None:
                    token_a = t
                elif token_b is None:
                    token_b = t

            # Parse resolution time
            end_date_str = raw.get("endDate") or raw.get("end_date_iso")
            minutes_until = None

            if end_date_str:
                try:
                    # Handle various date formats
                    if "T" in str(end_date_str):
                        end_date = datetime.fromisoformat(
                            end_date_str.replace("Z", "+00:00")
                        )
                    else:
                        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
                        end_date = end_date.replace(tzinfo=timezone.utc)

                    now = datetime.now(timezone.utc)
                    delta = end_date - now
                    minutes_until = delta.total_seconds() / 60
                except (ValueError, TypeError):
                    pass

            # Parse volume
            volume_24h = None
            volume_source = ""

            # Try different volume fields
            for vol_field in ["volume24hr", "volume_24h", "volume24h", "volume"]:
                vol = raw.get(vol_field)
                if vol is not None:
                    try:
                        volume_24h = float(vol)
                        volume_source = vol_field
                        break
                    except (ValueError, TypeError):
                        pass

            return BinaryMarket(
                condition_id=condition_id,
                market_id=market_id,
                slug=slug,
                question=question,
                raw_outcome_count=raw_outcome_count,
                priced_token_count=priced_count,
                token_a=token_a,
                token_b=token_b,
                outcome_labels=outcome_labels,
                minutes_until_resolution=minutes_until,
                volume_24h=volume_24h,
                volume_source=volume_source,
                active=active,
            )

        except Exception as e:
            print(f"Error parsing market: {e}")
            return None

    def scan(self) -> ScanResult:
        """
        Run full scan:
        1. Fetch markets
        2. Apply exclusion funnel
        3. Return results
        """
        timestamp = datetime.now(timezone.utc)
        scan_id = f"gamma_{timestamp.strftime('%Y%m%d_%H%M%S')}"

        funnel = ExclusionFunnel()
        targets = []
        near_misses = []

        # Fetch markets
        raw_markets = self.fetch_markets()
        funnel.total_fetched = len(raw_markets)

        for raw in raw_markets:
            market = self.parse_market(raw)

            if market is None:
                continue

            # Stage 1: Active check
            if not market.active:
                funnel.excluded_inactive += 1
                continue
            funnel.passed_active += 1

            # Stage 2: Binary check
            if market.raw_outcome_count != 2:
                funnel.excluded_not_binary += 1
                continue
            funnel.passed_binary += 1

            # Stage 3: Priced tokens check
            if market.priced_token_count != 2 or not market.is_valid_binary:
                funnel.excluded_missing_prices += 1
                continue
            funnel.passed_priced += 1

            # Stage 4: Resolution window check
            if market.minutes_until_resolution is None:
                funnel.excluded_outside_window += 1
                continue

            hours_until = market.minutes_until_resolution / 60
            if not (self.config.min_resolution_hours <= hours_until <= self.config.max_resolution_hours):
                funnel.excluded_outside_window += 1
                continue
            funnel.passed_window += 1

            # Stage 5: Volume check
            if market.volume_24h is None or market.volume_24h < self.config.min_volume_24h:
                funnel.excluded_low_volume += 1
                continue
            funnel.passed_volume += 1

            # Stage 6: Price sum threshold
            price_sum = market.price_sum

            if price_sum is None or price_sum >= self.config.max_price_sum_threshold:
                funnel.excluded_above_threshold += 1

                # Track near misses (within 2% of threshold)
                if price_sum is not None and price_sum < 1.0:
                    near_miss = {
                        "slug": market.slug,
                        "price_sum": price_sum,
                        "edge": 1.0 - price_sum,
                        "threshold": self.config.max_price_sum_threshold,
                        "gap_to_threshold": price_sum - self.config.max_price_sum_threshold,
                        "minutes_until": market.minutes_until_resolution,
                    }
                    near_misses.append(near_miss)
                    funnel.near_misses.append(near_miss)
                continue

            funnel.passed_threshold += 1

            # Build target record
            target = {
                "slug": market.slug,
                "question": market.question,
                "condition_id": market.condition_id,
                "market_id": market.market_id,
                "token_a_id": market.token_a.token_id if market.token_a else None,
                "token_b_id": market.token_b.token_id if market.token_b else None,
                "token_a_price": market.token_a.price if market.token_a else None,
                "token_b_price": market.token_b.price if market.token_b else None,
                "price_sum_indicative": price_sum,
                "gross_edge": market.gross_edge,
                "net_edge_estimate": market.gross_edge - self.config.total_friction if market.gross_edge else 0,
                "minutes_until": market.minutes_until_resolution,
                "hours_until": hours_until,
                "volume_24h": market.volume_24h,
                "volume_source": market.volume_source,
                "outcome_labels": market.outcome_labels,
            }
            targets.append(target)

        # Sort near misses by closeness to threshold
        near_misses.sort(key=lambda x: x.get("gap_to_threshold", 999))

        # Sort targets by edge (best first)
        targets.sort(key=lambda x: x.get("gross_edge", 0), reverse=True)

        return ScanResult(
            scan_id=scan_id,
            timestamp=timestamp.isoformat(),
            config={
                "max_resolution_hours": self.config.max_resolution_hours,
                "min_volume_24h": self.config.min_volume_24h,
                "max_price_sum_threshold": self.config.max_price_sum_threshold,
                "total_friction": self.config.total_friction,
            },
            funnel=funnel,
            total_markets_fetched=len(raw_markets),
            targets=targets,
            near_misses=near_misses[:20],  # Top 20 near misses
        )

    def log_result(self, result: ScanResult) -> None:
        """Log scan result to JSONL file"""
        log_file = self.log_dir / "gamma_scans.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")

    def print_summary(self, result: ScanResult) -> None:
        """Print human-readable summary"""
        print(f"\n{'='*60}")
        print(f"GAMMA SCAN RESULTS - {result.scan_id}")
        print(f"{'='*60}")

        funnel = result.funnel
        print(f"\nExclusion Funnel:")
        print(f"  Total fetched:     {funnel.total_fetched}")
        print(f"  ├─ Active:         {funnel.passed_active} ({funnel.excluded_inactive} excluded)")
        print(f"  ├─ Binary:         {funnel.passed_binary} ({funnel.excluded_not_binary} excluded)")
        print(f"  ├─ Priced:         {funnel.passed_priced} ({funnel.excluded_missing_prices} excluded)")
        print(f"  ├─ In window:      {funnel.passed_window} ({funnel.excluded_outside_window} excluded)")
        print(f"  ├─ Volume OK:      {funnel.passed_volume} ({funnel.excluded_low_volume} excluded)")
        print(f"  └─ Below thresh:   {funnel.passed_threshold} ({funnel.excluded_above_threshold} excluded)")

        print(f"\nConfiguration:")
        print(f"  Max resolution:    {self.config.max_resolution_hours} hours")
        print(f"  Min volume:        ${self.config.min_volume_24h:,.0f}")
        print(f"  Price threshold:   {self.config.max_price_sum_threshold:.4f}")
        print(f"  Total friction:    {self.config.total_friction*100:.1f}%")

        if result.targets:
            print(f"\n{'='*60}")
            print(f"CANDIDATES ({len(result.targets)} found)")
            print(f"{'='*60}")

            for t in result.targets[:10]:
                print(f"\n  {t['slug'][:50]}")
                print(f"    Prices: {t['token_a_price']:.4f} + {t['token_b_price']:.4f} = {t['price_sum_indicative']:.4f}")
                print(f"    Gross edge: {t['gross_edge']*100:.2f}%")
                print(f"    Net edge (est): {t['net_edge_estimate']*100:.2f}%")
                print(f"    Resolves in: {t['hours_until']:.1f} hours")
                print(f"    Volume 24h: ${t['volume_24h']:,.0f}")
        else:
            print(f"\n  No candidates found meeting all criteria.")

        if result.near_misses:
            print(f"\n{'='*60}")
            print(f"NEAR MISSES (closest {len(result.near_misses)} to threshold)")
            print(f"{'='*60}")

            for nm in result.near_misses[:5]:
                gap_bps = nm['gap_to_threshold'] * 10000
                print(f"\n  {nm['slug'][:50]}")
                print(f"    Price sum: {nm['price_sum']:.4f} (threshold: {nm['threshold']:.4f})")
                print(f"    Gap: {gap_bps:.0f} bps above threshold")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run scanner in standalone mode"""
    print("""
+======================================================================+
|  POLYMARKET GAMMA SCANNER v4                                          |
|                                                                      |
|  Scanning for arbitrage opportunities using Gamma API                 |
|  NOTE: Prices are INDICATIVE - validate with CLOB before execution   |
+======================================================================+
    """)

    config = ScanConfig(
        max_resolution_hours=24,
        min_volume_24h=1000.0,
        fee_buffer=0.02,
        slippage_buffer=0.005,
        risk_buffer=0.005,
    )

    print(f"Scanner Configuration:")
    print(f"  API: {config.gamma_api_url}")
    print(f"  Window: {config.min_resolution_hours}-{config.max_resolution_hours} hours")
    print(f"  Min volume: ${config.min_volume_24h:,.0f}")
    print(f"  Threshold: price_sum < {config.max_price_sum_threshold:.4f}")

    scanner = MarketScanner(config)

    print(f"\nFetching markets from Gamma API...")
    result = scanner.scan()

    scanner.print_summary(result)
    scanner.log_result(result)

    print(f"\nResults logged to: {scanner.log_dir}/gamma_scans.jsonl")

    # White paper metrics
    print(f"\n{'='*60}")
    print("WHITE PAPER METRICS")
    print(f"{'='*60}")
    print(f"""
For your white paper, track these metrics over time:

1. OPPORTUNITY FREQUENCY
   - Candidates found this scan: {len(result.targets)}
   - Scan timestamp: {result.timestamp}

2. EXCLUSION FUNNEL
   - Shows where opportunities are lost
   - {result.funnel.passed_volume} passed volume, {result.funnel.passed_threshold} passed threshold

3. NEAR MISSES
   - {len(result.near_misses)} markets close to threshold
   - Suggests arbs may appear with small price movements

4. NEXT STEP
   - These are INDICATIVE prices from Gamma
   - Run step_e_integrated_scanner.py to validate against CLOB
   - Track Gamma vs CLOB price slip for white paper
    """)


if __name__ == "__main__":
    main()
