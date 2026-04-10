# Hawkes-GM Engine Dashboard

Real-time dashboard for the Hawkes-Glosten-Milgrom high-frequency
binary engine (Polymarket 5m / 15m markets). Cascade detection via a
Hawkes self-exciting process + Bayesian posterior via Glosten-Milgrom.

Currently runs in **simulation mode** so the UI can be built and
deployed before the live Polymarket feed is wired in.

## Architecture

```
┌─────────────────────────────────────┐
│         Railway Container           │
│                                     │
│  Express Server (port $PORT)        │
│  ├── GET  /                → UI     │
│  ├── GET  /health          → JSON   │
│  ├── GET  /api/state       → JSON   │
│  ├── GET  /api/trades      → JSON   │
│  ├── GET  /api/export/trades    CSV │
│  ├── GET  /api/export/snapshot  CSV │
│  └── WS   /ws              → state  │
│                                     │
│  Engine Loop (HGM_TICK_MS=1500)     │
│  ├── Market scanning                │
│  ├── Hawkes intensity tracking      │
│  ├── GM posterior updates           │
│  ├── Signal generation              │
│  └── Position management            │
└─────────────────────────────────────┘
```

## Deploy to Railway

This dashboard lives in a sub-directory of the existing
`cryptoarbitrage` repository so it can be deployed as its **own**
Railway service, completely independent of the Python HFT server at
the repo root.

1. In the Railway dashboard click **New Service → GitHub Repo** and
   pick `cryptoarbitrage`.
2. Open the new service's **Settings → Source**.
3. Set **Root Directory** to `hawkes-gm-railway`.
4. Railway will detect `Dockerfile` + `railway.toml` automatically and
   switch the builder to `DOCKERFILE`.
5. (Optional) Set any of the environment variables below in
   **Settings → Variables**.
6. Deploy.

The existing Python service (root `railway.json`, NIXPACKS builder)
continues to run untouched — the two services share the repository but
not the build.

## Local Development

```bash
cd hawkes-gm-railway
npm install       # or: npm ci
npm start
# Open http://localhost:3000
```

## Docker

```bash
docker build -t hawkes-gm-dashboard .
docker run --rm -p 3000:3000 hawkes-gm-dashboard
```

The Dockerfile uses `npm ci --omit=dev` for reproducible builds, runs
as the unprivileged `node` user, and ships with a container-level
`HEALTHCHECK` that hits `/health`.

## Environment Variables

| Variable      | Default | Purpose                                           |
|---------------|---------|---------------------------------------------------|
| `PORT`        | `3000`  | HTTP port (Railway injects automatically)         |
| `HGM_TICK_MS` | `1500`  | Engine tick + WS broadcast interval (ms)          |

No secrets are required for simulation mode. The `.env.example` file
lists the placeholders that will be added when live trading is wired
in.

## Endpoints

| Endpoint                | Method | Description                                         |
|-------------------------|--------|-----------------------------------------------------|
| `/`                     | GET    | Dashboard UI (static HTML + vanilla JS)             |
| `/health`               | GET    | `{status, uptime, markets, tick}` for Railway probe |
| `/api/state`            | GET    | Full engine snapshot as JSON                        |
| `/api/trades`           | GET    | All resolved paper trades as JSON                   |
| `/api/export/trades`    | GET    | Download resolved trades as CSV                     |
| `/api/export/snapshot`  | GET    | Download current market snapshot as CSV             |
| `/ws`                   | WS     | Real-time state push (every `HGM_TICK_MS`)          |

## Production Mode (Roadmap)

The current `src/engine.js` is a fast, self-contained simulator that
produces a continuous stream of synthetic markets so the dashboard can
be developed and reviewed visually.

To replace it with the live engine:

1. **Polymarket CLOB feed** — wire `src/engine.js` to a WebSocket feed
   reading the real `last_trade_price` events (mirroring what the
   Python `hf_engine/feed.py` already does).
2. **Shared state with the Python engine** — alternatively, consume
   state from the Python `hf_engine` via an HTTP/WebSocket bridge, so
   both engines share a single source of truth.
3. **Calibrated parameters** — load priors produced by
   `hf_engine/calibration.py` so the JS engine uses the same α / β /
   π values that the Python side has learnt online.
4. **Live execution** — add a Polygon/CLOB order executor and wire it
   to the 6-wallet architecture.

## Files

```
hawkes-gm-railway/
├── Dockerfile          # node:20-slim, npm ci, non-root, HEALTHCHECK
├── railway.toml        # Railway DOCKERFILE build + /health probe
├── .dockerignore       # keep node_modules and .env out of the image
├── .env.example        # env var placeholders
├── .gitignore          # ignore node_modules / .env locally
├── package.json        # express, ws, dotenv
├── package-lock.json   # pinned for reproducible builds
├── public/
│   └── index.html      # vanilla-JS dashboard (no build step)
├── src/
│   ├── server.js       # Express + WS + graceful shutdown
│   └── engine.js       # simulation engine (Market + Engine classes)
└── README.md           # this file
```
