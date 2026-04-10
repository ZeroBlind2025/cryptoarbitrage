require("dotenv").config();
const express = require("express");
const path = require("path");
const { WebSocketServer } = require("ws");
const http = require("http");
const { Engine } = require("./engine");

const PORT = Number(process.env.PORT) || 3000;
const TICK_MS = Number(process.env.HGM_TICK_MS) || 1500;

// ── Engine instance (hoisted above route registration so closures
// capture a defined reference from the very first request). ──
const engine = new Engine();

const app = express();

// ── Serve static dashboard assets ──
app.use(express.static(path.join(__dirname, "..", "public")));

// ── Health check for Railway ──
app.get("/health", (req, res) => {
  res.json({
    status: "ok",
    uptime: process.uptime(),
    markets: engine.getActiveCount(),
    tick: engine.tickCount,
  });
});

// ── API: current engine state ──
app.get("/api/state", (req, res) => {
  res.json(engine.getState());
});

// ── API: resolved trades ──
app.get("/api/trades", (req, res) => {
  res.json(engine.getResolvedTrades());
});

// ── API: export CSVs ──
app.get("/api/export/trades", (req, res) => {
  const csv = engine.exportTradesCSV();
  const day = new Date().toISOString().slice(0, 10);
  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", `attachment; filename=hgm-trades-${day}.csv`);
  res.send(csv);
});

app.get("/api/export/snapshot", (req, res) => {
  const csv = engine.exportSnapshotCSV();
  const day = new Date().toISOString().slice(0, 10);
  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", `attachment; filename=hgm-snapshot-${day}.csv`);
  res.send(csv);
});

// ── SPA fallback: any other GET returns the dashboard HTML. ──
app.get("*", (req, res) => {
  res.sendFile(path.join(__dirname, "..", "public", "index.html"));
});

// ── HTTP + WebSocket server ──
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });

wss.on("connection", (ws) => {
  console.log("[WS] Client connected");
  try {
    ws.send(JSON.stringify({ type: "state", data: engine.getState() }));
  } catch (e) {
    console.error("[WS] initial send failed:", e.message);
  }
  ws.on("close", () => console.log("[WS] Client disconnected"));
  ws.on("error", (err) => console.error("[WS] Client error:", err.message));
});

function broadcast(data) {
  const msg = JSON.stringify(data);
  wss.clients.forEach((client) => {
    if (client.readyState === 1) {
      try {
        client.send(msg);
      } catch (e) {
        console.error("[WS] broadcast send failed:", e.message);
      }
    }
  });
}

// ── Engine loop. Wrapped so a bad tick cannot take down the process. ──
const tickTimer = setInterval(() => {
  try {
    engine.tick();
    broadcast({ type: "state", data: engine.getState() });
  } catch (e) {
    console.error("[HGM] tick error:", e);
  }
}, TICK_MS);

// ── Start ──
server.listen(PORT, () => {
  console.log(`[HGM] Hawkes-GM Engine running on port ${PORT}`);
  console.log(`[HGM] Dashboard: http://localhost:${PORT}`);
  console.log(`[HGM] WebSocket: ws://localhost:${PORT}/ws`);
  console.log(`[HGM] Health:    http://localhost:${PORT}/health`);
});

// ── Graceful shutdown: Railway and Docker send SIGTERM; drain the
// tick timer and close the HTTP/WS server so in-flight requests can
// complete before the process exits. ──
function shutdown(signal) {
  console.log(`[HGM] received ${signal}, shutting down`);
  clearInterval(tickTimer);
  wss.clients.forEach((c) => {
    try {
      c.close(1001, "server shutting down");
    } catch (_) {}
  });
  server.close(() => {
    console.log("[HGM] HTTP server closed");
    process.exit(0);
  });
  // Hard exit if nothing closes within 5s.
  setTimeout(() => process.exit(0), 5000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
