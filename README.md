# Polymarket Arbitrage Research Bot

Research-grade arbitrage scanner for Polymarket prediction markets.
Built for white paper data collection, not production trading.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GAMMA API (Read-only)                       │
│                  Market Discovery & Indicative Prices           │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Step C: Market Scanner                        │
│  - Fetch all active markets                                     │
│  - Filter: crypto, short-window, liquid, binary                 │
│  - Calculate indicative price_sum from Gamma marks              │
│  - Track exclusion funnel for white paper                       │
│  - Output: Candidate opportunities (indicative only!)           │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     CLOB API (Auth required)                    │
│                  Order Books & Order Placement                  │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Step D: CLOB Client                           │
│  - Fetch real order books (best bid/ask)                        │
│  - Calculate executable price_sum (not indicative!)             │
│  - Place orders (paper or live)                                 │
│  - Track positions and balance                                  │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Step E: Integrated Scanner                    │
│  - Gamma scan → CLOB validation pipeline                        │
│  - Compare indicative vs executable prices                      │
│  - Log price slip for white paper analysis                      │
│  - Execute arbs (paper or live mode)                            │
└─────────────────────────────────────────────────────────────────┘
```

## Key Insight: Indicative vs Executable

**Gamma marks are NOT what you pay.**

| Source | What it is | Use for |
|--------|------------|---------|
| Gamma API `price` | Last trade or mark price | Discovery, filtering |
| CLOB `best_ask` | Actual cost to buy now | Execution decisions |

The difference between these is "price slip" - a key metric for your white paper.

## Files

| File | Purpose |
|------|---------|
| `step_a_fundamentals.py` | Core concepts: how prediction markets work |
| `step_b_capital_velocity.py` | Economics: why short windows matter |
| `step_c_scanner_v4.py` | Gamma scanner with exclusion funnel |
| `step_d_clob_client.py` | CLOB client for real prices & orders |
| `step_e_integrated_scanner.py` | Combined pipeline |

## Quick Start

```bash
# Install dependencies
pip install requests python-dotenv

# For live trading (optional)
pip install py-clob-client web3

# Run in scan mode (no API keys needed)
python step_c_scanner_v4.py

# Run integrated scanner (needs network access)
python step_e_integrated_scanner.py --mode scan --duration 3600
```

## Environment Setup (for live trading)

Create `.env` file:

```
POLYMARKET_API_KEY=your_key
POLYMARKET_API_SECRET=your_secret
POLYMARKET_PASSPHRASE=your_passphrase
POLYGON_PRIVATE_KEY=0x...
```

## Modes

| Mode | What happens |
|------|--------------|
| `scan` | Watch only, log opportunities |
| `paper` | Simulate trades, track P&L |
| `live` | Real money (requires confirmation) |

## White Paper Metrics

The scanner logs everything you need:

1. **Opportunity frequency**: How often do arbs appear?
2. **Exclusion funnel**: Why markets get filtered out
3. **Price slip**: Gamma mark vs CLOB executable
4. **Closest misses**: Markets just above threshold
5. **Execution success rate**: Both legs filled?

## Risk Warnings

1. **Partial fills**: If only one leg fills, you have directional exposure
2. **Latency**: Opportunities close in seconds
3. **Fees**: 2% on winnings eats most small edges
4. **This is research code**: Not battle-tested for production

## Research Budget Suggestion

- Start with $100-200
- Max $25-50 per trade
- Expect to lose some to learning
- Goal is data, not profit
