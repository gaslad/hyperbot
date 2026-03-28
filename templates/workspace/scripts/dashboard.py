#!/usr/bin/env python3
"""Hyperbot Workspace Dashboard.

Local web interface for monitoring and controlling trading.
Run from a generated workspace directory:

    python3 scripts/dashboard.py              # view-only mode
    python3 scripts/dashboard.py --live --confirm-risk   # live trading

Opens a browser to a local dashboard showing:
- Live price and chart
- Strategy signals in real-time
- Open positions and P&L
- Trade history
- Start/Stop controls (when --live is enabled)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Any

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hl_client
import signals

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

class TradingState:
    def __init__(self) -> None:
        self.live_enabled = False
        self.trading_active = False
        self.last_signals: list[dict] = []
        self.last_price: float | None = None
        self.last_update: str = ""
        self.positions: list[dict] = []
        self.equity: float = 0.0
        self.pnl: float = 0.0
        self.trade_log: list[dict] = []
        self.error: str | None = None
        self.coin: str = ""
        self.symbol: str = ""
        self.daily_loss: float = 0.0
        self.max_daily_loss: float = 100.0  # USD, from policy

    def to_dict(self) -> dict:
        return {
            "live_enabled": self.live_enabled,
            "trading_active": self.trading_active,
            "last_signals": self.last_signals,
            "last_price": self.last_price,
            "last_update": self.last_update,
            "positions": self.positions,
            "equity": self.equity,
            "pnl": self.pnl,
            "trade_log": self.trade_log[-50:],
            "error": self.error,
            "coin": self.coin,
            "symbol": self.symbol,
            "daily_loss": self.daily_loss,
            "max_daily_loss": self.max_daily_loss,
        }


STATE = TradingState()
STOP_EVENT = threading.Event()


# ---------------------------------------------------------------------------
# Trading loop
# ---------------------------------------------------------------------------

def load_policy() -> dict:
    if POLICY_PATH.exists():
        return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    return {}


def trading_loop() -> None:
    """Main loop: fetch data, detect signals, optionally execute trades."""
    workspace = hl_client.load_workspace()
    STATE.symbol = workspace.get("symbol", "BTCUSDT")
    STATE.coin = hl_client.infer_coin(STATE.symbol)

    policy = load_policy()
    STATE.max_daily_loss = policy.get("auto_apply", {}).get("safe_bands", {}).get("max_daily_loss_usd", 100.0)
    max_leverage = policy.get("auto_apply", {}).get("safe_bands", {}).get("leverage_max", 4.0)
    risk_per_trade_pct = policy.get("auto_apply", {}).get("safe_bands", {}).get("risk_per_trade_pct_max", 1.0)

    creds = hl_client.get_credentials()
    master_address = creds.get("master_address")

    print(f"[dashboard] Monitoring {STATE.coin} ({STATE.symbol})", flush=True)
    print(f"[dashboard] Live trading: {'ENABLED' if STATE.live_enabled else 'disabled (view-only)'}", flush=True)

    while not STOP_EVENT.is_set():
        try:
            # Fetch current price
            price = hl_client.get_mid_price(STATE.coin)
            if price:
                STATE.last_price = price

            # Fetch candles for signal detection
            candles_1d = hl_client.get_candles(STATE.coin, "1d", 30)
            candles_4h = hl_client.get_candles(STATE.coin, "4h", 14)

            # Detect signals
            sigs = signals.detect_all_signals(candles_1d, candles_4h, price or 0.0)
            STATE.last_signals = [
                {
                    "direction": s.direction.value,
                    "strategy_id": s.strategy_id,
                    "pack_id": s.pack_id,
                    "confidence": s.confidence,
                    "reasons": s.reasons,
                    "entry_price": s.entry_price,
                    "stop_loss": s.stop_loss,
                    "take_profit": s.take_profit,
                }
                for s in sigs
            ]

            # Fetch account state
            if master_address:
                try:
                    ch_state = hl_client.get_clearinghouse_state(master_address)
                    margin = ch_state.get("marginSummary", {})
                    STATE.equity = float(margin.get("accountValue", 0))
                    STATE.pnl = float(margin.get("totalUnrealizedPnl", 0))
                    STATE.positions = [
                        {
                            "coin": p["position"]["coin"],
                            "size": p["position"]["szi"],
                            "entry_price": p["position"]["entryPx"],
                            "unrealized_pnl": p["position"]["unrealizedPnl"],
                            "leverage": p["position"].get("leverage", {}).get("value", "?"),
                        }
                        for p in ch_state.get("assetPositions", [])
                        if float(p["position"]["szi"]) != 0
                    ]
                except Exception:
                    pass

            STATE.last_update = time.strftime("%H:%M:%S")
            STATE.error = None

            # Execute trades if live and active
            if STATE.live_enabled and STATE.trading_active:
                # Daily loss circuit breaker
                if STATE.daily_loss >= STATE.max_daily_loss:
                    STATE.trading_active = False
                    STATE.error = f"Daily loss limit reached (${STATE.daily_loss:.2f} >= ${STATE.max_daily_loss:.2f}). Trading halted."
                    log_trade("HALT", "system", 0, 0, "daily loss limit reached")
                    continue

                for sig_data, sig_obj in zip(STATE.last_signals, sigs):
                    if sig_obj.direction == signals.Direction.NONE:
                        continue
                    if sig_obj.confidence < 0.5:
                        continue

                    # Position sizing: risk_per_trade_pct of equity
                    if STATE.equity <= 0 or not sig_obj.stop_loss:
                        continue

                    risk_amount = STATE.equity * (risk_per_trade_pct / 100)
                    price_risk = abs(price - sig_obj.stop_loss) if price else 0
                    if price_risk <= 0:
                        continue

                    size = risk_amount / price_risk
                    # Cap by leverage
                    max_notional = STATE.equity * max_leverage
                    max_size = max_notional / price if price else 0
                    size = min(size, max_size)

                    if size <= 0:
                        continue

                    is_buy = sig_obj.direction == signals.Direction.BUY
                    log_trade(
                        "BUY" if is_buy else "SELL",
                        sig_obj.strategy_id,
                        size, price or 0,
                        f"confidence={sig_obj.confidence:.2f}, SL={sig_obj.stop_loss:.2f}",
                    )

                    result = hl_client.place_order(
                        STATE.coin, is_buy, size,
                        order_type="market",
                    )

                    if result.ok:
                        log_trade("FILLED", sig_obj.strategy_id, size, price or 0, f"oid={result.order_id}")
                    else:
                        log_trade("REJECTED", sig_obj.strategy_id, size, price or 0, result.error or "unknown")

        except Exception as e:
            STATE.error = str(e)
            traceback.print_exc()

        # Poll interval: 15 seconds
        STOP_EVENT.wait(15)


def log_trade(action: str, strategy: str, size: float, price: float, note: str = "") -> None:
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "strategy": strategy,
        "size": round(size, 6),
        "price": round(price, 2),
        "note": note,
    }
    STATE.trade_log.append(entry)
    print(f"  [{action}] {strategy} size={size:.6f} price={price:.2f} {note}", flush=True)


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hyperbot Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0f; color: #e0e0e0; }
.header { display: flex; justify-content: space-between; align-items: center; padding: 1rem 2rem; border-bottom: 1px solid #1a1a2a; }
.logo { font-size: 1.3rem; font-weight: 700; color: #4ade80; }
.price-display { font-size: 1.8rem; font-weight: 700; font-family: 'SF Mono', monospace; }
.price-up { color: #4ade80; }
.price-down { color: #f87171; }
.status-badge { padding: 0.3rem 0.8rem; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
.status-live { background: #1a3a1a; color: #4ade80; border: 1px solid #2a5a2a; }
.status-view { background: #1a1a3a; color: #60a5fa; border: 1px solid #2a2a5a; }
.status-halted { background: #3a1a1a; color: #f87171; border: 1px solid #5a2a2a; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; padding: 1.5rem 2rem; }
.card { background: #14141f; border: 1px solid #2a2a3a; border-radius: 10px; padding: 1.25rem; }
.card h3 { font-size: 0.85rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 1rem; }
.full-width { grid-column: 1 / -1; }
.signal-row { display: flex; justify-content: space-between; align-items: center; padding: 0.6rem 0; border-bottom: 1px solid #1a1a2a; }
.signal-row:last-child { border: none; }
.signal-dir { font-weight: 700; font-size: 0.85rem; padding: 0.2rem 0.6rem; border-radius: 4px; }
.dir-buy { color: #4ade80; background: #1a3a1a; }
.dir-sell { color: #f87171; background: #3a1a1a; }
.dir-none { color: #888; background: #1a1a1a; }
.signal-name { font-size: 0.85rem; color: #ccc; }
.signal-conf { font-size: 0.8rem; color: #888; font-family: monospace; }
.signal-reasons { font-size: 0.75rem; color: #666; margin-top: 0.3rem; }
.pos-row { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid #1a1a2a; font-size: 0.85rem; }
.pos-row:last-child { border: none; }
.equity-big { font-size: 2rem; font-weight: 700; font-family: monospace; color: #fff; }
.pnl { font-size: 1rem; margin-top: 0.25rem; }
.pnl-pos { color: #4ade80; }
.pnl-neg { color: #f87171; }
.log-entry { font-size: 0.8rem; font-family: monospace; padding: 0.3rem 0; color: #aaa; border-bottom: 1px solid #0a0a15; }
.controls { display: flex; gap: 0.75rem; align-items: center; }
.btn { padding: 0.5rem 1.2rem; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 0.85rem; }
.btn-start { background: #4ade80; color: #0a0a0f; }
.btn-start:hover { background: #22c55e; }
.btn-stop { background: #f87171; color: #fff; }
.btn-stop:hover { background: #ef4444; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.error-bar { background: #2a1515; border: 1px solid #5a2020; color: #f87171; padding: 0.75rem 2rem; font-size: 0.85rem; }
.updated { font-size: 0.75rem; color: #555; }
.metric { display: flex; justify-content: space-between; padding: 0.4rem 0; font-size: 0.85rem; }
.metric-label { color: #888; }
.metric-value { color: #ccc; font-family: monospace; }
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:1rem">
    <span class="logo">Hyperbot</span>
    <span id="symbol" style="color:#888;font-size:0.9rem"></span>
    <span id="status-badge" class="status-badge status-view">VIEW ONLY</span>
  </div>
  <div style="display:flex;align-items:center;gap:1.5rem">
    <div id="controls" class="controls" style="display:none">
      <button id="btn-start" class="btn btn-start" onclick="startTrading()">Start Trading</button>
      <button id="btn-stop" class="btn btn-stop" onclick="stopTrading()" disabled>Stop</button>
    </div>
    <div>
      <div id="price" class="price-display">—</div>
      <div id="updated" class="updated"></div>
    </div>
  </div>
</div>
<div id="error-bar" class="error-bar" style="display:none"></div>

<div class="grid">
  <div class="card">
    <h3>Account</h3>
    <div id="equity" class="equity-big">$—</div>
    <div id="pnl" class="pnl">—</div>
    <div style="margin-top:1rem">
      <div class="metric"><span class="metric-label">Daily P&L Limit</span><span id="daily-limit" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Daily Loss</span><span id="daily-loss" class="metric-value">—</span></div>
    </div>
  </div>

  <div class="card">
    <h3>Positions</h3>
    <div id="positions"><span style="color:#555;font-size:0.85rem">No open positions</span></div>
  </div>

  <div class="card full-width">
    <h3>Strategy Signals</h3>
    <div id="signals"><span style="color:#555;font-size:0.85rem">Waiting for data...</span></div>
  </div>

  <div class="card full-width">
    <h3>Trade Log</h3>
    <div id="trade-log" style="max-height:200px;overflow-y:auto"><span style="color:#555;font-size:0.85rem">No trades yet</span></div>
  </div>
</div>

<script>
let prevPrice = null;

async function poll() {
  try {
    const resp = await fetch('/api/state');
    const s = await resp.json();

    // Symbol
    document.getElementById('symbol').textContent = s.symbol;

    // Status badge
    const badge = document.getElementById('status-badge');
    const controls = document.getElementById('controls');
    if (s.live_enabled) {
      controls.style.display = 'flex';
      if (s.trading_active) {
        badge.className = 'status-badge status-live';
        badge.textContent = 'LIVE';
        document.getElementById('btn-start').disabled = true;
        document.getElementById('btn-stop').disabled = false;
      } else {
        badge.className = 'status-badge status-view';
        badge.textContent = 'READY';
        document.getElementById('btn-start').disabled = false;
        document.getElementById('btn-stop').disabled = true;
      }
    }

    // Price
    if (s.last_price) {
      const el = document.getElementById('price');
      el.textContent = '$' + s.last_price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
      el.className = 'price-display ' + (prevPrice && s.last_price >= prevPrice ? 'price-up' : 'price-down');
      prevPrice = s.last_price;
    }
    document.getElementById('updated').textContent = s.last_update ? 'Updated ' + s.last_update : '';

    // Error
    const errBar = document.getElementById('error-bar');
    if (s.error) { errBar.textContent = s.error; errBar.style.display = 'block'; }
    else { errBar.style.display = 'none'; }

    // Account
    document.getElementById('equity').textContent = '$' + s.equity.toLocaleString(undefined, {minimumFractionDigits: 2});
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = 'Unrealized P&L: ' + (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
    pnlEl.className = 'pnl ' + (s.pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
    document.getElementById('daily-limit').textContent = '$' + s.max_daily_loss.toFixed(2);
    document.getElementById('daily-loss').textContent = '$' + s.daily_loss.toFixed(2);

    // Positions
    const posDiv = document.getElementById('positions');
    if (s.positions.length === 0) {
      posDiv.innerHTML = '<span style="color:#555;font-size:0.85rem">No open positions</span>';
    } else {
      posDiv.innerHTML = s.positions.map(p =>
        `<div class="pos-row">
          <span>${p.coin} <span style="color:${parseFloat(p.size)>0?'#4ade80':'#f87171'}">${parseFloat(p.size)>0?'LONG':'SHORT'}</span></span>
          <span style="font-family:monospace">${p.size} @ ${p.entry_price}</span>
          <span style="color:${parseFloat(p.unrealized_pnl)>=0?'#4ade80':'#f87171'};font-family:monospace">$${parseFloat(p.unrealized_pnl).toFixed(2)}</span>
        </div>`
      ).join('');
    }

    // Signals
    const sigDiv = document.getElementById('signals');
    if (s.last_signals.length === 0) {
      sigDiv.innerHTML = '<span style="color:#555;font-size:0.85rem">Waiting for data...</span>';
    } else {
      sigDiv.innerHTML = s.last_signals.map(sig => {
        const dirClass = sig.direction === 'buy' ? 'dir-buy' : sig.direction === 'sell' ? 'dir-sell' : 'dir-none';
        const dirText = sig.direction.toUpperCase();
        const conf = (sig.confidence * 100).toFixed(0) + '%';
        const sl = sig.stop_loss ? '$' + sig.stop_loss.toFixed(2) : '—';
        const tp = sig.take_profit ? '$' + sig.take_profit.toFixed(2) : '—';
        return `<div class="signal-row">
          <div>
            <span class="signal-dir ${dirClass}">${dirText}</span>
            <span class="signal-name">${sig.strategy_id}</span>
          </div>
          <div style="text-align:right">
            <span class="signal-conf">${conf} | SL: ${sl} | TP: ${tp}</span>
            <div class="signal-reasons">${sig.reasons.slice(0,2).join(' · ')}</div>
          </div>
        </div>`;
      }).join('');
    }

    // Trade log
    const logDiv = document.getElementById('trade-log');
    if (s.trade_log.length > 0) {
      logDiv.innerHTML = s.trade_log.slice().reverse().map(t =>
        `<div class="log-entry">${t.time} [${t.action}] ${t.strategy} size=${t.size} price=${t.price} ${t.note}</div>`
      ).join('');
    }
  } catch (e) { console.error(e); }
}

async function startTrading() {
  if (!confirm('Start live trading? Real orders will be placed.')) return;
  await fetch('/api/start', {method: 'POST'});
  poll();
}

async function stopTrading() {
  await fetch('/api/stop', {method: 'POST'});
  poll();
}

setInterval(poll, 3000);
poll();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(STATE.to_dict())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/start":
            if STATE.live_enabled:
                STATE.trading_active = True
                log_trade("START", "operator", 0, STATE.last_price or 0, "live trading activated")
            self._json({"ok": True, "trading_active": STATE.trading_active})
        elif self.path == "/api/stop":
            STATE.trading_active = False
            log_trade("STOP", "operator", 0, STATE.last_price or 0, "trading stopped by operator")
            self._json({"ok": True, "trading_active": False})
        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Hyperbot workspace dashboard")
    parser.add_argument("--live", action="store_true", help="Enable live trading controls")
    parser.add_argument("--confirm-risk", action="store_true", help="Confirm you understand the risk of live trading")
    parser.add_argument("--port", type=int, default=0, help="Port to run on (0 = auto)")
    args = parser.parse_args()

    if args.live and not args.confirm_risk:
        print("ERROR: Live trading requires --confirm-risk flag.", flush=True)
        print("  This means real orders with real money.", flush=True)
        print("  Usage: python3 scripts/dashboard.py --live --confirm-risk", flush=True)
        return 1

    STATE.live_enabled = args.live

    # Start trading loop in background
    thread = threading.Thread(target=trading_loop, daemon=True)
    thread.start()

    port = args.port or find_free_port()
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)

    url = f"http://127.0.0.1:{port}"
    mode = "LIVE TRADING" if args.live else "VIEW ONLY"
    print(f"[dashboard] {mode} — {url}", flush=True)
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        print("\n[dashboard] Shutting down.", flush=True)
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
