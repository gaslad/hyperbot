import { useState, useCallback } from "react";
import { Plus, X, TrendingUp, TrendingDown, Bell, ChevronRight, ChevronDown, AlertCircle, CheckCircle2, ArrowUpRight, ArrowDownRight, Settings, Power, Clock, Zap, Shield, BookOpen, AlertTriangle, Eye, Target, Minus } from "lucide-react";

// ── Mock Data ────────────────────────────────────────────────────────────
const AVAILABLE_TOKENS = [
  { symbol: "ETH", name: "Ethereum", icon: "\u039E" },
  { symbol: "BTC", name: "Bitcoin", icon: "\u20BF" },
  { symbol: "SOL", name: "Solana", icon: "\u25CE" },
  { symbol: "ARB", name: "Arbitrum", icon: "A" },
  { symbol: "DOGE", name: "Dogecoin", icon: "D" },
  { symbol: "AVAX", name: "Avalanche", icon: "Av" },
  { symbol: "LINK", name: "Chainlink", icon: "L" },
  { symbol: "MATIC", name: "Polygon", icon: "P" },
];

const STRATEGIES = [
  { id: "trend-follow", name: "Trend Follower", desc: "Rides momentum when multiple timeframes align in the same direction", risk: "Medium" },
  { id: "mean-revert", name: "Mean Reversion", desc: "Buys dips and sells rips when price stretches too far from average", risk: "Low" },
  { id: "breakout", name: "Breakout Hunter", desc: "Catches big moves when price breaks key support or resistance levels", risk: "High" },
];

const MOCK_ACTIVE_TRADES = [
  {
    id: "eth-1",
    symbol: "ETH", name: "Ethereum", icon: "\u039E",
    strategy: "Trend Follower",
    managed: true,
    status: "in_trade",
    statusLabel: "In a trade",
    direction: "LONG",
    entryPrice: 3420,
    currentPrice: 3485,
    stopLoss: 3380,
    takeProfit: 3560,
    pnl: +65,
    pnlPercent: +1.9,
    duration: "2h 14m",
    riskAmount: 50,
    leverage: 5,
  },
  {
    id: "sol-1",
    symbol: "SOL", name: "Solana", icon: "\u25CE",
    strategy: "Breakout Hunter",
    managed: true,
    status: "watching",
    statusLabel: "Watching for entry",
    direction: null,
    pnl: 0,
    pnlPercent: 0,
    lastTrade: { pnl: +32, time: "4h ago" },
  },
  {
    id: "btc-1",
    symbol: "BTC", name: "Bitcoin", icon: "\u20BF",
    strategy: "Mean Reversion",
    managed: true,
    status: "in_trade",
    statusLabel: "In a trade",
    direction: "SHORT",
    entryPrice: 67200,
    currentPrice: 66890,
    stopLoss: 67800,
    takeProfit: 66200,
    pnl: +31,
    pnlPercent: +0.46,
    duration: "45m",
    riskAmount: 100,
    leverage: 3,
  },
];

// Positions found on Hyperliquid that Hyperbot didn't open
const MOCK_UNMANAGED = [
  {
    id: "doge-ext",
    symbol: "DOGE", name: "Dogecoin", icon: "D",
    managed: false,
    status: "in_trade",
    direction: "LONG",
    entryPrice: 0.1342,
    currentPrice: 0.1298,
    stopLoss: null,
    takeProfit: null,
    pnl: -22,
    pnlPercent: -3.28,
    leverage: 10,
    size: 50000,
    rating: "poor",
    ratingLabel: "High risk",
    issues: [
      "No stop loss set \u2014 if DOGE drops further, losses are uncapped",
      "10x leverage on a meme coin is extremely aggressive",
      "Current trend is bearish on 1h and 4h timeframes",
    ],
    suggestions: [
      { action: "Add stop loss", detail: "Place at $0.1260 to cap losses at ~$41", type: "critical" },
      { action: "Reduce leverage", detail: "Lower to 3x to reduce liquidation risk", type: "warning" },
      { action: "Close position", detail: "Price is trending against you with no reversal signal yet", type: "info" },
    ],
  },
  {
    id: "arb-ext",
    symbol: "ARB", name: "Arbitrum", icon: "A",
    managed: false,
    status: "in_trade",
    direction: "LONG",
    entryPrice: 1.12,
    currentPrice: 1.18,
    stopLoss: 1.08,
    takeProfit: null,
    pnl: +15,
    pnlPercent: +5.36,
    leverage: 2,
    size: 250,
    rating: "decent",
    ratingLabel: "Looks reasonable",
    issues: [
      "No take-profit target \u2014 profits could evaporate on a reversal",
    ],
    suggestions: [
      { action: "Add take profit", detail: "Resistance at $1.24 would lock in +10.7%", type: "info" },
      { action: "Let Hyperbot manage", detail: "Assign the Trend Follower strategy to manage TP/SL automatically", type: "info" },
    ],
  },
];

const MOCK_NOTIFICATIONS = [
  {
    id: 1, time: "2m ago", type: "action", icon: "entry",
    title: "Moved stop loss to breakeven on ETH",
    why: "Price moved 1R in our favor ($3,470). The Trend Follower strategy locks in the entry price as a safety net once the trade is profitable enough \u2014 this way, the worst outcome is breaking even.",
    token: "ETH",
  },
  {
    id: 2, time: "18m ago", type: "action", icon: "entry",
    title: "Opened SHORT on BTC at $67,200",
    why: "The 4-hour chart shows price rejected a key resistance level at $67,500 twice. The Mean Reversion strategy sees this as overextended \u2014 when price fails to break higher after multiple attempts, it often pulls back.",
    token: "BTC",
  },
  {
    id: 3, time: "45m ago", type: "info", icon: "scan",
    title: "SOL is approaching a breakout zone",
    why: "Price is compressing into a tighter and tighter range near $142. The Breakout Hunter is watching for a decisive move above $143.50 or below $139 before entering.",
    token: "SOL",
  },
  {
    id: 4, time: "1h ago", type: "action", icon: "exit",
    title: "Closed SOL trade at $141.20 (+2.8%)",
    why: "Hit the take-profit target. This trade earned 2.1R \u2014 meaning the bot made 2.1\u00d7 what it risked. The Breakout Hunter captured the move from $137.40 to $141.20 as price broke above the previous consolidation range.",
    token: "SOL",
  },
  {
    id: 5, time: "2h ago", type: "action", icon: "entry",
    title: "Opened LONG on ETH at $3,420",
    why: "The 4-hour trend turned bullish and the 1-hour momentum confirmed the move. The Trend Follower waits for both timeframes to agree before entering \u2014 this reduces false signals.",
    token: "ETH",
  },
  {
    id: 6, time: "3h ago", type: "system", icon: "system",
    title: "Daily loss limit: 12% used ($60 of $500)",
    why: "The bot tracks how much it can lose in a single day. Once the limit is hit, all trading pauses automatically until the next day \u2014 this prevents a bad streak from doing serious damage.",
    token: null,
  },
  {
    id: 7, time: "3h ago", type: "warning", icon: "unmanaged",
    title: "Found 2 positions on your account not managed by Hyperbot",
    why: "These positions were opened outside of Hyperbot (likely on the Hyperliquid UI directly). The bot can review them and suggest improvements, but won't modify them unless you ask.",
    token: null,
  },
];

// ── Components ───────────────────────────────────────────────────────────

function Header({ equity, pnl, isRunning, notifCount, onToggleNotifs, unmanagedCount }) {
  return (
    <div className="flex items-center justify-between px-5 py-3 border-b border-white border-opacity-5" style={{ background: "#070707" }}>
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold" style={{ background: "#22c55e", color: "#000" }}>H</div>
          <span className="text-sm font-semibold text-white text-opacity-90">HYPERBOT</span>
        </div>
        <div className="flex items-center gap-1.5 px-2 py-0.5 rounded text-xs" style={{ background: isRunning ? "rgba(34,197,94,0.12)" : "rgba(255,255,255,0.06)", color: isRunning ? "#22c55e" : "#888" }}>
          <div className="w-1.5 h-1.5 rounded-full" style={{ background: isRunning ? "#22c55e" : "#555" }} />
          {isRunning ? "Live" : "Stopped"}
        </div>
        <div className="px-2 py-0.5 rounded text-xs" style={{ background: "rgba(59,130,246,0.12)", color: "#60a5fa" }}>
          Simulation
        </div>
      </div>

      <div className="flex items-center gap-6">
        <div className="text-right">
          <div className="text-xs text-white text-opacity-40">Equity</div>
          <div className="text-sm font-mono text-white">${equity.toLocaleString()}</div>
        </div>
        <div className="text-right">
          <div className="text-xs text-white text-opacity-40">Today</div>
          <div className={`text-sm font-mono ${pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
            {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)}%
          </div>
        </div>

        <button onClick={onToggleNotifs} className="relative p-2 rounded-lg hover:bg-white hover:bg-opacity-5 transition-colors">
          <Bell size={18} className="text-white text-opacity-50" />
          {notifCount > 0 && (
            <span className="absolute -top-0.5 -right-0.5 w-4 h-4 rounded-full text-xs flex items-center justify-center font-bold" style={{ background: "#22c55e", color: "#000", fontSize: 10 }}>
              {notifCount}
            </span>
          )}
        </button>

        <button className="p-2 rounded-lg hover:bg-white hover:bg-opacity-5 transition-colors">
          <Settings size={18} className="text-white text-opacity-50" />
        </button>

        <button className="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors" style={{ background: isRunning ? "rgba(239,68,68,0.15)" : "rgba(34,197,94,0.15)", color: isRunning ? "#ef4444" : "#22c55e" }}>
          <Power size={14} className="inline mr-1" style={{ marginTop: -2 }} />
          {isRunning ? "Stop" : "Start"}
        </button>
      </div>
    </div>
  );
}

// ── Expanded card detail panel (TP/SL controls, close trade) ─────────
function TradeControls({ trade, onClose, onUpdateSl, onUpdateTp, onCloseTrade }) {
  const isLong = trade.direction === "LONG";

  return (
    <div className="mt-3 pt-3 border-t border-white border-opacity-5 flex flex-col gap-3" onClick={(e) => e.stopPropagation()}>
      {/* Entry / Current / Leverage row */}
      <div className="grid grid-cols-3 gap-2">
        <div className="p-2 rounded-lg" style={{ background: "rgba(255,255,255,0.03)" }}>
          <div className="text-xs text-white text-opacity-30 mb-0.5">Entry</div>
          <div className="text-xs font-mono text-white text-opacity-70">${trade.entryPrice?.toLocaleString()}</div>
        </div>
        <div className="p-2 rounded-lg" style={{ background: "rgba(255,255,255,0.03)" }}>
          <div className="text-xs text-white text-opacity-30 mb-0.5">Mark</div>
          <div className="text-xs font-mono text-white text-opacity-70">${trade.currentPrice?.toLocaleString()}</div>
        </div>
        <div className="p-2 rounded-lg" style={{ background: "rgba(255,255,255,0.03)" }}>
          <div className="text-xs text-white text-opacity-30 mb-0.5">Leverage</div>
          <div className="text-xs font-mono text-white text-opacity-70">{trade.leverage}x</div>
        </div>
      </div>

      {/* SL / TP controls */}
      <div className="flex gap-2">
        <div className="flex-1 p-2.5 rounded-lg" style={{ background: "rgba(239,68,68,0.06)", border: "1px solid rgba(239,68,68,0.12)" }}>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-red-400 text-opacity-80">Stop Loss</span>
            <Shield size={10} className="text-red-400 text-opacity-50" />
          </div>
          <div className="text-sm font-mono text-red-400">${trade.stopLoss?.toLocaleString() || "None"}</div>
          <div className="flex gap-1 mt-2">
            <button onClick={() => onUpdateSl("tighter")} className="flex-1 py-1 rounded text-xs text-red-400 text-opacity-60 hover:text-opacity-100 transition-colors" style={{ background: "rgba(239,68,68,0.08)" }}>Tighter</button>
            <button onClick={() => onUpdateSl("wider")} className="flex-1 py-1 rounded text-xs text-red-400 text-opacity-60 hover:text-opacity-100 transition-colors" style={{ background: "rgba(239,68,68,0.08)" }}>Wider</button>
          </div>
        </div>
        <div className="flex-1 p-2.5 rounded-lg" style={{ background: "rgba(34,197,94,0.06)", border: "1px solid rgba(34,197,94,0.12)" }}>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-green-400 text-opacity-80">Take Profit</span>
            <Target size={10} className="text-green-400 text-opacity-50" />
          </div>
          <div className="text-sm font-mono text-green-400">${trade.takeProfit?.toLocaleString() || "None"}</div>
          <div className="flex gap-1 mt-2">
            <button onClick={() => onUpdateTp("closer")} className="flex-1 py-1 rounded text-xs text-green-400 text-opacity-60 hover:text-opacity-100 transition-colors" style={{ background: "rgba(34,197,94,0.08)" }}>Closer</button>
            <button onClick={() => onUpdateTp("further")} className="flex-1 py-1 rounded text-xs text-green-400 text-opacity-60 hover:text-opacity-100 transition-colors" style={{ background: "rgba(34,197,94,0.08)" }}>Further</button>
          </div>
        </div>
      </div>

      {/* Close trade button */}
      <button
        onClick={onCloseTrade}
        className="w-full py-2 rounded-lg text-xs font-medium transition-colors"
        style={{ background: "rgba(239,68,68,0.1)", color: "#ef4444", border: "1px solid rgba(239,68,68,0.15)" }}
      >
        Close Position
      </button>
    </div>
  );
}

function TradeCard({ trade, isExpanded, onToggle }) {
  const inTrade = trade.status === "in_trade";
  const isLong = trade.direction === "LONG";

  return (
    <div
      onClick={inTrade ? onToggle : undefined}
      className="rounded-xl p-4 flex flex-col gap-3 transition-all"
      style={{
        background: "#0c0c0c",
        border: isExpanded ? "1px solid rgba(255,255,255,0.12)" : "1px solid rgba(255,255,255,0.06)",
        cursor: inTrade ? "pointer" : "default",
      }}
    >
      {/* Token header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-full flex items-center justify-center text-lg" style={{ background: "rgba(255,255,255,0.06)" }}>
            {trade.icon}
          </div>
          <div>
            <div className="text-sm font-semibold text-white">{trade.symbol}</div>
            <div className="text-xs text-white text-opacity-35">{trade.strategy}</div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {inTrade && (
            <ChevronDown
              size={14}
              className="text-white text-opacity-20 transition-transform"
              style={{ transform: isExpanded ? "rotate(180deg)" : "rotate(0deg)" }}
            />
          )}
          <button onClick={(e) => { e.stopPropagation(); }} className="p-1 rounded hover:bg-white hover:bg-opacity-5 text-white text-opacity-20 hover:text-opacity-50 transition-colors">
            <X size={14} />
          </button>
        </div>
      </div>

      {/* Status badge */}
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs" style={{
          background: inTrade
            ? (isLong ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)")
            : "rgba(255,255,255,0.04)",
          color: inTrade
            ? (isLong ? "#22c55e" : "#ef4444")
            : "rgba(255,255,255,0.5)"
        }}>
          {inTrade ? (
            isLong ? <TrendingUp size={12} /> : <TrendingDown size={12} />
          ) : (
            <Clock size={12} />
          )}
          {inTrade ? `${trade.direction} \u00b7 ${trade.duration}` : trade.statusLabel}
        </div>
      </div>

      {/* P&L or last trade */}
      {inTrade ? (
        <div className="flex items-center justify-between pt-1 border-t border-white border-opacity-5">
          <div>
            <div className="text-xs text-white text-opacity-35">Unrealized P&L</div>
            <div className={`text-lg font-mono font-semibold ${trade.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
              {trade.pnl >= 0 ? "+" : ""}${Math.abs(trade.pnl).toFixed(0)}
              <span className="text-xs ml-1 font-normal text-opacity-60">
                ({trade.pnlPercent >= 0 ? "+" : ""}{trade.pnlPercent.toFixed(1)}%)
              </span>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs text-white text-opacity-35">Risking</div>
            <div className="text-sm font-mono text-white text-opacity-60">${trade.riskAmount}</div>
          </div>
        </div>
      ) : (
        <div className="pt-1 border-t border-white border-opacity-5">
          {trade.lastTrade ? (
            <div className="flex items-center gap-1.5 text-xs text-white text-opacity-40">
              <CheckCircle2 size={12} className="text-green-400" />
              Last trade: <span className="text-green-400 font-mono">+${trade.lastTrade.pnl}</span> \u00b7 {trade.lastTrade.time}
            </div>
          ) : (
            <div className="text-xs text-white text-opacity-30">No trades yet</div>
          )}
        </div>
      )}

      {/* Expanded controls */}
      {isExpanded && inTrade && (
        <TradeControls
          trade={trade}
          onUpdateSl={(dir) => console.log("SL", dir)}
          onUpdateTp={(dir) => console.log("TP", dir)}
          onCloseTrade={() => console.log("Close", trade.symbol)}
        />
      )}
    </div>
  );
}

// ── Unmanaged Position Card ──────────────────────────────────────────────
function UnmanagedCard({ position, isExpanded, onToggle }) {
  const isLong = position.direction === "LONG";
  const ratingColors = {
    poor: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.2)", text: "#ef4444", label: "High Risk" },
    decent: { bg: "rgba(234,179,8,0.08)", border: "rgba(234,179,8,0.2)", text: "#eab308", label: "Needs Attention" },
    good: { bg: "rgba(34,197,94,0.08)", border: "rgba(34,197,94,0.2)", text: "#22c55e", label: "Looks Good" },
  };
  const rc = ratingColors[position.rating] || ratingColors.decent;

  return (
    <div
      onClick={onToggle}
      className="rounded-xl p-4 flex flex-col gap-3 transition-all cursor-pointer"
      style={{
        background: "#0c0c0c",
        border: isExpanded ? `1px solid ${rc.border}` : "1px solid rgba(255,255,255,0.06)",
        borderLeft: `3px solid ${rc.text}`,
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-full flex items-center justify-center text-lg" style={{ background: "rgba(255,255,255,0.06)" }}>
            {position.icon}
          </div>
          <div>
            <div className="text-sm font-semibold text-white">{position.symbol}</div>
            <div className="flex items-center gap-1.5">
              <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: rc.bg, color: rc.text, fontSize: 10 }}>
                Unmanaged
              </span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 px-2 py-0.5 rounded text-xs" style={{ background: rc.bg, color: rc.text }}>
            <AlertTriangle size={10} />
            {rc.label}
          </div>
          <ChevronDown
            size={14}
            className="text-white text-opacity-20 transition-transform"
            style={{ transform: isExpanded ? "rotate(180deg)" : "rotate(0deg)" }}
          />
        </div>
      </div>

      {/* Direction + P&L */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs" style={{
          background: isLong ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
          color: isLong ? "#22c55e" : "#ef4444"
        }}>
          {isLong ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          {position.direction} \u00b7 {position.leverage}x \u00b7 {position.size?.toLocaleString()} {position.symbol}
        </div>
        <div className={`text-sm font-mono font-semibold ${position.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
          {position.pnl >= 0 ? "+" : ""}${Math.abs(position.pnl).toFixed(0)}
          <span className="text-xs ml-1 font-normal text-opacity-60">
            ({position.pnlPercent >= 0 ? "+" : ""}{position.pnlPercent.toFixed(1)}%)
          </span>
        </div>
      </div>

      {/* Expanded: Issues + Suggestions */}
      {isExpanded && (
        <div className="mt-1 flex flex-col gap-3" onClick={(e) => e.stopPropagation()}>
          {/* Issues */}
          <div className="p-3 rounded-lg" style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.05)" }}>
            <div className="flex items-center gap-1.5 mb-2">
              <Eye size={11} className="text-white text-opacity-40" />
              <span className="text-xs font-medium text-white text-opacity-50">What we see</span>
            </div>
            <div className="flex flex-col gap-1.5">
              {position.issues.map((issue, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-white text-opacity-45 leading-relaxed">
                  <Minus size={8} className="mt-1 flex-shrink-0 text-white text-opacity-20" />
                  {issue}
                </div>
              ))}
            </div>
          </div>

          {/* Suggestions */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-1.5 px-1">
              <Zap size={11} className="text-white text-opacity-40" />
              <span className="text-xs font-medium text-white text-opacity-50">Suggested actions</span>
            </div>
            {position.suggestions.map((s, i) => {
              const sColors = {
                critical: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.15)", accent: "#ef4444" },
                warning: { bg: "rgba(234,179,8,0.08)", border: "rgba(234,179,8,0.15)", accent: "#eab308" },
                info: { bg: "rgba(59,130,246,0.08)", border: "rgba(59,130,246,0.15)", accent: "#60a5fa" },
              };
              const sc = sColors[s.type] || sColors.info;
              return (
                <button
                  key={i}
                  className="w-full text-left p-2.5 rounded-lg flex items-center justify-between transition-all hover:brightness-110"
                  style={{ background: sc.bg, border: `1px solid ${sc.border}` }}
                >
                  <div>
                    <div className="text-xs font-medium" style={{ color: sc.accent }}>{s.action}</div>
                    <div className="text-xs text-white text-opacity-40 mt-0.5">{s.detail}</div>
                  </div>
                  <ChevronRight size={14} style={{ color: sc.accent, opacity: 0.5 }} />
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function AddTokenCard({ onClick }) {
  return (
    <button
      onClick={onClick}
      className="rounded-xl p-4 flex flex-col items-center justify-center gap-2 transition-all cursor-pointer group"
      style={{
        background: "transparent",
        border: "1px dashed rgba(255,255,255,0.1)",
        minHeight: 160,
      }}
    >
      <div className="w-10 h-10 rounded-full flex items-center justify-center transition-colors" style={{ background: "rgba(255,255,255,0.04)" }}>
        <Plus size={20} className="text-white text-opacity-30 group-hover:text-opacity-60 transition-colors" />
      </div>
      <span className="text-xs text-white text-opacity-30 group-hover:text-opacity-50 transition-colors">Add token</span>
    </button>
  );
}

function AddTokenModal({ onClose, onAdd, activeTrades }) {
  const [selected, setSelected] = useState(null);
  const [strategy, setStrategy] = useState(null);
  const activeSymbols = activeTrades.map(t => t.symbol);
  const available = AVAILABLE_TOKENS.filter(t => !activeSymbols.includes(t.symbol));

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50" style={{ background: "rgba(0,0,0,0.7)", backdropFilter: "blur(8px)" }}>
      <div className="rounded-2xl p-6 w-full max-w-md" style={{ background: "#111", border: "1px solid rgba(255,255,255,0.08)" }}>
        {!selected ? (
          <>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-white">Choose a token</h3>
              <button onClick={onClose} className="p-1 rounded hover:bg-white hover:bg-opacity-5">
                <X size={16} className="text-white text-opacity-40" />
              </button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              {available.map(token => (
                <button
                  key={token.symbol}
                  onClick={() => setSelected(token)}
                  className="flex items-center gap-3 p-3 rounded-xl hover:bg-white hover:bg-opacity-5 transition-colors text-left"
                  style={{ border: "1px solid rgba(255,255,255,0.06)" }}
                >
                  <div className="w-8 h-8 rounded-full flex items-center justify-center text-base" style={{ background: "rgba(255,255,255,0.06)" }}>
                    {token.icon}
                  </div>
                  <div>
                    <div className="text-sm font-medium text-white">{token.symbol}</div>
                    <div className="text-xs text-white text-opacity-35">{token.name}</div>
                  </div>
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-base font-semibold text-white">Pick a strategy for {selected.symbol}</h3>
              <button onClick={onClose} className="p-1 rounded hover:bg-white hover:bg-opacity-5">
                <X size={16} className="text-white text-opacity-40" />
              </button>
            </div>
            <p className="text-xs text-white text-opacity-35 mb-4">Each strategy has a different approach to finding trades.</p>
            <div className="flex flex-col gap-2">
              {STRATEGIES.map(s => (
                <button
                  key={s.id}
                  onClick={() => {
                    setStrategy(s);
                    onAdd({
                      id: `${selected.symbol.toLowerCase()}-new`,
                      ...selected, strategy: s.name, managed: true,
                      status: "watching", statusLabel: "Watching for entry",
                      direction: null, pnl: 0, pnlPercent: 0,
                    });
                    onClose();
                  }}
                  className="flex flex-col gap-1 p-3 rounded-xl hover:bg-white hover:bg-opacity-5 transition-colors text-left"
                  style={{ border: "1px solid rgba(255,255,255,0.06)" }}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-white">{s.name}</span>
                    <span className="text-xs px-1.5 py-0.5 rounded" style={{
                      background: s.risk === "Low" ? "rgba(34,197,94,0.1)" : s.risk === "Medium" ? "rgba(234,179,8,0.1)" : "rgba(239,68,68,0.1)",
                      color: s.risk === "Low" ? "#22c55e" : s.risk === "Medium" ? "#eab308" : "#ef4444"
                    }}>
                      {s.risk} risk
                    </span>
                  </div>
                  <span className="text-xs text-white text-opacity-40 leading-relaxed">{s.desc}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function NotificationIcon({ type }) {
  if (type === "exit") return <ArrowDownRight size={14} />;
  if (type === "entry") return <ArrowUpRight size={14} />;
  if (type === "scan") return <Zap size={14} />;
  if (type === "system") return <Shield size={14} />;
  if (type === "unmanaged") return <AlertTriangle size={14} />;
  return <Bell size={14} />;
}

function NotificationPanel({ notifications, isOpen, onClose }) {
  const [expandedId, setExpandedId] = useState(null);

  return (
    <div
      className="fixed top-0 right-0 h-full z-40 transition-transform duration-300 ease-in-out flex flex-col"
      style={{
        width: 380,
        background: "#0a0a0a",
        borderLeft: "1px solid rgba(255,255,255,0.06)",
        transform: isOpen ? "translateX(0)" : "translateX(100%)",
      }}
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-white border-opacity-5">
        <div className="flex items-center gap-2">
          <BookOpen size={16} className="text-white text-opacity-50" />
          <span className="text-sm font-semibold text-white">Activity & Insights</span>
        </div>
        <button onClick={onClose} className="p-1 rounded hover:bg-white hover:bg-opacity-5">
          <X size={16} className="text-white text-opacity-40" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-1">
        {notifications.map(n => {
          const isExpanded = expandedId === n.id;
          const colorMap = {
            action: { bg: "rgba(34,197,94,0.06)", accent: "#22c55e" },
            info: { bg: "rgba(59,130,246,0.06)", accent: "#60a5fa" },
            system: { bg: "rgba(234,179,8,0.06)", accent: "#eab308" },
            warning: { bg: "rgba(239,68,68,0.06)", accent: "#ef4444" },
          };
          const colors = colorMap[n.type] || colorMap.info;

          return (
            <button
              key={n.id}
              onClick={() => setExpandedId(isExpanded ? null : n.id)}
              className="w-full text-left rounded-xl p-3 transition-all"
              style={{ background: isExpanded ? colors.bg : "transparent" }}
            >
              <div className="flex items-start gap-2.5">
                <div className="w-6 h-6 rounded-full flex items-center justify-center mt-0.5 flex-shrink-0" style={{ background: colors.bg, color: colors.accent }}>
                  <NotificationIcon type={n.icon} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-white text-opacity-80 leading-snug">{n.title}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className="text-xs text-white text-opacity-25">{n.time}</span>
                    {n.token && <span className="text-xs text-white text-opacity-25">\u00b7 {n.token}</span>}
                  </div>

                  {isExpanded && (
                    <div className="mt-2.5 p-2.5 rounded-lg" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.05)" }}>
                      <div className="flex items-center gap-1 mb-1.5">
                        <BookOpen size={10} style={{ color: colors.accent }} />
                        <span className="text-xs font-medium" style={{ color: colors.accent }}>Why this happened</span>
                      </div>
                      <p className="text-xs text-white text-opacity-50 leading-relaxed">{n.why}</p>
                    </div>
                  )}
                </div>
                <ChevronRight
                  size={14}
                  className="text-white text-opacity-15 mt-0.5 flex-shrink-0 transition-transform"
                  style={{ transform: isExpanded ? "rotate(90deg)" : "rotate(0deg)" }}
                />
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function PerformanceSummary({ trades, unmanaged }) {
  const allPositions = [...trades, ...unmanaged];
  const totalPnl = allPositions.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const activeTrades = trades.filter(t => t.status === "in_trade").length;
  const watchingCount = trades.filter(t => t.status === "watching").length;

  return (
    <div className="flex items-center justify-between px-5 py-2.5 border-b border-white border-opacity-5" style={{ background: "#080808" }}>
      <div className="flex items-center gap-4 text-xs">
        <div className="flex items-center gap-1.5 text-white text-opacity-40">
          <div className="w-1.5 h-1.5 rounded-full bg-green-400" />
          <span>{activeTrades} active</span>
        </div>
        <div className="flex items-center gap-1.5 text-white text-opacity-40">
          <div className="w-1.5 h-1.5 rounded-full bg-blue-400" />
          <span>{watchingCount} watching</span>
        </div>
        {unmanaged.length > 0 && (
          <div className="flex items-center gap-1.5 text-white text-opacity-40">
            <div className="w-1.5 h-1.5 rounded-full bg-yellow-400" />
            <span>{unmanaged.length} unmanaged</span>
          </div>
        )}
        <div className="text-white text-opacity-20">|</div>
        <div className="flex items-center gap-1.5 text-white text-opacity-40">
          Open P&L:
          <span className={`font-mono ${totalPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
            {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(0)}
          </span>
        </div>
      </div>
    </div>
  );
}

// ── Main Dashboard ───────────────────────────────────────────────────────

export default function HyperbotDashboard() {
  const [trades, setTrades] = useState(MOCK_ACTIVE_TRADES);
  const [unmanaged] = useState(MOCK_UNMANAGED);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showNotifs, setShowNotifs] = useState(false);
  const [expandedCard, setExpandedCard] = useState(null);

  const handleAddToken = (token) => {
    setTrades(prev => [...prev, token]);
  };

  const toggleCard = useCallback((id) => {
    setExpandedCard(prev => prev === id ? null : id);
  }, []);

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "#050505", color: "#fff", fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" }}>
      <Header
        equity={10245}
        pnl={1.85}
        isRunning={true}
        notifCount={3}
        unmanagedCount={unmanaged.length}
        onToggleNotifs={() => setShowNotifs(!showNotifs)}
      />

      <PerformanceSummary trades={trades} unmanaged={unmanaged} />

      {/* Card Grid */}
      <div className="flex-1 p-5 overflow-y-auto">
        {/* Managed tokens */}
        <div className="grid gap-4 mb-6" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
          {trades.map(trade => (
            <TradeCard
              key={trade.id}
              trade={trade}
              isExpanded={expandedCard === trade.id}
              onToggle={() => toggleCard(trade.id)}
            />
          ))}
          <AddTokenCard onClick={() => setShowAddModal(true)} />
        </div>

        {/* Unmanaged positions section */}
        {unmanaged.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-3 px-1">
              <AlertTriangle size={13} className="text-yellow-400 text-opacity-60" />
              <span className="text-xs font-medium text-white text-opacity-40">Positions found on your account (not managed by Hyperbot)</span>
            </div>
            <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
              {unmanaged.map(pos => (
                <UnmanagedCard
                  key={pos.id}
                  position={pos}
                  isExpanded={expandedCard === pos.id}
                  onToggle={() => toggleCard(pos.id)}
                />
              ))}
            </div>
          </div>
        )}

        {/* Empty state */}
        {trades.length === 0 && unmanaged.length === 0 && (
          <div className="flex flex-col items-center justify-center mt-16 gap-3">
            <div className="w-14 h-14 rounded-2xl flex items-center justify-center" style={{ background: "rgba(255,255,255,0.04)" }}>
              <Plus size={24} className="text-white text-opacity-20" />
            </div>
            <div className="text-center">
              <div className="text-sm text-white text-opacity-50 mb-1">No tokens yet</div>
              <div className="text-xs text-white text-opacity-25 max-w-xs">Pick a token and a strategy to get started. The bot will watch for opportunities and trade automatically.</div>
            </div>
          </div>
        )}
      </div>

      {/* Notification Panel */}
      <NotificationPanel
        notifications={MOCK_NOTIFICATIONS}
        isOpen={showNotifs}
        onClose={() => setShowNotifs(false)}
      />

      {/* Modal */}
      {showAddModal && (
        <AddTokenModal
          onClose={() => setShowAddModal(false)}
          onAdd={handleAddToken}
          activeTrades={[...trades, ...unmanaged]}
        />
      )}
    </div>
  );
}