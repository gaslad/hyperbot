#!/usr/bin/env python3
"""Hyperbot — full onboarding wizard + live trading dashboard.

Single-page web app that takes a user from nothing to automated trading:

  Step 1  Pick a trading pair
  Step 2  Select strategies
  Step 3  Set risk parameters
  Step 4  Enter wallet credentials
  Step 5  Build workspace (loading animation while profiling runs)
  Step 6  Live dashboard — signals, positions, trades, education

Launch:
    python3 scripts/dashboard.py                   # starts wizard
    python3 scripts/dashboard.py --live --confirm-risk   # enables order execution
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hl_client
import signals

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"
MANIFEST_PATH = ROOT / "hyperbot.workspace.json"

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
        self.max_daily_loss_pct: float = 5.0  # % of account equity
        self.setup_complete: bool = False
        self.build_status: str = ""  # for step 5 progress
        self.build_log: list[str] = []
        self.thinking: str = ""  # rotating system status message
        self.max_leverage: float = 4.0
        self.risk_per_trade_pct: float = 1.0
        self.start_of_day_equity: float = 0.0
        self.master_address: str = ""
        self.network: str = "mainnet"

    def daily_loss_limit_usd(self) -> float:
        base = self.start_of_day_equity if self.start_of_day_equity > 0 else self.equity
        return base * (self.max_daily_loss_pct / 100)

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
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_daily_loss_usd": self.daily_loss_limit_usd(),
            "setup_complete": self.setup_complete,
            "build_status": self.build_status,
            "build_log": self.build_log[-30:],
            "thinking": self.thinking,
            "max_leverage": self.max_leverage,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "master_address": self.master_address,
            "network": self.network,
        }


STATE = TradingState()
STOP_EVENT = threading.Event()
BUILD_THREAD: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Credential storage (same as connect/server.py — duplicated so dashboard
# is self-contained in generated workspaces)
# ---------------------------------------------------------------------------

SERVICE_NAME = "hyperbot"

def store_credential(key: str, value: str) -> None:
    if platform.system() == "Darwin":
        account = f"{SERVICE_NAME}.{key}"
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE_NAME, "-a", account], capture_output=True)
        subprocess.run(["security", "add-generic-password", "-s", SERVICE_NAME, "-a", account, "-w", value, "-U"], check=True, capture_output=True)
    else:
        cred_dir = Path.home() / ".hyperbot" / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        f = cred_dir / f"{key}.secret"
        f.write_text(value, encoding="utf-8")
        f.chmod(0o600)

def read_credential(key: str) -> str | None:
    if platform.system() == "Darwin":
        account = f"{SERVICE_NAME}.{key}"
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE_NAME, "-a", account, "-w"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    f = Path.home() / ".hyperbot" / "credentials" / f"{key}.secret"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


# ---------------------------------------------------------------------------
# Build workspace (Step 5 background task)
# ---------------------------------------------------------------------------

def build_workspace_background(config: dict) -> None:
    """Run in a background thread: profile + auto-apply revisions."""
    global BUILD_THREAD, ROOT, POLICY_PATH, MANIFEST_PATH
    try:
        symbol = config["symbol"]
        coin = hl_client.infer_coin(symbol)
        strategies = config.get("strategies", [])
        STATE.build_status = "building"
        STATE.build_log = []

        def blog(msg: str) -> None:
            STATE.build_log.append(msg)
            print(f"  [build] {msg}", flush=True)

        blog(f"Setting up {symbol} workspace with {len(strategies)} strategies...")

        # Rename workspace folder to reflect the chosen pair
        # e.g. hyperbot-workspace → hyperbot-SOL
        desired_name = f"hyperbot-{coin}"
        if ROOT.name != desired_name:
            new_path = ROOT.parent / desired_name
            if not new_path.exists():
                try:
                    ROOT.rename(new_path)
                    ROOT = new_path
                    POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"
                    MANIFEST_PATH = ROOT / "hyperbot.workspace.json"
                    # Also update hl_client's ROOT reference
                    hl_client.ROOT = ROOT
                    hl_client.WORKSPACE_MANIFEST = MANIFEST_PATH
                    blog(f"Workspace renamed to {desired_name}/")
                except OSError as e:
                    blog(f"Note: Could not rename workspace folder — {e}")

        # Update workspace manifest with chosen symbol
        if MANIFEST_PATH.exists():
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest["symbol"] = symbol
            MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            blog(f"Workspace manifest updated for {symbol}")

        # Update policy with risk settings
        if POLICY_PATH.exists():
            policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            safe = policy.get("auto_apply", {}).get("safe_bands", {})
            safe["leverage_max"] = config.get("max_leverage", 4.0)
            safe["risk_per_trade_pct_max"] = config.get("risk_per_trade_pct", 1.0)
            safe["max_daily_loss_pct"] = config.get("max_daily_loss_pct", 5.0)
            POLICY_PATH.write_text(json.dumps(policy, indent=2), encoding="utf-8")
            blog(f"Risk policy updated: leverage={safe['leverage_max']}x, risk/trade={safe['risk_per_trade_pct_max']}%, daily limit={safe['max_daily_loss_pct']}% of equity")

        # Install strategy configs for the chosen pair
        # The workspace was created with placeholder configs (BTCUSDT).
        # Re-create them for the actual coin chosen in the wizard.
        config_dir = ROOT / "config" / "strategies"
        config_dir.mkdir(parents=True, exist_ok=True)
        strategy_ids = []
        for strat_id in strategies:
            new_id = f"{coin.lower()}_{strat_id}"
            strategy_ids.append(new_id)
            # Check if there's already a config for this pack (from placeholder creation)
            # Find any existing config with this pack_id and copy/update it
            existing = None
            for f in config_dir.glob("*.json"):
                try:
                    cfg = json.loads(f.read_text(encoding="utf-8"))
                    if cfg.get("strategy_id", "").endswith(strat_id):
                        existing = (f, cfg)
                        break
                except Exception:
                    pass

            if existing:
                old_path, cfg = existing
                # Update the config for the new coin
                cfg["strategy_id"] = new_id
                cfg["pack_id"] = strat_id  # required for signal dispatcher
                cfg["display_name"] = f"{coin} {strat_id.replace('_', ' ').title()}"
                cfg["market"] = {"symbol": symbol, "coin": coin, "market_type": "perpetual"}
                new_path = config_dir / f"{new_id}.json"
                new_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                if old_path != new_path:
                    old_path.unlink(missing_ok=True)
                blog(f"Strategy config: {new_id}")
            else:
                # Create a minimal config from scratch
                cfg = {
                    "strategy_id": new_id,
                    "display_name": f"{coin} {strat_id.replace('_', ' ').title()}",
                    "enabled": True,
                    "pack_id": strat_id,
                    "market": {"symbol": symbol, "coin": coin, "market_type": "perpetual"},
                    "runner": {"source": "hyperliquid_candles", "anchor_timeframe": "1D", "trigger_timeframe": "4H", "confirmation_timeframe": "1H"},
                    "entry": {"sma_period": 10, "pullback_zone_pct": 5.0, "confirmation_type": "close_above_prev_high"},
                    "filters": {"overextension_max_pct": 20.0, "min_pullback_pct": 3.0},
                    "risk": {"invalidation_below_sma_pct": 3.0, "position_sizing": {"risk_per_trade_pct": 1.5, "max_leverage": 4.0}},
                    "take_profit": {"tp1_r_multiple": 1.0, "tp2_r_multiple": 2.0},
                }
                (config_dir / f"{new_id}.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                blog(f"Strategy config created: {new_id}")

        # Also update the signals module's CONFIG_DIR reference
        signals.CONFIG_DIR = config_dir
        blog(f"Installed {len(strategy_ids)} strategy configs for {coin}")

        # Fetch and cache 90-day candle data for profiling
        blog(f"Fetching 90-day price history for {coin}...")
        try:
            candles_1d = hl_client.get_candles(coin, "1d", 90)
            blog(f"Got {len(candles_1d)} daily candles")
            candles_4h = hl_client.get_candles(coin, "4h", 14)
            blog(f"Got {len(candles_4h)} 4H candles")
        except Exception as e:
            blog(f"WARNING: Could not fetch candles — {e}")
            candles_1d = []
            candles_4h = []

        # Run signal detection as a smoke test
        blog("Running initial signal scan...")
        try:
            price = hl_client.get_mid_price(coin)
            if price and candles_1d:
                sigs = signals.detect_all_signals(candles_1d, candles_4h, price)
                for s in sigs:
                    status = f"{s.direction.value.upper()}" if s.direction != signals.Direction.NONE else "NO SIGNAL"
                    blog(f"  {s.strategy_id}: {status} (confidence {s.confidence:.0%})")
            blog(f"Current price: ${price:,.2f}" if price else "Could not fetch price")
        except Exception as e:
            blog(f"Signal scan failed: {e}")

        blog("Workspace build complete.")
        STATE.build_status = "done"

        # Now kick off the trading loop
        STATE.coin = coin
        STATE.symbol = symbol
        STATE.setup_complete = True
        STATE.max_daily_loss_pct = config.get("max_daily_loss_pct", 5.0)
        STATE.max_leverage = config.get("max_leverage", 4.0)
        STATE.risk_per_trade_pct = config.get("risk_per_trade_pct", 1.0)

    except Exception as e:
        STATE.build_status = "error"
        STATE.build_log.append(f"ERROR: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Trading loop (runs after setup)
# ---------------------------------------------------------------------------

def _thinking_message(cycle: int, state: TradingState) -> str:
    """Generate a rotating system status message."""
    msgs: list[str] = []
    price = state.last_price
    coin = state.coin

    # Always-available messages
    msgs.append(f"Scanning {coin} across all strategy timeframes...")
    msgs.append(f"Fetching latest {coin} candles from Hyperliquid...")
    msgs.append(f"Checking 1D and 4H charts for signal conditions...")

    # Signal-aware messages
    if state.last_signals:
        active = [s for s in state.last_signals if s.get("direction") != "none"]
        inactive = [s for s in state.last_signals if s.get("direction") == "none"]
        if active:
            best = max(active, key=lambda s: s.get("confidence", 0))
            msgs.append(f"Signal detected: {best['strategy_id']} at {best['confidence']*100:.0f}% confidence. Evaluating entry...")
            msgs.append(f"{best['strategy_id']} sees a {best['direction'].upper()} setup. Checking risk parameters...")
            if best.get("stop_loss") and price:
                risk_dist = abs(price - best["stop_loss"]) / price * 100
                msgs.append(f"Potential entry: stop-loss is {risk_dist:.1f}% away. Sizing position...")
        if inactive:
            names = [s["strategy_id"].replace("_", " ").title() for s in inactive[:2]]
            msgs.append(f"{' and '.join(names)}: conditions not met yet. Waiting for setup...")
        if len(inactive) == len(state.last_signals):
            msgs.append(f"No signals firing. All strategies are waiting for the right conditions.")
            msgs.append(f"Market is quiet for {coin}. Patience is part of the strategy.")

    # Position-aware messages
    if state.positions:
        for p in state.positions[:1]:
            pnl = float(p.get("unrealized_pnl", 0))
            direction = "long" if float(p["size"]) > 0 else "short"
            msgs.append(f"Monitoring open {direction} position. Unrealised P&L: ${pnl:+.2f}")
    elif state.trading_active:
        msgs.append(f"No open positions. Waiting for a high-confidence signal to enter...")

    # Trading state messages
    if not state.live_enabled:
        msgs.append(f"View-only mode. Signals are live but no orders will be placed.")
    elif not state.trading_active:
        msgs.append(f"Trading engine ready. Press Start Trading to begin execution.")

    return msgs[cycle % len(msgs)]


def trading_loop() -> None:
    """Background loop: fetch data, detect signals, optionally execute trades."""
    # Wait for setup to complete
    while not STATE.setup_complete and not STOP_EVENT.is_set():
        STOP_EVENT.wait(1)

    if STOP_EVENT.is_set():
        return

    creds = hl_client.get_credentials()
    master_address = creds.get("master_address")
    STATE.master_address = master_address or ""

    print(f"[dashboard] Monitoring {STATE.coin} ({STATE.symbol})", flush=True)
    print(f"[dashboard] Wallet: {master_address or 'NOT CONNECTED'}", flush=True)
    print(f"[dashboard] Network: MAINNET (real funds)", flush=True)
    print(f"[dashboard] Live trading: {'ENABLED' if STATE.live_enabled else 'disabled (view-only)'}", flush=True)

    cycle = 0
    while not STOP_EVENT.is_set():
        try:
            # Thinking message — rotate each cycle
            STATE.thinking = _thinking_message(cycle, STATE)
            cycle += 1

            price = hl_client.get_mid_price(STATE.coin)
            if price:
                STATE.last_price = price

            candles_1d = hl_client.get_candles(STATE.coin, "1d", 30)
            candles_4h = hl_client.get_candles(STATE.coin, "4h", 14)

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

            if master_address:
                try:
                    portfolio = hl_client.get_portfolio_value(master_address)
                    STATE.equity = portfolio["total_equity"]
                    STATE.pnl = portfolio["unrealized_pnl"]
                    STATE.positions = portfolio["positions"]
                    # Set start-of-day equity on first fetch
                    if STATE.start_of_day_equity <= 0 and STATE.equity > 0:
                        STATE.start_of_day_equity = STATE.equity
                    if cycle <= 1:
                        print(f"[dashboard] Portfolio: ${STATE.equity:.2f} (perps=${portfolio['perps_equity']:.2f} + spot=${portfolio['spot_total_usd']:.2f})", flush=True)
                except Exception as e:
                    print(f"[dashboard] Account fetch error: {e}", flush=True)
            else:
                if cycle <= 1:
                    print("[dashboard] WARNING: No master_address in Keychain. Account data unavailable.", flush=True)

            STATE.last_update = time.strftime("%H:%M:%S")
            STATE.error = None

            # Execute trades if live and active
            if STATE.live_enabled and STATE.trading_active:
                loss_limit = STATE.daily_loss_limit_usd()
                if loss_limit > 0 and STATE.daily_loss >= loss_limit:
                    STATE.trading_active = False
                    STATE.error = f"Daily loss limit reached (${STATE.daily_loss:.2f} >= ${loss_limit:.2f} = {STATE.max_daily_loss_pct}% of equity). Trading halted."
                    log_trade("HALT", "system", 0, 0, "daily loss limit reached")
                    continue

                max_leverage = STATE.max_leverage
                risk_per_trade_pct = STATE.risk_per_trade_pct

                for sig_data, sig_obj in zip(STATE.last_signals, sigs):
                    if sig_obj.direction == signals.Direction.NONE:
                        continue
                    if sig_obj.confidence < 0.5:
                        continue
                    if STATE.equity <= 0 or not sig_obj.stop_loss:
                        continue

                    risk_amount = STATE.equity * (risk_per_trade_pct / 100)
                    price_risk = abs(price - sig_obj.stop_loss) if price else 0
                    if price_risk <= 0:
                        continue

                    size = risk_amount / price_risk
                    max_notional = STATE.equity * max_leverage
                    max_size = max_notional / price if price else 0
                    size = min(size, max_size)
                    if size <= 0:
                        continue

                    is_buy = sig_obj.direction == signals.Direction.BUY
                    log_trade("BUY" if is_buy else "SELL", sig_obj.strategy_id, size, price or 0,
                              f"confidence={sig_obj.confidence:.2f}, SL={sig_obj.stop_loss:.2f}")

                    result = hl_client.place_order(STATE.coin, is_buy, size, order_type="market")
                    if result.ok:
                        log_trade("FILLED", sig_obj.strategy_id, size, price or 0, f"oid={result.order_id}")
                    else:
                        log_trade("REJECTED", sig_obj.strategy_id, size, price or 0, result.error or "unknown")

        except Exception as e:
            STATE.error = str(e)
            traceback.print_exc()

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
# Embedded HTML — single-page app with wizard + dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hyperbot</title>
<style>
:root {
  --bg: #0a0a0f; --bg2: #12121c; --bg3: #1a1a28; --border: #2a2a3a;
  --text: #e0e0e0; --dim: #999; --dim2: #777; --muted: #555;
  --green: #4ade80; --red: #f87171; --blue: #60a5fa; --yellow: #facc15;
  --accent: #4ade80; --accent-dim: #1a3a1a;
  --font-body: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-mono: 'SF Mono',SFMono-Regular,Menlo,monospace;
  --radius: 10px; --radius-sm: 6px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:var(--font-body); background:var(--bg); color:var(--text); min-height:100vh; }
a { color:var(--blue); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

/* --- Wizard container --- */
.wizard { max-width:640px; margin:0 auto; padding:2rem 1.5rem; min-height:100vh; display:flex; flex-direction:column; justify-content:center; }
.wizard-hide { display:none !important; }
.step-title { font-size:1.6rem; font-weight:700; margin-bottom:0.3rem; }
.step-sub { color:var(--dim); font-size:0.9rem; margin-bottom:2rem; line-height:1.5; }

/* Progress bar */
.progress { display:flex; gap:0.5rem; margin-bottom:2.5rem; }
.progress-dot { flex:1; height:4px; border-radius:2px; background:var(--border); transition:background 0.3s; }
.progress-dot.done { background:var(--accent); }
.progress-dot.active { background:var(--accent); opacity:0.6; }

/* Pair picker */
.pair-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:0.75rem; margin-bottom:1.5rem; }
.pair-card { background:var(--bg2); border:2px solid var(--border); border-radius:var(--radius); padding:1rem 0.75rem; text-align:center; cursor:pointer; transition:all 0.2s; }
.pair-card:hover { border-color:var(--accent); }
.pair-card.selected { border-color:var(--accent); background:var(--accent-dim); }
.pair-card .pair-name { font-weight:700; font-size:1rem; }
.pair-card .pair-price { font-size:0.8rem; color:var(--dim); margin-top:0.3rem; font-family:var(--font-mono); }
.pair-search { width:100%; padding:0.75rem 1rem; background:var(--bg2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-size:0.9rem; margin-bottom:1rem; outline:none; }
.pair-search:focus { border-color:var(--accent); }
.pair-search::placeholder { color:var(--muted); }

/* Strategy cards */
.strat-card { background:var(--bg2); border:2px solid var(--border); border-radius:var(--radius); padding:1.25rem; margin-bottom:0.75rem; cursor:pointer; transition:all 0.2s; }
.strat-card:hover { border-color:var(--accent); }
.strat-card.selected { border-color:var(--accent); background:var(--accent-dim); }
.strat-name { font-weight:700; font-size:1rem; display:flex; align-items:center; gap:0.5rem; }
.strat-tag { font-size:0.7rem; padding:0.15rem 0.5rem; border-radius:10px; font-weight:600; }
.tag-high { background:#1a3a1a; color:var(--green); }
.tag-med { background:#3a3a1a; color:var(--yellow); }
.strat-desc { color:var(--dim); font-size:0.85rem; margin-top:0.5rem; line-height:1.5; }
.strat-meta { display:flex; gap:1.5rem; margin-top:0.6rem; font-size:0.8rem; color:var(--dim2); }

/* Risk sliders */
.risk-group { margin-bottom:1.75rem; }
.risk-label { display:flex; justify-content:space-between; margin-bottom:0.5rem; font-size:0.9rem; }
.risk-label .val { color:var(--accent); font-weight:600; font-family:var(--font-mono); }
input[type=range] { width:100%; accent-color:var(--accent); }
.risk-help { font-size:0.75rem; color:var(--muted); margin-top:0.3rem; }

/* Credential input */
.cred-group { margin-bottom:1.25rem; }
.cred-group label { display:block; font-size:0.85rem; color:var(--dim); margin-bottom:0.4rem; }
.cred-input { width:100%; padding:0.7rem 1rem; background:var(--bg2); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:var(--font-mono); font-size:0.85rem; outline:none; }
.cred-input:focus { border-color:var(--accent); }
.cred-note { font-size:0.75rem; color:var(--muted); margin-top:0.3rem; }
.cred-skip { font-size:0.8rem; color:var(--dim); margin-top:1rem; }
.cred-connected { background:var(--accent-dim); border:1px solid #2a5a2a; border-radius:8px; padding:0.75rem 1rem; font-size:0.85rem; color:var(--green); margin-bottom:1.5rem; }

/* Build / loading step */
.build-container { text-align:center; padding:2rem 0; }
.spinner { width:48px; height:48px; border:4px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin 0.8s linear infinite; margin:0 auto 1.5rem; }
@keyframes spin { to { transform:rotate(360deg); } }
.build-log { text-align:left; background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:1rem; max-height:220px; overflow-y:auto; font-family:var(--font-mono); font-size:0.8rem; color:#aaa; margin-top:1.5rem; }
.build-log div { padding:0.15rem 0; }

/* Buttons */
.btn { padding:0.7rem 1.5rem; border:none; border-radius:8px; font-weight:600; cursor:pointer; font-size:0.9rem; transition:all 0.2s; }
.btn-primary { background:var(--accent); color:var(--bg); }
.btn-primary:hover { background:#22c55e; }
.btn-primary:disabled { opacity:0.3; cursor:not-allowed; }
.btn-secondary { background:transparent; color:var(--dim); border:1px solid var(--border); }
.btn-secondary:hover { border-color:var(--dim); }
.btn-row { display:flex; justify-content:space-between; margin-top:2rem; }

/* --- Dashboard --- */
.dashboard { display:none; }
.dashboard.active { display:block; }

/* Header — two rows */
.header { padding:0; border-bottom:1px solid var(--border); }
.header-top { display:flex; justify-content:space-between; align-items:center; padding:0.75rem 2rem; }
.header-bottom { display:flex; justify-content:space-between; align-items:center; padding:0.5rem 2rem; background:var(--bg2); border-top:1px solid #1a1a28; }
.logo { font-size:1.3rem; font-weight:700; color:var(--accent); }
.price-display { font-size:1.8rem; font-weight:700; font-family:var(--font-mono); }
.price-up { color:var(--green); }
.price-down { color:var(--red); }
.status-badge { padding:0.3rem 0.8rem; border-radius:20px; font-size:0.75rem; font-weight:600; }
.status-live { background:#1a3a1a; color:var(--green); border:1px solid #2a5a2a; }
.status-view { background:#1a1a3a; color:var(--blue); border:1px solid #2a2a5a; }

/* Live status indicator */
.status-indicator { display:flex; align-items:center; gap:0.5rem; font-size:0.85rem; font-weight:600; }
.status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
.status-dot.active { background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s ease-in-out infinite; }
.status-dot.scanning { background:var(--yellow); box-shadow:0 0 6px var(--yellow); animation:pulse 3s ease-in-out infinite; }
.status-dot.stopped { background:var(--dim2); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

.grid { display:grid; grid-template-columns:1fr 1fr; gap:1rem; padding:1.5rem 2rem; }
.card { background:var(--bg2); border:1px solid var(--border); border-radius:var(--radius); padding:1.25rem; }
.card h3 { font-size:0.8rem; color:var(--dim2); text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.75rem; display:flex; justify-content:space-between; align-items:center; }
.full-width { grid-column:1/-1; }
.equity-big { font-size:2.2rem; font-weight:700; font-family:var(--font-mono); color:#fff; }
.pnl { font-size:0.95rem; margin-top:0.25rem; }
.pnl-pos { color:var(--green); }
.pnl-neg { color:var(--red); }
.metric { display:flex; justify-content:space-between; padding:0.4rem 0; font-size:0.85rem; }
.metric-label { color:var(--dim2); }
.metric-value { color:#ccc; font-family:var(--font-mono); }
.signal-row { display:flex; justify-content:space-between; align-items:center; padding:0.6rem 0; border-bottom:1px solid #1a1a2a; }
.signal-row:last-child { border:none; }
.signal-dir { font-weight:700; font-size:0.85rem; padding:0.2rem 0.6rem; border-radius:4px; }
.dir-buy { color:var(--green); background:#1a3a1a; }
.dir-sell { color:var(--red); background:#3a1a1a; }
.dir-none { color:var(--dim2); background:#1a1a1a; }
.signal-name { font-size:0.85rem; color:#ccc; }
.signal-conf { font-size:0.8rem; color:var(--dim2); font-family:var(--font-mono); }
.signal-reasons { font-size:0.75rem; color:var(--muted); margin-top:0.3rem; }

/* Collapsible edu tips */
.edu-tip { font-size:0.75rem; color:var(--dim2); padding:0.6rem 0.8rem; background:#0f0f18; border:1px solid #1f1f2f; border-radius:var(--radius-sm); margin-top:0.75rem; line-height:1.5; display:none; }
.edu-tip.show { display:block; }
.edu-tip b { color:var(--dim); }
.edu-toggle { background:none; border:1px solid var(--border); color:var(--dim2); width:22px; height:22px; border-radius:50%; cursor:pointer; font-size:0.7rem; display:flex; align-items:center; justify-content:center; }
.edu-toggle:hover { border-color:var(--dim); color:var(--text); }

.pos-row { display:flex; justify-content:space-between; padding:0.5rem 0; border-bottom:1px solid #1a1a2a; font-size:0.85rem; }
.pos-row:last-child { border:none; }

/* Formatted trade log */
.log-entry { display:flex; gap:0.75rem; align-items:baseline; font-size:0.8rem; font-family:var(--font-mono); padding:0.35rem 0; border-bottom:1px solid #0f0f18; }
.log-time { color:var(--muted); white-space:nowrap; min-width:5rem; }
.log-action { font-weight:600; min-width:5rem; }
.log-action.act-buy,.log-action.act-test_buy_ok,.log-action.act-filled,.log-action.act-start { color:var(--green); }
.log-action.act-sell,.log-action.act-test_sell_ok,.log-action.act-stop,.log-action.act-halt { color:var(--red); }
.log-action.act-test_buy,.log-action.act-test_sell { color:var(--blue); }
.log-action.act-rejected,.log-action.act-test_buy_fail,.log-action.act-test_sell_fail { color:var(--red); }
.log-action.act-settings { color:var(--yellow); }
.log-detail { color:var(--dim2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

.controls { display:flex; gap:0.75rem; align-items:center; }
.btn-start { background:var(--accent); color:var(--bg); padding:0.6rem 1.5rem; border:none; border-radius:var(--radius-sm); font-weight:700; cursor:pointer; font-size:0.9rem; letter-spacing:0.02em; }
.btn-start:hover { background:#22c55e; }
.btn-stop { background:var(--red); color:#fff; padding:0.6rem 1.5rem; border:none; border-radius:var(--radius-sm); font-weight:700; cursor:pointer; font-size:0.9rem; }
.btn-stop:hover { background:#ef4444; }
.btn-start:disabled,.btn-stop:disabled { opacity:0.4; cursor:not-allowed; }
.error-bar { background:#2a1515; border:1px solid #5a2020; color:var(--red); padding:0.75rem 2rem; font-size:0.85rem; }
.updated { font-size:0.75rem; color:var(--muted); }

/* Thinking ticker */
.thinking-bar { padding:0.5rem 2rem; font-size:0.8rem; color:var(--dim2); border-bottom:1px solid var(--border); font-style:italic; transition:opacity 0.4s; min-height:2rem; display:flex; align-items:center; gap:0.5rem; }
.thinking-bar::before { content:''; width:6px; height:6px; border-radius:50%; background:var(--yellow); animation:pulse 2s ease-in-out infinite; flex-shrink:0; }

/* Wallet bar */
.wallet-bar { padding:0.4rem 2rem; font-size:0.75rem; color:var(--dim2); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
.wallet-addr { font-family:var(--font-mono); color:var(--dim); cursor:pointer; }
.wallet-addr:hover { color:var(--text); }
.copy-toast { font-size:0.7rem; color:var(--green); margin-left:0.5rem; opacity:0; transition:opacity 0.3s; }

/* Settings overlay */
.settings-overlay { display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:100; justify-content:center; align-items:center; }
.settings-overlay.open { display:flex; }
.settings-panel { background:var(--bg2); border:1px solid var(--border); border-radius:12px; padding:2rem; max-width:480px; width:90%; max-height:90vh; overflow-y:auto; }
.settings-panel h2 { font-size:1.2rem; margin-bottom:1.5rem; }
.settings-close { float:right; background:none; border:none; color:var(--dim); font-size:1.2rem; cursor:pointer; }
.settings-close:hover { color:var(--text); }

/* Icon buttons */
.icon-btn { background:none; border:1px solid var(--border); color:var(--dim); padding:0.5rem 1rem; border-radius:var(--radius-sm); cursor:pointer; font-size:0.8rem; }
.icon-btn:hover { border-color:var(--dim); color:var(--text); }
.test-btn { background:#1a1a3a; border:1px solid #2a2a5a; color:var(--blue); }
.test-btn:hover { background:#2a2a4a; }
.test-btn:disabled { opacity:0.3; cursor:not-allowed; }
</style>
</head>
<body>

<!-- ======================== WIZARD ======================== -->
<div id="wizard" class="wizard">
  <div class="progress" id="progress"></div>
  <div id="wizard-content"></div>
</div>

<!-- ======================== DASHBOARD ======================== -->
<div id="dashboard" class="dashboard">
  <div class="header">
    <div class="header-top">
      <div style="display:flex;align-items:center;gap:1rem">
        <span class="logo">Hyperbot</span>
        <span id="d-symbol" style="color:var(--dim);font-size:0.9rem;font-weight:600"></span>
        <span id="d-status" class="status-badge status-view">VIEW ONLY</span>
        <span id="d-network" class="status-badge" style="background:#3a2a1a;color:var(--yellow);border:1px solid #5a4a2a;font-size:0.7rem">MAINNET</span>
      </div>
      <div style="display:flex;align-items:center;gap:1.5rem">
        <div id="d-status-indicator" class="status-indicator">
          <span class="status-dot scanning"></span>
          <span id="d-status-label" style="color:var(--yellow)">Scanning</span>
        </div>
        <div style="text-align:right">
          <div id="d-price" class="price-display">&mdash;</div>
          <div id="d-updated" class="updated"></div>
        </div>
      </div>
    </div>
    <div class="header-bottom">
      <div class="wallet-bar" style="padding:0;border:none;font-size:0.75rem">
        <span>Wallet: <span id="d-wallet-addr" class="wallet-addr" onclick="copyWallet()" title="Click to copy">&mdash;</span><span id="copy-toast" class="copy-toast">Copied!</span></span>
        <span style="color:var(--muted)">API wallet (trade-only, no withdrawals)</span>
      </div>
      <div style="display:flex;align-items:center;gap:0.75rem">
        <button class="icon-btn test-btn" id="d-test-btn" onclick="testTrade()">Test Trade</button>
        <button class="icon-btn" onclick="openSettings()">Settings</button>
        <div id="d-controls" class="controls" style="display:none">
          <button id="d-btn-start" class="btn-start" onclick="startTrading()">Start Trading</button>
          <button id="d-btn-stop" class="btn-stop" onclick="stopTrading()" disabled>Stop</button>
        </div>
      </div>
    </div>
  </div>
  <div id="d-thinking" class="thinking-bar"></div>
  <div id="d-error" class="error-bar" style="display:none"></div>

  <div class="grid">
    <div class="card">
      <h3>Account <button class="edu-toggle" onclick="toggleEdu(this)" title="What does this mean?">?</button></h3>
      <div id="d-equity" class="equity-big">$&mdash;</div>
      <div id="d-pnl" class="pnl">&mdash;</div>
      <div style="margin-top:1rem">
        <div class="metric"><span class="metric-label">Daily Loss Limit</span><span id="d-daily-limit" class="metric-value">&mdash;</span></div>
        <div class="metric"><span class="metric-label">Daily Loss Used</span><span id="d-daily-loss" class="metric-value">&mdash;</span></div>
        <div class="metric"><span class="metric-label">Leverage / Risk</span><span id="d-lev-risk" class="metric-value">&mdash;</span></div>
      </div>
      <div class="edu-tip"><b>What this means:</b> Your account equity is your total deposited balance plus unrealised gains/losses. The daily loss limit is a % of your equity — if hit, trading auto-stops to protect your capital.</div>
    </div>

    <div class="card">
      <h3>Positions <button class="edu-toggle" onclick="toggleEdu(this)" title="What are positions?">?</button></h3>
      <div id="d-positions"><span style="color:var(--muted);font-size:0.85rem">No open positions</span></div>
      <div class="edu-tip"><b>Positions</b> show your current open trades. LONG = betting price goes up, SHORT = betting price goes down. Unrealised P&amp;L updates in real-time until you close.</div>
    </div>

    <div class="card full-width">
      <h3>Strategy Signals <button class="edu-toggle" onclick="toggleEdu(this)" title="How do signals work?">?</button></h3>
      <div id="d-signals"><span style="color:var(--muted);font-size:0.85rem">Waiting for data...</span></div>
      <div class="edu-tip"><b>How signals work:</b> Each strategy scans the market every 15 seconds using technical indicators (SMA, Bollinger Bands, ATR, wick analysis). A BUY/SELL signal fires only when multiple conditions align. Confidence shows how many conditions passed. Below 50% = no trade. SL = stop-loss (where to exit if wrong), TP = take-profit (where to take gains).</div>
    </div>

    <div class="card full-width">
      <h3>Trade Log <button class="edu-toggle" onclick="toggleEdu(this)" title="What is the trade log?">?</button></h3>
      <div id="d-trade-log" style="max-height:220px;overflow-y:auto"><span style="color:var(--muted);font-size:0.85rem">No trades yet</span></div>
      <div class="edu-tip"><b>Trade log</b> records every action: signal detections, orders placed, fills, rejections, and system events like halt. This is your audit trail.</div>
    </div>
  </div>
</div>

<!-- Settings overlay -->
<div id="settings-overlay" class="settings-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="settings-panel">
    <button class="settings-close" onclick="closeSettings()">&times;</button>
    <h2>Settings</h2>
    <div class="risk-group">
      <div class="risk-label"><span>Max leverage</span><span class="val" id="s-lev-val">4x</span></div>
      <input type="range" id="s-leverage" min="1" max="10" step="0.5" value="4" oninput="document.getElementById('s-lev-val').textContent=this.value+'x'">
    </div>
    <div class="risk-group">
      <div class="risk-label"><span>Risk per trade</span><span class="val" id="s-rpt-val">1%</span></div>
      <input type="range" id="s-risk" min="0.25" max="3" step="0.25" value="1" oninput="document.getElementById('s-rpt-val').textContent=this.value+'%'">
    </div>
    <div class="risk-group">
      <div class="risk-label"><span>Daily loss limit</span><span class="val" id="s-dll-val">5%</span></div>
      <input type="range" id="s-daily" min="1" max="20" step="0.5" value="5" oninput="document.getElementById('s-dll-val').textContent=this.value+'%'">
      <div class="risk-help">Percentage of your perps account equity. Circuit breaker halts all trading if hit.</div>
    </div>
    <h2 style="margin-top:1.5rem;font-size:1rem">Wallet</h2>
    <div class="cred-group">
      <label style="font-size:0.8rem;color:var(--dim)">Account address (holds your funds)</label>
      <input class="cred-input" id="s-master-addr" type="text" placeholder="0x..." style="font-size:0.8rem">
      <div class="cred-note">Your main Hyperliquid wallet — used for balance/position queries</div>
    </div>
    <div class="cred-group">
      <label style="font-size:0.8rem;color:var(--dim)">API wallet private key</label>
      <input class="cred-input" id="s-api-key" type="password" placeholder="0x..." style="font-size:0.8rem">
      <div class="cred-note">Leave blank to keep current key</div>
    </div>
    <div class="btn-row" style="margin-top:1.5rem">
      <button class="btn btn-secondary" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-primary" onclick="saveSettings()">Save Changes</button>
    </div>
  </div>
</div>

<script>
// ============================================================
// WIZARD STATE
// ============================================================
const STEPS = ['pair','strategies','risk','credentials','build'];
let currentStep = 0;
let wizardData = {
  symbol: '', coin: '', price: null,
  strategies: [],
  max_leverage: 4, risk_per_trade_pct: 1.0, max_daily_loss_pct: 5,
  master_address: '', agent_private_key: '',
};

// Top traded pairs to show by default
const TOP_PAIRS = [
  {coin:'BTC',symbol:'BTCUSDT'}, {coin:'ETH',symbol:'ETHUSDT'},
  {coin:'SOL',symbol:'SOLUSDT'}, {coin:'DOGE',symbol:'DOGEUSDT'},
  {coin:'ARB',symbol:'ARBUSDT'}, {coin:'AVAX',symbol:'AVAXUSDT'},
  {coin:'LINK',symbol:'LINKUSDT'}, {coin:'OP',symbol:'OPUSDT'},
  {coin:'WIF',symbol:'WIFUSDT'},
];

const STRATEGIES = [
  {
    id:'trend_pullback', name:'Trend Pullback', family:'Continuation',
    confidence:'High', suitability:'High',
    desc:'Buys dips in an uptrend. Waits for price to pull back toward the moving average, then enters when trend confirmation holds. Low-frequency, high-probability.',
    timeframes:'1D, 4H, 1H', risk:'1.5% per trade, 4x max leverage',
  },
  {
    id:'compression_breakout', name:'Compression Breakout', family:'Breakout',
    confidence:'High', suitability:'High',
    desc:'Detects tight Bollinger Band squeezes, then enters when price breaks out with volume expansion. Catches the start of big moves.',
    timeframes:'4H, 1H, 15M', risk:'1.0% per trade, 4x max leverage',
  },
  {
    id:'liquidity_sweep_reversal', name:'Liquidity Sweep Reversal', family:'Reversal',
    confidence:'Medium', suitability:'Medium',
    desc:'Watches for stop-hunts below support or above resistance. Enters the reversal after a sweep-and-reject candle pattern. Higher reward, lower frequency.',
    timeframes:'4H, 1H, 15M', risk:'0.75% per trade, 3x max leverage',
  },
];

let pairPrices = {};  // coin -> price

// ============================================================
// RENDER WIZARD
// ============================================================
function renderProgress() {
  const el = document.getElementById('progress');
  el.innerHTML = STEPS.map((s,i) =>
    `<div class="progress-dot ${i < currentStep ? 'done' : ''} ${i === currentStep ? 'active' : ''}"></div>`
  ).join('');
}

function renderStep() {
  renderProgress();
  const el = document.getElementById('wizard-content');
  switch(STEPS[currentStep]) {
    case 'pair': return renderPairStep(el);
    case 'strategies': return renderStrategyStep(el);
    case 'risk': return renderRiskStep(el);
    case 'credentials': return renderCredentialStep(el);
    case 'build': return renderBuildStep(el);
  }
}

// --- STEP 1: Pick Pair ---
function renderPairStep(el) {
  el.innerHTML = `
    <div class="step-title">Pick a trading pair</div>
    <div class="step-sub">Choose the asset you want to trade on Hyperliquid. Prices are live.</div>
    <input class="pair-search" id="pair-search" type="text" placeholder="Search pairs... (BTC, ETH, SOL...)" oninput="filterPairs()">
    <div class="pair-grid" id="pair-grid"></div>
    <div class="btn-row">
      <div></div>
      <button class="btn btn-primary" id="pair-next" disabled onclick="nextStep()">Continue</button>
    </div>
  `;
  fetchPrices();
  renderPairGrid();
}

async function fetchPrices() {
  try {
    const r = await fetch('/api/pairs');
    pairPrices = await r.json();
    renderPairGrid();
  } catch(e) { console.error(e); }
}

function renderPairGrid() {
  const search = (document.getElementById('pair-search')?.value || '').toUpperCase();
  let pairs = TOP_PAIRS;
  if (search) {
    // Search all known pairs from the server
    const allCoins = Object.keys(pairPrices);
    const filtered = allCoins.filter(c => c.toUpperCase().includes(search)).slice(0,9);
    pairs = filtered.map(c => ({coin:c, symbol:c+'USDT'}));
  }
  const grid = document.getElementById('pair-grid');
  if (!grid) return;
  grid.innerHTML = pairs.map(p => {
    const price = pairPrices[p.coin];
    const priceStr = price ? '$' + parseFloat(price).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : '...';
    const sel = wizardData.coin === p.coin ? 'selected' : '';
    return `<div class="pair-card ${sel}" onclick="selectPair('${p.coin}','${p.symbol}')">
      <div class="pair-name">${p.coin}</div>
      <div class="pair-price">${priceStr}</div>
    </div>`;
  }).join('');
}
function filterPairs() { renderPairGrid(); }

function selectPair(coin, symbol) {
  wizardData.coin = coin;
  wizardData.symbol = symbol;
  wizardData.price = pairPrices[coin] ? parseFloat(pairPrices[coin]) : null;
  renderPairGrid();
  document.getElementById('pair-next').disabled = false;
}

// --- STEP 2: Select Strategies ---
function renderStrategyStep(el) {
  el.innerHTML = `
    <div class="step-title">Select your strategies</div>
    <div class="step-sub">Pick one or more. Each strategy watches for different market conditions. Running multiple gives broader coverage.</div>
    <div id="strat-list"></div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="prevStep()">Back</button>
      <button class="btn btn-primary" id="strat-next" disabled onclick="nextStep()">Continue</button>
    </div>
  `;
  renderStratList();
}

function renderStratList() {
  const el = document.getElementById('strat-list');
  el.innerHTML = STRATEGIES.map(s => {
    const sel = wizardData.strategies.includes(s.id) ? 'selected' : '';
    const tagClass = s.confidence === 'High' ? 'tag-high' : 'tag-med';
    return `<div class="strat-card ${sel}" onclick="toggleStrategy('${s.id}')">
      <div class="strat-name">${s.name} <span class="strat-tag ${tagClass}">${s.confidence} confidence</span></div>
      <div class="strat-desc">${s.desc}</div>
      <div class="strat-meta">
        <span>${s.family}</span>
        <span>Timeframes: ${s.timeframes}</span>
        <span>Default: ${s.risk}</span>
      </div>
    </div>`;
  }).join('');
}

function toggleStrategy(id) {
  const idx = wizardData.strategies.indexOf(id);
  if (idx >= 0) wizardData.strategies.splice(idx,1);
  else wizardData.strategies.push(id);
  renderStratList();
  document.getElementById('strat-next').disabled = wizardData.strategies.length === 0;
}

// --- STEP 3: Risk ---
function renderRiskStep(el) {
  el.innerHTML = `
    <div class="step-title">Set your risk parameters</div>
    <div class="step-sub">These limits protect your capital. You can always change them later in operator-policy.json.</div>
    <div class="risk-group">
      <div class="risk-label"><span>Max leverage</span><span class="val" id="lev-val">${wizardData.max_leverage}x</span></div>
      <input type="range" min="1" max="10" step="0.5" value="${wizardData.max_leverage}" oninput="wizardData.max_leverage=parseFloat(this.value);document.getElementById('lev-val').textContent=this.value+'x'">
      <div class="risk-help">How much your position size can be multiplied. 1x = no leverage. Higher leverage = higher risk and reward.</div>
    </div>
    <div class="risk-group">
      <div class="risk-label"><span>Risk per trade</span><span class="val" id="rpt-val">${wizardData.risk_per_trade_pct}%</span></div>
      <input type="range" min="0.25" max="3" step="0.25" value="${wizardData.risk_per_trade_pct}" oninput="wizardData.risk_per_trade_pct=parseFloat(this.value);document.getElementById('rpt-val').textContent=this.value+'%'">
      <div class="risk-help">Percentage of your account risked on each trade. 1% means a losing trade costs 1% of your equity.</div>
    </div>
    <div class="risk-group">
      <div class="risk-label"><span>Daily loss limit</span><span class="val" id="dll-val">${wizardData.max_daily_loss_pct}%</span></div>
      <input type="range" min="1" max="20" step="0.5" value="${wizardData.max_daily_loss_pct}" oninput="wizardData.max_daily_loss_pct=parseFloat(this.value);document.getElementById('dll-val').textContent=this.value+'%'">
      <div class="risk-help">Percentage of your perps account equity. Circuit breaker halts all trading if daily losses reach this %.</div>
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="prevStep()">Back</button>
      <button class="btn btn-primary" onclick="nextStep()">Continue</button>
    </div>
  `;
}

// --- STEP 4: Credentials ---
function renderCredentialStep(el) {
  // Check if already connected
  fetch('/api/credential-status').then(r=>r.json()).then(s => {
    if (s.connected) {
      el.innerHTML = `
        <div class="step-title">Wallet connected</div>
        <div class="step-sub">Your Hyperliquid API wallet is already configured.</div>
        <div class="cred-connected">Connected: ${s.master_address}</div>
        <div class="btn-row">
          <button class="btn btn-secondary" onclick="prevStep()">Back</button>
          <button class="btn btn-primary" onclick="nextStep()">Continue with this wallet</button>
        </div>
        <div class="cred-skip" style="margin-top:1.5rem"><a href="#" onclick="showCredentialForm();return false">Use a different wallet</a></div>
      `;
    } else {
      showCredentialForm();
    }
  }).catch(() => showCredentialForm());
}

function showCredentialForm() {
  const el = document.getElementById('wizard-content');
  el.innerHTML = `
    <div class="step-title">Connect your wallet</div>
    <div class="step-sub">
      Go to <a href="https://app.hyperliquid.xyz/API" target="_blank">app.hyperliquid.xyz/API</a> to create an API wallet.
      You need two things from that page:
    </div>
    <div class="cred-group">
      <label style="color:var(--text);font-weight:600">Your account address (the one that holds your funds)</label>
      <input class="cred-input" id="cred-addr" type="text" placeholder="0x5d87..." oninput="validateCreds()">
      <div class="cred-note">This is your <b>main wallet</b> address shown in the top-right of Hyperliquid (e.g. 0x5d87...8290). NOT the API wallet address. This is used to read your balances and positions.</div>
    </div>
    <div class="cred-group">
      <label style="color:var(--text);font-weight:600">API wallet private key</label>
      <input class="cred-input" id="cred-key" type="password" placeholder="0x..." oninput="validateCreds()">
      <div class="cred-note">The private key shown when you created the API wallet (starts with 0x, 66 characters). This key can only trade — it cannot withdraw your funds. Stored in macOS Keychain only.</div>
    </div>
    <div id="cred-error" style="color:var(--red);font-size:0.85rem;margin-top:1rem;padding:0.75rem;border-radius:8px;background:#2a1515;display:none"></div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="prevStep()">Back</button>
      <button class="btn btn-primary" id="cred-next" disabled onclick="saveCreds()">Save & Continue</button>
    </div>
  `;
}

function validateCreds() {
  const addr = document.getElementById('cred-addr').value.trim();
  const key = document.getElementById('cred-key').value.trim();
  const addrOk = addr.startsWith('0x') && addr.length === 42;
  const keyOk = key.startsWith('0x') && key.length === 66;
  document.getElementById('cred-next').disabled = !(addrOk && keyOk);
}

async function saveCreds() {
  const addr = document.getElementById('cred-addr').value.trim();
  const key = document.getElementById('cred-key').value.trim();
  document.getElementById('cred-next').disabled = true;
  document.getElementById('cred-next').textContent = 'Saving...';
  try {
    const r = await fetch('/api/save-credentials', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({master_address:addr, agent_private_key:key})
    });
    const res = await r.json();
    if (res.ok) {
      wizardData.master_address = addr;
      if (res.verify && res.verify.startsWith('Warning')) {
        document.getElementById('cred-error').textContent = res.verify;
        document.getElementById('cred-error').style.display = 'block';
        document.getElementById('cred-error').style.background = '#3a3a1a';
        document.getElementById('cred-error').style.color = '#facc15';
        document.getElementById('cred-next').disabled = false;
        document.getElementById('cred-next').textContent = 'Continue Anyway';
        document.getElementById('cred-next').onclick = function(){ nextStep(); };
        return;
      }
      nextStep();
    } else {
      document.getElementById('cred-error').textContent = res.error || 'Failed to save';
      document.getElementById('cred-error').style.display = 'block';
      document.getElementById('cred-next').disabled = false;
      document.getElementById('cred-next').textContent = 'Save & Continue';
    }
  } catch(e) {
    document.getElementById('cred-error').textContent = 'Connection error';
    document.getElementById('cred-error').style.display = 'block';
    document.getElementById('cred-next').disabled = false;
    document.getElementById('cred-next').textContent = 'Save & Continue';
  }
}

// --- STEP 5: Build ---
function renderBuildStep(el) {
  el.innerHTML = `
    <div class="build-container">
      <div class="spinner" id="build-spinner"></div>
      <div class="step-title" id="build-title">Building your workspace</div>
      <div class="step-sub" id="build-sub">Fetching 90-day price data, profiling strategies, and setting up your ${wizardData.coin} trading workspace...</div>
      <div class="build-log" id="build-log"></div>
    </div>
  `;
  // Start build
  fetch('/api/build', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      symbol: wizardData.symbol,
      coin: wizardData.coin,
      strategies: wizardData.strategies,
      max_leverage: wizardData.max_leverage,
      risk_per_trade_pct: wizardData.risk_per_trade_pct,
      max_daily_loss_pct: wizardData.max_daily_loss_pct,
    })
  });
  pollBuild();
}

async function pollBuild() {
  try {
    const r = await fetch('/api/build-status');
    const s = await r.json();
    const logEl = document.getElementById('build-log');
    if (logEl) {
      logEl.innerHTML = s.log.map(l => `<div>${l}</div>`).join('');
      logEl.scrollTop = logEl.scrollHeight;
    }
    if (s.status === 'done') {
      document.getElementById('build-spinner').style.display = 'none';
      document.getElementById('build-title').textContent = 'Workspace ready';
      document.getElementById('build-sub').textContent = `Your ${wizardData.coin} trading bot is configured and monitoring the market.`;
      setTimeout(() => {
        document.getElementById('wizard').classList.add('wizard-hide');
        document.getElementById('dashboard').classList.add('active');
        startDashboardPoll();
      }, 1500);
      return;
    }
    if (s.status === 'error') {
      document.getElementById('build-spinner').style.display = 'none';
      document.getElementById('build-title').textContent = 'Build failed';
      document.getElementById('build-sub').innerHTML = 'Check the log below. <a href="#" onclick="renderBuildStep(document.getElementById(\'wizard-content\'));return false">Retry</a>';
      return;
    }
  } catch(e) { console.error(e); }
  setTimeout(pollBuild, 1000);
}

// ============================================================
// WIZARD NAV
// ============================================================
function nextStep() {
  if (currentStep < STEPS.length - 1) { currentStep++; renderStep(); }
}
function prevStep() {
  if (currentStep > 0) { currentStep--; renderStep(); }
}

// ============================================================
// DASHBOARD POLL (same as before but with edu-tips)
// ============================================================
let prevPrice = null;

async function dashPoll() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    document.getElementById('d-symbol').textContent = s.symbol;

    // Wallet
    if (s.master_address) {
      const addr = s.master_address;
      document.getElementById('d-wallet-addr').textContent = addr.slice(0,6) + '...' + addr.slice(-4);
      document.getElementById('d-wallet-addr').title = addr;
    }

    // Network
    const netBadge = document.getElementById('d-network');
    if (netBadge) netBadge.textContent = s.network === 'mainnet' ? 'MAINNET' : 'TESTNET';

    // Status
    const badge = document.getElementById('d-status');
    const ctrl = document.getElementById('d-controls');
    if (s.live_enabled) {
      ctrl.style.display = 'flex';
      if (s.trading_active) {
        badge.className = 'status-badge status-live'; badge.textContent = 'LIVE';
        document.getElementById('d-btn-start').disabled = true;
        document.getElementById('d-btn-stop').disabled = false;
      } else {
        badge.className = 'status-badge status-view'; badge.textContent = 'READY';
        document.getElementById('d-btn-start').disabled = false;
        document.getElementById('d-btn-stop').disabled = true;
      }
    }

    // Price
    if (s.last_price) {
      const el = document.getElementById('d-price');
      el.textContent = '$' + s.last_price.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
      el.className = 'price-display ' + (prevPrice && s.last_price >= prevPrice ? 'price-up' : 'price-down');
      prevPrice = s.last_price;
    }
    document.getElementById('d-updated').textContent = s.last_update ? 'Updated ' + s.last_update : '';

    // Error
    const errBar = document.getElementById('d-error');
    if (s.error) { errBar.textContent = s.error; errBar.style.display = 'block'; }
    else { errBar.style.display = 'none'; }

    // Thinking ticker
    const thinkEl = document.getElementById('d-thinking');
    if (s.thinking) thinkEl.textContent = s.thinking;

    // Account
    document.getElementById('d-equity').textContent = '$' + s.equity.toLocaleString(undefined,{minimumFractionDigits:2});
    const pnlEl = document.getElementById('d-pnl');
    pnlEl.textContent = 'Unrealized P&L: ' + (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2);
    pnlEl.className = 'pnl ' + (s.pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
    document.getElementById('d-daily-limit').textContent = s.max_daily_loss_pct.toFixed(1) + '% ($' + s.max_daily_loss_usd.toFixed(2) + ')';
    document.getElementById('d-daily-loss').textContent = '$' + s.daily_loss.toFixed(2);
    document.getElementById('d-lev-risk').textContent = s.max_leverage + 'x / ' + s.risk_per_trade_pct + '%';

    // Positions
    const posDiv = document.getElementById('d-positions');
    if (!s.positions.length) {
      posDiv.innerHTML = '<span style="color:#555;font-size:0.85rem">No open positions</span>';
    } else {
      posDiv.innerHTML = s.positions.map(p =>
        `<div class="pos-row">
          <span>${p.coin} <span style="color:${parseFloat(p.size)>0?'var(--green)':'var(--red)'}">${parseFloat(p.size)>0?'LONG':'SHORT'}</span></span>
          <span style="font-family:monospace">${p.size} @ ${p.entry_price}</span>
          <span style="color:${parseFloat(p.unrealized_pnl)>=0?'var(--green)':'var(--red)'};font-family:monospace">$${parseFloat(p.unrealized_pnl).toFixed(2)}</span>
        </div>`
      ).join('');
    }

    // Signals
    const sigDiv = document.getElementById('d-signals');
    if (!s.last_signals.length) {
      sigDiv.innerHTML = '<span style="color:#555;font-size:0.85rem">Waiting for data...</span>';
    } else {
      sigDiv.innerHTML = s.last_signals.map(sig => {
        const dc = sig.direction==='buy'?'dir-buy':sig.direction==='sell'?'dir-sell':'dir-none';
        const dt = sig.direction.toUpperCase();
        const conf = (sig.confidence*100).toFixed(0)+'%';
        const sl = sig.stop_loss ? '$'+sig.stop_loss.toFixed(2) : '\u2014';
        const tp = sig.take_profit ? '$'+sig.take_profit.toFixed(2) : '\u2014';
        const name = humanName(sig.strategy_id);
        return `<div class="signal-row">
          <div><span class="signal-dir ${dc}">${dt}</span> <span class="signal-name">${name}</span></div>
          <div style="text-align:right">
            <span class="signal-conf">${conf} | SL: ${sl} | TP: ${tp}</span>
            <div class="signal-reasons">${sig.reasons.slice(0,2).join(' \u00b7 ')}</div>
          </div>
        </div>`;
      }).join('');
    }

    // Trade log — formatted with color-coded actions
    const logDiv = document.getElementById('d-trade-log');
    if (s.trade_log.length) {
      logDiv.innerHTML = s.trade_log.slice().reverse().map(t => {
        const timeShort = t.time.split(' ')[1] || t.time;
        const actionClass = 'act-' + t.action.toLowerCase();
        let detail = '';
        if (t.size > 0) detail += t.size + ' @ $' + t.price.toLocaleString();
        if (t.note) detail += (detail ? ' — ' : '') + t.note;
        return `<div class="log-entry"><span class="log-time">${timeShort}</span><span class="log-action ${actionClass}">${t.action}</span><span class="log-detail" title="${(t.strategy + ' ' + t.note).trim()}">${t.strategy !== 'system' && t.strategy !== 'operator' ? t.strategy.replace(/_/g,' ') + ': ' : ''}${detail}</span></div>`;
      }).join('');
    }

    // Status indicator
    const dot = document.querySelector('.status-dot');
    const label = document.getElementById('d-status-label');
    if (s.trading_active) {
      dot.className = 'status-dot active';
      label.textContent = 'Trading Active';
      label.style.color = 'var(--green)';
    } else if (s.setup_complete) {
      dot.className = 'status-dot scanning';
      label.textContent = 'Scanning';
      label.style.color = 'var(--yellow)';
    } else {
      dot.className = 'status-dot stopped';
      label.textContent = 'Setting Up';
      label.style.color = 'var(--dim)';
    }
  } catch(e) { console.error(e); }
}

function toggleEdu(btn) {
  const tip = btn.closest('.card').querySelector('.edu-tip');
  if (tip) { tip.classList.toggle('show'); btn.textContent = tip.classList.contains('show') ? '\u00d7' : '?'; }
}
function copyWallet() {
  const addr = document.getElementById('d-wallet-addr').textContent;
  if (addr && addr !== '\u2014') {
    navigator.clipboard.writeText(addr).then(() => {
      const toast = document.getElementById('copy-toast');
      toast.style.opacity = '1';
      setTimeout(() => toast.style.opacity = '0', 1500);
    });
  }
}
function humanName(id) { return id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
function startDashboardPoll() { setInterval(dashPoll, 3000); dashPoll(); }

async function startTrading() {
  if (!confirm('Start live trading? Real orders will be placed with real money.')) return;
  await fetch('/api/start',{method:'POST'}); dashPoll();
}
async function stopTrading() { await fetch('/api/stop',{method:'POST'}); dashPoll(); }

// ============================================================
// SETTINGS
// ============================================================
function openSettings() {
  fetch('/api/state').then(r=>r.json()).then(s => {
    document.getElementById('s-leverage').value = s.max_leverage;
    document.getElementById('s-lev-val').textContent = s.max_leverage + 'x';
    document.getElementById('s-risk').value = s.risk_per_trade_pct;
    document.getElementById('s-rpt-val').textContent = s.risk_per_trade_pct + '%';
    document.getElementById('s-daily').value = s.max_daily_loss_pct;
    document.getElementById('s-dll-val').textContent = s.max_daily_loss_pct + '%';
    document.getElementById('s-master-addr').value = s.master_address || '';
    document.getElementById('s-api-key').value = '';
    document.getElementById('settings-overlay').classList.add('open');
  });
}
function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
}
async function saveSettings() {
  const data = {
    max_leverage: parseFloat(document.getElementById('s-leverage').value),
    risk_per_trade_pct: parseFloat(document.getElementById('s-risk').value),
    max_daily_loss_pct: parseFloat(document.getElementById('s-daily').value),
  };
  // Save risk settings
  await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  // Save wallet if changed
  const addr = document.getElementById('s-master-addr').value.trim();
  const key = document.getElementById('s-api-key').value.trim();
  if (addr || key) {
    const creds = {};
    if (addr) creds.master_address = addr;
    if (key) creds.agent_private_key = key;
    await fetch('/api/save-credentials', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(creds)});
  }
  closeSettings();
  dashPoll();
}

// ============================================================
// TEST TRADE
// ============================================================
async function testTrade() {
  const btn = document.getElementById('d-test-btn');
  btn.disabled = true;
  btn.textContent = 'Testing...';
  try {
    const r = await fetch('/api/test-trade', {method:'POST'});
    const res = await r.json();
    if (res.ok) {
      btn.textContent = res.message || 'Test OK';
      btn.style.borderColor = 'var(--green)';
      btn.style.color = 'var(--green)';
    } else {
      btn.textContent = res.error || 'Failed';
      btn.style.borderColor = 'var(--red)';
      btn.style.color = 'var(--red)';
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.style.borderColor = 'var(--red)';
    btn.style.color = 'var(--red)';
  }
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = 'Test Trade';
    btn.style.borderColor = '';
    btn.style.color = '';
  }, 4000);
  dashPoll();
}

// ============================================================
// INIT
// ============================================================
renderStep();
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
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/state":
            self._json(STATE.to_dict())

        elif path == "/api/pairs":
            # Return live mid prices for all pairs
            try:
                mids = hl_client.get_all_mids()
                self._json(mids)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path == "/api/credential-status":
            master = read_credential("master_address")
            self._json({"connected": master is not None, "master_address": master or ""})

        elif path == "/api/build-status":
            self._json({"status": STATE.build_status, "log": STATE.build_log[-30:]})

        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}
        path = self.path.split("?")[0]

        if path == "/api/save-credentials":
            try:
                master = body.get("master_address", "").strip()
                agent_pk = body.get("agent_private_key", "").strip()
                if master:
                    if not master.startswith("0x") or len(master) != 42:
                        raise ValueError("Address must be 0x + 40 hex chars")
                    store_credential("master_address", master)
                    STATE.master_address = master
                    print(f"  Master address saved: {master}", flush=True)
                if agent_pk:
                    pk = agent_pk.removeprefix("0x")
                    if len(pk) != 64 or not all(c in "0123456789abcdefABCDEF" for c in pk):
                        raise ValueError("Invalid private key format")
                    store_credential("agent_private_key", agent_pk)
                    # Derive and log the agent address for verification
                    try:
                        from eth_account import Account
                        agent_wallet = Account.from_key(agent_pk)
                        print(f"  API key saved (agent address: {agent_wallet.address})", flush=True)
                    except Exception:
                        print(f"  API key saved", flush=True)
                if not master and not agent_pk:
                    raise ValueError("No credentials provided")
                # Verify master address has an account on Hyperliquid
                verify_msg = ""
                if master:
                    try:
                        ch = hl_client.get_clearinghouse_state(master)
                        equity = float(ch.get("marginSummary", {}).get("accountValue", 0))
                        if equity > 0:
                            verify_msg = f"Verified: account has ${equity:.2f}"
                            print(f"  {verify_msg}", flush=True)
                        else:
                            verify_msg = "Warning: account shows $0 equity. Make sure this is your MAIN wallet address (the one with funds), not the API wallet address."
                            print(f"  WARNING: {verify_msg}", flush=True)
                    except Exception as e:
                        verify_msg = f"Could not verify account: {e}"
                        print(f"  {verify_msg}", flush=True)
                self._json({"ok": True, "verify": verify_msg})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/build":
            global BUILD_THREAD
            if STATE.build_status == "building":
                self._json({"ok": False, "error": "Build already in progress"})
                return
            BUILD_THREAD = threading.Thread(target=build_workspace_background, args=(body,), daemon=True)
            BUILD_THREAD.start()
            self._json({"ok": True})

        elif path == "/api/start":
            if STATE.live_enabled:
                STATE.trading_active = True
                log_trade("START", "operator", 0, STATE.last_price or 0, "live trading activated")
            self._json({"ok": True, "trading_active": STATE.trading_active})

        elif path == "/api/stop":
            STATE.trading_active = False
            log_trade("STOP", "operator", 0, STATE.last_price or 0, "trading stopped by operator")
            self._json({"ok": True, "trading_active": False})

        elif path == "/api/settings":
            try:
                STATE.max_leverage = body.get("max_leverage", STATE.max_leverage)
                STATE.risk_per_trade_pct = body.get("risk_per_trade_pct", STATE.risk_per_trade_pct)
                STATE.max_daily_loss_pct = body.get("max_daily_loss_pct", STATE.max_daily_loss_pct)
                # Persist to policy file
                if POLICY_PATH.exists():
                    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
                    safe = policy.get("auto_apply", {}).get("safe_bands", {})
                    safe["leverage_max"] = STATE.max_leverage
                    safe["risk_per_trade_pct_max"] = STATE.risk_per_trade_pct
                    safe["max_daily_loss_pct"] = STATE.max_daily_loss_pct
                    POLICY_PATH.write_text(json.dumps(policy, indent=2), encoding="utf-8")
                log_trade("SETTINGS", "operator", 0, STATE.last_price or 0,
                          f"lev={STATE.max_leverage}x risk={STATE.risk_per_trade_pct}% daily={STATE.max_daily_loss_pct}%")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/test-trade":
            # Tiny buy then immediate sell to verify connectivity + credentials
            try:
                coin = STATE.coin
                if not coin:
                    self._json({"ok": False, "error": "No coin configured yet"})
                    return
                price = hl_client.get_mid_price(coin)
                if not price:
                    self._json({"ok": False, "error": f"Cannot get price for {coin}"})
                    return

                # Look up asset metadata for proper minimum size
                import math as _math
                asset_info = hl_client.get_asset_info(coin)
                sz_decimals = asset_info.get("szDecimals", 3) if asset_info else 3

                # Minimum size: must exceed $10 notional (HL minimum)
                # Round UP to the next valid size step to guarantee ≥ $10
                raw_min = 11.0 / price  # aim for $11 to leave margin
                step = 1 / (10 ** sz_decimals) if sz_decimals > 0 else 1
                min_size = _math.ceil(raw_min / step) * step
                if min_size <= 0:
                    min_size = step
                notional = min_size * price
                log_trade("TEST_BUY", "test", min_size, price,
                          f"connectivity test — ${notional:.2f} notional, szDecimals={sz_decimals}")
                buy_result = hl_client.place_order(coin, True, min_size, order_type="market")
                if not buy_result.ok:
                    log_trade("TEST_BUY_FAIL", "test", min_size, price,
                              f"error={buy_result.error}")
                    self._json({"ok": False, "error": f"Buy failed: {buy_result.error}",
                                "raw": buy_result.raw})
                    return
                log_trade("TEST_BUY_OK", "test", min_size, price, f"oid={buy_result.order_id}")
                # Brief pause then close
                time.sleep(2)
                log_trade("TEST_SELL", "test", min_size, price, "closing test position")
                sell_result = hl_client.place_order(coin, False, min_size, order_type="market", reduce_only=True)
                if sell_result.ok:
                    log_trade("TEST_SELL_OK", "test", min_size, price, f"oid={sell_result.order_id}")
                    self._json({"ok": True, "message": f"Test complete: bought and sold {min_size} {coin}",
                                "buy_raw": buy_result.raw, "sell_raw": sell_result.raw})
                else:
                    log_trade("TEST_SELL_FAIL", "test", min_size, price,
                              f"error={sell_result.error}")
                    self._json({"ok": False,
                                "error": f"Buy filled but sell failed: {sell_result.error}",
                                "buy_raw": buy_result.raw, "sell_raw": sell_result.raw})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json({"ok": False, "error": str(e)})

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

    # Start trading loop in background (waits for setup_complete)
    thread = threading.Thread(target=trading_loop, daemon=True)
    thread.start()

    port = args.port or find_free_port()
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)

    url = f"http://127.0.0.1:{port}"
    mode = "LIVE TRADING" if args.live else "VIEW ONLY"
    print(f"[hyperbot] {mode} — {url}", flush=True)
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        print("\n[hyperbot] Shutting down.", flush=True)
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
