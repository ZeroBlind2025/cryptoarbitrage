import React, { useState, useEffect } from 'react';
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { Activity, TrendingUp, AlertTriangle, DollarSign, Clock, Target, Zap, Eye } from 'lucide-react';

// Simulated data - in production this would come from your JSONL logs or a websocket
const generateMockData = () => {
  const now = Date.now();
  
  // Scan history
  const scanHistory = Array.from({ length: 20 }, (_, i) => ({
    timestamp: new Date(now - (19 - i) * 5000).toISOString(),
    marketsScanned: 150 + Math.floor(Math.random() * 30),
    gammaOpportunities: Math.floor(Math.random() * 4),
    clobValidated: Math.floor(Math.random() * 3),
    executable: Math.floor(Math.random() * 2),
  }));

  // Exclusion funnel
  const exclusionFunnel = [
    { reason: 'not_crypto', count: 89, pct: 52.4 },
    { reason: 'resolves_too_far', count: 34, pct: 20.0 },
    { reason: 'volume_24h_missing', count: 21, pct: 12.4 },
    { reason: 'volume_24h_low', count: 12, pct: 7.1 },
    { reason: 'price_sum_too_high', count: 8, pct: 4.7 },
    { reason: 'not_binary_raw', count: 4, pct: 2.4 },
    { reason: 'missing_token_ids', count: 2, pct: 1.2 },
  ];

  // Price slip data (Gamma vs CLOB)
  const priceSlipHistory = Array.from({ length: 15 }, (_, i) => ({
    time: `${i * 5}m ago`,
    gammaSum: 0.96 + Math.random() * 0.02,
    clobSum: 0.97 + Math.random() * 0.02,
    slipBps: Math.floor(Math.random() * 50) + 10,
  }));

  // Current opportunities
  const opportunities = [
    {
      slug: 'btc-above-98k-jan-15-12pm',
      question: 'Will BTC be above $98,000 on Jan 15 at 12:00 PM?',
      gammaSum: 0.962,
      clobSum: 0.968,
      edge: 0.032,
      netProfit: 0.012,
      minutesUntil: 45,
      volume24h: 12500,
      depth: 850,
      status: 'executable',
    },
    {
      slug: 'eth-above-3500-jan-15-1pm',
      question: 'Will ETH be above $3,500 on Jan 15 at 1:00 PM?',
      gammaSum: 0.958,
      clobSum: 0.971,
      edge: 0.029,
      netProfit: 0.009,
      minutesUntil: 105,
      volume24h: 8200,
      depth: 620,
      status: 'validating',
    },
  ];

  // Closest misses
  const closestMisses = [
    { slug: 'sol-above-200-jan-15', sum: 0.978, gapBps: 8, minutes: 30 },
    { slug: 'btc-above-99k-jan-15', sum: 0.982, gapBps: 12, minutes: 60 },
    { slug: 'eth-above-3600-jan-15', sum: 0.985, gapBps: 15, minutes: 90 },
  ];

  // Paper trading history
  const trades = [
    { time: '10:45:23', market: 'btc-97k-jan-15', side: 'ARB', cost: 48.50, status: 'filled', pnl: 0.52 },
    { time: '10:32:11', market: 'eth-3400-jan-15', side: 'ARB', cost: 47.20, status: 'filled', pnl: 0.48 },
    { time: '10:15:44', market: 'btc-96k-jan-15', side: 'ARB', cost: 49.10, status: 'partial', pnl: -0.15 },
  ];

  return { scanHistory, exclusionFunnel, priceSlipHistory, opportunities, closestMisses, trades };
};

const COLORS = ['#ef4444', '#f97316', '#eab308', '#22c55e', '#3b82f6', '#8b5cf6', '#ec4899'];

const StatCard = ({ icon: Icon, label, value, subValue, trend, color = 'blue' }) => (
  <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
    <div className="flex items-center justify-between">
      <div className={`p-2 rounded-lg bg-${color}-500/20`}>
        <Icon className={`w-5 h-5 text-${color}-400`} />
      </div>
      {trend && (
        <span className={`text-xs ${trend > 0 ? 'text-green-400' : 'text-red-400'}`}>
          {trend > 0 ? '↑' : '↓'} {Math.abs(trend)}%
        </span>
      )}
    </div>
    <div className="mt-3">
      <p className="text-2xl font-bold text-white">{value}</p>
      <p className="text-sm text-gray-400">{label}</p>
      {subValue && <p className="text-xs text-gray-500 mt-1">{subValue}</p>}
    </div>
  </div>
);

const OpportunityRow = ({ opp }) => (
  <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700 hover:border-gray-600 transition-colors">
    <div className="flex items-start justify-between">
      <div className="flex-1">
        <p className="text-sm font-medium text-white truncate">{opp.question}</p>
        <p className="text-xs text-gray-500 mt-1">{opp.slug}</p>
      </div>
      <span className={`px-2 py-1 rounded text-xs font-medium ${
        opp.status === 'executable' ? 'bg-green-500/20 text-green-400' : 'bg-yellow-500/20 text-yellow-400'
      }`}>
        {opp.status}
      </span>
    </div>
    <div className="grid grid-cols-4 gap-4 mt-3 text-xs">
      <div>
        <p className="text-gray-500">Gamma Sum</p>
        <p className="text-white font-mono">${opp.gammaSum.toFixed(3)}</p>
      </div>
      <div>
        <p className="text-gray-500">CLOB Sum</p>
        <p className="text-white font-mono">${opp.clobSum.toFixed(3)}</p>
      </div>
      <div>
        <p className="text-gray-500">Net Profit</p>
        <p className="text-green-400 font-mono">{(opp.netProfit * 100).toFixed(2)}%</p>
      </div>
      <div>
        <p className="text-gray-500">Resolves</p>
        <p className="text-white">{opp.minutesUntil}m</p>
      </div>
    </div>
    <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
      <span>Vol: ${opp.volume24h.toLocaleString()}</span>
      <span>Depth: {opp.depth} shares</span>
      <span>Slip: {Math.round((opp.clobSum - opp.gammaSum) * 10000)}bps</span>
    </div>
  </div>
);

export default function Dashboard() {
  const [data, setData] = useState(generateMockData());
  const [mode, setMode] = useState('scan');
  const [isRunning, setIsRunning] = useState(true);
  const [scanCount, setScanCount] = useState(142);

  // Simulate live updates
  useEffect(() => {
    if (!isRunning) return;
    
    const interval = setInterval(() => {
      setData(generateMockData());
      setScanCount(c => c + 1);
    }, 5000);

    return () => clearInterval(interval);
  }, [isRunning]);

  const totalExcluded = data.exclusionFunnel.reduce((sum, e) => sum + e.count, 0);
  const avgSlip = data.priceSlipHistory.reduce((sum, p) => sum + p.slipBps, 0) / data.priceSlipHistory.length;

  return (
    <div className="min-h-screen bg-gray-900 text-white p-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Polymarket Arbitrage Scanner</h1>
          <p className="text-gray-400 text-sm">Research Dashboard</p>
        </div>
        <div className="flex items-center gap-4">
          <select 
            value={mode} 
            onChange={(e) => setMode(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"
          >
            <option value="scan">Scan Mode</option>
            <option value="paper">Paper Trading</option>
            <option value="live">Live Trading</option>
          </select>
          <button
            onClick={() => setIsRunning(!isRunning)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              isRunning 
                ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30' 
                : 'bg-green-500/20 text-green-400 hover:bg-green-500/30'
            }`}
          >
            {isRunning ? 'Stop' : 'Start'}
          </button>
          <div className={`flex items-center gap-2 px-3 py-2 rounded-lg ${
            isRunning ? 'bg-green-500/20' : 'bg-gray-700'
          }`}>
            <div className={`w-2 h-2 rounded-full ${isRunning ? 'bg-green-400 animate-pulse' : 'bg-gray-500'}`} />
            <span className="text-sm">{isRunning ? 'Running' : 'Stopped'}</span>
          </div>
        </div>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-6">
        <StatCard 
          icon={Activity} 
          label="Scans" 
          value={scanCount} 
          subValue="5s interval"
          color="blue"
        />
        <StatCard 
          icon={Eye} 
          label="Markets Scanned" 
          value={data.scanHistory[data.scanHistory.length - 1]?.marketsScanned || 0}
          subValue="Last scan"
          color="purple"
        />
        <StatCard 
          icon={Target} 
          label="Gamma Opportunities" 
          value={data.opportunities.length}
          subValue="Indicative"
          color="yellow"
        />
        <StatCard 
          icon={Zap} 
          label="Executable" 
          value={data.opportunities.filter(o => o.status === 'executable').length}
          subValue="CLOB validated"
          color="green"
        />
        <StatCard 
          icon={TrendingUp} 
          label="Avg Price Slip" 
          value={`${avgSlip.toFixed(0)}bps`}
          subValue="Gamma → CLOB"
          color="orange"
        />
        <StatCard 
          icon={DollarSign} 
          label="Paper Balance" 
          value="$9,847"
          subValue="-$153 today"
          trend={-1.5}
          color="red"
        />
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        {/* Left Column - Opportunities */}
        <div className="lg:col-span-2 space-y-6">
          
          {/* Active Opportunities */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Target className="w-5 h-5 text-green-400" />
              Active Opportunities
            </h2>
            <div className="space-y-3">
              {data.opportunities.length > 0 ? (
                data.opportunities.map((opp, i) => <OpportunityRow key={i} opp={opp} />)
              ) : (
                <p className="text-gray-500 text-center py-8">No opportunities found. Markets are efficient.</p>
              )}
            </div>
          </div>

          {/* Price Slip Chart */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4">Gamma vs CLOB Price Sum</h2>
            <p className="text-xs text-gray-500 mb-4">
              Shows how indicative Gamma marks differ from executable CLOB prices
            </p>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={data.priceSlipHistory}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="time" tick={{ fill: '#9ca3af', fontSize: 10 }} />
                <YAxis domain={[0.94, 1.0]} tick={{ fill: '#9ca3af', fontSize: 10 }} />
                <Tooltip 
                  contentStyle={{ backgroundColor: '#1f2937', border: '1px solid #374151' }}
                  labelStyle={{ color: '#9ca3af' }}
                />
                <Line 
                  type="monotone" 
                  dataKey="gammaSum" 
                  stroke="#3b82f6" 
                  strokeWidth={2}
                  dot={false}
                  name="Gamma (indicative)"
                />
                <Line 
                  type="monotone" 
                  dataKey="clobSum" 
                  stroke="#ef4444" 
                  strokeWidth={2}
                  dot={false}
                  name="CLOB (executable)"
                />
              </LineChart>
            </ResponsiveContainer>
            <div className="flex justify-center gap-6 mt-2 text-xs">
              <span className="flex items-center gap-2">
                <div className="w-3 h-0.5 bg-blue-500" /> Gamma (indicative)
              </span>
              <span className="flex items-center gap-2">
                <div className="w-3 h-0.5 bg-red-500" /> CLOB (executable)
              </span>
            </div>
          </div>

          {/* Scan Activity */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4">Scan Activity</h2>
            <ResponsiveContainer width="100%" height={150}>
              <BarChart data={data.scanHistory.slice(-10)}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="timestamp" tick={false} />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 10 }} />
                <Tooltip 
                  contentStyle={{ backgroundColor: '#1f2937', border: '1px solid #374151' }}
                  labelFormatter={(v) => new Date(v).toLocaleTimeString()}
                />
                <Bar dataKey="gammaOpportunities" fill="#eab308" name="Gamma Opps" />
                <Bar dataKey="executable" fill="#22c55e" name="Executable" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Right Column - Funnel & Misses */}
        <div className="space-y-6">
          
          {/* Exclusion Funnel */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <AlertTriangle className="w-5 h-5 text-yellow-400" />
              Exclusion Funnel
            </h2>
            <p className="text-xs text-gray-500 mb-4">
              Why markets get filtered out ({totalExcluded} excluded)
            </p>
            <div className="space-y-2">
              {data.exclusionFunnel.map((item, i) => (
                <div key={i} className="flex items-center gap-2">
                  <div className="w-24 text-xs text-gray-400 truncate" title={item.reason}>
                    {item.reason.replace(/_/g, ' ')}
                  </div>
                  <div className="flex-1 bg-gray-700 rounded-full h-4 overflow-hidden">
                    <div 
                      className="h-full rounded-full transition-all duration-500"
                      style={{ 
                        width: `${item.pct}%`,
                        backgroundColor: COLORS[i % COLORS.length]
                      }}
                    />
                  </div>
                  <div className="w-12 text-xs text-right text-gray-400">
                    {item.pct.toFixed(1)}%
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Closest Misses */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Clock className="w-5 h-5 text-orange-400" />
              Closest Misses
            </h2>
            <p className="text-xs text-gray-500 mb-4">
              Markets just above threshold - watch during volatility
            </p>
            <div className="space-y-3">
              {data.closestMisses.map((miss, i) => (
                <div key={i} className="bg-gray-700/50 rounded p-3">
                  <p className="text-sm text-white truncate">{miss.slug}</p>
                  <div className="flex items-center justify-between mt-2 text-xs">
                    <span className="text-gray-400">Sum: ${miss.sum.toFixed(3)}</span>
                    <span className="text-orange-400">+{miss.gapBps}bps</span>
                    <span className="text-gray-400">{miss.minutes}m</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Trade History */}
          {mode !== 'scan' && (
            <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
              <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
                <DollarSign className="w-5 h-5 text-green-400" />
                Recent Trades
              </h2>
              <div className="space-y-2">
                {data.trades.map((trade, i) => (
                  <div key={i} className="flex items-center justify-between text-xs py-2 border-b border-gray-700 last:border-0">
                    <div>
                      <p className="text-white">{trade.market}</p>
                      <p className="text-gray-500">{trade.time}</p>
                    </div>
                    <div className="text-right">
                      <p className="text-gray-400">${trade.cost.toFixed(2)}</p>
                      <p className={trade.pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                        {trade.pnl >= 0 ? '+' : ''}{trade.pnl.toFixed(2)}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Config Summary */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
            <h2 className="text-lg font-semibold mb-4">Config</h2>
            <div className="space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-gray-400">Max Resolution</span>
                <span className="text-white">24 hours</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Min Volume 24h</span>
                <span className="text-white">$1,000</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Total Friction</span>
                <span className="text-white">3.0%</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Max Price Sum</span>
                <span className="text-white">$0.970</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Max Position</span>
                <span className="text-white">$50</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="mt-6 text-center text-xs text-gray-500">
        <p>⚠️ Research tool only. Gamma prices are indicative, not executable.</p>
        <p className="mt-1">For white paper data collection. Not financial advice.</p>
      </div>
    </div>
  );
}
