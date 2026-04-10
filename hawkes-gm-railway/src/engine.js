// Hawkes-GM Engine — simulation mode
// Replace the data feed section with live Polymarket WebSocket for production

let marketSeq = 0;

class Market {
  constructor() {
    marketSeq++;
    const duration = [5, 10, 15][Math.floor(Math.random() * 3)];
    const now = Date.now();
    const questions = [
      "BTC above $", "ETH above $", "SOL above $", "Will Fed cut rates by ",
      "NVDA closes above $", "BTC below $", "Gold above $", "DOGE above $",
    ];

    this.id = `MKT-${String(marketSeq).padStart(4, "0")}`;
    this.question = questions[Math.floor(Math.random() * questions.length)] + Math.floor(1000 + Math.random() * 99000);
    this.question = this.question.slice(0, 36);
    this.duration = duration;
    this.createdAt = now;
    this.expiresAt = now + duration * 60 * 1000;

    // Hawkes
    this.hawkesBuy = { intensity: 0.5 + Math.random() * 0.5, n: 0.2 + Math.random() * 0.4 };
    this.hawkesSell = { intensity: 0.3 + Math.random() * 0.5, n: 0.15 + Math.random() * 0.35 };

    // GM
    this.posterior = 0.35 + Math.random() * 0.3;
    this.logOdds = 0;
    this.piBase = 0.12 + Math.random() * 0.08;
    this.piEffective = 0.15;

    // CLOB
    this.clobPrice = 0.4 + Math.random() * 0.2;

    // Derived
    this.flowImbalance = 0;
    this.cascadeActive = false;
    this.tradeCount = Math.floor(Math.random() * 5);
    this.recentTrades = [];

    // State
    this.resolved = false;
    this.outcome = null;
    this.signal = null;
    this.position = null;
    this.remaining = duration * 60 * 1000;
    this.progress = 0;
  }

  update() {
    if (this.resolved) return;

    const now = Date.now();
    this.remaining = Math.max(0, this.expiresAt - now);
    this.progress = 1 - this.remaining / (this.duration * 60 * 1000);

    // Hawkes intensity evolution
    const buyJolt = Math.random() > 0.85 ? 0.3 + Math.random() * 0.5 : 0;
    const sellJolt = Math.random() > 0.87 ? 0.2 + Math.random() * 0.4 : 0;
    this.hawkesBuy.intensity = Math.max(0.1, this.hawkesBuy.intensity * 0.92 + buyJolt);
    this.hawkesSell.intensity = Math.max(0.1, this.hawkesSell.intensity * 0.93 + sellJolt);
    this.hawkesBuy.n = Math.min(0.95, Math.max(0.05, this.hawkesBuy.n + (Math.random() - 0.48) * 0.06));
    this.hawkesSell.n = Math.min(0.95, Math.max(0.05, this.hawkesSell.n + (Math.random() - 0.48) * 0.06));

    // Flow imbalance
    const total = this.hawkesBuy.intensity + this.hawkesSell.intensity;
    this.flowImbalance = total > 0 ? (this.hawkesBuy.intensity / total) - 0.5 : 0;

    // Cascade detection
    const maxN = Math.max(this.hawkesBuy.n, this.hawkesSell.n);
    this.cascadeActive = maxN > 0.5;

    // GM update
    this.piEffective = this.cascadeActive
      ? Math.min(0.95, this.piBase + 0.25 * Math.max(0, maxN - 0.5) / 0.5)
      : this.piBase;

    const w = Math.log((1 + this.piEffective) / (1 - this.piEffective));
    if (Math.random() > 0.6) {
      const isBuy = Math.random() > 0.45;
      const size = 5 + Math.random() * 50;
      const sizeWeight = Math.log(1 + size / 20);
      this.logOdds += (isBuy ? 1 : -1) * w * sizeWeight * 0.15;
      this.logOdds = Math.max(-8, Math.min(8, this.logOdds));
      this.tradeCount++;
      this.recentTrades.push({ time: now, side: isBuy ? "BUY" : "SELL", size: Math.floor(size) });
      if (this.recentTrades.length > 30) this.recentTrades.shift();
    }

    this.posterior = 1 / (1 + Math.exp(-this.logOdds));
    this.clobPrice = Math.max(0.02, Math.min(0.98,
      this.clobPrice + (this.posterior - this.clobPrice) * 0.08 + (Math.random() - 0.5) * 0.02
    ));

    // Signal
    const edge = this.posterior - this.clobPrice;
    const gatesMet = this.cascadeActive && Math.abs(this.flowImbalance) > 0.15 && Math.abs(edge) > 0.05 && this.remaining > 30000;
    const dirAgree = (edge > 0 && this.flowImbalance > 0) || (edge < 0 && this.flowImbalance < 0);
    this.signal = gatesMet && dirAgree ? (edge > 0 ? "BUY_YES" : "BUY_NO") : null;

    // Position entry
    if (this.signal && !this.position && Math.random() > 0.5) {
      this.position = {
        side: this.signal === "BUY_YES" ? "YES" : "NO",
        entry: this.clobPrice,
        size: 5 + Math.floor(Math.random() * 10),
        entryTime: now,
        edge: Math.abs(edge),
      };
    }

    // Resolve
    if (this.remaining <= 0) {
      this.resolved = true;
      this.outcome = this.posterior > 0.5 ? "YES" : "NO";
    }
  }

  toJSON() {
    return {
      id: this.id, question: this.question, duration: this.duration,
      remaining: this.remaining, progress: this.progress,
      hawkesBuy: this.hawkesBuy, hawkesSell: this.hawkesSell,
      posterior: this.posterior, logOdds: this.logOdds,
      piBase: this.piBase, piEffective: this.piEffective,
      clobPrice: this.clobPrice, flowImbalance: this.flowImbalance,
      cascadeActive: this.cascadeActive, tradeCount: this.tradeCount,
      recentTrades: this.recentTrades.slice(-10),
      resolved: this.resolved, outcome: this.outcome,
      signal: this.signal, position: this.position,
    };
  }
}

class Engine {
  constructor() {
    this.markets = [];
    this.resolvedTrades = [];
    this.totalPnl = 0;
    this.totalTrades = 0;
    this.totalWins = 0;
    this.tickCount = 0;

    // Seed initial markets
    for (let i = 0; i < 4; i++) this.markets.push(new Market());
  }

  tick() {
    this.tickCount++;

    this.markets.forEach((m) => {
      const wasResolved = m.resolved;
      m.update();

      // Track newly resolved positions
      if (m.resolved && !wasResolved && m.position) {
        const won = m.position.side === m.outcome;
        const pnl = (won ? 1 - m.position.entry : -m.position.entry) * m.position.size;
        this.totalPnl += pnl;
        this.totalTrades++;
        if (won) this.totalWins++;
        this.resolvedTrades.push({
          id: m.id, question: m.question, duration: m.duration,
          side: m.position.side, entry: m.position.entry,
          outcome: m.outcome, won, pnl, edge: m.position.edge,
          tradeCount: m.tradeCount, resolvedAt: Date.now(),
        });
      }
    });

    // Spawn new markets
    const activeCount = this.markets.filter((m) => !m.resolved).length;
    if (this.tickCount % 8 === 0 && activeCount < 6) {
      this.markets.push(new Market());
    }

    // Trim old resolved (keep last 50)
    if (this.markets.length > 60) {
      const active = this.markets.filter((m) => !m.resolved);
      const resolved = this.markets.filter((m) => m.resolved).slice(-20);
      this.markets = [...active, ...resolved];
    }
  }

  getState() {
    return {
      markets: this.markets.map((m) => m.toJSON()),
      totalPnl: this.totalPnl,
      totalTrades: this.totalTrades,
      totalWins: this.totalWins,
      balance: 200 + this.totalPnl,
      activeCount: this.markets.filter((m) => !m.resolved).length,
      cascadeCount: this.markets.filter((m) => !m.resolved && m.cascadeActive).length,
      signalCount: this.markets.filter((m) => !m.resolved && m.signal).length,
      positionCount: this.markets.filter((m) => !m.resolved && m.position).length,
      tickCount: this.tickCount,
    };
  }

  getActiveCount() {
    return this.markets.filter((m) => !m.resolved).length;
  }

  getResolvedTrades() {
    return this.resolvedTrades;
  }

  exportTradesCSV() {
    const headers = ["Market", "Question", "Duration", "Side", "Entry", "Outcome", "Won", "P/L", "Edge", "Trades", "Resolved At"];
    const rows = this.resolvedTrades.map((t) => [
      t.id, `"${t.question}"`, t.duration, t.side, t.entry.toFixed(4),
      t.outcome, t.won ? "Y" : "N", t.pnl.toFixed(4),
      (t.edge * 100).toFixed(1), t.tradeCount,
      new Date(t.resolvedAt).toISOString(),
    ].join(","));
    return [headers.join(","), ...rows].join("\n");
  }

  exportSnapshotCSV() {
    const active = this.markets.filter((m) => !m.resolved);
    const headers = ["Market", "Remaining(s)", "BuyIntensity", "SellIntensity", "BuyN", "SellN", "Posterior", "CLOB", "Edge(c)", "FlowImbalance", "Cascade", "Signal", "Position"];
    const rows = active.map((m) => [
      m.id, Math.floor(m.remaining / 1000),
      m.hawkesBuy.intensity.toFixed(3), m.hawkesSell.intensity.toFixed(3),
      m.hawkesBuy.n.toFixed(3), m.hawkesSell.n.toFixed(3),
      m.posterior.toFixed(4), m.clobPrice.toFixed(4),
      ((m.posterior - m.clobPrice) * 100).toFixed(2),
      m.flowImbalance.toFixed(3),
      m.cascadeActive ? "Y" : "N",
      m.signal || "",
      m.position ? m.position.side : "",
    ].join(","));
    return [headers.join(","), ...rows].join("\n");
  }
}

module.exports = { Engine };
