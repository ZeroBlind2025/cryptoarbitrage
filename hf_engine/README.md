# hf_engine — Hawkes-Glosten-Milgrom HF Binary Engine

A high-frequency paper-trading engine for ultra-short-duration
Polymarket binary markets (5m / 15m / 30m / 1h). Implements the
theoretical spec verbatim:

- **Hawkes Process** (one tracker per side) detects self-exciting
  informed-flow cascades and produces a branching ratio `n = alpha /
  beta` plus a flow imbalance `lambda_buy / (lambda_buy + lambda_sell)
  - 0.5`.
- **Glosten-Milgrom** posterior updates in log-odds space using a
  cascade-adaptive informed-fraction `pi`.

The two models agree on "is something happening" (cascade) and "which
way" (flow imbalance + posterior divergence from CLOB) before the
engine will fire a paper signal.

## Running

Headless engine (logs + JSONL only, no HTTP):

```
python -m hf_engine.main
```

Engine + live dashboard (the Railway entry point):

```
python -m hf_engine.server --port $PORT
```

The server hosts a vanilla-JS dashboard at `/` which polls `/api/state`
every 1.5 s and renders live Hawkes intensities, GM posteriors, cascade
badges, signal firings, and paper P&L. All state comes from the same
``HFEngine`` instance that is scanning Polymarket + consuming the CLOB
WebSocket feed underneath it — no simulation, no mocks.

Tuning happens through environment variables. All variables are
prefixed `HFE_*` and are documented below.

## Configuration (`HFE_*` only)

| Env var                           | Default   | Purpose                                       |
|-----------------------------------|-----------|-----------------------------------------------|
| `HFE_HAWKES_ALPHA`                | 0.5       | Hawkes self-excitation amplitude              |
| `HFE_HAWKES_BETA`                 | 1.0       | Hawkes decay rate                             |
| `HFE_HAWKES_MU_FALLBACK`          | 0.2       | Baseline rate fallback (trades/sec)           |
| `HFE_MU_OBSERVATION_WINDOW_SEC`   | 15.0      | Seconds to estimate fresh mu per market       |
| `HFE_CASCADE_THRESHOLD`           | 0.5       | Branching ratio to flag a cascade             |
| `HFE_PI_BASE`                     | 0.15      | Baseline informed-trader fraction             |
| `HFE_PI_HAWKES_BOOST`             | 0.30      | Max pi boost during a cascade                 |
| `HFE_PI_CAP`                      | 0.95      | Hard cap on effective pi                      |
| `HFE_MIN_FLOW_IMBALANCE`          | 0.15      | Gate: directional conviction                  |
| `HFE_MIN_EDGE`                    | 0.05      | Gate: posterior - CLOB edge (cents)           |
| `HFE_MIN_TIME_REMAINING_SEC`      | 30.0      | Gate: seconds left before resolution          |
| `HFE_MIN_BOOK_DEPTH`              | 50.0      | Gate: contracts at top of book                |
| `HFE_KELLY_BASE_FRACTION`         | 0.25      | Quarter-Kelly                                 |
| `HFE_MAX_DOLLAR_RISK_PER_MARKET`  | 10.0      | Per-market paper cap                          |
| `HFE_MAX_TOTAL_DOLLAR_RISK`       | 50.0      | Aggregate open paper exposure cap             |
| `HFE_EDGE_COLLAPSE_THRESHOLD`     | 0.10      | Posterior distance from 0.5 to exit           |
| `HFE_CONTRARY_FLOW_THRESHOLD`     | 0.20      | Contrary-cascade exit threshold               |
| `HFE_EXIT_TIME_BUFFER_SEC`        | 15.0      | Force-exit losing positions this close to end |
| `HFE_SCAN_INTERVAL_SEC`           | 10.0      | Market scanner poll interval                  |
| `HFE_MIN_MARKET_DURATION_MIN`     | 4.0       | Shortest market we will track                 |
| `HFE_MAX_MARKET_DURATION_MIN`     | 65.0      | Longest market we will track                  |
| `HFE_TRACKED_COINS`               | btc,eth…  | Comma-separated slug filter                   |
| `HFE_ENABLE_ONLINE_CALIBRATION`   | true      | Post-resolution calibrator                    |
| `HFE_CALIBRATION_EMA_ALPHA`       | 0.10      | EMA weight applied to each new fit            |
| `HFE_LOG_DIR`                     | hf_engine_logs | Directory for JSONL output               |
| `HFE_LOG_PREFIX`                  | [HFE]     | Log tag                                       |
| `HFE_PAPER_TRADING`               | true      | Locked on in v1                               |

**Deliberately absent:** every variable starts with `HFE_`. There is no
`MOMENTUM_*` read anywhere in the package. The engine cannot trigger the
momentum engine directly or indirectly — it does not import from
`momentum_engine.py`, `hft_server.py`, or `copy_trader.py`, and it does
not touch `positions.json`. Its only persistent state is the JSONL log
files under `HFE_LOG_DIR`.

## Files

```
hf_engine/
├── __init__.py           # public surface
├── config.py             # HFE_* env var loader
├── hawkes.py             # recursive Hawkes tracker + offline MLE helper
├── glosten_milgrom.py    # log-odds posterior updates + size weight
├── market_state.py       # MarketState, signal gates, Kelly sizing
├── feed.py               # trade-aware Polymarket WS client (self-contained)
├── scanner.py            # Gamma REST discovery for 5m/15m markets
├── paper_executor.py     # paper P&L ledger and JSONL logger
├── calibration.py        # post-resolution alpha/beta/pi refit
├── engine.py             # orchestrator / main loop
├── main.py               # headless CLI (`python -m hf_engine.main`)
├── server.py             # Flask HTTP + dashboard (`python -m hf_engine.server`)
├── snapshot.py           # Python -> dashboard JSON + CSV exporters
├── static/
│   └── index.html        # vanilla-JS dashboard (no build step)
└── README.md             # this file
```

## Dashboard endpoints

| Endpoint               | Method | Description                                             |
|------------------------|--------|---------------------------------------------------------|
| `/`                    | GET    | Dashboard UI (polls `/api/state` every 1500ms)          |
| `/health`              | GET    | `{status, uptime, markets, tick, feed_connected, ...}`  |
| `/api/state`           | GET    | Full engine snapshot as JSON                            |
| `/api/trades`          | GET    | All resolved paper trades as JSON                       |
| `/api/stats`           | GET    | Engine + feed + executor + calibrator stats             |
| `/api/export/trades`   | GET    | Download resolved trades as CSV                         |
| `/api/export/snapshot` | GET    | Download current market snapshot as CSV                 |

## Calibration strategy

Since we are going straight to paper trading the engine ships with:

1. **Literature priors** — `alpha=0.5`, `beta=1.0`, `pi_base=0.15`,
   `cascade_threshold=0.5`.
2. **Fresh per-market `mu`** — the first 15 seconds of a new market is
   used to estimate the baseline rate; signals are gated off entirely
   until that window closes.
3. **Online self-calibration** — every time a market resolves, the
   calibrator refits `alpha`, `beta`, and `pi` against the observed
   trades and true outcome, then EMA-blends the fitted values into the
   running priors with `alpha_ema=0.1`. Subsequent markets inherit the
   improved priors automatically.
4. **Full observability** — every signal (accepted or rejected) is
   written to `hf_signals.jsonl`; every paper fill to `hf_trades.jsonl`;
   every calibration fit to `hf_calibration.jsonl`. Run any of these
   through `pandas` to retune gate thresholds empirically.

## Signal gates (all must pass)

1. Observation window closed (`mu` has been finalized)
2. No position already open on this market
3. Cascade active (`n > cascade_threshold`)
4. Sufficient flow imbalance (`|imbalance| >= 0.15`)
5. Posterior diverges from CLOB mid by at least `min_edge`
6. Enough time remains in the market
7. Top-of-book depth above `min_book_depth`
8. Posterior and flow agree on direction

## Exit logic

1. **Hold to resolution** (primary)
2. **Edge collapse** — posterior reverted to within `0.10` of 0.5
3. **Contrary cascade** — opposing flow imbalance > `0.20`
4. **Time buffer** — less than 15 seconds remain *and* we are losing

## Paper trading output

```
hf_engine_logs/
├── hf_trades.jsonl        # open / close / resolve fills
├── hf_signals.jsonl       # every gated and accepted signal
└── hf_calibration.jsonl   # per-market refit results
```

Tail them live:

```
tail -f hf_engine_logs/hf_trades.jsonl
```
