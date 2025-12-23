"""
POLYMARKET INTEGRATED SCANNER - Research Edition
=================================================

Step E: Combines Gamma discovery + CLOB validation.

The flow:
1. Gamma scan finds INDICATIVE opportunities (fast, broad)
2. CLOB validation checks EXECUTABLE prices (slower, targeted)
3. Only execute if CLOB prices still show profit

This two-stage approach is efficient:
- Don't hit CLOB for every market (rate limits, latency)
- Only validate the promising ones
- Log the Gamma→CLOB price slip for white paper

Usage:
    python step_e_integrated_scanner.py --mode scan
    python step_e_integrated_scanner.py --mode paper --duration 3600
    python step_e_integrated_scanner.py --mode live --max-position 25
"""

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

# Import our modules
from step_c_scanner_v4 import (
    ScanConfig as GammaConfig,
    MarketScanner as GammaScanner,
    BinaryMarket,
)
from step_d_clob_client import (
    CLOBConfig,
    CLOBClient,
    PairedOrderBook,
    ArbExecution,
)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class IntegratedConfig:
    """Combined config for integrated scanner"""
    
    # Gamma config
    gamma: GammaConfig
    
    # CLOB config  
    clob: CLOBConfig
    
    # Integration settings
    scan_interval_seconds: float = 5.0
    clob_validation_enabled: bool = True
    
    # Thresholds
    gamma_indicative_threshold: float = 0.97  # Pre-filter on Gamma
    clob_executable_threshold: float = 0.97   # Final check on CLOB
    min_edge_after_friction: float = 0.005    # 0.5% minimum
    
    # Position limits
    max_position_usd: float = 50.0
    max_total_exposure: float = 200.0
    
    # Logging
    log_dir: str = "logs"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ValidatedOpportunity:
    """An opportunity validated against CLOB"""
    
    # Source market
    market: BinaryMarket
    
    # Gamma (indicative)
    gamma_price_sum: float
    gamma_edge: float
    
    # CLOB (executable)
    clob_validated: bool
    clob_price_sum: Optional[float]
    clob_edge: Optional[float]
    clob_depth: Optional[float]  # Max executable size
    
    # Comparison
    price_slip_bps: Optional[int]  # How much worse is CLOB vs Gamma
    
    # Decision
    is_executable: bool
    reject_reason: Optional[str]
    
    def to_dict(self) -> dict:
        return {
            "market_slug": self.market.slug,
            "gamma_price_sum": self.gamma_price_sum,
            "gamma_edge": self.gamma_edge,
            "clob_validated": self.clob_validated,
            "clob_price_sum": self.clob_price_sum,
            "clob_edge": self.clob_edge,
            "clob_depth": self.clob_depth,
            "price_slip_bps": self.price_slip_bps,
            "is_executable": self.is_executable,
            "reject_reason": self.reject_reason,
        }


@dataclass  
class IntegratedScanResult:
    """Result of an integrated scan"""
    
    scan_id: str
    timestamp: str
    
    # Gamma stage
    gamma_markets_scanned: int
    gamma_opportunities: int
    
    # CLOB validation stage
    clob_validations_attempted: int
    clob_validations_passed: int
    
    # Final
    executable_opportunities: list[ValidatedOpportunity]
    
    # Stats for white paper
    avg_price_slip_bps: Optional[float]
    
    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "timestamp": self.timestamp,
            "gamma_markets_scanned": self.gamma_markets_scanned,
            "gamma_opportunities": self.gamma_opportunities,
            "clob_validations_attempted": self.clob_validations_attempted,
            "clob_validations_passed": self.clob_validations_passed,
            "executable_count": len(self.executable_opportunities),
            "avg_price_slip_bps": self.avg_price_slip_bps,
            "opportunities": [o.to_dict() for o in self.executable_opportunities],
        }


# =============================================================================
# INTEGRATED SCANNER
# =============================================================================

class IntegratedScanner:
    """
    Two-stage scanner: Gamma discovery → CLOB validation.
    """
    
    def __init__(self, config: IntegratedConfig, paper_mode: bool = True):
        self.config = config
        self.paper_mode = paper_mode
        
        # Initialize components
        self.gamma_scanner = GammaScanner(config.gamma)
        self.clob_client = CLOBClient(config.clob, paper_mode=paper_mode)
        
        # State
        self.total_exposure: float = 0.0
        self.positions: dict[str, dict] = {}
        
        # Logging
        self.log_dir = Path(config.log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Stats
        self.scans_completed = 0
        self.opportunities_found = 0
        self.trades_executed = 0
    
    def scan(self) -> IntegratedScanResult:
        """
        Run integrated scan:
        1. Gamma scan for indicative opportunities
        2. CLOB validation for executable prices
        """
        
        timestamp = datetime.now(timezone.utc)
        scan_id = f"int_{timestamp.strftime('%Y%m%d_%H%M%S')}"
        
        # Stage 1: Gamma scan
        gamma_result = self.gamma_scanner.scan()
        
        gamma_opportunities = []
        for target in gamma_result.targets:
            # Reconstruct market object for CLOB validation
            market = BinaryMarket(
                condition_id=target.get("condition_id", ""),
                market_id=target.get("market_id", ""),
                slug=target.get("slug", ""),
                question=target.get("question", ""),
                raw_outcome_count=2,
                priced_token_count=2,
                token_a=None,  # Will use IDs directly
                token_b=None,
                outcome_labels=target.get("outcome_labels", []),
                minutes_until_resolution=target.get("minutes_until"),
                volume_24h=target.get("volume_24h"),
                volume_source=target.get("volume_source", ""),
                active=True,
            )
            
            gamma_opportunities.append({
                "market": market,
                "price_sum": target.get("price_sum_indicative"),
                "token_a_id": target.get("token_a_id"),
                "token_b_id": target.get("token_b_id"),
                "token_a_price": target.get("token_a_price"),
                "token_b_price": target.get("token_b_price"),
            })
        
        # Stage 2: CLOB validation
        validated = []
        price_slips = []
        
        for opp in gamma_opportunities:
            if not self.config.clob_validation_enabled:
                # Skip CLOB, trust Gamma
                validated.append(ValidatedOpportunity(
                    market=opp["market"],
                    gamma_price_sum=opp["price_sum"],
                    gamma_edge=1.0 - opp["price_sum"] if opp["price_sum"] else 0,
                    clob_validated=False,
                    clob_price_sum=None,
                    clob_edge=None,
                    clob_depth=None,
                    price_slip_bps=None,
                    is_executable=True,  # Trust Gamma
                    reject_reason=None,
                ))
                continue
            
            # Validate against CLOB
            token_a_id = opp.get("token_a_id")
            token_b_id = opp.get("token_b_id")
            
            if not token_a_id or not token_b_id:
                validated.append(ValidatedOpportunity(
                    market=opp["market"],
                    gamma_price_sum=opp["price_sum"],
                    gamma_edge=1.0 - opp["price_sum"] if opp["price_sum"] else 0,
                    clob_validated=False,
                    clob_price_sum=None,
                    clob_edge=None,
                    clob_depth=None,
                    price_slip_bps=None,
                    is_executable=False,
                    reject_reason="missing_token_ids",
                ))
                continue
            
            paired_book = self.clob_client.get_paired_order_book(token_a_id, token_b_id)
            
            if paired_book is None:
                validated.append(ValidatedOpportunity(
                    market=opp["market"],
                    gamma_price_sum=opp["price_sum"],
                    gamma_edge=1.0 - opp["price_sum"] if opp["price_sum"] else 0,
                    clob_validated=False,
                    clob_price_sum=None,
                    clob_edge=None,
                    clob_depth=None,
                    price_slip_bps=None,
                    is_executable=False,
                    reject_reason="clob_fetch_failed",
                ))
                continue
            
            # Calculate CLOB metrics
            clob_sum = paired_book.executable_price_sum
            clob_edge = paired_book.executable_edge
            clob_depth = paired_book.max_executable_size()
            
            gamma_sum = opp["price_sum"]
            
            # Calculate price slip (how much worse is CLOB)
            price_slip_bps = None
            if clob_sum and gamma_sum:
                slip = clob_sum - gamma_sum
                price_slip_bps = int(slip * 10000)
                price_slips.append(price_slip_bps)
            
            # Determine if executable
            is_executable = True
            reject_reason = None
            
            if clob_sum is None:
                is_executable = False
                reject_reason = "no_clob_price"
            elif clob_sum >= self.config.clob_executable_threshold:
                is_executable = False
                reject_reason = f"clob_sum_too_high_{clob_sum:.4f}"
            elif clob_depth < 1.0:  # Less than 1 share available
                is_executable = False
                reject_reason = f"insufficient_depth_{clob_depth:.2f}"
            
            validated.append(ValidatedOpportunity(
                market=opp["market"],
                gamma_price_sum=gamma_sum,
                gamma_edge=1.0 - gamma_sum if gamma_sum else 0,
                clob_validated=True,
                clob_price_sum=clob_sum,
                clob_edge=clob_edge,
                clob_depth=clob_depth,
                price_slip_bps=price_slip_bps,
                is_executable=is_executable,
                reject_reason=reject_reason,
            ))
        
        # Calculate average price slip
        avg_slip = sum(price_slips) / len(price_slips) if price_slips else None
        
        executable = [v for v in validated if v.is_executable]
        
        self.scans_completed += 1
        
        return IntegratedScanResult(
            scan_id=scan_id,
            timestamp=timestamp.isoformat(),
            gamma_markets_scanned=gamma_result.total_markets_fetched,
            gamma_opportunities=len(gamma_opportunities),
            clob_validations_attempted=sum(1 for v in validated if v.clob_validated),
            clob_validations_passed=len(executable),
            executable_opportunities=executable,
            avg_price_slip_bps=avg_slip,
        )
    
    def execute_opportunity(self, opp: ValidatedOpportunity) -> Optional[ArbExecution]:
        """Execute a validated opportunity"""
        
        # Check exposure limits
        available = self.config.max_total_exposure - self.total_exposure
        if available <= 0:
            print(f"  Exposure limit reached: ${self.total_exposure:.2f}")
            return None
        
        # Calculate position size
        price_sum = opp.clob_price_sum or opp.gamma_price_sum
        position_usd = min(self.config.max_position_usd, available)
        size = position_usd / price_sum if price_sum else 0
        
        if size < 1:
            print(f"  Position too small: {size:.2f} shares")
            return None
        
        # Get token IDs from market
        # Note: In a real implementation, you'd have these from the original scan
        token_a_id = ""  # Would come from market data
        token_b_id = ""
        
        execution = self.clob_client.execute_arb(
            market_slug=opp.market.slug,
            token_a_id=token_a_id,
            token_b_id=token_b_id,
            size=size,
            max_price_sum=self.config.clob_executable_threshold,
        )
        
        if execution.success:
            self.total_exposure += execution.total_cost
            self.trades_executed += 1
            self.positions[opp.market.market_id] = {
                "entry_time": execution.timestamp.isoformat(),
                "size": size,
                "cost": execution.total_cost,
            }
        
        return execution
    
    def run_continuous(
        self, 
        mode: str = "scan",
        duration_seconds: Optional[int] = None,
    ) -> None:
        """
        Run continuous scanning loop.
        
        Modes:
        - scan: Just watch, don't execute
        - paper: Simulate trades
        - live: Real money (requires confirmation)
        """
        
        print(f"\n{'='*60}")
        print(f"INTEGRATED SCANNER - Mode: {mode}")
        print(f"{'='*60}")
        print(f"Gamma threshold: {self.config.gamma_indicative_threshold}")
        print(f"CLOB threshold: {self.config.clob_executable_threshold}")
        print(f"CLOB validation: {'enabled' if self.config.clob_validation_enabled else 'disabled'}")
        print(f"Scan interval: {self.config.scan_interval_seconds}s")
        
        if duration_seconds:
            print(f"Duration: {duration_seconds}s")
        
        start_time = time.time()
        
        try:
            while True:
                if duration_seconds:
                    elapsed = time.time() - start_time
                    if elapsed >= duration_seconds:
                        break
                
                # Run scan
                result = self.scan()
                
                # Log
                self._log_scan(result)
                
                # Display
                self._display_scan(result)
                
                # Execute if not in scan mode
                if mode != "scan" and result.executable_opportunities:
                    for opp in result.executable_opportunities:
                        print(f"\n  Executing: {opp.market.slug}")
                        execution = self.execute_opportunity(opp)
                        if execution:
                            self._log_execution(execution)
                            print(f"  Result: {'✅' if execution.success else '❌'} {execution.notes}")
                
                time.sleep(self.config.scan_interval_seconds)
        
        except KeyboardInterrupt:
            print("\n\nInterrupted.")
        
        finally:
            self._print_summary()
    
    def _log_scan(self, result: IntegratedScanResult) -> None:
        """Log scan to JSONL"""
        log_file = self.log_dir / "integrated_scans.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")
    
    def _log_execution(self, execution: ArbExecution) -> None:
        """Log execution to JSONL"""
        log_file = self.log_dir / "executions.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(execution.to_dict()) + "\n")
    
    def _display_scan(self, result: IntegratedScanResult) -> None:
        """Display scan results"""
        print(f"\n[{result.timestamp[:19]}] Scan #{self.scans_completed}")
        print(f"  Gamma: {result.gamma_markets_scanned} markets → {result.gamma_opportunities} indicative")
        
        if self.config.clob_validation_enabled:
            print(f"  CLOB: {result.clob_validations_attempted} validated → {result.clob_validations_passed} executable")
            if result.avg_price_slip_bps is not None:
                direction = "worse" if result.avg_price_slip_bps > 0 else "better"
                print(f"  Avg price slip: {abs(result.avg_price_slip_bps)}bps ({direction} than Gamma)")
        
        if result.executable_opportunities:
            print(f"\n  💰 OPPORTUNITIES:")
            for opp in result.executable_opportunities:
                edge = opp.clob_edge or opp.gamma_edge
                src = "CLOB" if opp.clob_validated else "Gamma"
                print(f"     {opp.market.slug[:40]}: {edge*100:.2f}% edge ({src})")
    
    def _print_summary(self) -> None:
        """Print session summary"""
        print(f"\n{'='*60}")
        print("SESSION SUMMARY")
        print(f"{'='*60}")
        print(f"Scans completed: {self.scans_completed}")
        print(f"Trades executed: {self.trades_executed}")
        print(f"Total exposure: ${self.total_exposure:.2f}")
        print(f"Paper balance: ${self.clob_client.get_balance():,.2f}")
        print(f"\nLogs: {self.log_dir}/")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Integrated Scanner",
    )
    parser.add_argument(
        "--mode",
        choices=["scan", "paper", "live"],
        default="scan",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Duration in seconds",
    )
    parser.add_argument(
        "--no-clob",
        action="store_true",
        help="Disable CLOB validation (Gamma only)",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        default=50.0,
    )
    
    args = parser.parse_args()
    
    # Safety check
    if args.mode == "live":
        print("⚠️  LIVE MODE - Real money!")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return
    
    # Build config
    gamma_config = GammaConfig(
        max_resolution_hours=24,
        min_volume_24h=1000.0,
        fee_buffer=0.02,
        slippage_buffer=0.005,
        risk_buffer=0.005,
    )
    
    clob_config = CLOBConfig.from_env()
    
    config = IntegratedConfig(
        gamma=gamma_config,
        clob=clob_config,
        clob_validation_enabled=not args.no_clob,
        max_position_usd=args.max_position,
    )
    
    # Run
    scanner = IntegratedScanner(
        config,
        paper_mode=(args.mode != "live"),
    )
    
    scanner.run_continuous(
        mode=args.mode,
        duration_seconds=args.duration,
    )


if __name__ == "__main__":
    main()
