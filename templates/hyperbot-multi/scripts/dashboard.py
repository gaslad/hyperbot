#!/usr/bin/env python3
"""Hyperbot — card-based trading dashboard.

Simplified dashboard where users add tokens via a + button, pick a strategy,
and the bot trades automatically. Every action comes with an educational
explanation. Credentials come from wallet connect (EIP-6963 / WalletConnect),
not manual entry.

Launch:
    python3 scripts/dashboard.py                          # view-only mode
    python3 scripts/dashboard.py --live --confirm-risk    # enables order execution
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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

import pandas as pd

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hl_client
import signals
import regime as portfolio_regime
import position_manager
from scalp_strategy_v2 import ScalpStrategy, StrategyConfig as ScalpConfig
from trade_journal import TradeJournal

# Shared strategy instances (stateful — track consecutive losses, performance)
SCALP_STRATEGY = ScalpStrategy(config=ScalpConfig())

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"
MANIFEST_PATH = ROOT / "hyperbot.workspace.json"
TRADE_JOURNAL = TradeJournal(ROOT, manifest_path=MANIFEST_PATH)


def normalize_margin_mode(value: Any, default: str = "isolated") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in {"isolated", "cross"} else default


LIVE_MAX_LEVERAGE_CAP = 2.0
CONFIDENCE_LEVERAGE_THRESHOLD = 0.8
PAIR_COOLDOWN_SECONDS = 30 * 60
PAIR_REENTRY_LOCKOUT_SECONDS = 2 * 60 * 60
BREAKEVEN_BUFFER_PCT = 0.0012
SCAN_BATCH_SIZE = 3
PORTFOLIO_SYNC_SECONDS = 45.0
ERROR_LOG_THROTTLE_SECONDS = 60.0
MIN_LEGACY_SIGNAL_CONFIDENCE = 0.65
AUTO_STRATEGY_PACK_IDS = (
    "scalp_v2",
    "trend_pullback",
    "compression_breakout",
    "liquidity_sweep_reversal",
)
STRATEGY_LABELS = {
    "scalp_v2": "5m Scalper",
    "trend_pullback": "Trend Follower",
    "compression_breakout": "Breakout Hunter",
    "liquidity_sweep_reversal": "Mean Reversion",
}


def clamp_live_leverage(value: Any) -> float:
    try:
        lev = float(value)
    except (TypeError, ValueError):
        lev = LIVE_MAX_LEVERAGE_CAP
    return max(1.0, min(LIVE_MAX_LEVERAGE_CAP, lev))


def _normalized_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0:
        conf /= 10.0
    return max(0.0, min(1.0, conf))


def _confidence_to_leverage(value: Any, max_leverage: Any = LIVE_MAX_LEVERAGE_CAP) -> int:
    cap = max(1, int(round(clamp_live_leverage(max_leverage))))
    if cap <= 1:
        return 1
    confidence = _normalized_confidence(value)
    return cap if confidence >= CONFIDENCE_LEVERAGE_THRESHOLD else 1


def _confidence_risk_scale(value: Any) -> float:
    return 0.5 + (0.5 * _normalized_confidence(value))


def strategy_label(pack_id: str) -> str:
    return STRATEGY_LABELS.get(pack_id, pack_id.replace("_", " ").title())


def _signal_confidence(sig: dict) -> float:
    try:
        return float(sig.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _signal_rank(sig: dict) -> tuple[int, float, int]:
    direction_rank = 1 if sig.get("direction") not in {None, "", "none"} else 0
    confidence = _signal_confidence(sig)
    pack_id = str(sig.get("pack_id", ""))
    preference = {
        "scalp_v2": 4,
        "compression_breakout": 3,
        "trend_pullback": 2,
        "liquidity_sweep_reversal": 1,
    }.get(pack_id, 0)
    return (direction_rank, confidence, preference)


def _best_signal(sig_dicts: list[dict], *, actionable_only: bool = False) -> dict | None:
    candidates = [
        sig for sig in sig_dicts
        if (not actionable_only or sig.get("direction") not in {None, "", "none"})
    ]
    if not candidates:
        return None
    return max(candidates, key=_signal_rank)


def _find_signal_by_pack(sig_dicts: list[dict], pack_id: str) -> dict | None:
    for sig in sig_dicts:
        if str(sig.get("pack_id", "")) == pack_id:
            return sig
    return None


def _update_pair_plan_from_signal(ps: PairState, sig: dict | None) -> None:
    if not sig:
        ps.plan_entry = None
        ps.plan_sl = None
        ps.plan_tp = None
        ps.plan_strategy = strategy_label(ps.selected_pack_id or ps.pack_id)
        ps.plan_reasons = []
        return
    ps.plan_entry = sig.get("entry_price")
    ps.plan_sl = sig.get("stop_loss")
    ps.plan_tp = sig.get("take_profit")
    ps.plan_strategy = strategy_label(str(sig.get("pack_id", ps.pack_id)))
    ps.plan_reasons = list(sig.get("reasons", []) or [])


def _update_pair_bot_context(ps: PairState) -> None:
    selected_pack = ps.selected_pack_id or ps.pack_id
    selected_signal = None
    for sig in ps.last_signals:
        if str(sig.get("pack_id")) == selected_pack:
            selected_signal = sig
            break
    if selected_signal is None:
        selected_signal = _best_signal(ps.last_signals)

    details: list[str] = []
    note = "Waiting for the first scan."

    if ps.positions:
        managed = ps.managed_position or {}
        strategy_name = strategy_label(selected_pack)
        if managed.get("exit_pending"):
            reason = managed.get("exit_reason") or "risk conditions changed"
            note = f"Supervisor is closing the open {strategy_name} trade because {reason}."
            details.append(f"Exit is pending now; the bot has stopped trying to manage this trade normally.")
            if managed.get("exit_reason"):
                details.append(str(managed["exit_reason"]))
        elif managed.get("tp1_moved"):
            note = f"Managing {strategy_name}: TP1 was reached and the stop has been moved to breakeven plus fees."
        else:
            note = f"Managing {strategy_name}: the bot is protecting the open position and waiting for TP or SL."
        if ps.plan_sl:
            details.append(f"Protective stop is staged around ${ps.plan_sl:.2f}.")
        if ps.plan_tp:
            details.append(f"Profit target is staged around ${ps.plan_tp:.2f}.")
    elif _is_in_cooldown(ps):
        try:
            cool_until = datetime.fromisoformat(ps.cooldown_until)
            note = f"Cooling down after the last trade until {cool_until.astimezone().strftime('%H:%M')}."
        except ValueError:
            note = "Cooling down after the last trade before allowing another entry."
    elif selected_signal:
        strategy_name = strategy_label(str(selected_signal.get("pack_id", selected_pack)))
        reasons = list(selected_signal.get("reasons", []) or [])
        if ps.auto_strategy:
            if selected_signal.get("direction") not in {None, "", "none"}:
                note = f"Auto mode prefers {strategy_name} right now because it has the strongest live setup."
            else:
                note = f"Auto mode is waiting. {strategy_name} is the closest valid setup, but conditions are not complete yet."
        else:
            note = f"Manual mode is watching {strategy_name} and will only enter if this exact setup confirms."
        details.extend(reasons[:3])
    elif ps.auto_strategy:
        note = "Auto mode is scanning all approved strategies and waiting for a clean setup."
    else:
        note = f"Manual mode is watching {strategy_label(ps.pack_id)}."

    details.append(f"Risk is capped at {ps.risk_per_trade_pct:.2f}% with max {ps.max_leverage:.1f}x {ps.margin_mode} margin.")
    ps.bot_note = note
    ps.bot_details = details[:4]


_ERROR_LOG_NEXT_TS: dict[str, float] = {}


def _log_throttled_error(key: str, message: str, *, throttle_seconds: float = ERROR_LOG_THROTTLE_SECONDS) -> None:
    now = time.monotonic()
    next_ts = _ERROR_LOG_NEXT_TS.get(key, 0.0)
    if now >= next_ts:
        print(message, flush=True)
        _ERROR_LOG_NEXT_TS[key] = now + throttle_seconds


def _is_transient_read_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "429" in text
        or "Too Many Requests" in text
        or "Remote end closed connection without response" in text
        or "IncompleteRead" in text
        or "timed out" in text.lower()
        or isinstance(exc, (TimeoutError, ConnectionError, OSError))
    )


def _read_manifest_pairs() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    pairs = manifest.get("pairs", [])
    if not pairs and manifest.get("coin"):
        return [{"coin": manifest["coin"], "symbol": manifest.get("symbol", manifest["coin"]), "enabled": True}]
    return [p for p in pairs if isinstance(p, dict)]


def _write_manifest_pairs(pairs: list[dict[str, Any]]) -> None:
    if not MANIFEST_PATH.exists():
        return
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    if pairs:
        manifest["pairs"] = pairs
        manifest["coin"] = pairs[0].get("coin", manifest.get("coin", ""))
        manifest["symbol"] = pairs[0].get("symbol", manifest.get("symbol", ""))
    else:
        manifest["pairs"] = []
        manifest.pop("coin", None)
        manifest.pop("symbol", None)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

class PairState:
    """Per-pair trading state (signals, price, positions for one coin)."""
    def __init__(self, coin: str = "", symbol: str = "") -> None:
        self.coin: str = coin
        self.symbol: str = symbol
        self.last_signals: list[dict] = []
        self.last_price: float | None = None
        self.positions: list[dict] = []
        self.pnl: float = 0.0
        self.enabled: bool = True
        # Per-pair risk settings (defaults inherited from global)
        self.max_leverage: float = LIVE_MAX_LEVERAGE_CAP
        self.risk_per_trade_pct: float = 0.5
        self.margin_mode: str = "isolated"
        self.cooldown_until: str = ""
        self.reentry_lock_until: str = ""
        self.last_position_size: float = 0.0
        self.last_closed_side: str = ""
        self.last_closed_strategy: str = ""
        self.managed_position: dict | None = None
        # Persisted trade plan from the originating signal
        self.plan_entry: float | None = None
        self.plan_sl: float | None = None
        self.plan_tp: float | None = None
        self.plan_strategy: str = ""
        self.plan_reasons: list[str] = []
        self.pack_id: str = "trend_pullback"
        self.auto_strategy: bool = True
        self.selected_pack_id: str = "trend_pullback"
        self.bot_note: str = ""
        self.bot_details: list[str] = []
        self.last_scan_ts: str = ""
        self.trading_live: bool = True  # Per-card trading toggle (on by default)

    def to_dict(self) -> dict:
        return {
            "coin": self.coin,
            "symbol": self.symbol,
            "last_signals": list(self.last_signals),
            "last_price": self.last_price,
            "positions": list(self.positions),
            "pnl": self.pnl,
            "enabled": self.enabled,
            "max_leverage": self.max_leverage,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "margin_mode": self.margin_mode,
            "cooldown_until": self.cooldown_until,
            "reentry_lock_until": self.reentry_lock_until,
            "managed_position": self.managed_position,
            "last_closed_side": self.last_closed_side,
            "last_closed_strategy": self.last_closed_strategy,
            "plan_entry": self.plan_entry,
            "plan_sl": self.plan_sl,
            "plan_tp": self.plan_tp,
            "plan_strategy": self.plan_strategy,
            "plan_reasons": list(self.plan_reasons),
            "pack_id": self.pack_id,
            "auto_strategy": self.auto_strategy,
            "selected_pack_id": self.selected_pack_id,
            "bot_note": self.bot_note,
            "bot_details": list(self.bot_details),
            "last_scan_ts": self.last_scan_ts,
            "trading_live": self.trading_live,
        }


class TradingState:
    def __init__(self) -> None:
        # Some API handlers log while already mutating state; use an RLock to
        # avoid deadlocking on those nested state updates.
        self.lock = threading.RLock()
        self.live_enabled = False
        self.trading_active = False
        self.last_update: str = ""
        self.equity: float = 0.0
        self.pnl: float = 0.0
        self.trade_log: list[dict] = []
        self.error: str | None = None
        self.daily_loss: float = 0.0
        self.max_daily_loss_pct: float = 5.0  # % of account equity
        self.setup_complete: bool = False
        self.build_status: str = ""  # for step 5 progress
        self.build_log: list[str] = []
        self.thinking: str = ""  # rotating system status message
        self.max_leverage: float = LIVE_MAX_LEVERAGE_CAP
        self.risk_per_trade_pct: float = 0.5
        self.margin_mode: str = "isolated"
        self.start_of_day_equity: float = 0.0
        self.master_address: str = ""
        self.network: str = "mainnet"
        self.scan_cursor: int = 0
        self.last_portfolio_sync_mono: float = 0.0
        self.portfolio_backoff_until_mono: float = 0.0
        self.portfolio_backoff_seconds: float = 0.0
        self.portfolio_ready: bool = False
        # Portfolio-level market regime
        self.market_regime: str = "green"  # green/yellow/red
        self.regime_size_multiplier: float = 1.0
        self.regime_min_confidence: float = 0.0
        self.regime_reasons: list[str] = []
        self.last_regime_check_mono: float = 0.0
        # Nightly tuner state
        self.last_tuner_date: str = ""  # "YYYY-MM-DD" of last tuner run
        self.last_tuner_report: str = ""  # summary of last tuner run
        # Multi-pair state
        self.pairs: dict[str, PairState] = {}  # coin -> PairState
        self.active_coin: str = ""  # which pair the UI is focused on
        # Legacy single-pair aliases (populated from active_coin)
        self.coin: str = ""
        self.symbol: str = ""
        self.last_signals: list[dict] = []
        self.last_price: float | None = None
        self.positions: list[dict] = []

    def add_pair(self, coin: str, symbol: str) -> PairState:
        ps = PairState(coin, symbol)
        ps.max_leverage = self.max_leverage
        ps.risk_per_trade_pct = self.risk_per_trade_pct
        ps.margin_mode = self.margin_mode
        self.pairs[coin] = ps
        if not self.active_coin:
            self.active_coin = coin
        return ps

    def active_pair(self) -> PairState | None:
        return self.pairs.get(self.active_coin)

    def all_coins(self) -> list[str]:
        return list(self.pairs.keys())

    def _sync_legacy(self) -> None:
        """Keep legacy single-pair fields in sync with active pair."""
        ap = self.active_pair()
        if ap:
            self.coin = ap.coin
            self.symbol = ap.symbol
            self.last_signals = ap.last_signals
            self.last_price = ap.last_price
            self.positions = ap.positions

    def daily_loss_limit_usd(self) -> float:
        base = self.start_of_day_equity if self.start_of_day_equity > 0 else self.equity
        return base * (self.max_daily_loss_pct / 100)

    def to_dict_unlocked(self) -> dict:
        self._sync_legacy()
        # Aggregate positions and pnl across all pairs
        all_positions = []
        total_pnl = self.pnl
        for ps in self.pairs.values():
            all_positions.extend(ps.positions)

        return {
            "live_enabled": self.live_enabled,
            "trading_active": self.trading_active,
            "last_signals": list(self.last_signals),
            "last_price": self.last_price,
            "last_update": self.last_update,
            "positions": list(self.positions),
            "all_positions": all_positions,
            "equity": self.equity,
            "pnl": self.pnl,
            "trade_log": list(self.trade_log[-50:]),
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
            "margin_mode": self.margin_mode,
            "master_address": self.master_address,
            "network": self.network,
            # Nightly tuner
            "last_tuner_date": self.last_tuner_date,
            "last_tuner_report": self.last_tuner_report,
            # Portfolio regime
            "market_regime": self.market_regime,
            "regime_size_multiplier": self.regime_size_multiplier,
            "regime_min_confidence": self.regime_min_confidence,
            "regime_reasons": list(self.regime_reasons),
            # Multi-pair data
            "pairs": {c: ps.to_dict() for c, ps in self.pairs.items()},
            "active_coin": self.active_coin,
            "all_coins": self.all_coins(),
        }

    def to_dict(self) -> dict:
        with self.lock:
            return self.to_dict_unlocked()


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
        pair_configs = config.get("pairs", [])
        # If no explicit pairs array, treat as single-pair
        if not pair_configs:
            pair_configs = [{"coin": coin, "symbol": symbol}]
        with STATE.lock:
            STATE.build_status = "building"
            STATE.build_log = []

        def blog(msg: str) -> None:
            with STATE.lock:
                STATE.build_log.append(msg)
            print(f"  [build] {msg}", flush=True)

        coin_names = ", ".join(pc["coin"] for pc in pair_configs)
        blog(f"Setting up workspace for {coin_names} with {len(strategies)} strategies...")

        # Rename workspace folder to reflect the chosen pair(s)
        # e.g. hyperbot-workspace → hyperbot-SOL or hyperbot-multi
        if len(pair_configs) == 1:
            desired_name = f"hyperbot-{coin}"
        else:
            desired_name = "hyperbot-multi"
        if ROOT.name != desired_name:
            new_path = ROOT.parent / desired_name
            if not new_path.exists():
                try:
                    ROOT.rename(new_path)
                    ROOT = new_path
                    POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"
                    MANIFEST_PATH = ROOT / "hyperbot.workspace.json"
                    # Also update sibling module ROOT references
                    hl_client.ROOT = ROOT
                    hl_client.WORKSPACE_MANIFEST = MANIFEST_PATH
                    signals.ROOT = ROOT
                    signals.CONFIG_DIR = ROOT / "config" / "strategies"
                    blog(f"Workspace renamed to {desired_name}/")
                except OSError as e:
                    blog(f"Note: Could not rename workspace folder — {e}")

        # Update workspace manifest with chosen pair(s)
        if MANIFEST_PATH.exists():
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest["symbol"] = symbol
            manifest["coin"] = coin
            manifest["pairs"] = [{"symbol": pc["symbol"], "coin": pc["coin"], "enabled": True, "strategies": []} for pc in pair_configs]
            MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            blog(f"Workspace manifest updated for {coin_names}")

        # Update policy with risk settings
        if POLICY_PATH.exists():
            policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            safe = policy.get("auto_apply", {}).get("safe_bands", {})
            safe["leverage_max"] = clamp_live_leverage(config.get("max_leverage", LIVE_MAX_LEVERAGE_CAP))
            safe["risk_per_trade_pct_max"] = config.get("risk_per_trade_pct", 0.5)
            safe["max_daily_loss_pct"] = config.get("max_daily_loss_pct", 5.0)
            POLICY_PATH.write_text(json.dumps(policy, indent=2), encoding="utf-8")
            blog(f"Risk policy updated: leverage={safe['leverage_max']}x, risk/trade={safe['risk_per_trade_pct_max']}%, daily limit={safe['max_daily_loss_pct']}% of equity")

        # Install strategy configs for all chosen pairs
        # The workspace was created with placeholder configs (BTCUSDT).
        # Re-create them for the actual coins chosen in the wizard.
        config_dir = ROOT / "config" / "strategies"
        config_dir.mkdir(parents=True, exist_ok=True)
        strategy_ids = []

        # Clear old placeholder configs
        for old_cfg in config_dir.glob("*.json"):
            old_cfg.unlink(missing_ok=True)

        for pc in pair_configs:
            pc_coin = pc["coin"]
            pc_symbol = pc["symbol"]
            blog(f"Installing strategy configs for {pc_coin}...")
            for strat_id in strategies:
                new_id = f"{pc_coin.lower()}_{strat_id}"
                strategy_ids.append(new_id)
                if strat_id == "scalp_v2":
                    cfg = _scalp_v2_config_payload(pc_coin, pc_symbol)
                    cfg["strategy_id"] = new_id
                else:
                    # Create config from scratch for this pair + strategy
                    cfg = {
                        "strategy_id": new_id,
                        "display_name": f"{pc_coin} {strat_id.replace('_', ' ').title()}",
                        "enabled": True,
                        "pack_id": strat_id,
                        "market": {"symbol": pc_symbol, "coin": pc_coin, "market_type": "perpetual"},
                        "runner": {"source": "hyperliquid_candles", "anchor_timeframe": "1D", "trigger_timeframe": "4H", "confirmation_timeframe": "1H"},
                        "entry": {"sma_period": 10, "pullback_zone_pct": 3.0, "confirmation_type": "close_above_prev_high"},
                        "filters": {"overextension_max_pct": 15.0, "min_pullback_pct": 4.0},
                        "risk": {"invalidation_below_sma_pct": 2.0, "position_sizing": {"risk_per_trade_pct": 0.5, "max_leverage": LIVE_MAX_LEVERAGE_CAP, "margin_mode": "isolated"}},
                        "take_profit": {"tp1_r_multiple": 1.0, "tp2_r_multiple": 2.0},
                        "exit_management": {
                            "signal_flip_exit": True,
                            "max_hold_minutes": 90 if strat_id == "liquidity_sweep_reversal" else (240 if strat_id == "trend_pullback" else 180),
                            "stale_after_minutes": 30 if strat_id == "liquidity_sweep_reversal" else (120 if strat_id == "trend_pullback" else 60),
                            "stale_min_follow_through_r": 0.5 if strat_id == "liquidity_sweep_reversal" else (0.25 if strat_id == "trend_pullback" else 0.5),
                            "underwater_exit_r": -0.10 if strat_id == "liquidity_sweep_reversal" else (-0.25 if strat_id == "trend_pullback" else -0.15),
                            "hard_fail_r": -0.20 if strat_id == "liquidity_sweep_reversal" else (-0.75 if strat_id == "trend_pullback" else -0.35),
                        },
                    }
                (config_dir / f"{new_id}.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                blog(f"  Strategy config: {new_id}")

        # Also update the signals module's CONFIG_DIR reference
        signals.CONFIG_DIR = config_dir
        blog(f"Installed {len(strategy_ids)} strategy configs across {len(pair_configs)} pair(s)")

        # Fetch and cache candle data for each pair
        for pc in pair_configs:
            pc_coin = pc["coin"]
            blog(f"Fetching 90-day price history for {pc_coin}...")
            try:
                candles_1d = hl_client.get_candles(pc_coin, "1d", 90)
                blog(f"  {pc_coin}: {len(candles_1d)} daily candles")
                candles_4h = hl_client.get_candles(pc_coin, "4h", 14)
                blog(f"  {pc_coin}: {len(candles_4h)} 4H candles")
            except Exception as e:
                blog(f"  WARNING: Could not fetch {pc_coin} candles — {e}")
                candles_1d = []
                candles_4h = []

            # Run signal detection as a smoke test
            try:
                price = hl_client.get_mid_price(pc_coin)
                if price and candles_1d:
                    sigs = signals.detect_all_signals(candles_1d, candles_4h, price, coin=pc_coin)
                    for s in sigs:
                        status = f"{s.direction.value.upper()}" if s.direction != signals.Direction.NONE else "NO SIGNAL"
                        blog(f"  {s.strategy_id}: {status} (confidence {s.confidence:.0%})")
                blog(f"  {pc_coin} price: ${price:,.2f}" if price else f"  Could not fetch {pc_coin} price")
            except Exception as e:
                blog(f"  {pc_coin} signal scan failed: {e}")

        blog("Workspace build complete.")
        STATE.build_status = "done"

        # Register pair(s) and kick off the trading loop
        # Support both single-pair (legacy) and multi-pair configs
        pair_configs = config.get("pairs", [])
        if pair_configs:
            for pc in pair_configs:
                STATE.add_pair(pc["coin"], pc["symbol"])
            blog(f"Registered {len(pair_configs)} trading pair(s): {', '.join(pc['coin'] for pc in pair_configs)}")
        else:
            STATE.add_pair(coin, symbol)

        STATE.coin = coin
        STATE.symbol = symbol
        STATE.setup_complete = True
        STATE.max_daily_loss_pct = config.get("max_daily_loss_pct", 5.0)
        STATE.max_leverage = clamp_live_leverage(config.get("max_leverage", LIVE_MAX_LEVERAGE_CAP))
        STATE.risk_per_trade_pct = config.get("risk_per_trade_pct", 0.5)
        STATE.margin_mode = normalize_margin_mode(config.get("margin_mode", "isolated"))
        for ps in STATE.pairs.values():
            ps.max_leverage = STATE.max_leverage
            ps.risk_per_trade_pct = STATE.risk_per_trade_pct
            ps.margin_mode = STATE.margin_mode

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


def _evaluate_scalp_v2_signal(
    coin: str,
    ps,
    price: float | None,
    read_stats: dict[str, int] | None = None,
) -> tuple[Any | None, dict | None]:
    _sync_scalp_v2_strategy(coin)
    market_data = _load_scalp_v2_market_data(coin, ps, price, read_stats=read_stats)
    if not market_data:
        return None, None

    signal = SCALP_STRATEGY.evaluate(coin, market_data)
    if signal.action == "TRADE" and signal.order_params:
        op = signal.order_params
        risk_scale = _confidence_risk_scale(signal.confidence / 10.0)
        op.size = round(op.size * risk_scale, 6)
        op.tp1_size = round(op.tp1_size * risk_scale, 6)
        op.tp_final_size = round(op.tp_final_size * risk_scale, 6)
        target_lev = _confidence_to_leverage(signal.confidence / 10.0, op.leverage)
        if target_lev != int(round(op.leverage)):
            scale = max(0.0, target_lev / max(op.leverage, 1.0))
            op.size = round(op.size * scale, 6)
            op.tp1_size = round(op.tp1_size * scale, 6)
            op.tp_final_size = round(op.tp_final_size * scale, 6)
        op.leverage = float(target_lev)

    if signal.action == "TRADE":
        proximity = signal.confidence / 10.0
    elif signal.regime:
        r = signal.regime
        checks = [r.ema_aligned, r.adx_ok, r.choppiness_ok, r.vwap_ok,
                  r.atr_above_median, r.rvol_ok, r.cvd_confirming, r.time_ok]
        passing = sum(1 for c in checks if c)
        proximity = (passing / len(checks)) * 0.7
    else:
        proximity = 0.0

    sig_dict = {
        "direction": signal.direction or "none",
        "strategy_id": "scalp_v2",
        "pack_id": "scalp_v2",
        "confidence": proximity,
        "reasons": signal.rejection_reasons if signal.action == "NO_TRADE" else [
            f"Net R: {signal.effective_r_net:.2f}",
            f"Confidence: {signal.confidence}/10",
        ],
        "entry_price": signal.order_params.entry_price if signal.order_params else None,
        "stop_loss": signal.order_params.stop_trigger if signal.order_params else None,
        "take_profit": signal.order_params.tp_final_trigger if signal.order_params else None,
    }
    return signal, sig_dict


def _load_scalp_v2_market_data(
    coin: str,
    ps,
    price: float | None,
    read_stats: dict[str, int] | None = None,
) -> dict | None:
    candles_5m = hl_client.get_candles(coin, "5m", 1)
    candles_15m = hl_client.get_candles(coin, "15m", 2)
    if read_stats is not None:
        read_stats["candles"] = read_stats.get("candles", 0) + 2
    if not candles_5m or not candles_15m:
        print(f"  [scalp_v2] {coin}: insufficient candle data", flush=True)
        return None

    bba = hl_client.get_best_bid_ask(coin)
    best_bid = bba["best_bid"]
    best_ask = bba["best_ask"]
    mark_price = price or 0.0
    if not best_bid or not best_ask or not mark_price:
        print(f"  [scalp_v2] {coin}: no price/book data", flush=True)
        return None

    with STATE.lock:
        coin_positions = ps.positions if ps else []
        has_open_position = any(abs(float(p.get("size", 0))) > 0 for p in coin_positions)
        equity = STATE.equity
        daily_loss = STATE.daily_loss

    market_data = {
        "candles_5m": candles_5m,
        "candles_15m": candles_15m,
        "account_equity": equity,
        "session_daily_loss": daily_loss,
        "session_consecutive_losses": SCALP_STRATEGY._consecutive_losses,
        "session_trade_count": len(SCALP_STRATEGY._performance),
        "mark_price": mark_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "open_position": {"side": "long", "entry": 0, "size": 0} if has_open_position else None,
    }
    return market_data


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _position_age_minutes(managed: dict) -> float:
    opened_at = managed.get("opened_at")
    if not opened_at:
        return 0.0
    try:
        opened_dt = datetime.fromisoformat(opened_at)
        return max(0.0, (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60.0)
    except ValueError:
        return 0.0


def _signal_for_strategy(ps: PairState | None, strategy_id: str) -> dict | None:
    if not ps or not ps.last_signals:
        return None
    for sig in ps.last_signals:
        if str(sig.get("strategy_id", "")) == strategy_id or str(sig.get("pack_id", "")) == strategy_id:
            return sig
    return None


def _legacy_strategy_config(ps: PairState, pack_id: str) -> dict[str, Any]:
    strategy_id = f"{ps.coin.lower()}_{pack_id}" if ps.coin else pack_id
    try:
        return signals.load_strategy_config(strategy_id)
    except Exception:
        return {}


def _load_scalp_v2_strategy_payload(coin: str | None = None) -> dict[str, Any]:
    candidates = []
    if coin:
        candidates.append(f"{coin.lower()}_scalp_v2")
    candidates.append("scalp_v2")
    for strategy_id in candidates:
        try:
            return signals.load_strategy_config(strategy_id)
        except Exception:
            continue
    return {}


def _scalp_v2_strategy_config(payload: dict[str, Any] | None = None) -> ScalpConfig:
    payload = payload or {}
    strategy_cfg = dict(payload.get("strategy") or payload)
    defaults = ScalpConfig()

    def _value(name: str) -> Any:
        value = strategy_cfg.get(name, getattr(defaults, name))
        if value is None:
            return getattr(defaults, name)
        return value

    return ScalpConfig(
        risk_per_trade_pct=float(_value("risk_per_trade_pct")),
        max_daily_loss_pct=float(_value("max_daily_loss_pct")),
        max_leverage=float(_value("max_leverage")),
        max_consecutive_losses=int(_value("max_consecutive_losses")),
        post_loss_size_multiplier=float(_value("post_loss_size_multiplier")),
        max_session_losses=int(_value("max_session_losses")),
        adx_min=float(_value("adx_min")),
        choppiness_max=float(_value("choppiness_max")),
        rvol_min=float(_value("rvol_min")),
        atr_period=int(_value("atr_period")),
        ema_fast_15m=int(_value("ema_fast_15m")),
        ema_slow_15m=int(_value("ema_slow_15m")),
        ema_fast_5m=int(_value("ema_fast_5m")),
        vwap_exclusion_hours=int(_value("vwap_exclusion_hours")),
        min_r_distance=float(_value("min_r_distance")),
        max_chase_atr=float(_value("max_chase_atr")),
        max_chase_atr_high_rvol=float(_value("max_chase_atr_high_rvol")),
        high_rvol_chase_threshold=float(_value("high_rvol_chase_threshold")),
        stop_atr_min=float(_value("stop_atr_min")),
        stop_atr_max=float(_value("stop_atr_max")),
        sl_limit_buffer_pct=float(_value("sl_limit_buffer_pct")),
        tp_limit_buffer_pct=float(_value("tp_limit_buffer_pct")),
        mark_price_buffer_pct=float(_value("mark_price_buffer_pct")),
        partial_exit_r=float(_value("partial_exit_r")),
        partial_exit_pct=float(_value("partial_exit_pct")),
        final_target_r=float(_value("final_target_r")),
        taker_fee=float(_value("taker_fee")),
        maker_fee=float(_value("maker_fee")),
        estimated_slippage=float(_value("estimated_slippage")),
        ioc_price_offset_pct=float(_value("ioc_price_offset_pct")),
        max_spread_pct=float(_value("max_spread_pct")),
        max_latency_ms=int(_value("max_latency_ms")),
        session_windows=list(_value("session_windows")),
        blocked_hours=list(_value("blocked_hours")),
        breakout_lookback=int(_value("breakout_lookback")),
        breakout_lookback_min=int(_value("breakout_lookback_min")),
        cvd_lookback=int(_value("cvd_lookback")),
    )


def _sync_scalp_v2_strategy(coin: str | None = None) -> dict[str, Any]:
    payload = _load_scalp_v2_strategy_payload(coin)
    SCALP_STRATEGY.config = _scalp_v2_strategy_config(payload)
    return payload


def _scalp_v2_config_payload(coin: str, symbol: str) -> dict[str, Any]:
    return {
        "strategy_id": f"{coin.lower()}_scalp_v2",
        "display_name": f"{coin} 5m Scalper",
        "enabled": True,
        "pack_id": "scalp_v2",
        "market": {"symbol": symbol, "coin": coin, "market_type": "perpetual"},
        "runner": {
            "source": "hyperliquid_candles",
            "anchor_timeframe": "15m",
            "trigger_timeframe": "5m",
            "eval_interval_seconds": 300,
        },
        "strategy": {
            "risk_per_trade_pct": 0.005,
            "max_daily_loss_pct": 0.015,
            "max_leverage": 2.0,
            "margin_mode": "isolated",
            "max_consecutive_losses": 3,
            "max_session_losses": 5,
            "adx_min": 20.0,
            "choppiness_max": 55.0,
            "rvol_min": 1.5,
            "min_r_distance": 1.5,
            "final_target_r": 1.8,
            "partial_exit_pct": 0.30,
            "partial_exit_r": 1.0,
            "max_spread_pct": 0.0005,
            "session_windows": [[8, 12], [13, 17]],
            "blocked_hours": [20, 21, 22, 23, 0, 1, 2, 3],
        },
        "exit_management": {
            "signal_flip_exit": True,
            "max_hold_minutes": 90,
            "stale_after_minutes": 30,
            "stale_min_follow_through_r": 0.5,
            "underwater_exit_r": -0.10,
            "hard_fail_r": -0.20,
        },
    }


# Load any available scalp config immediately so startup uses the installed
# workspace defaults rather than the hardcoded dataclass defaults.
_sync_scalp_v2_strategy()


def _restore_managed_position_from_portfolio(ps: PairState) -> bool:
    if ps.managed_position or not ps.positions:
        return False

    pos = ps.positions[0]
    raw_size = float(pos.get("size", 0) or 0)
    if raw_size == 0:
        return False

    strategy_id = ps.selected_pack_id or ps.plan_strategy or ps.pack_id or "unknown"
    entry_price = float(pos.get("entry_price", 0) or ps.plan_entry or 0)
    is_long = raw_size > 0
    stop_trigger = float(ps.plan_sl or 0) if ps.plan_sl else (entry_price * (0.99 if is_long else 1.01) if entry_price > 0 else 0.0)
    now_iso = datetime.now(timezone.utc).isoformat()
    managed = {
        "strategy_id": strategy_id,
        "side": "buy" if is_long else "sell",
        "entry_price": entry_price,
        "stop_trigger": stop_trigger,
        "stop_limit": stop_trigger * (0.997 if is_long else 1.003) if stop_trigger else None,
        "tp1_trigger": float(ps.plan_tp or 0) if ps.plan_tp else None,
        "tp1_limit": None,
        "tp2_trigger": None,
        "tp2_limit": None,
        "initial_size": abs(raw_size),
        "tp1_size": abs(raw_size) * 0.5,
        "tp2_size": abs(raw_size) * 0.5,
        "sl_oid": None,
        "tp1_oid": None,
        "tp2_oid": None,
        "opened_at": now_iso,
        "restored_at": now_iso,
        "restored_from_portfolio": True,
        "tp1_moved": False,
        "exit_pending": False,
    }
    ps.managed_position = managed
    if not ps.plan_entry and entry_price > 0:
        ps.plan_entry = entry_price
    if not ps.plan_strategy:
        ps.plan_strategy = strategy_label(strategy_id)
    if not ps.plan_reasons:
        ps.plan_reasons = ["Restored from live position after restart"]
    ps.bot_note = f"Restored {strategy_label(strategy_id)} position from live account state."
    ps.bot_details = [
        f"Recovered size: {abs(raw_size):.6f}",
        f"Recovered entry: ${entry_price:.2f}" if entry_price > 0 else "Recovered entry price unavailable",
    ]
    return True


def _scalp_v2_supervision_reason(
    coin: str,
    managed: dict,
    market_data: dict,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether an open scalp_v2 position should be closed now.

    The first cut is deliberately conservative: only flatten when the original
    trend thesis has clearly broken or the trade has gone stale.
    """
    payload = _sync_scalp_v2_strategy(coin)
    exit_cfg = dict(payload.get("exit_management") or {})
    candles_5m = market_data["candles_5m"]
    candles_15m = market_data["candles_15m"]
    df5 = _candles_to_df(candles_5m)
    df15 = _candles_to_df(candles_15m)
    regime = SCALP_STRATEGY._compute_regime(df5, df15)
    is_long = managed.get("side") == "buy"
    entry_price = float(managed.get("entry_price") or 0.0)
    stop_trigger = float(managed.get("stop_trigger") or managed.get("entry_price") or 0.0)
    risk = abs(entry_price - stop_trigger)
    current_price = float(market_data.get("mark_price") or 0.0)
    if risk <= 0 or current_price <= 0 or entry_price <= 0:
        return False, "", {"regime": regime, "current_r": 0.0, "age_minutes": _position_age_minutes(managed)}

    current_r = ((current_price - entry_price) / risk) if is_long else ((entry_price - current_price) / risk)
    age_minutes = _position_age_minutes(managed)

    reasons: list[str] = []
    try:
        ema_series = df5["close"].ewm(span=SCALP_STRATEGY.config.ema_fast_5m, adjust=False).mean()
        ema_5m = float(ema_series.iloc[-1])
        close_5m = float(df5["close"].iloc[-1])
    except Exception:
        ema_5m = 0.0
        close_5m = 0.0

    checks = [
        regime.ema_aligned,
        regime.adx_ok,
        regime.choppiness_ok,
        regime.vwap_ok,
        regime.atr_above_median,
        regime.rvol_ok,
        regime.cvd_confirming,
        regime.time_ok,
    ]
    regime_confidence = sum(1 for c in checks if c) / len(checks)

    if is_long:
        if regime.trend_bias == "bearish":
            reasons.append("15m trend bias flipped bearish")
        if close_5m and ema_5m and close_5m < ema_5m:
            reasons.append("5m close lost EMA20 support")
        if not regime.vwap_ok:
            reasons.append("price lost VWAP alignment")
        if not regime.cvd_confirming:
            reasons.append("order flow no longer confirms the long")
    else:
        if regime.trend_bias == "bullish":
            reasons.append("15m trend bias flipped bullish")
        if close_5m and ema_5m and close_5m > ema_5m:
            reasons.append("5m close lost EMA20 resistance")
        if not regime.vwap_ok:
            reasons.append("price lost VWAP alignment")
        if not regime.cvd_confirming:
            reasons.append("order flow no longer confirms the short")

    if regime.adx_value < 18:
        reasons.append("15m ADX faded below the regime boundary")
    if regime.choppiness > 58:
        reasons.append("5m market has become choppy")
    if regime_confidence < CONFIDENCE_LEVERAGE_THRESHOLD and (age_minutes >= 20 or current_r < 0.5):
        reasons.append("scalp confidence faded below leverage threshold")

    max_hold_minutes = float(exit_cfg.get("max_hold_minutes", 90))
    stale_after_minutes = float(exit_cfg.get("stale_after_minutes", max_hold_minutes / 2))
    stale_min_follow_through_r = float(exit_cfg.get("stale_min_follow_through_r", 0.5))
    underwater_exit_r = float(exit_cfg.get("underwater_exit_r", -0.10))
    hard_fail_r = float(exit_cfg.get("hard_fail_r", -0.20))

    if age_minutes >= max_hold_minutes:
        reasons.append("trade has been open too long")
    elif age_minutes >= stale_after_minutes and current_r < stale_min_follow_through_r:
        reasons.append("trade has stalled without follow-through")

    if current_r <= underwater_exit_r:
        reasons.append(f"position is down {current_r:.2f}R")
    if current_r <= hard_fail_r:
        reasons.append("scalp thesis failed hard")

    if len(reasons) >= 2 and (current_r < 0.0 or age_minutes >= stale_after_minutes):
        summary = "; ".join(reasons[:3])
        return True, summary, {"regime": regime, "current_r": current_r, "age_minutes": age_minutes}

    if age_minutes >= max_hold_minutes:
        summary = "; ".join(reasons[:3]) if reasons else "trade exceeded maximum open time"
        return True, summary, {"regime": regime, "current_r": current_r, "age_minutes": age_minutes}

    if len(reasons) >= 3:
        summary = "; ".join(reasons[:3])
        return True, summary, {"regime": regime, "current_r": current_r, "age_minutes": age_minutes}

    return False, "; ".join(reasons[:2]), {"regime": regime, "current_r": current_r, "age_minutes": age_minutes}


def _legacy_supervision_reason(
    coin: str,
    ps: PairState,
    managed: dict,
    price: float | None,
) -> tuple[bool, str, dict[str, Any]]:
    current_price = float(price or ps.last_price or 0.0)
    entry_price = float(managed.get("entry_price") or ps.plan_entry or 0.0)
    stop_trigger = float(managed.get("stop_trigger") or ps.plan_sl or 0.0)
    risk = abs(entry_price - stop_trigger)
    age_minutes = _position_age_minutes(managed)
    if current_price <= 0 or entry_price <= 0 or risk <= 0:
        return False, "", {"current_r": 0.0, "age_minutes": age_minutes, "signal": None}

    is_long = managed.get("side") == "buy"
    current_r = ((current_price - entry_price) / risk) if is_long else ((entry_price - current_price) / risk)
    strategy_id = str(managed.get("strategy_id") or ps.selected_pack_id or ps.pack_id)
    pack_id = str(ps.selected_pack_id or ps.pack_id or strategy_id)
    config = _legacy_strategy_config(ps, pack_id)
    exit_cfg = dict(config.get("exit_management") or {})
    sig = _signal_for_strategy(ps, pack_id) or _best_signal(ps.last_signals, actionable_only=True)
    sig_dir = str(sig.get("direction", "none")) if sig else "none"
    sig_conf = _normalized_confidence(sig.get("confidence", 0)) if sig else 0.0

    max_hold_minutes = float(exit_cfg.get("max_hold_minutes", 180 if pack_id not in {"liquidity_sweep_reversal"} else 90))
    stale_after_minutes = float(exit_cfg.get("stale_after_minutes", max_hold_minutes / 2))
    stale_min_follow_through_r = float(exit_cfg.get("stale_min_follow_through_r", 0.25))
    underwater_exit_r = float(exit_cfg.get("underwater_exit_r", -0.25))
    hard_fail_r = float(exit_cfg.get("hard_fail_r", underwater_exit_r - 0.5))
    use_signal_flip = bool(exit_cfg.get("signal_flip_exit", True))

    reasons: list[str] = []
    if sig and use_signal_flip:
        if is_long and sig_dir in {"sell", "short"}:
            reasons.append("latest signal flipped bearish")
        elif (not is_long) and sig_dir in {"buy", "long"}:
            reasons.append("latest signal flipped bullish")
    else:
        reasons.append("no fresh signal to support the position")
    if sig_conf and sig_conf < CONFIDENCE_LEVERAGE_THRESHOLD and (age_minutes >= stale_after_minutes or current_r < 0.5):
        reasons.append("signal confidence faded below leverage threshold")

    if current_r <= underwater_exit_r:
        reasons.append(f"position is down {current_r:.2f}R")
    if managed.get("restored_from_portfolio") and age_minutes <= 30 and current_r < 0.0:
        reasons.append("restored position is already under water")

    if pack_id == "trend_pullback":
        if sig_dir in {"", "none", None}:
            reasons.append("trend pullback scan is no longer confirmed")
        if age_minutes >= max_hold_minutes:
            reasons.append("trend pullback has been open too long")
        elif age_minutes >= stale_after_minutes and current_r < stale_min_follow_through_r:
            reasons.append("trend pullback lacks follow-through")
        if current_r <= hard_fail_r:
            reasons.append("trend pullback thesis failed hard")
    elif pack_id == "compression_breakout":
        if sig_dir in {"", "none", None}:
            reasons.append("breakout is no longer expanding")
        if age_minutes >= max_hold_minutes:
            reasons.append("breakout has gone stale")
        elif age_minutes >= stale_after_minutes and current_r < stale_min_follow_through_r:
            reasons.append("breakout has not followed through")
        if current_r <= hard_fail_r:
            reasons.append("breakout failed and momentum faded")
    elif pack_id == "liquidity_sweep_reversal":
        if sig_dir in {"", "none", None}:
            reasons.append("reversal scan is no longer clean")
        if age_minutes >= max_hold_minutes:
            reasons.append("sweep reversal has been open too long")
        elif age_minutes >= stale_after_minutes and current_r < stale_min_follow_through_r:
            reasons.append("sweep reversal did not snap back quickly")
        if current_r <= hard_fail_r:
            reasons.append("sweep reversal failed")
    else:
        if sig_dir in {"", "none", None}:
            reasons.append("legacy scan is no longer actionable")
        if age_minutes >= max_hold_minutes:
            reasons.append("legacy position has been open too long")
        elif age_minutes >= stale_after_minutes and current_r < stale_min_follow_through_r:
            reasons.append("legacy position has gone stale")
        if current_r <= hard_fail_r:
            reasons.append("legacy position failed")

    if len(reasons) >= 2 and (current_r <= underwater_exit_r or age_minutes >= max_hold_minutes):
        summary = "; ".join(reasons[:3])
        return True, summary, {"current_r": current_r, "age_minutes": age_minutes, "signal": sig, "pack_id": pack_id}

    if len(reasons) >= 3:
        summary = "; ".join(reasons[:3])
        return True, summary, {"current_r": current_r, "age_minutes": age_minutes, "signal": sig, "pack_id": pack_id}

    return False, "; ".join(reasons[:2]), {"current_r": current_r, "age_minutes": age_minutes, "signal": sig, "pack_id": pack_id}


def _close_scalp_v2_position(coin: str, ps, managed: dict, reason: str) -> bool:
    current_size = _position_size(ps)
    if current_size <= 0:
        return False

    is_long = managed.get("side") == "buy"
    with STATE.lock:
        managed["exit_pending"] = True
        managed["exit_reason"] = reason
        ps.bot_note = f"Supervisor is closing {coin} because {reason}."
        ps.bot_details = [
            f"Open size: {current_size:.6f}",
            f"Entry: ${float(managed.get('entry_price') or 0.0):.2f}",
        ]

    close_result = hl_client.place_order(coin, not is_long, current_size, order_type="market", reduce_only=True)
    if not close_result.ok:
        with STATE.lock:
            managed["exit_pending"] = False
        log_trade("CLOSE_FAIL", "scalp_v2", current_size, ps.last_price or 0,
                  f"{coin}: {reason} — {close_result.error}")
        return False

    cancel_results = hl_client.cancel_all_orders(coin)
    cancel_errors = [r.error for r in cancel_results if not r.ok and r.error]
    if cancel_errors:
        log_trade("MANAGE_FAIL", "scalp_v2", 0, ps.last_price or 0,
                  f"{coin}: cancelled position but some child orders failed to cancel: {cancel_errors[0]}")

    log_trade("CLOSE", "scalp_v2", current_size, ps.last_price or 0,
              f"{coin}: {reason}")
    return True


def _supervise_open_position(coin: str, ps, price: float | None, read_stats: dict[str, int] | None = None) -> bool:
    managed = ps.managed_position
    if not managed or managed.get("exit_pending"):
        return False

    strategy_id = str(managed.get("strategy_id") or ps.selected_pack_id or ps.pack_id)
    if strategy_id == "scalp_v2":
        market_data = _load_scalp_v2_market_data(coin, ps, price, read_stats=read_stats)
        if not market_data:
            return False

        should_close, reason, metrics = _scalp_v2_supervision_reason(coin, managed, market_data)
        current_r = metrics.get("current_r", 0.0)
        age_minutes = metrics.get("age_minutes", 0.0)

        if should_close:
            pretty_reason = reason or "scalp_v2 thesis failed"
            print(
                f"  [scalp_v2] {coin} SUPERVISION EXIT — {pretty_reason} "
                f"(R={current_r:+.2f}, age={age_minutes:.0f}m)",
                flush=True,
            )
            return _close_scalp_v2_position(coin, ps, managed, pretty_reason)

        with STATE.lock:
            ps.bot_note = f"Managing scalp_v2 thesis. Open R={current_r:+.2f}, age={age_minutes:.0f}m."
        return False

    should_close, reason, metrics = _legacy_supervision_reason(coin, ps, managed, price)
    current_r = metrics.get("current_r", 0.0)
    age_minutes = metrics.get("age_minutes", 0.0)

    if should_close:
        pretty_reason = reason or f"{strategy_id} thesis failed"
        print(
            f"  [supervisor] {coin} EXIT — {pretty_reason} "
            f"(strategy={strategy_id}, R={current_r:+.2f}, age={age_minutes:.0f}m)",
            flush=True,
        )
        return _close_scalp_v2_position(coin, ps, managed, pretty_reason) if strategy_id == "scalp_v2" else _close_managed_position(coin, ps, managed, pretty_reason, strategy_id)

    with STATE.lock:
        ps.bot_note = f"Managing {strategy_label(strategy_id)} thesis. Open R={current_r:+.2f}, age={age_minutes:.0f}m."
    return False


def _close_managed_position(coin: str, ps, managed: dict, reason: str, strategy_id: str) -> bool:
    current_size = _position_size(ps)
    if current_size <= 0:
        return False

    is_long = managed.get("side") == "buy"
    with STATE.lock:
        managed["exit_pending"] = True
        managed["exit_reason"] = reason
        ps.bot_note = f"Supervisor is closing {coin} because {reason}."
        ps.bot_details = [
            f"Open size: {current_size:.6f}",
            f"Entry: ${float(managed.get('entry_price') or 0.0):.2f}",
        ]

    close_result = hl_client.place_order(coin, not is_long, current_size, order_type="market", reduce_only=True)
    if not close_result.ok:
        with STATE.lock:
            managed["exit_pending"] = False
        log_trade("CLOSE_FAIL", strategy_id, current_size, ps.last_price or 0,
                  f"{coin}: {reason} — {close_result.error}")
        return False

    cancel_results = hl_client.cancel_all_orders(coin)
    cancel_errors = [r.error for r in cancel_results if not r.ok and r.error]
    if cancel_errors:
        log_trade("MANAGE_FAIL", strategy_id, 0, ps.last_price or 0,
                  f"{coin}: cancelled position but some child orders failed to cancel: {cancel_errors[0]}")

    log_trade("CLOSE", strategy_id, current_size, ps.last_price or 0, f"{coin}: {reason}")
    return True


def _evaluate_legacy_signals(
    coin: str,
    price: float,
    read_stats: dict[str, int] | None = None,
) -> tuple[list[Any], list[dict]]:
    candles_1d = hl_client.get_candles(coin, "1d", 30)
    candles_4h = hl_client.get_candles(coin, "4h", 14)
    if read_stats is not None:
        read_stats["candles"] = read_stats.get("candles", 0) + 2
    sigs = signals.detect_all_signals(candles_1d, candles_4h, price, coin=coin)
    sig_dicts = [
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
    return sigs, sig_dicts


def _run_scalp_v2_cycle(
    coin: str,
    ps,
    price: float | None,
    master_address: str | None,
    read_stats: dict[str, int] | None = None,
) -> None:
    """Run one evaluation cycle of the scalp_v2 strategy for a coin.

    Fetches 5m/15m candles, evaluates via ScalpStrategy, and if live + TRADE signal,
    submits entry + TP/SL trigger orders via hl_client.
    """
    try:
        with STATE.lock:
            has_open_position = bool(ps and ps.last_position_size > 0)
        if has_open_position:
            return

        signal, sig_dict = _evaluate_scalp_v2_signal(coin, ps, price, read_stats=read_stats)
        if not signal or not sig_dict:
            return
        if ps:
            with STATE.lock:
                ps.last_signals = [sig_dict]
                ps.last_scan_ts = datetime.now(timezone.utc).isoformat()
                ps.selected_pack_id = "scalp_v2"
                if signal.action == "TRADE" and signal.order_params:
                    op = signal.order_params
                    ps.plan_entry = op.entry_price
                    ps.plan_sl = op.stop_trigger
                    ps.plan_tp = op.tp_final_trigger
                    ps.plan_strategy = "5m Scalper"
                    ps.plan_reasons = [signal.setup.direction if signal.setup else ""]
                _update_pair_bot_context(ps)

        with STATE.lock:
            if not STATE.portfolio_ready:
                return

        # Log every evaluation
        if signal.action == "TRADE":
            print(f"  [scalp_v2] {signal.summary()}", flush=True)
        else:
            reasons_str = "; ".join(signal.rejection_reasons[:3])
            print(f"  [scalp_v2] {coin} NO TRADE — {reasons_str}", flush=True)

        # Execute if live
        with STATE.lock:
            live_enabled = STATE.live_enabled
            card_live = ps.trading_live if ps else False
            margin_mode = ps.margin_mode if ps else STATE.margin_mode
            portfolio_ready = STATE.portfolio_ready
            if ps and (_is_in_cooldown(ps) or _is_in_reentry_lockout(ps)):
                return

        if not portfolio_ready:
            return

        if signal.action == "TRADE" and live_enabled and card_live and signal.order_params:
            op = signal.order_params
            if signal.effective_r_net < 0.9:
                log_trade("SKIP", "scalp_v2", 0, op.entry_price,
                          f"net edge {signal.effective_r_net:.2f}R below 0.90R threshold")
                return
            if signal.confidence < 7:
                log_trade("SKIP", "scalp_v2", 0, op.entry_price,
                          f"confidence {signal.confidence}/10 below live threshold")
                return

            # Portfolio regime gate
            with STATE.lock:
                regime_level = STATE.market_regime
                regime_mult = STATE.regime_size_multiplier
                regime_min_conf = STATE.regime_min_confidence
            if regime_level == "red":
                log_trade("SKIP", "scalp_v2", 0, op.entry_price,
                          f"portfolio regime RED — no new entries")
                return
            if regime_level == "yellow" and (signal.confidence / 10.0) < regime_min_conf:
                log_trade("SKIP", "scalp_v2", 0, op.entry_price,
                          f"portfolio regime YELLOW — confidence {signal.confidence}/10 below {regime_min_conf:.0%} threshold")
                return
            # Apply regime size reduction
            if regime_mult < 1.0:
                op.size = round(op.size * regime_mult, 6)
                op.tp1_size = round(op.tp1_size * regime_mult, 6)
                op.tp_final_size = round(op.tp_final_size * regime_mult, 6)

            op.leverage = float(_confidence_to_leverage(signal.confidence / 10.0, op.leverage))

            # Order book depth check
            notional = op.size * op.entry_price
            depth = hl_client.check_depth(coin, notional)
            if not depth["sufficient"]:
                log_trade("SKIP", "scalp_v2", op.size, op.entry_price,
                          f"insufficient depth: {depth['depth_ratio']:.1f}x (need 2.0x) for ${notional:.0f} order")
                return

            # Enforce Hyperliquid minimum order value ($10)
            if notional < 10.0:
                log_trade("SKIP", "scalp_v2", op.size, op.entry_price,
                          f"notional ${notional:.2f} < $10 min")
                return

            # 1. Set leverage
            lev_result = hl_client.update_leverage(coin, int(op.leverage), margin_mode=margin_mode)
            if not lev_result.ok:
                log_trade("LEV_FAIL", "scalp_v2", 0, 0,
                          f"leverage {int(op.leverage)}x failed: {lev_result.error}")

            # 2. Place entry order (prefer maker/ALO for retest, IOC for momentum)
            is_buy = op.side == "buy"
            if op.entry_order_type == "Alo":
                entry_result = hl_client.place_order(
                    coin, is_buy, op.size, price=op.entry_price,
                    order_type="post_only",
                )
            else:
                entry_result = hl_client.place_order(
                    coin, is_buy, op.size, order_type="market",
                )

            log_trade(
                "BUY" if is_buy else "SELL", "scalp_v2", op.size, op.entry_price,
                f"conf={signal.confidence}/10 Lev={op.leverage}x R={signal.effective_r_net:.2f} "
                f"SL={op.stop_trigger} TP={op.tp_final_trigger}",
            )

            if entry_result.ok:
                log_trade("FILLED", "scalp_v2", op.size, op.entry_price,
                          f"oid={entry_result.order_id}")

                # 3. Place stop loss (trigger + explicit limit)
                sl_result = hl_client.place_trigger_order(
                    coin,
                    is_buy=not is_buy,  # opposite side for exit
                    size=op.size,
                    trigger_price=op.stop_trigger,
                    limit_price=op.stop_limit,
                    tp_or_sl="sl",
                    reduce_only=True,
                )
                if not sl_result.ok:
                    log_trade("SL_FAIL", "scalp_v2", op.size, op.stop_trigger,
                              f"SL placement failed: {sl_result.error} — FLATTENING")
                    # Failsafe: flatten immediately if SL can't be placed
                    hl_client.place_order(coin, not is_buy, op.size,
                                          order_type="market", reduce_only=True)
                    return

                # 4. Place TP1 (partial exit at 1R)
                tp1_result = hl_client.place_trigger_order(
                    coin,
                    is_buy=not is_buy,
                    size=op.tp1_size,
                    trigger_price=op.tp1_trigger,
                    limit_price=op.tp1_limit,
                    tp_or_sl="tp",
                    reduce_only=True,
                )
                if tp1_result.ok:
                    log_trade("TP1_SET", "scalp_v2", op.tp1_size, op.tp1_trigger,
                              f"partial 30% at 1R")

                # 5. Place TP final (remainder at 1.8R)
                tp_final_result = hl_client.place_trigger_order(
                    coin,
                    is_buy=not is_buy,
                    size=op.tp_final_size,
                    trigger_price=op.tp_final_trigger,
                    limit_price=op.tp_final_limit,
                    tp_or_sl="tp",
                    reduce_only=True,
                )
                if tp_final_result.ok:
                    log_trade("TP2_SET", "scalp_v2", op.tp_final_size, op.tp_final_trigger,
                              f"final 70% at 1.8R")
                if ps:
                    with STATE.lock:
                        ps.managed_position = {
                            "strategy_id": "scalp_v2",
                            "side": op.side,
                            "entry_price": op.entry_price,
                            "entry_confidence": signal.confidence / 10.0,
                            "target_leverage": op.leverage,
                            "stop_trigger": op.stop_trigger,
                            "stop_limit": op.stop_limit,
                            "tp1_trigger": op.tp1_trigger,
                            "tp1_limit": op.tp1_limit,
                            "tp2_trigger": op.tp_final_trigger,
                            "tp2_limit": op.tp_final_limit,
                            "initial_size": op.size,
                            "tp1_size": op.tp1_size,
                            "tp2_size": op.tp_final_size,
                            "sl_oid": sl_result.order_id,
                            "tp1_oid": tp1_result.order_id if tp1_result.ok else None,
                            "tp2_oid": tp_final_result.order_id if tp_final_result.ok else None,
                            "opened_at": datetime.now(timezone.utc).isoformat(),
                            "tp1_moved": False,
                            "exit_pending": False,
                        }
            else:
                log_trade("REJECTED", "scalp_v2", op.size, op.entry_price,
                          entry_result.error or "unknown")

    except Exception as e:
        print(f"  [scalp_v2] {coin} cycle error: {e}", flush=True)
        traceback.print_exc()


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

    coins = STATE.all_coins() or [STATE.coin]
    print(f"[dashboard] Monitoring {', '.join(coins)}", flush=True)
    print(f"[dashboard] Wallet: {master_address or 'NOT CONNECTED'}", flush=True)
    print(f"[dashboard] Network: MAINNET (real funds)", flush=True)
    print(f"[dashboard] Live trading: {'ENABLED' if STATE.live_enabled else 'disabled (view-only)'}", flush=True)

    cycle = 0
    while not STOP_EVENT.is_set():
        try:
            # Sync legacy fields before generating thinking message
            with STATE.lock:
                STATE._sync_legacy()
                STATE.thinking = _thinking_message(cycle, STATE)
            cycle += 1

            cycle_reads = {"mids": 0, "candles": 0, "portfolio": 0, "open_orders": 0}
            try:
                mids_snapshot = hl_client.get_all_mids()
                cycle_reads["mids"] += 1
            except Exception as price_err:
                mids_snapshot = {}
                _log_throttled_error(
                    "price-snapshot",
                    f"[dashboard] Price snapshot error (using cached values when available): {price_err}",
                    throttle_seconds=30.0,
                )

            # Portfolio-level regime check (every 5 minutes)
            now_mono_regime = time.monotonic()
            with STATE.lock:
                regime_due = now_mono_regime - STATE.last_regime_check_mono >= 300.0 or cycle <= 1
            if regime_due and mids_snapshot:
                try:
                    btc_candles = hl_client.get_candles("BTC", "1d", 60)
                    # Gather funding rates from meta+asset contexts
                    funding_rates = {}
                    try:
                        mac = hl_client.get_meta_and_asset_ctxs()
                        if isinstance(mac, list) and len(mac) == 2:
                            universe = mac[0].get("universe", [])
                            ctxs = mac[1]
                            for i, asset in enumerate(universe):
                                if i < len(ctxs):
                                    coin_name = asset.get("name", "")
                                    fr = ctxs[i].get("funding")
                                    if fr is not None and coin_name:
                                        try:
                                            funding_rates[coin_name] = float(fr)
                                        except (TypeError, ValueError):
                                            pass
                    except Exception:
                        pass
                    btc_price_str = mids_snapshot.get("BTC")
                    btc_price = float(btc_price_str) if btc_price_str else None
                    mr = portfolio_regime.evaluate(btc_candles, funding_rates or None, None, btc_price)
                    with STATE.lock:
                        STATE.market_regime = mr.level
                        STATE.regime_size_multiplier = mr.size_multiplier
                        STATE.regime_min_confidence = mr.min_confidence
                        STATE.regime_reasons = mr.reasons
                        STATE.last_regime_check_mono = now_mono_regime
                    if cycle <= 2 or mr.level != "green":
                        print(f"[dashboard] Market regime: {mr.level.upper()} (size={mr.size_multiplier}x) — {'; '.join(mr.reasons[:2])}", flush=True)
                except Exception as regime_err:
                    _log_throttled_error("regime", f"[dashboard] Regime check error: {regime_err}", throttle_seconds=60.0)

            # Iterate over all registered pairs
            with STATE.lock:
                coins = STATE.all_coins()
                active_coins = [
                    coin for coin in coins
                    if (ps := STATE.pairs.get(coin)) and (ps.managed_position or ps.last_position_size > 0)
                ]
                scan_cursor = STATE.scan_cursor
                scan_budget = min(len(coins), max(SCAN_BATCH_SIZE, len(active_coins) or 0))
                selected_coins: list[str] = list(dict.fromkeys(active_coins))
                if coins:
                    idx = scan_cursor
                    while len(selected_coins) < scan_budget and len(selected_coins) < len(coins):
                        coin = coins[idx % len(coins)]
                        if coin not in selected_coins:
                            selected_coins.append(coin)
                        idx += 1
                    STATE.scan_cursor = (scan_cursor + max(scan_budget, 1)) % len(coins)

            if not coins:
                STOP_EVENT.wait(3)
                continue
            for coin in selected_coins:
                if not coin:
                    continue
                with STATE.lock:
                    ps = STATE.pairs.get(coin)
                    pair_enabled = ps.enabled if ps else True
                if ps and not pair_enabled:
                    continue

                # Determine which strategy pack this pair uses
                with STATE.lock:
                    current_pack_id = ps.pack_id if ps else "trend_pullback"
                    auto_strategy = ps.auto_strategy if ps else True

                try:
                    price_str = mids_snapshot.get(coin)
                    price = float(price_str) if price_str else None
                    if price is None:
                        price = hl_client.get_mid_price(coin, mids=mids_snapshot)
                    if price and ps:
                        with STATE.lock:
                            ps.last_price = price

                    scalp_signal = None
                    scalp_sig_dict = None
                    if auto_strategy or current_pack_id == "scalp_v2":
                        scalp_signal, scalp_sig_dict = _evaluate_scalp_v2_signal(coin, ps, price, read_stats=cycle_reads)

                    sigs, sig_dicts = _evaluate_legacy_signals(coin, price or 0.0, read_stats=cycle_reads)
                    legacy_by_pack = {s.pack_id: s for s in sigs}

                    if auto_strategy:
                        combined_sig_dicts = [sig for sig in sig_dicts if sig.get("pack_id") in AUTO_STRATEGY_PACK_IDS]
                        if scalp_sig_dict:
                            combined_sig_dicts.append(scalp_sig_dict)
                        selected_sig = _best_signal(combined_sig_dicts) or {"pack_id": current_pack_id, "direction": "none", "reasons": []}
                        selected_pack_id = str(selected_sig.get("pack_id", current_pack_id))
                        if ps:
                            with STATE.lock:
                                ps.last_signals = combined_sig_dicts
                                ps.last_scan_ts = datetime.now(timezone.utc).isoformat()
                                ps.selected_pack_id = selected_pack_id
                                _update_pair_plan_from_signal(ps, selected_sig)
                                _update_pair_bot_context(ps)
                    else:
                        selected_pack_id = current_pack_id
                        selected_sig = _find_signal_by_pack(sig_dicts, current_pack_id)
                        if current_pack_id == "scalp_v2" and scalp_sig_dict:
                            selected_sig = scalp_sig_dict
                        if ps:
                            with STATE.lock:
                                ps.last_signals = [scalp_sig_dict] if current_pack_id == "scalp_v2" and scalp_sig_dict else sig_dicts
                                ps.last_scan_ts = datetime.now(timezone.utc).isoformat()
                                ps.selected_pack_id = current_pack_id
                                _update_pair_plan_from_signal(ps, selected_sig or _best_signal(ps.last_signals))
                                _update_pair_bot_context(ps)
                except Exception as scan_err:
                    if _is_transient_read_error(scan_err):
                        _log_throttled_error(f"scan:{coin}", f"  [scan] {coin} throttled: {scan_err}", throttle_seconds=60.0)
                    else:
                        print(f"  [scan] {coin} error: {scan_err}", flush=True)
                    continue

                if not auto_strategy and current_pack_id == "scalp_v2":
                    _run_scalp_v2_cycle(coin, ps, price, master_address)
                    continue

                # Execute trades if live and this card is active (legacy strategies)
                with STATE.lock:
                    live_enabled = STATE.live_enabled
                    card_live = ps.trading_live if ps else False
                    trading_active = STATE.trading_active
                    loss_limit = STATE.daily_loss_limit_usd()
                    daily_loss = STATE.daily_loss
                    max_daily_loss_pct = STATE.max_daily_loss_pct
                    max_leverage = ps.max_leverage if ps else STATE.max_leverage
                    risk_per_trade_pct = ps.risk_per_trade_pct if ps else STATE.risk_per_trade_pct
                    margin_mode = ps.margin_mode if ps else STATE.margin_mode
                    equity = STATE.equity

                if live_enabled and card_live and price:
                    if ps and (_is_in_cooldown(ps) or _is_in_reentry_lockout(ps)):
                        continue
                    if loss_limit > 0 and daily_loss >= loss_limit:
                        with STATE.lock:
                            STATE.trading_active = False
                            STATE.error = f"Daily loss limit reached (${STATE.daily_loss:.2f} >= ${loss_limit:.2f} = {max_daily_loss_pct}% of equity). Trading halted."
                        log_trade("HALT", "system", 0, 0, "daily loss limit reached")
                        break

                    # Skip entry signals if we already have an open position for this coin
                    with STATE.lock:
                        coin_positions = STATE.pairs[coin].positions if coin in STATE.pairs else []
                        has_open_position = any(
                            abs(float(p.get("size", 0))) > 0 for p in coin_positions
                        )

                    if auto_strategy and selected_pack_id == "scalp_v2" and selected_sig.get("direction") != "none":
                        _run_scalp_v2_cycle(coin, ps, price, master_address)
                        continue

                    # Portfolio regime gate for legacy strategies
                    with STATE.lock:
                        regime_level = STATE.market_regime
                        regime_mult = STATE.regime_size_multiplier
                        regime_min_conf = STATE.regime_min_confidence
                    if regime_level == "red":
                        log_trade("SKIP", "legacy", 0, price, "portfolio regime RED — no new entries")
                        continue

                    for sig_data, sig_obj in zip(sig_dicts, sigs):
                        if sig_obj.pack_id != selected_pack_id:
                            continue
                        if sig_obj.direction == signals.Direction.NONE:
                            continue
                        if sig_obj.confidence < MIN_LEGACY_SIGNAL_CONFIDENCE:
                            continue
                        if regime_level == "yellow" and sig_obj.confidence < regime_min_conf:
                            log_trade("SKIP", sig_obj.strategy_id, 0, price,
                                      f"portfolio regime YELLOW — confidence {sig_obj.confidence:.2f} below {regime_min_conf:.0%}")
                            continue
                        if coin == "TAO" and sig_obj.pack_id == "trend_pullback":
                            log_trade("BLOCK", sig_obj.strategy_id, 0, price,
                                      "TAO trend_pullback live trading disabled pending better sample")
                            continue
                        if equity <= 0 or not sig_obj.stop_loss:
                            continue

                        # Don't open a new position if one is already open for this coin
                        if has_open_position:
                            continue

                        risk_amount = equity * (risk_per_trade_pct / 100) * _confidence_risk_scale(sig_obj.confidence)
                        # Apply regime size reduction
                        if regime_mult < 1.0:
                            risk_amount *= regime_mult
                        price_risk = abs(price - sig_obj.stop_loss)
                        if price_risk <= 0:
                            continue

                        size = risk_amount / price_risk
                        live_max_leverage = _confidence_to_leverage(sig_obj.confidence, max_leverage)
                        max_notional = equity * live_max_leverage
                        max_size = max_notional / price if price else 0
                        size = min(size, max_size)
                        if size <= 0:
                            continue

                        # Order book depth check
                        notional = size * price
                        depth = hl_client.check_depth(coin, notional)
                        if not depth["sufficient"]:
                            log_trade("SKIP", sig_obj.strategy_id, size, price,
                                      f"insufficient depth: {depth['depth_ratio']:.1f}x (need 2.0x) for ${notional:.0f}")
                            continue

                        # Enforce Hyperliquid minimum order value ($10)
                        if notional < 10.0:
                            log_trade("SKIP", sig_obj.strategy_id, size, price,
                                      f"notional ${notional:.2f} < $10 min")
                            continue

                        is_buy = sig_obj.direction == signals.Direction.BUY
                        log_trade("BUY" if is_buy else "SELL", sig_obj.strategy_id, size, price,
                                  f"confidence={sig_obj.confidence:.2f}, Lev={live_max_leverage}x, SL={sig_obj.stop_loss:.2f}")

                        lev_result = hl_client.update_leverage(coin, int(live_max_leverage), margin_mode=margin_mode)
                        if not lev_result.ok:
                            log_trade("LEV_FAIL", sig_obj.strategy_id, 0, 0,
                                      f"leverage {int(live_max_leverage)}x {margin_mode} failed: {lev_result.error}")
                            continue

                        result = hl_client.place_order(coin, is_buy, size, order_type="market")
                        if result.ok:
                            log_trade("FILLED", sig_obj.strategy_id, size, price, f"oid={result.order_id}")
                            if sig_obj.stop_loss:
                                sl_trigger = float(sig_obj.stop_loss)
                                sl_limit = _legacy_trigger_limit(sl_trigger, is_buy_entry=is_buy, tp_or_sl="sl")
                                sl_result = hl_client.place_trigger_order(
                                    coin,
                                    is_buy=not is_buy,
                                    size=size,
                                    trigger_price=sl_trigger,
                                    limit_price=sl_limit,
                                    tp_or_sl="sl",
                                    reduce_only=True,
                                )
                                if not sl_result.ok:
                                    log_trade("SL_FAIL", sig_obj.strategy_id, size, sl_trigger,
                                              f"SL placement failed: {sl_result.error} — FLATTENING")
                                    hl_client.place_order(coin, not is_buy, size,
                                                          order_type="market", reduce_only=True)
                                    continue
                                log_trade("SL_SET", sig_obj.strategy_id, size, sl_trigger, "reduce-only trigger")

                            risk_dist = abs(price - float(sig_obj.stop_loss))
                            tp1_trigger = float(sig_obj.take_profit or (price + risk_dist if is_buy else price - risk_dist))
                            tp2_trigger = price + (2 * risk_dist if is_buy else -2 * risk_dist)
                            tp1_size = size * 0.5
                            tp2_size = size - tp1_size

                            tp1_limit = _legacy_trigger_limit(tp1_trigger, is_buy_entry=is_buy, tp_or_sl="tp")
                            tp1_result = hl_client.place_trigger_order(
                                coin,
                                is_buy=not is_buy,
                                size=tp1_size,
                                trigger_price=tp1_trigger,
                                limit_price=tp1_limit,
                                tp_or_sl="tp",
                                reduce_only=True,
                            )
                            if tp1_result.ok:
                                log_trade("TP1_SET", sig_obj.strategy_id, tp1_size, tp1_trigger, "reduce-only trigger")
                            else:
                                log_trade("TP1_FAIL", sig_obj.strategy_id, tp1_size, tp1_trigger,
                                          tp1_result.error or "unknown")

                            tp2_result = None
                            if tp2_size > 0:
                                tp2_limit = _legacy_trigger_limit(tp2_trigger, is_buy_entry=is_buy, tp_or_sl="tp")
                                tp2_result = hl_client.place_trigger_order(
                                    coin,
                                    is_buy=not is_buy,
                                    size=tp2_size,
                                    trigger_price=tp2_trigger,
                                    limit_price=tp2_limit,
                                    tp_or_sl="tp",
                                    reduce_only=True,
                                )
                            if tp2_result.ok:
                                log_trade("TP2_SET", sig_obj.strategy_id, tp2_size, tp2_trigger, "reduce-only trigger")
                            else:
                                log_trade("TP2_FAIL", sig_obj.strategy_id, tp2_size, tp2_trigger,
                                          tp2_result.error or "unknown")

                            if ps:
                                with STATE.lock:
                                    ps.managed_position = {
                                        "strategy_id": sig_obj.strategy_id,
                                        "side": "buy" if is_buy else "sell",
                                        "entry_price": price,
                                        "entry_confidence": _normalized_confidence(sig_obj.confidence),
                                        "target_leverage": live_max_leverage,
                                        "stop_trigger": float(sig_obj.stop_loss) if sig_obj.stop_loss else None,
                                        "stop_limit": sl_limit if sig_obj.stop_loss else None,
                                        "tp1_trigger": tp1_trigger,
                                        "tp1_limit": tp1_limit,
                                        "tp2_trigger": tp2_trigger,
                                        "tp2_limit": tp2_limit if tp2_result else None,
                                        "initial_size": size,
                                        "tp1_size": tp1_size,
                                        "tp2_size": tp2_size,
                                        "sl_oid": sl_result.order_id if sig_obj.stop_loss else None,
                                        "tp1_oid": tp1_result.order_id if tp1_result.ok else None,
                                        "tp2_oid": tp2_result.order_id if tp2_result and tp2_result.ok else None,
                                        "opened_at": datetime.now(timezone.utc).isoformat(),
                                        "tp1_moved": False,
                                        "exit_pending": False,
                                    }
                        else:
                            log_trade("REJECTED", sig_obj.strategy_id, size, price, result.error or "unknown")

            # Fetch portfolio once (shared across all pairs)
            now_mono = time.monotonic()
            if master_address:
                with STATE.lock:
                    portfolio_due = (
                        cycle <= 1
                        or (
                            now_mono >= STATE.portfolio_backoff_until_mono
                            and now_mono - STATE.last_portfolio_sync_mono >= PORTFOLIO_SYNC_SECONDS
                        )
                    )
                if portfolio_due:
                    portfolio = None
                try:
                    portfolio = hl_client.get_portfolio_value(master_address)
                    cycle_reads["portfolio"] += 1
                    with STATE.lock:
                        STATE.last_portfolio_sync_mono = now_mono
                        STATE.portfolio_backoff_seconds = 0.0
                        STATE.portfolio_backoff_until_mono = 0.0
                        STATE.equity = portfolio["total_equity"]
                        STATE.pnl = portfolio["unrealized_pnl"]
                        # Distribute positions to their respective pair states
                        all_positions = portfolio["positions"]
                        # Auto-add pairs for coins with open positions not yet tracked
                        for pos in all_positions:
                            pc = pos.get("coin", "")
                            if pc and pc not in STATE.pairs:
                                STATE.add_pair(pc, pc)
                                print(f"[dashboard] Auto-added pair {pc} (open position detected)", flush=True)
                        pair_positions: dict[str, list[dict]] = {c: [] for c in STATE.pairs}
                        for pos in all_positions:
                            pc = pos.get("coin", "")
                            if pc in pair_positions:
                                pair_positions[pc].append(pos)
                        for c, ps in STATE.pairs.items():
                            prev_size = ps.last_position_size
                            ps.positions = pair_positions.get(c, [])
                            ps.pnl = sum(float(p.get("unrealized_pnl", 0)) for p in ps.positions)
                            ps.last_position_size = _position_size(ps)
                            if ps.last_position_size > 0 and not ps.managed_position:
                                _restore_managed_position_from_portfolio(ps)
                                log_trade(
                                    "RESTORE",
                                    ps.plan_strategy or ps.pack_id,
                                    ps.last_position_size,
                                    ps.last_price or 0,
                                    f"{c} open position restored into supervisor state",
                                )
                            if prev_size > 0 and ps.last_position_size <= 0:
                                ps.managed_position = None
                                strategy_name = ps.plan_strategy or ps.pack_id
                                _arm_pair_cooldown(ps, strategy_name, f"{c} trade closed; cooling down", log_event=False)
                                _arm_pair_reentry_lockout(ps, strategy_name, f"{c} trade closed; cooling down")
                                log_trade(
                                    "COOLDOWN",
                                    strategy_name,
                                    0,
                                    ps.last_price or 0,
                                    f"{c} trade closed; cooldown {PAIR_COOLDOWN_SECONDS//60}m, same-side lockout {PAIR_REENTRY_LOCKOUT_SECONDS//60}m",
                                )
                            elif prev_size <= 0 and ps.last_position_size > 0:
                                ps.cooldown_until = ""
                        if portfolio.get("error"):
                            STATE.error = f"Portfolio partial: {portfolio['error']}"
                        if STATE.start_of_day_equity <= 0 and STATE.equity > 0:
                            STATE.start_of_day_equity = STATE.equity
                        STATE.portfolio_ready = True
                    if cycle <= 1:
                        print(f"[dashboard] Portfolio: ${STATE.equity:.2f} (perps=${portfolio['perps_equity']:.2f} + spot=${portfolio['spot_total_usd']:.2f})", flush=True)
                except (ConnectionError, TimeoutError, OSError) as e:
                    if _is_transient_read_error(e):
                        with STATE.lock:
                            next_delay = STATE.portfolio_backoff_seconds or 15.0
                            if "429" in str(e):
                                next_delay = min(max(next_delay * 2.0, 15.0), 180.0)
                            else:
                                next_delay = min(max(next_delay * 1.5, 15.0), 120.0)
                            STATE.portfolio_backoff_seconds = next_delay
                            STATE.portfolio_backoff_until_mono = now_mono + next_delay
                        _log_throttled_error(
                            "portfolio",
                            f"[dashboard] Account sync throttled; using cached values for {int(next_delay)}s: {e}",
                        )
                    else:
                        with STATE.lock:
                            STATE.error = f"Network error fetching account: {e}"
                        print(f"[dashboard] Account network error (keeping cached values): {e}", flush=True)
                except Exception as e:
                    with STATE.lock:
                        STATE.error = f"Account sync error: {e}"
                    _log_throttled_error("portfolio-generic", f"[dashboard] Account fetch error: {e}")
            with STATE.lock:
                managed_pairs = [(coin, ps) for coin, ps in STATE.pairs.items() if ps.managed_position and ps.last_position_size > 0]
            for coin, ps in managed_pairs:
                try:
                    price = ps.last_price
                    price_str = mids_snapshot.get(coin)
                    if price_str:
                        try:
                            price = float(price_str)
                        except (TypeError, ValueError):
                            pass
                    _supervise_open_position(coin, ps, price, read_stats=cycle_reads)
                    _manage_pair_position(coin, ps)
                except Exception as manage_err:
                    log_trade("MANAGE_FAIL", ps.plan_strategy or ps.pack_id, ps.last_position_size, ps.last_price or 0,
                              f"{coin}: {manage_err}")
            if not master_address:
                if cycle <= 1:
                    print("[dashboard] WARNING: No master_address in Keychain. Account data unavailable.", flush=True)

            if cycle <= 2 or cycle % 10 == 0:
                scan_list = ",".join(selected_coins[:3]) if selected_coins else "-"
                print(
                    f"[dashboard] HL reads: mids={cycle_reads['mids']} candles={cycle_reads['candles']} portfolio={cycle_reads['portfolio']} scan={scan_list}",
                    flush=True,
                )

            if master_address:
                try:
                    fill_count = TRADE_JOURNAL.sync_user_fills(master_address, hl_client.HL_MAINNET)
                    report_path = TRADE_JOURNAL.maybe_generate_weekly_report()
                    if cycle <= 1 and fill_count > 0:
                        print(f"[dashboard] Journal sync: +{fill_count} fill(s)", flush=True)
                    if report_path and cycle <= 1:
                        print(f"[dashboard] Weekly report written: {report_path}", flush=True)
                except Exception as journal_err:
                    _log_throttled_error("journal-sync", f"[dashboard] Journal sync error: {journal_err}", throttle_seconds=60.0)

            # Nightly tuner — runs once per UTC day (on first cycle past midnight)
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with STATE.lock:
                tuner_ran_today = STATE.last_tuner_date == today_utc
            if not tuner_ran_today:
                try:
                    import nightly_tuner
                    report = nightly_tuner.run(ROOT, dry_run=False)
                    with STATE.lock:
                        STATE.last_tuner_date = today_utc
                        # Keep first 500 chars as summary
                        STATE.last_tuner_report = report[:500] if report else "No adjustments needed."
                    if report and "No fills" not in report:
                        print(f"[dashboard] Nightly tuner ran for {today_utc}", flush=True)
                        log_trade("TUNER", "system", 0, 0, f"Nightly tuner applied adjustments for {today_utc}")
                        # Reload strategy configs after tuner adjustments
                        _sync_scalp_v2_strategy()
                except Exception as tuner_err:
                    _log_throttled_error("nightly-tuner", f"[dashboard] Nightly tuner error: {tuner_err}", throttle_seconds=3600.0)
                    with STATE.lock:
                        STATE.last_tuner_date = today_utc  # don't retry every cycle

            # Daily email report — once per UTC day, after tuner
            with STATE.lock:
                email_ran_today = getattr(STATE, '_last_email_date', '') == today_utc
            if not email_ran_today and master_address:
                try:
                    report = TRADE_JOURNAL.build_daily_report()
                    if report:
                        import daily_email_report
                        config = daily_email_report.load_email_config()
                        if config.get("from") and config.get("app_password") and config.get("to"):
                            current = report["current"]
                            pnl = current["closed_pnl"]
                            subject = f"Hyperbot Daily: {'+' if pnl >= 0 else ''}{pnl:.2f} USDC | {current['wins']}W/{current['losses']}L | {today_utc}"
                            daily_email_report.send_report(report["markdown"], subject, config)
                            log_trade("EMAIL", "system", 0, 0, f"Daily report sent for {today_utc}")
                except Exception as email_err:
                    _log_throttled_error("daily-email", f"[dashboard] Daily email error: {email_err}", throttle_seconds=3600.0)
                with STATE.lock:
                    STATE._last_email_date = today_utc

            with STATE.lock:
                STATE.last_update = time.strftime("%H:%M:%S")
                # Sync legacy fields from active pair
                STATE._sync_legacy()
                if not STATE.error:
                    STATE.error = None

        except Exception as e:
            with STATE.lock:
                STATE.error = str(e)
            traceback.print_exc()

        STOP_EVENT.wait(15)


def log_trade(action: str, strategy: str, size: float, price: float, note: str = "") -> None:
    now = datetime.now(timezone.utc)
    entry = {
        "time": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "ts": now.isoformat().replace("+00:00", "Z"),
        "ts_ms": int(now.timestamp() * 1000),
        "action": action,
        "strategy": strategy,
        "size": round(size, 6),
        "price": round(price, 2),
        "note": note,
    }
    with STATE.lock:
        STATE.trade_log.append(entry)
    try:
        TRADE_JOURNAL.record_trade_log(entry)
    except Exception as e:
        print(f"  [journal] trade log write failed: {e}", flush=True)
    print(f"  [{action}] {strategy} size={size:.6f} price={price:.2f} {note}", flush=True)


def _position_size(ps: PairState | None) -> float:
    if not ps or not ps.positions:
        return 0.0
    try:
        return abs(sum(float(p.get("size", 0)) for p in ps.positions))
    except Exception:
        return 0.0


def _is_in_cooldown(ps: PairState | None) -> bool:
    if not ps or not ps.cooldown_until:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(ps.cooldown_until)
    except ValueError:
        return False


def _is_in_reentry_lockout(ps: PairState | None) -> bool:
    if not ps or not ps.reentry_lock_until:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(ps.reentry_lock_until)
    except ValueError:
        return False


def _arm_pair_cooldown(ps: PairState, strategy: str, note: str, *, log_event: bool = True) -> None:
    until = datetime.now(timezone.utc).timestamp() + PAIR_COOLDOWN_SECONDS
    ps.cooldown_until = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    if log_event:
        log_trade("COOLDOWN", strategy, 0, ps.last_price or 0, note)


def _arm_pair_reentry_lockout(ps: PairState, strategy: str, note: str) -> None:
    until = datetime.now(timezone.utc).timestamp() + PAIR_REENTRY_LOCKOUT_SECONDS
    ps.reentry_lock_until = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()


def _legacy_trigger_limit(trigger_price: float, is_buy_entry: bool, tp_or_sl: str) -> float:
    """Derive a conservative explicit limit price for legacy exit triggers.

    Hyperliquid trigger orders need a real limit price. For exits, the limit
    should be slightly worse than the trigger so fills are more likely once the
    trigger fires.
    """
    if tp_or_sl == "sl":
        return trigger_price * (0.997 if is_buy_entry else 1.003)
    return trigger_price * (0.999 if is_buy_entry else 1.001)


def _breakeven_trigger_limit(entry_price: float, is_long: bool, size: float) -> tuple[float, float]:
    trigger = entry_price * (1 + BREAKEVEN_BUFFER_PCT) if is_long else entry_price * (1 - BREAKEVEN_BUFFER_PCT)
    limit_price = _legacy_trigger_limit(trigger, is_buy_entry=is_long, tp_or_sl="sl")
    return trigger, limit_price


def _manage_pair_position(coin: str, ps: PairState) -> None:
    managed = ps.managed_position
    if not managed:
        return
    if managed.get("exit_pending"):
        return

    current_size = _position_size(ps)
    if current_size <= 0:
        strategy = managed.get("strategy_id", ps.pack_id)
        ps.managed_position = None
        ps.last_closed_strategy = strategy
        ps.last_closed_side = "buy" if managed.get("side") == "buy" else "sell"
        _arm_pair_cooldown(ps, strategy, f"{coin} position closed; pause new entries for {PAIR_COOLDOWN_SECONDS//60}m", log_event=False)
        _arm_pair_reentry_lockout(ps, strategy, f"{coin} position closed; pause same-side re-entry for {PAIR_REENTRY_LOCKOUT_SECONDS//60}m")
        log_trade(
            "COOLDOWN",
            strategy,
            0,
            ps.last_price or 0,
            f"{coin} position closed; cooldown {PAIR_COOLDOWN_SECONDS//60}m, same-side lockout {PAIR_REENTRY_LOCKOUT_SECONDS//60}m",
        )
        return

    # Detect TP1 fill: current_size dropped to tp2_size (or smaller)
    tp1_filled = managed.get("tp1_moved", False)
    if not tp1_filled:
        tp2_size = float(managed.get("tp2_size", 0) or 0)
        tolerance = max(float(managed.get("initial_size", 0) or 0) * 0.05, 1e-6)
        if current_size <= tp2_size + tolerance:
            tp1_filled = True

    # Build ManagementState for the position_manager module
    is_long = managed.get("side") == "buy"
    entry_price = float(managed.get("entry_price") or 0)
    stop_trigger = float(managed.get("stop_trigger") or ps.plan_sl or 0)

    # Get current ATR for volatility-adjusted management
    entry_atr = float(managed.get("entry_atr", 0) or 0)
    current_atr = entry_atr  # default to entry ATR
    try:
        candles_5m = hl_client.get_candles(coin, "5m", 1)
        if candles_5m and len(candles_5m) >= 15:
            df = pd.DataFrame(candles_5m)
            rename = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
            df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            high, low, close = df["high"], df["low"], df["close"]
            prev_close = close.shift(1)
            tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
            current_atr = float(tr.ewm(span=10, adjust=False).mean().iloc[-1])
            if entry_atr <= 0:
                managed["entry_atr"] = current_atr
                entry_atr = current_atr
    except Exception:
        pass

    # Load exit management config
    pack_id = str(managed.get("strategy_id") or ps.selected_pack_id or ps.pack_id)
    exit_cfg = {}
    try:
        if pack_id == "scalp_v2":
            payload = _load_scalp_v2_strategy_payload(coin)
            exit_cfg = dict(payload.get("exit_management") or {})
        else:
            cfg = _legacy_strategy_config(ps, pack_id)
            exit_cfg = dict(cfg.get("exit_management") or {})
    except Exception:
        pass

    ms = position_manager.ManagementState(
        coin=coin,
        is_long=is_long,
        entry_price=entry_price,
        current_price=float(ps.last_price or 0),
        stop_trigger=stop_trigger,
        initial_size=float(managed.get("initial_size", 0) or 0),
        current_size=current_size,
        entry_atr=entry_atr,
        current_atr=current_atr,
        opened_at=managed.get("opened_at", ""),
        age_minutes=_position_age_minutes(managed),
        sl_oid=managed.get("sl_oid"),
        tp1_oid=managed.get("tp1_oid"),
        tp2_oid=managed.get("tp2_oid"),
        tp1_filled=tp1_filled,
        tp1_moved=managed.get("tp1_moved", False),
        pack_id=pack_id,
        max_hold_minutes=float(exit_cfg.get("max_hold_minutes", 90)),
        stale_after_minutes=float(exit_cfg.get("stale_after_minutes", 30)),
        breakeven_buffer_pct=BREAKEVEN_BUFFER_PCT,
    )

    actions = position_manager.manage(ms)

    for action in actions:
        if action.action == "move_sl" and action.new_trigger and action.new_limit:
            # Cancel existing SL first
            if action.cancel_oid:
                cancel_result = hl_client.cancel_order(coin, int(action.cancel_oid))
                if not cancel_result.ok:
                    log_trade("MANAGE_FAIL", pack_id, current_size, ps.last_price or 0,
                              f"Could not cancel old SL oid={action.cancel_oid}: {cancel_result.error}")
                    continue

            # Place new SL
            new_sl = hl_client.place_trigger_order(
                coin,
                is_buy=not is_long,
                size=current_size,
                trigger_price=action.new_trigger,
                limit_price=action.new_limit,
                tp_or_sl="sl",
                reduce_only=True,
            )
            if new_sl.ok:
                with STATE.lock:
                    managed["sl_oid"] = new_sl.order_id
                    managed["stop_trigger"] = action.new_trigger
                    if tp1_filled and not managed.get("tp1_moved"):
                        managed["tp1_moved"] = True
                    ps.plan_sl = action.new_trigger
                log_trade("SL_MOVE", pack_id, current_size, action.new_trigger, action.reason)
            else:
                log_trade("MANAGE_FAIL", pack_id, current_size, action.new_trigger,
                          f"SL replace failed: {new_sl.error}")

        elif action.action == "close" and action.size and action.is_buy is not None:
            close_result = hl_client.place_order(coin, action.is_buy, action.size,
                                                  order_type="market", reduce_only=True)
            if close_result.ok:
                log_trade("CLOSE", pack_id, action.size, ps.last_price or 0, action.reason)
            else:
                log_trade("CLOSE_FAIL", pack_id, action.size, ps.last_price or 0,
                          f"{action.reason} — {close_result.error}")


# ---------------------------------------------------------------------------
# Embedded HTML — single-page app with wizard + dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Hyperbot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset & Base ───────────────────────────────────────────── */
*{margin:0;padding:0;box-sizing:border-box}

/* ── LIGHT THEME ─────────────────────────────────────────────── */
[data-theme="light"]{
  --bg:#faf8f4;--surface:#fff;--border:rgba(0,0,0,0.06);
  --border-h:rgba(0,0,0,0.1);--text:#1a1917;--text1:#1a1917;--text2:#6b6760;
  --text3:#9e978d;--text4:#c4bfb4;
  --green:#2d8a56;--red:#c4453a;--blue:#4a7bbd;--yellow:#b8942d;
  --green-bg:rgba(45,138,86,0.08);--red-bg:rgba(196,69,58,0.06);
  --blue-bg:rgba(74,123,189,0.08);--yellow-bg:rgba(184,148,45,0.08);
  --radius:16px;--radius-sm:10px;
  --shadow-md:0 4px 16px rgba(0,0,0,0.06);
  --accent:#7c6a4f;--accent-bg:rgba(124,106,79,0.06);
  /* Theme-adaptive inline helpers */
  --subtle-bg:rgba(0,0,0,0.03);--subtle-bg2:rgba(0,0,0,0.05);
  --subtle-border:rgba(0,0,0,0.06);--subtle-hover:rgba(0,0,0,0.06);
  --text-bright:#1a1917;--text-muted:#9e978d;--text-dim:#c4bfb4;
  --overlay-bg:#f0ede6;--overlay-border:rgba(0,0,0,0.08);
  --input-bg:rgba(0,0,0,0.03);--input-border:rgba(0,0,0,0.08);
  --badge-inactive-bg:rgba(0,0,0,0.04);--badge-inactive-color:#9e978d;
}

/* ── DARK THEME (Moss / Evergreen) ───────────────────────────── */
:root, [data-theme="dark"]{
  --bg:#0f1a12;--surface:#1a2b1e;--border:rgba(255,255,255,0.06);
  --border-h:rgba(255,255,255,0.10);--text:#e8e4d9;--text1:#e8e4d9;--text2:#a3a08e;
  --text3:#6d7a66;--text4:#3d4f3a;
  --green:#5cb97a;--red:#e07065;--blue:#7ba4d4;--yellow:#d4b85c;
  --green-bg:rgba(92,185,122,0.12);--red-bg:rgba(224,112,101,0.1);
  --blue-bg:rgba(123,164,212,0.1);--yellow-bg:rgba(212,184,92,0.1);
  --radius:16px;--radius-sm:10px;
  --shadow-md:0 4px 16px rgba(0,0,0,0.25);
  --accent:#8fa87a;--accent-bg:rgba(143,168,122,0.1);
  /* Theme-adaptive inline helpers */
  --subtle-bg:rgba(255,255,255,0.03);--subtle-bg2:rgba(255,255,255,0.05);
  --subtle-border:rgba(255,255,255,0.06);--subtle-hover:rgba(255,255,255,0.06);
  --text-bright:#e8e4d9;--text-muted:#6d7a66;--text-dim:#3d4f3a;
  --overlay-bg:#152019;--overlay-border:rgba(255,255,255,0.08);
  --input-bg:rgba(255,255,255,0.04);--input-border:rgba(255,255,255,0.08);
  --badge-inactive-bg:rgba(255,255,255,0.06);--badge-inactive-color:#6d7a66;
}

/* Theme transition */
html.transitioning,html.transitioning *,html.transitioning *::before,html.transitioning *::after{
  transition:background 0.45s ease,color 0.45s ease,border-color 0.45s ease,box-shadow 0.45s ease!important;
}

body{background:var(--bg);color:var(--text);font-family:'Outfit',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;overflow:hidden;height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;background:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");pointer-events:none;z-index:9999;opacity:0.02}
[data-theme="dark"] body::before{opacity:0.035}
/* Ambient glow in dark mode */
[data-theme="dark"] .grid-wrap::before{content:'';position:fixed;bottom:-40%;right:-15%;width:500px;height:500px;border-radius:50%;background:radial-gradient(circle,rgba(92,138,101,0.05)0%,transparent 70%);pointer-events:none;z-index:0}

button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit}
.mono{font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace}
::selection{background:var(--accent-bg);color:var(--text)}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--text4);border-radius:3px}

/* ── Theme Toggle ────────────────────────────────────────────── */
.theme-toggle{width:50px;height:28px;border-radius:14px;background:var(--text4);position:relative;cursor:pointer;border:1px solid var(--border-h);transition:background 0.4s,border-color 0.4s;flex-shrink:0}
.theme-toggle .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:var(--surface);box-shadow:0 1px 4px rgba(0,0,0,0.2);transition:transform 0.4s cubic-bezier(0.68,-0.2,0.27,1.2);display:flex;align-items:center;justify-content:center;overflow:hidden}
[data-theme="dark"] .theme-toggle .knob{transform:translateX(22px)}
.theme-toggle .icon-sun,.theme-toggle .icon-moon{position:absolute;width:12px;height:12px;transition:opacity 0.3s,transform 0.4s}
.theme-toggle .icon-sun{opacity:1;transform:rotate(0deg);color:var(--yellow)}
.theme-toggle .icon-moon{opacity:0;transform:rotate(-90deg);color:var(--yellow)}
[data-theme="dark"] .theme-toggle .icon-sun{opacity:0;transform:rotate(90deg)}
[data-theme="dark"] .theme-toggle .icon-moon{opacity:1;transform:rotate(0deg)}

/* ── Hero Section (above cards) ──────────────────────────────── */
.hero-section{padding:24px 20px 16px;flex-shrink:0;display:flex;align-items:flex-end;justify-content:space-between}
.hero-greeting{font-size:12px;color:var(--text3);margin-bottom:2px;letter-spacing:0.3px}
.hero-equity-display{font-family:'DM Serif Display',Georgia,serif;font-size:42px;color:var(--text);letter-spacing:-1px;line-height:1.1}
.hero-equity-display .cents{font-size:24px;color:var(--text3)}
.hero-pnl-badge{display:inline-flex;align-items:center;gap:4px;margin-top:6px;padding:3px 8px;border-radius:6px;font-size:12px;font-weight:500;font-family:'JetBrains Mono',monospace}
.hero-pnl-badge.up{background:var(--green-bg);color:var(--green)}
.hero-pnl-badge.down{background:var(--red-bg);color:var(--red)}
.hero-pnl-badge svg{width:10px;height:10px}

/* ── Header ─────────────────────────────────────────────────── */
.header{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.header-left{display:flex;align-items:center;gap:12px}
.logo{width:32px;height:32px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:'DM Serif Display',Georgia,serif;font-size:16px;font-weight:400}
[data-theme="light"] .logo{background:linear-gradient(135deg,#7c6a4f,#a89477);color:#fff;box-shadow:0 2px 8px rgba(124,106,79,0.3)}
[data-theme="dark"] .logo{background:linear-gradient(135deg,#3d5a3a,#5c7a55);color:#d4cfbf;box-shadow:0 2px 8px rgba(61,90,58,0.4)}
.brand{font-size:14px;font-weight:600;color:var(--text);letter-spacing:0.5px}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;display:flex;align-items:center;gap:5px;font-weight:500}
.badge-dot{width:6px;height:6px;border-radius:50%;animation:breathe 3s ease-in-out infinite}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:0.4}}
.header-right{display:flex;align-items:center;gap:14px}
.stat-block{text-align:right}
.stat-label{font-size:11px;color:var(--text3)}
.stat-value{font-size:13px}
.icon-btn{padding:8px;border-radius:10px;position:relative;transition:background 0.15s}
.icon-btn:hover{background:var(--accent-bg)}
.icon-btn svg{width:18px;height:18px;color:var(--text2)}
.notif-dot{position:absolute;top:2px;right:2px;width:16px;height:16px;border-radius:50%;background:var(--red);color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center}
.btn-power{padding:6px 14px;border-radius:10px;font-size:12px;font-weight:600;display:flex;align-items:center;gap:5px;transition:all 0.15s}

/* ── Summary Bar ────────────────────────────────────────────── */
.summary{display:flex;align-items:center;gap:16px;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--surface);font-size:12px;flex-shrink:0}
.summary-item{display:flex;align-items:center;gap:6px;color:var(--text3)}
.summary-dot{width:6px;height:6px;border-radius:50%}
.summary-sep{color:var(--text4)}

/* ── Card Grid ──────────────────────────────────────────────── */
.grid-wrap{flex:1;overflow-y:auto;padding:20px}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.section-label{display:flex;align-items:center;gap:8px;margin:24px 0 12px;padding:0 4px;font-size:12px;font-weight:500;color:var(--text3)}
.section-label svg{width:13px;height:13px;opacity:0.6}

/* ── Trade Card ─────────────────────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;display:flex;flex-direction:column;gap:12px;transition:all 0.25s ease}
.card:hover{border-color:var(--border-h);box-shadow:var(--shadow-md);transform:translateY(-1px)}
.card.expanded{border-color:var(--border-h)}
.card.clickable{cursor:pointer}
.card-head{display:flex;align-items:center;justify-content:space-between}
.card-token{display:flex;align-items:center;gap:10px}
.token-icon{width:40px;height:40px;border-radius:50%;background:var(--accent-bg);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:14px;font-family:'JetBrains Mono',monospace;font-weight:600;color:var(--text2)}
.token-name{font-size:14px;font-weight:600}
.token-strategy{font-size:12px;color:var(--text3)}
.card-actions{display:flex;align-items:center;gap:4px}
.chevron{width:14px;height:14px;color:var(--text4);transition:transform 0.2s}
.chevron.open{transform:rotate(180deg)}
.btn-x{padding:4px;border-radius:4px;color:var(--text4);transition:color 0.15s}
.btn-x:hover{color:var(--text2)}
.btn-x svg{width:14px;height:14px}

.summary-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:4px;border-top:1px solid var(--border)}
.status-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:6px;font-size:12px;flex-shrink:0}
.summary-main{min-width:0;display:flex;align-items:center;gap:8px}
.summary-side{min-width:0;text-align:right}
.summary-value{font-size:18px;font-weight:600}
.summary-subvalue{font-size:11px;color:var(--text4);margin-top:2px;white-space:nowrap}
.pnl-pct{font-size:12px;font-weight:400;margin-left:4px;opacity:0.6}

/* ── Expanded Controls ──────────────────────────────────────── */
.controls{margin-top:12px;padding-top:12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:12px}
.watch-edu .edu-section{padding:10px 0;border-bottom:1px solid var(--border)}
.watch-edu .edu-section:last-child{border-bottom:none;padding-bottom:0}
.edu-label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
.edu-strat-name{font-size:15px;font-weight:600;color:var(--text1);margin-bottom:4px}
.edu-strat-desc{font-size:13px;color:var(--text2);line-height:1.5}
.edu-risk{font-size:12px;color:var(--text3);margin-top:6px}
.edu-risk-val{font-weight:600;padding:2px 6px;border-radius:4px;font-size:11px}
.edu-risk-low{color:#22c55e;background:rgba(34,197,94,0.1)}
.edu-risk-medium{color:#f59e0b;background:rgba(245,158,11,0.1)}
.edu-risk-high{color:#ef4444;background:rgba(239,68,68,0.1)}
.edu-hints{margin:0;padding:0 0 0 18px;list-style:none}
.edu-hints li{font-size:13px;color:var(--text2);line-height:1.6;position:relative;padding-left:4px}
.edu-hints li::before{content:'\2192';position:absolute;left:-18px;color:var(--text3)}
.edu-explain{font-size:13px;color:var(--text2);line-height:1.6;font-style:italic;opacity:0.85}
.edu-patience{padding-bottom:0!important;border-bottom:none!important}
.strat-pills{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.strat-pill{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:8px;border:1px solid var(--border);background:var(--subtle-bg);color:var(--text2);font-size:13px;cursor:pointer;transition:all 0.15s}
.strat-pill:hover{background:var(--subtle-hover);border-color:var(--border-h)}
.strat-pill.active{background:rgba(99,102,241,0.12);border-color:rgba(99,102,241,0.3);color:#a5b4fc}
.pill-risk{font-size:10px;font-weight:600;padding:1px 5px;border-radius:3px;text-transform:uppercase;letter-spacing:0.3px}
.pill-risk-low{color:#22c55e;background:rgba(34,197,94,0.12)}
.pill-risk-medium{color:#f59e0b;background:rgba(245,158,11,0.12)}
.pill-risk-high{color:#ef4444;background:rgba(239,68,68,0.12)}
.card-slider-row{display:flex;gap:12px}
.card-slider-group{flex:1}
.card-slider-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:12px;color:var(--text3)}
.card-slider-val{color:var(--text1);font-size:13px}
.card-slider{width:100%;height:4px;-webkit-appearance:none;appearance:none;background:var(--subtle-hover);border-radius:2px;outline:none;cursor:pointer}
.card-slider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#6366f1;border:2px solid var(--bg);cursor:pointer}
.card-slider::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#6366f1;border:2px solid var(--bg);cursor:pointer}
.conf-bar-wrap{margin-bottom:4px}
.conf-bar{width:100%;height:6px;background:var(--subtle-hover);border-radius:3px;overflow:hidden;margin-bottom:6px}
.conf-fill{height:100%;border-radius:3px;transition:width 0.6s ease,background 0.3s ease}
.conf-meta{display:flex;justify-content:space-between;align-items:center}
.conf-label{font-size:12px}
.conf-pct{font-size:13px;font-weight:600}
.explain-block{padding:12px;border-radius:12px;background:var(--subtle-bg);border:1px solid var(--subtle-border)}
.explain-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}
.explain-title{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px}
.explain-summary{font-size:12px;color:var(--text);line-height:1.55;margin-bottom:10px}
.explain-list{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.explain-item{display:flex;align-items:flex-start;gap:8px;font-size:13px;color:var(--text2);line-height:1.5}
.explain-kicker{font-size:10px;font-weight:600;letter-spacing:0.4px;text-transform:uppercase;color:var(--text4);margin-bottom:2px}
.explain-body{flex:1;min-width:0}
.sig-direction{font-size:12px;margin-top:6px;padding:4px 8px;border-radius:6px;display:inline-block}
.sig-dir-buy{color:var(--green);background:var(--green-bg)}
.sig-dir-sell{color:var(--red);background:var(--red-bg)}
.scan-ago{font-size:10px;color:var(--text3);font-weight:400;text-transform:none;letter-spacing:0;margin-left:6px;opacity:0.7}
.edu-hints li{position:relative;padding-left:18px}
.edu-hints li::before{display:none}
.cue-dot{position:absolute;left:0;top:2px;font-size:8px}
.btn-start-trading{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(34,197,94,0.2);background:rgba(34,197,94,0.1);color:var(--green);font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all 0.15s}
.btn-start-trading:hover{background:rgba(34,197,94,0.2);border-color:rgba(34,197,94,0.3)}
.btn-stop-trading{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(239,68,68,0.2);background:rgba(239,68,68,0.08);color:var(--red);font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all 0.15s}
.btn-stop-trading:hover{background:rgba(239,68,68,0.15);border-color:rgba(239,68,68,0.3)}
.card-action-row{display:flex;gap:8px}
.btn-secondary{flex:1;padding:10px;border-radius:8px;border:1px solid var(--border);background:var(--subtle-bg);color:var(--text2);font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all 0.15s}
.btn-secondary:hover{background:var(--subtle-hover);border-color:var(--border-h)}
.card-live{border-color:var(--green)!important}
[data-theme="dark"] .card-live{box-shadow:0 0 24px rgba(92,185,122,0.04)}
.live-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);margin-left:6px;vertical-align:middle;animation:pulse 1.5s infinite}
.info-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.info-cell{padding:8px;border-radius:8px;background:var(--subtle-bg)}
.info-cell-label{font-size:11px;color:var(--text3);margin-bottom:2px}
.info-cell-value{font-size:12px;color:var(--text2)}
.sl-tp-row{display:flex;gap:8px}
.sl-tp-box{flex:1;padding:10px;border-radius:8px}
.sl-tp-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.sl-tp-header span{font-size:12px}
.sl-tp-value{font-size:14px;margin-bottom:8px}
.sl-tp-btns{display:flex;gap:4px}
.sl-tp-btn{flex:1;padding:5px;border-radius:4px;font-size:11px;opacity:0.6;transition:opacity 0.15s}
.sl-tp-btn:hover{opacity:1}
.btn-close-position{width:100%;padding:8px;border-radius:8px;font-size:12px;font-weight:500;background:var(--red-bg);color:var(--red);border:1px solid rgba(239,68,68,0.15);transition:background 0.15s}
.btn-close-position:hover{background:rgba(239,68,68,0.2)}

/* ── Unmanaged Card ─────────────────────────────────────────── */
.card.unmanaged{border-left:3px solid var(--yellow)}
.card.unmanaged.poor{border-left-color:var(--red)}
.card.unmanaged.good{border-left-color:var(--green)}
.rating-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:10px}
.issues-box{padding:12px;border-radius:8px;background:var(--subtle-bg);border:1px solid var(--subtle-border)}
.issues-title{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:500;color:var(--text2);margin-bottom:8px}
.issues-title svg{width:11px;height:11px;opacity:0.4}
.issue-item{font-size:12px;color:var(--text3);line-height:1.5;padding-left:12px;position:relative;margin-bottom:4px}
.issue-item::before{content:"\2013";position:absolute;left:0;color:var(--text4)}
.suggestion-btn{width:100%;text-align:left;padding:10px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;transition:filter 0.15s;margin-bottom:6px}
.suggestion-btn:hover{filter:brightness(1.15)}
.suggestion-action{font-size:12px;font-weight:500}
.suggestion-detail{font-size:11px;color:var(--text3);margin-top:2px}

/* ── Add Token Card ─────────────────────────────────────────── */
.card-add{background:var(--bg);border:2px dashed var(--text4);border-radius:var(--radius);min-height:160px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;cursor:pointer;transition:all 0.25s}
.card-add:hover{border-color:var(--text3);transform:translateY(-1px)}
.add-icon{width:44px;height:44px;border-radius:50%;background:var(--surface);border:1px solid var(--border-h);display:flex;align-items:center;justify-content:center;transition:all 0.2s}
.card-add:hover .add-icon{background:var(--accent);border-color:var(--accent);box-shadow:0 4px 12px rgba(0,0,0,0.15)}
.add-icon svg{width:20px;height:20px;color:var(--text3)}
.card-add:hover .add-icon svg{color:#fff}
.card-add span{font-size:13px;font-weight:500;color:var(--text3)}

/* ── Empty State ────────────────────────────────────────────── */
.empty-state{text-align:center;padding:64px 20px}
.empty-icon{width:56px;height:56px;border-radius:16px;background:var(--subtle-bg2);display:flex;align-items:center;justify-content:center;margin:0 auto 12px}
.empty-icon svg{width:24px;height:24px;color:var(--text4)}
.empty-title{font-size:14px;color:var(--text2);margin-bottom:4px}
.empty-desc{font-size:12px;color:var(--text4);max-width:300px;margin:0 auto}

/* ── Notification Panel ─────────────────────────────────────── */
.notif-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.2);backdrop-filter:blur(4px);z-index:39;opacity:0;pointer-events:none;transition:opacity 0.3s}
[data-theme="dark"] .notif-overlay{background:rgba(10,15,11,0.5);backdrop-filter:blur(8px)}
.notif-overlay.open{opacity:1;pointer-events:auto}
.notif-panel{position:fixed;top:0;right:0;height:100%;width:380px;background:var(--bg);border-left:1px solid var(--border);z-index:40;transform:translateX(100%);transition:transform 0.35s cubic-bezier(0.4,0,0.2,1);display:flex;flex-direction:column;box-shadow:var(--shadow-md)}
.notif-panel.open{transform:translateX(0)}
.debrief-panel{position:fixed;top:0;right:0;height:100%;width:460px;background:var(--bg);border-left:1px solid var(--border);z-index:41;transform:translateX(100%);transition:transform 0.35s cubic-bezier(0.4,0,0.2,1);display:flex;flex-direction:column;overflow:hidden;box-shadow:var(--shadow-md)}
.debrief-panel.open{transform:translateX(0)}
.debrief-body{flex:1;overflow-y:auto;padding:16px;font-size:13px;line-height:1.6}
.debrief-body h3{font-size:14px;margin:16px 0 8px;color:var(--text)}
.debrief-body .trade-row{padding:8px 12px;border-radius:var(--radius-sm);margin-bottom:6px;font-family:'SF Mono',Menlo,monospace;font-size:12px}
.debrief-body .trade-win{background:var(--green-bg);border:1px solid rgba(34,197,94,0.15)}
.debrief-body .trade-loss{background:var(--red-bg);border:1px solid rgba(239,68,68,0.15)}
.debrief-body .trade-flat{background:var(--subtle-bg2);border:1px solid var(--border)}
.debrief-body .what-if{color:var(--text2);font-style:italic;padding-left:16px;margin:2px 0 6px}
.debrief-body .pattern{padding:8px 12px;border-radius:var(--radius-sm);margin-bottom:6px;border:1px solid var(--border)}
.debrief-body .pattern-high{border-color:rgba(239,68,68,0.3);background:var(--red-bg)}
.debrief-body .pattern-medium{border-color:rgba(234,179,8,0.3);background:var(--yellow-bg)}
.debrief-summary{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}
.debrief-stat{padding:10px 12px;border-radius:var(--radius-sm);background:var(--subtle-bg);border:1px solid var(--border)}
.debrief-stat .label{font-size:11px;color:var(--text3)}
.debrief-stat .value{font-size:16px;font-weight:600;margin-top:2px}
.regime-tooltip{position:fixed;top:56px;left:50%;transform:translateX(-50%);width:360px;background:var(--surface);border:1px solid var(--border-h);border-radius:var(--radius);padding:20px;z-index:50;display:none;font-size:13px;box-shadow:var(--shadow-md)}
.regime-tooltip.open{display:block}
.regime-tooltip .reason{color:var(--text2);padding:4px 0;border-bottom:1px solid var(--border)}
.notif-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border)}
.notif-header-title{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600}
.notif-header-title svg{width:16px;height:16px;color:var(--text2)}
.notif-list{flex:1;overflow-y:auto;padding:12px}
.notif-item{width:100%;text-align:left;padding:12px;border-radius:var(--radius);transition:background 0.15s;display:flex;align-items:flex-start;gap:10px;margin-bottom:4px}
.notif-item:hover{background:var(--subtle-bg)}
.notif-item.expanded{background:var(--green-bg)}
.notif-item.expanded.type-info{background:var(--blue-bg)}
.notif-item.expanded.type-system{background:var(--yellow-bg)}
.notif-item.expanded.type-warning{background:var(--red-bg)}
.notif-icon{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px}
.notif-icon svg{width:14px;height:14px}
.notif-body{flex:1;min-width:0}
.notif-title{font-size:12px;font-weight:500;color:var(--text);line-height:1.4}
.notif-meta{font-size:11px;color:var(--text4);margin-top:2px}
.notif-chevron{width:14px;height:14px;color:var(--text4);flex-shrink:0;margin-top:2px;transition:transform 0.2s}
.notif-item.expanded .notif-chevron{transform:rotate(90deg)}
.why-card{margin-top:10px;padding:10px;border-radius:8px;background:var(--subtle-bg);border:1px solid var(--subtle-border)}
.why-label{display:flex;align-items:center;gap:4px;font-size:11px;font-weight:500;margin-bottom:6px}
.why-label svg{width:10px;height:10px}
.why-text{font-size:12px;color:var(--text2);line-height:1.6}

/* ── Modal ──────────────────────────────────────────────────── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.3);backdrop-filter:blur(8px);z-index:50;display:none;align-items:center;justify-content:center}
[data-theme="dark"] .modal-overlay{background:rgba(10,15,11,0.6)}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border-h);border-radius:var(--radius);padding:24px;width:100%;max-width:640px;max-height:80vh;overflow-y:auto;box-shadow:var(--shadow-md)}
.modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.modal-head h3{font-size:15px;font-weight:600}
.modal-subtitle{font-size:12px;color:var(--text3);margin:-8px 0 16px}
.token-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
/* token rows are inline-styled */
.token-sym{font-size:13px;font-weight:500}
.token-full{font-size:11px;color:var(--text3)}
.strategy-list{display:flex;flex-direction:column;gap:8px}
.strategy-option{padding:12px;border-radius:var(--radius);border:1px solid var(--border);text-align:left;transition:background 0.15s}
.strategy-option:hover{background:var(--subtle-bg)}
.strategy-header{display:flex;align-items:center;justify-content:space-between}
.strategy-name{font-size:13px;font-weight:500}
.risk-tag{font-size:11px;padding:2px 6px;border-radius:4px}
.strategy-desc{font-size:12px;color:var(--text3);margin-top:4px;line-height:1.5}
.mode-pills{display:flex;gap:8px;flex-wrap:wrap}
.mode-pill{padding:8px 10px;border-radius:999px;border:1px solid var(--border);background:var(--subtle-bg);color:var(--text3);font-size:11px}
.mode-pill.active{border-color:rgba(34,197,94,0.25);background:rgba(34,197,94,0.08);color:var(--green)}
.bot-view{padding:12px;border-radius:12px;background:var(--subtle-bg);border:1px solid var(--subtle-border)}
.bot-view-title{font-size:11px;color:var(--text3);margin-bottom:6px}
.bot-view-note{font-size:12px;color:var(--text);line-height:1.55}
.bot-view-list{margin-top:8px;padding-left:16px;color:var(--text2);font-size:11px;line-height:1.5}

/* ── Settings Modal ─────────────────────────────────────────── */
.settings-group{margin-bottom:16px}
.settings-label{font-size:12px;color:var(--text3);margin-bottom:6px}
.settings-slider{width:100%;accent-color:var(--green)}
.settings-value{font-size:13px;text-align:right}
.btn-save{width:100%;padding:10px;border-radius:8px;background:var(--green);color:#000;font-weight:600;font-size:13px;transition:opacity 0.15s}
.btn-save:hover{opacity:0.9}

/* ── Thinking bar ───────────────────────────────────────────── */
.thinking-bar{padding:6px 20px;font-size:11px;color:var(--yellow);background:rgba(234,179,8,0.06);border-bottom:1px solid rgba(234,179,8,0.1);display:none;align-items:center;gap:8px;flex-shrink:0}
.thinking-bar.visible{display:flex}
.thinking-dot{width:6px;height:6px;border-radius:50%;background:var(--yellow);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}

/* ── Error bar ──────────────────────────────────────────────── */
.error-bar{padding:8px 20px;font-size:12px;color:var(--red);background:rgba(239,68,68,0.06);border-bottom:1px solid rgba(239,68,68,0.1);display:none;flex-shrink:0}
.error-bar.visible{display:block}

/* ── Wizard (pre-setup) ─────────────────────────────────────── */
.wizard-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:40px}
.wizard{max-width:460px;width:100%;text-align:center}
.wizard h2{font-size:20px;margin-bottom:8px}
.wizard p{font-size:13px;color:var(--text3);margin-bottom:24px}
.wizard-step{display:none}
.wizard-step.active{display:block}
</style>
</head>
<body>

<!-- ── Wallet Connect Overlay ────────────────────────────── -->
<div id="wallet-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:10000;display:flex;align-items:center;justify-content:center">
  <div style="max-width:420px;width:100%;padding:2rem;background:var(--overlay-bg);border:1px solid var(--overlay-border);border-radius:16px">
    <div style="text-align:center;margin-bottom:1.5rem">
      <div style="font-size:2rem;font-weight:700;color:#4ade80;margin-bottom:0.25rem">H</div>
      <div style="font-size:1.2rem;font-weight:600;color:var(--text);margin-bottom:0.5rem">Connect your wallet</div>
      <div style="color:var(--text3);font-size:0.85rem">Hyperbot needs your wallet address to show positions and trade on your behalf.</div>
    </div>
    <div id="wallet-list" style="margin-bottom:1rem"></div>
    <div id="wallet-status" style="display:none;padding:0.75rem;border-radius:8px;font-size:0.85rem;margin-bottom:1rem"></div>
    <div style="text-align:center">
      <button onclick="skipWalletConnect()" style="background:none;border:none;color:var(--text3);font-size:0.8rem;cursor:pointer;text-decoration:underline">Skip — browse without wallet</button>
    </div>
  </div>
</div>

<!-- ── Header ─────────────────────────────────────────────── -->
<div class="header">
  <div class="header-left">
    <div class="logo">H</div>
    <span class="brand">HYPERBOT</span>
    <div class="badge" id="d-status-badge" style="background:var(--badge-inactive-bg);color:var(--badge-inactive-color)">
      <span class="badge-dot" style="background:#555"></span>
      <span id="d-status-text">Stopped</span>
    </div>
    <div class="badge" id="d-mode-badge" style="background:rgba(59,130,246,0.12);color:#60a5fa">Simulation</div>
    <div class="badge" id="d-regime-badge" style="background:rgba(34,197,94,0.12);color:var(--green);cursor:pointer" onclick="toggleRegimePanel()" title="Portfolio market regime">
      <span class="badge-dot" id="d-regime-dot" style="background:var(--green)"></span>
      <span id="d-regime-text">Market: Green</span>
    </div>
    <button onclick="document.getElementById('wallet-overlay').style.display='flex';window.dispatchEvent(new Event('eip6963:requestProvider'));setTimeout(renderWalletList,300)" class="badge" id="d-wallet-badge" style="background:var(--badge-inactive-bg);color:var(--badge-inactive-color);cursor:pointer;border:none;font-family:inherit;font-size:inherit">
      <span id="d-wallet-addr">Connect Wallet</span>
    </button>
  </div>
  <div class="header-right">
    <div class="stat-block">
      <div class="stat-label">Equity</div>
      <div class="stat-value mono" id="d-equity">$0</div>
    </div>
    <div class="stat-block">
      <div class="stat-label">Today</div>
      <div class="stat-value mono" id="d-daily-pnl" style="color:var(--text3)">—</div>
    </div>
    <button class="btn-power" id="d-live-btn" onclick="toggleTrading()">
      <span class="badge-dot" id="d-live-dot" style="background:#555"></span>
      <span id="d-live-text">Go Live</span>
    </button>
    <div class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode">
      <div class="knob">
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      </div>
    </div>
    <button class="icon-btn" id="btn-debrief" onclick="toggleDebrief()" title="Morning Debrief">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
    </button>
    <button class="icon-btn" id="btn-notif" onclick="toggleNotifs()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
      <span class="notif-dot" id="d-notif-count" style="display:none">0</span>
    </button>
    <button class="icon-btn" onclick="toggleSettings()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    </button>
  </div>
</div>

<!-- ── Thinking / Error ───────────────────────────────────── -->
<div class="thinking-bar" id="d-thinking"><span class="thinking-dot"></span><span id="d-thinking-text"></span></div>
<div class="error-bar" id="d-error"></div>

<!-- ── Hero Section ──────────────────────────────────────── -->
<section class="hero-section" id="hero-section">
  <div>
    <div class="hero-greeting" id="hero-greeting">Good afternoon</div>
    <div class="hero-equity-display" id="hero-equity-display">$0<span class="cents">.00</span></div>
    <div class="hero-pnl-badge up" id="hero-pnl-badge" style="display:none">
      <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 8 6 3 11 8"/></svg>
      <span id="hero-pnl-text">+$0</span>
    </div>
  </div>
</section>

<!-- ── Summary Bar ────────────────────────────────────────── -->
<div class="summary" id="d-summary">
  <div class="summary-item"><span class="summary-dot" style="background:var(--green)"></span><span id="d-active-count">0 active</span></div>
  <div class="summary-item"><span class="summary-dot" style="background:var(--blue)"></span><span id="d-watching-count">0 watching</span></div>
  <span class="summary-sep">|</span>
  <div class="summary-item">Open P&L: <span class="mono" id="d-open-pnl" style="color:var(--text3)">$0</span></div>
</div>

<!-- ── Card Grid ──────────────────────────────────────────── -->
<div class="grid-wrap" id="d-grid-wrap">
  <div class="card-grid" id="d-card-grid"></div>
  <div id="d-unmanaged-section" style="display:none">
    <div class="section-label">
      <svg viewBox="0 0 24 24" fill="none" stroke="var(--yellow)" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      <span>Positions on your account (not managed by Hyperbot)</span>
    </div>
    <div class="card-grid" id="d-unmanaged-grid"></div>
  </div>
  <div class="empty-state" id="d-empty" style="display:none">
    <div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></div>
    <div class="empty-title">No tokens yet</div>
    <div class="empty-desc">Pick a token to get started. Hyperbot will add it in supervised auto mode and choose the best supported strategy automatically.</div>
  </div>
</div>

<!-- ── Notification Panel ─────────────────────────────────── -->
<div class="notif-overlay" id="notif-overlay" onclick="toggleNotifs()"></div>
<div class="notif-panel" id="notif-panel">
  <div class="notif-header">
    <div class="notif-header-title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
      Activity & Insights
    </div>
    <button class="icon-btn" onclick="toggleNotifs()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="notif-list" id="d-notif-list"></div>
</div>

<!-- ── Regime Tooltip ─────────────────────────────────────── -->
<div class="regime-tooltip" id="regime-tooltip">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
    <span class="badge-dot" id="regime-tt-dot" style="width:10px;height:10px;background:var(--green)"></span>
    <span style="font-weight:600" id="regime-tt-level">GREEN</span>
    <span style="color:var(--text3);margin-left:auto" id="regime-tt-mult">Size: 100%</span>
  </div>
  <div id="regime-tt-reasons"></div>
</div>

<!-- ── Debrief Panel ─────────────────────────────────────── -->
<div class="notif-overlay" id="debrief-overlay" onclick="toggleDebrief()"></div>
<div class="debrief-panel" id="debrief-panel">
  <div class="notif-header">
    <div class="notif-header-title">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      Morning Debrief
    </div>
    <button class="icon-btn" onclick="toggleDebrief()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="debrief-body" id="debrief-body">
    <div style="color:var(--text3);text-align:center;padding:40px">Click to load yesterday's trade review...</div>
  </div>
</div>

<!-- ── Add Token Modal ────────────────────────────────────── -->
<div class="modal-overlay" id="add-modal">
  <div class="modal">
    <div id="add-step-1">
      <div class="modal-head">
        <h3>Choose a token</h3>
        <button class="btn-x" onclick="closeAddModal()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
      </div>
      <div class="token-grid" id="d-token-list"></div>
    </div>
  </div>
</div>

<!-- ── Settings Modal ─────────────────────────────────────── -->
<div class="modal-overlay" id="settings-modal">
  <div class="modal">
    <div class="modal-head">
      <h3 id="settings-modal-title">Settings</h3>
      <button class="btn-x" onclick="toggleSettings()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="settings-group">
      <div style="display:flex;justify-content:space-between"><span class="settings-label">Max Leverage</span><span class="settings-value mono" id="d-set-lev">2x</span></div>
      <input type="range" class="settings-slider" id="set-lev" min="1" max="2" step="0.5" value="2" oninput="document.getElementById('d-set-lev').textContent=this.value+'x'">
    </div>
    <div class="settings-group">
      <div style="display:flex;justify-content:space-between"><span class="settings-label">Risk per Trade</span><span class="settings-value mono" id="d-set-risk">0.5%</span></div>
      <input type="range" class="settings-slider" id="set-risk" min="0.5" max="5" step="0.5" value="0.5" oninput="document.getElementById('d-set-risk').textContent=this.value+'%'">
    </div>
    <div class="settings-group" id="settings-daily-group">
      <div style="display:flex;justify-content:space-between"><span class="settings-label">Daily Loss Limit</span><span class="settings-value mono" id="d-set-daily">5.0%</span></div>
      <input type="range" class="settings-slider" id="set-daily" min="1" max="15" step="0.5" value="5" oninput="document.getElementById('d-set-daily').textContent=this.value+'%'">
    </div>
    <button class="btn-save" id="settings-save-btn" onclick="saveSettings()">Save Settings</button>
  </div>
</div>

<script>
// ── Theme Toggle ────────────────────────────────────────────
function toggleTheme(){
  const h=document.documentElement;
  h.classList.add('transitioning');
  const next=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',next);
  localStorage.setItem('hyperbot-theme',next);
  setTimeout(()=>h.classList.remove('transitioning'),500);
}
(function(){const s=localStorage.getItem('hyperbot-theme');if(s)document.documentElement.setAttribute('data-theme',s)})();

// ── Hero Updates ────────────────────────────────────────────
function updateHero(s){
  const h=new Date().getHours();
  let g=h<12?'Good morning':h<17?'Good afternoon':'Good evening';
  const el=document.getElementById('hero-greeting');
  if(el) el.textContent=g;

  const eq=s.equity||0;
  const dollars=Math.floor(eq);
  const cents=Math.round((eq-dollars)*100);
  const eqEl=document.getElementById('hero-equity-display');
  if(eqEl) eqEl.innerHTML='$'+dollars.toLocaleString()+'<span class="cents">.'+String(cents).padStart(2,'0')+'</span>';

  const sod=s.start_of_day_equity||0;
  const badge=document.getElementById('hero-pnl-badge');
  const pnlText=document.getElementById('hero-pnl-text');
  if(badge&&pnlText&&sod>0&&eq>0){
    const change=eq-sod;
    const pct=(change/sod*100);
    badge.style.display='inline-flex';
    badge.className='hero-pnl-badge '+(change>=0?'up':'down');
    badge.querySelector('svg').innerHTML=change>=0?'<polyline points="1 8 6 3 11 8"/>':'<polyline points="1 4 6 9 11 4"/>';
    pnlText.textContent=(change>=0?'+':'')+fmt$(change)+' ('+(change>=0?'+':'')+pct.toFixed(1)+'%)';
  }
}

// ── State ────────────────────────────────────────────────────
let expandedCard = null;
let expandedNotif = null;
let notifsOpen = false;
let addModalCoin = null;
let notifications = [];
let lastState = null;
let pollTimer = null;
let settingsCoin = null;

// Token metadata (icon lookup)
const TOKEN_ICONS = {
  ETH:'\u039E',BTC:'\u20BF',SOL:'\u25CE',ARB:'A',DOGE:'D',AVAX:'Av',
  LINK:'L',MATIC:'P',OP:'Op',PEPE:'Pe',WIF:'W',JUP:'J',TIA:'T',
  INJ:'I',SUI:'S',APT:'Ap',SEI:'Se',STRK:'St',NEAR:'N',ATOM:'At',
  DOT:'Dt',ADA:'Ad',XRP:'X',AAVE:'Aa',MKR:'Mk',UNI:'U',FTM:'F',
  RENDER:'R',WLD:'Wl',PYTH:'Py',JTO:'Jt',BONK:'Bk',ORDI:'Or'
};
function tokenIcon(coin){return TOKEN_ICONS[coin]||coin.substring(0,2)}

const STRATEGIES = [
  {id:'scalp_v2',name:'5m Scalper',desc:'Fast 5-minute breakout scalps with volume + trend confirmation. Targets 3-5 trades/day on liquid pairs.',risk:'Medium'},
  {id:'trend_pullback',name:'Trend Follower',desc:'Rides momentum when multiple timeframes align in the same direction',risk:'Medium'},
  {id:'compression_breakout',name:'Breakout Hunter',desc:'Catches big moves when price breaks key support or resistance levels',risk:'High'},
  {id:'liquidity_sweep_reversal',name:'Mean Reversion',desc:'Buys dips and sells rips when price stretches too far from average',risk:'Low'},
];

// ── API Helpers ──────────────────────────────────────────────
async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body)opts.body=JSON.stringify(body);
  const r=await fetch(path,opts);
  const data=await r.json();
  if(!r.ok) throw new Error(data.error||`API error ${r.status}`);
  return data;
}

// ── Notification Engine ──────────────────────────────────────
function addNotification(type,icon,title,why,token){
  const id=Date.now()+Math.random();
  notifications.unshift({id,time:new Date(),type,icon,title,why,token});
  if(notifications.length>50)notifications.pop();
  renderNotifications();
  updateNotifCount();
}

function timeSince(d){
  const s=Math.floor((Date.now()-d.getTime())/1000);
  if(s<60)return 'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}

function updateNotifCount(){
  const el=document.getElementById('d-notif-count');
  const c=notifications.length;
  if(c>0){el.style.display='flex';el.textContent=c>9?'9+':c}
  else{el.style.display='none'}
}

function renderNotifications(){
  const list=document.getElementById('d-notif-list');
  const colorMap={
    action:{bg:'var(--green-bg)',accent:'var(--green)',cls:'type-action'},
    info:{bg:'var(--blue-bg)',accent:'var(--blue)',cls:'type-info'},
    system:{bg:'var(--yellow-bg)',accent:'var(--yellow)',cls:'type-system'},
    warning:{bg:'var(--red-bg)',accent:'var(--red)',cls:'type-warning'},
  };
  const iconMap={
    entry:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>',
    exit:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="17" y1="7" x2="7" y2="17"/><polyline points="17 17 7 17 7 7"/></svg>',
    scan:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    system:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    sl:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  };
  list.innerHTML=notifications.map(n=>{
    const c=colorMap[n.type]||colorMap.info;
    const isExp=expandedNotif===n.id;
    return `<button class="notif-item ${isExp?'expanded':''} ${c.cls}" onclick="toggleNotifItem(${n.id})">
      <div class="notif-icon" style="background:${c.bg};color:${c.accent}">${iconMap[n.icon]||iconMap.system}</div>
      <div class="notif-body">
        <div class="notif-title">${esc(n.title)}</div>
        <div class="notif-meta">${timeSince(n.time)}${n.token?' \u00b7 '+n.token:''}</div>
        ${isExp&&n.why?`<div class="why-card">
          <div class="why-label" style="color:${c.accent}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg> Why this happened</div>
          <div class="why-text">${esc(n.why)}</div>
        </div>`:''}
      </div>
      <svg class="notif-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
    </button>`;
  }).join('');
}
function toggleNotifItem(id){expandedNotif=expandedNotif===id?null:id;renderNotifications()}
function toggleNotifs(){
  notifsOpen=!notifsOpen;
  document.getElementById('notif-panel').classList.toggle('open',notifsOpen);
  document.getElementById('notif-overlay').classList.toggle('open',notifsOpen);
}

// ── Regime Tooltip ──────────────────────────────────────────
let regimeOpen=false;
function toggleRegimePanel(){
  regimeOpen=!regimeOpen;
  document.getElementById('regime-tooltip').classList.toggle('open',regimeOpen);
  if(regimeOpen) setTimeout(()=>document.addEventListener('click',closeRegimeOutside,{once:true}),0);
}
function closeRegimeOutside(e){
  if(!document.getElementById('regime-tooltip').contains(e.target)&&!document.getElementById('d-regime-badge').contains(e.target)){
    regimeOpen=false;
    document.getElementById('regime-tooltip').classList.remove('open');
  }
}
function updateRegimeUI(s){
  const level=(s.market_regime||'green').toLowerCase();
  const colors={green:'var(--green)',yellow:'var(--yellow)',red:'var(--red)'};
  const bgs={green:'rgba(34,197,94,0.12)',yellow:'rgba(234,179,8,0.12)',red:'rgba(239,68,68,0.12)'};
  const c=colors[level]||colors.green;
  const bg=bgs[level]||bgs.green;
  const badge=document.getElementById('d-regime-badge');
  if(badge){badge.style.background=bg;badge.style.color=c}
  const dot=document.getElementById('d-regime-dot');
  if(dot) dot.style.background=c;
  const txt=document.getElementById('d-regime-text');
  if(txt) txt.textContent='Market: '+level.charAt(0).toUpperCase()+level.slice(1);
  // Tooltip
  const ttDot=document.getElementById('regime-tt-dot');
  if(ttDot) ttDot.style.background=c;
  const ttLevel=document.getElementById('regime-tt-level');
  if(ttLevel){ttLevel.textContent=level.toUpperCase();ttLevel.style.color=c}
  const ttMult=document.getElementById('regime-tt-mult');
  if(ttMult) ttMult.textContent='Size: '+(s.regime_size_multiplier*100||100).toFixed(0)+'%';
  const ttReasons=document.getElementById('regime-tt-reasons');
  if(ttReasons){
    const reasons=s.regime_reasons||[];
    ttReasons.innerHTML=reasons.map(r=>'<div class="reason">'+r+'</div>').join('')||'<div style="color:var(--text3)">All clear</div>';
  }
}

// ── Debrief Panel ──────────────────────────────────────────
let debriefOpen=false;
let debriefLoaded=false;
function toggleDebrief(){
  debriefOpen=!debriefOpen;
  document.getElementById('debrief-panel').classList.toggle('open',debriefOpen);
  document.getElementById('debrief-overlay').classList.toggle('open',debriefOpen);
  if(debriefOpen&&!debriefLoaded) loadDebrief();
}
async function loadDebrief(){
  const body=document.getElementById('debrief-body');
  body.innerHTML='<div style="color:var(--text3);text-align:center;padding:40px">Loading...</div>';
  try{
    const d=await api('/api/debrief');
    debriefLoaded=true;
    renderDebrief(d);
  }catch(e){
    body.innerHTML='<div style="color:var(--red);text-align:center;padding:40px">'+e.message+'</div>';
  }
}
function renderDebrief(d){
  const body=document.getElementById('debrief-body');
  const s=d.summary||{};
  let html='<div class="debrief-summary">';
  html+='<div class="debrief-stat"><div class="label">Trades</div><div class="value">'+(s.trade_count||0)+'</div></div>';
  html+='<div class="debrief-stat"><div class="label">Win Rate</div><div class="value" style="color:'+(s.win_rate>=0.5?'var(--green)':'var(--red)')+'">'+((s.win_rate||0)*100).toFixed(0)+'%</div></div>';
  html+='<div class="debrief-stat"><div class="label">Total P&L</div><div class="value" style="color:'+((s.total_pnl||0)>=0?'var(--green)':'var(--red)')+'">$'+((s.total_pnl||0)>=0?'+':'')+((s.total_pnl||0)).toFixed(2)+'</div></div>';
  html+='<div class="debrief-stat"><div class="label">Best / Worst</div><div class="value mono" style="font-size:12px"><span style="color:var(--green)">$'+(s.best_trade||0).toFixed(2)+'</span> / <span style="color:var(--red)">$'+(s.worst_trade||0).toFixed(2)+'</span></div></div>';
  html+='</div>';
  // Trades
  const trades=d.trades||[];
  if(trades.length){
    html+='<h3>Trade Timeline</h3>';
    trades.forEach((t,i)=>{
      const cls=t.outcome==='win'?'trade-win':t.outcome==='loss'?'trade-loss':'trade-flat';
      const icon=t.outcome==='win'?'+':t.outcome==='loss'?'-':'=';
      html+='<div class="trade-row '+cls+'">';
      html+=icon+' <b>'+t.coin+'</b> '+t.direction+' ('+t.strategy+') &mdash; ';
      html+='entry $'+(t.entry_price||0).toFixed(2)+', exit $'+((t.exit_price||0)).toFixed(2);
      html+=', P&L <b>$'+((t.closed_pnl||0)>=0?'+':'')+((t.closed_pnl||0)).toFixed(2)+'</b>';
      html+=', '+(t.duration_minutes||0).toFixed(0)+'m';
      html+='</div>';
      // What-ifs
      const wifs=d.what_ifs&&d.what_ifs[i]?d.what_ifs[i]:[];
      wifs.forEach(w=>{
        html+='<div class="what-if">What if: '+w.description+' &rarr; $'+((w.adjusted_pnl||0)>=0?'+':'')+((w.adjusted_pnl||0)).toFixed(2)+' ($'+((w.delta||0)>=0?'+':'')+((w.delta||0)).toFixed(2)+')</div>';
      });
    });
  }else{
    html+='<div style="color:var(--text3);text-align:center;padding:20px">No trades in this period</div>';
  }
  // Patterns
  const patterns=d.patterns||[];
  if(patterns.length){
    html+='<h3>Patterns Detected</h3>';
    patterns.forEach(p=>{
      const cls=p.severity==='high'?'pattern-high':'pattern-medium';
      html+='<div class="pattern '+cls+'">';
      html+='<div style="font-weight:600">'+p.pattern.replace(/_/g,' ')+'</div>';
      html+='<div style="color:var(--text2)">'+p.description+'</div>';
      if(p.suggestion) html+='<div style="color:var(--blue);margin-top:4px;font-size:12px">Suggestion: '+p.suggestion+'</div>';
      html+='</div>';
    });
  }
  // Suggestions
  const suggestions=d.suggestions||[];
  if(suggestions.length){
    html+='<h3>Actionable Suggestions</h3>';
    suggestions.forEach(sg=>{
      const c=sg.severity==='high'?'var(--red)':sg.severity==='medium'?'var(--yellow)':'var(--text2)';
      html+='<div style="padding:6px 0;border-bottom:1px solid var(--border);color:'+c+'">'+sg.text+'</div>';
    });
  }
  body.innerHTML=html;
}

// ── Add Token Modal ──────────────────────────────────────────
let activeTab='all';
let activeSubTab='';
let marketData={all:[],perps:[],tradfi:[],stocks:[],indices:[],commodities:[],fx:[],prelaunch:[],spot:[],hip3:[]};
const MARKET_TABS=[
  {id:'all',label:'All'},
  {id:'perps',label:'Perps'},
  {id:'tradfi',label:'TradFi'},
  {id:'spot',label:'Spot'},
  {id:'hip3',label:'HIP-3'},
  {id:'prelaunch',label:'Pre-launch'},
];
const TRADFI_SUBTABS=[
  {id:'tradfi',label:'All TradFi'},
  {id:'stocks',label:'Stocks'},
  {id:'indices',label:'Indices'},
  {id:'commodities',label:'Commodities'},
  {id:'fx',label:'FX'},
];

async function openAddModal(){
  addModalCoin=null;
  document.getElementById('add-step-1').style.display='block';
  const grid=document.getElementById('d-token-list');
  grid.innerHTML=`<div style="text-align:center;padding:2rem;color:var(--text3)"><span style="display:inline-block;width:16px;height:16px;border:2px solid var(--green);border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:8px"></span>Loading markets...</div>`;
  document.getElementById('add-modal').classList.add('open');
  try{
    const data=await api('/api/pairs');
    const existing=lastState?Object.keys(lastState.pairs||{}).map(c=>c.toUpperCase()):[];
    const allPerps=(data.perps||[])
      .filter(m=>m.coin&&!existing.includes(m.coin.toUpperCase()))
      .map(m=>({coin:m.coin,price:parseFloat(m.price||0),vol:parseFloat(m.dayNtlVlm||0),maxLev:m.maxLeverage||1,cat:m.category||'crypto',funding:m.funding||'0'}))
      .sort((a,b)=>b.vol-a.vol);
    const allSpot=(data.spot||[])
      .filter(m=>m.coin&&!existing.includes(m.coin.toUpperCase()))
      .map(m=>({coin:m.coin,price:parseFloat(m.price||0),vol:parseFloat(m.dayNtlVlm||0),maxLev:0,cat:m.category||'spot',subcat:m.subcategory||'',fullName:m.fullName||'',funding:''}))
      .sort((a,b)=>b.vol-a.vol);
    const tradfiTokens=allSpot.filter(m=>m.cat==='tradfi');
    const canonicalSpot=allSpot.filter(m=>m.cat==='spot');
    const hip3Tokens=allSpot.filter(m=>m.cat==='hip3');
    const prelaunchPerps=allPerps.filter(m=>m.cat==='prelaunch');
    const cryptoPerps=allPerps.filter(m=>m.cat==='crypto');
    const combined=[...cryptoPerps,...tradfiTokens,...canonicalSpot,...hip3Tokens,...prelaunchPerps];
    marketData.all=combined;
    marketData.perps=cryptoPerps;
    marketData.tradfi=tradfiTokens;
    marketData.stocks=tradfiTokens.filter(m=>m.subcat==='stocks');
    marketData.indices=tradfiTokens.filter(m=>m.subcat==='indices');
    marketData.commodities=tradfiTokens.filter(m=>m.subcat==='commodities');
    marketData.fx=tradfiTokens.filter(m=>m.subcat==='fx');
    marketData.prelaunch=prelaunchPerps;
    marketData.spot=canonicalSpot;
    marketData.hip3=hip3Tokens;
    renderTokenTabs();
  }catch(e){
    console.error('Failed to load tokens:',e);
    grid.innerHTML=`<div style="color:var(--red);padding:1rem;text-align:center;font-size:0.85rem">Failed to load markets: ${e.message}<br><br><button onclick="openAddModal()" style="background:var(--subtle-hover);border:1px solid var(--border-h);border-radius:6px;color:var(--text);padding:8px 16px;cursor:pointer">Retry</button></div>`;
  }
}

function switchTab(tab){activeTab=tab;activeSubTab='';renderTokenTabs()}
function switchSubTab(sub){activeSubTab=sub;renderTokenTabs()}

function renderTokenTabs(){
  const grid=document.getElementById('d-token-list');
  const tabsHtml=MARKET_TABS.map(t=>{
    const count=marketData[t.id]?marketData[t.id].length:0;
    if(count===0)return '';
    const isActive=activeTab===t.id;
    return `<button onclick="switchTab('${t.id}')" style="padding:6px 14px;border-radius:8px;font-size:12px;font-weight:${isActive?'600':'500'};background:${isActive?'var(--accent-bg)':'none'};color:${isActive?'var(--accent)':'var(--text3)'};border:${isActive?'1px solid var(--accent)':'1px solid transparent'};cursor:pointer;transition:all 0.15s">${t.label} <span style="font-size:10px;opacity:0.7">${count}</span></button>`;
  }).join('');
  // TradFi sub-tabs
  let subTabsHtml='';
  if(activeTab==='tradfi'){
    subTabsHtml='<div style="display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap">'+TRADFI_SUBTABS.map(t=>{
      const count=marketData[t.id]?marketData[t.id].length:0;
      if(count===0 && t.id!=='tradfi')return '';
      const isActive=(!activeSubTab&&t.id==='tradfi')||(activeSubTab===t.id);
      return `<button onclick="switchSubTab('${t.id==='tradfi'?'':t.id}')" style="padding:4px 10px;border-radius:6px;font-size:11px;font-weight:${isActive?'600':'400'};background:${isActive?'var(--subtle-bg)':'none'};color:${isActive?'var(--text)':'var(--text3)'};border:none;cursor:pointer">${t.label} <span style="font-size:9px;opacity:0.6">${count}</span></button>`;
    }).join('')+'</div>';
  }
  const placeholders={all:'Search all markets...',perps:'Search perpetuals...',tradfi:'Search stocks, ETFs, commodities, forex...',spot:'Search spot tokens...',hip3:'Search HIP-3 tokens...',prelaunch:'Search pre-launch perps...'};
  grid.innerHTML=`
    <div style="display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap">${tabsHtml}</div>
    ${subTabsHtml}
    <input type="text" id="token-search" placeholder="${placeholders[activeTab]||'Search...'}" style="width:100%;padding:10px 14px;background:var(--input-bg);border:1px solid var(--input-border);border-radius:8px;color:var(--text);font-size:14px;margin-bottom:12px;outline:none" oninput="filterTokens()">
    <div id="token-results" style="max-height:360px;overflow-y:auto"></div>`;
  filterTokens();
}

function fmtVol(v){
  if(v>=1e9)return '$'+(v/1e9).toFixed(1)+'B';
  if(v>=1e6)return '$'+(v/1e6).toFixed(1)+'M';
  if(v>=1e3)return '$'+(v/1e3).toFixed(0)+'K';
  return '$'+v.toFixed(0);
}

function filterTokens(){
  const q=(document.getElementById('token-search')?.value||'').toUpperCase();
  const listKey=(activeTab==='tradfi'&&activeSubTab)?activeSubTab:activeTab;
  const list=marketData[listKey]||[];
  const filtered=list.filter(t=>!q||t.coin.toUpperCase().includes(q)||(t.fullName||'').toUpperCase().includes(q)).slice(0,100);
  const el=document.getElementById('token-results');
  if(!el)return;
  if(filtered.length===0){
    el.innerHTML='<div style="color:var(--text3);padding:20px;text-align:center">No tokens found</div>';
    return;
  }
  const catLabels={crypto:'',tradfi:'TradFi',prelaunch:'',hip3:'HIP-3',spot:'Spot'};
  el.innerHTML=filtered.map(t=>{
    const coin=t.coin;
    const pair=t.cat==='spot'?coin:coin+'/USDC';
    const priceStr=t.price>=1?'$'+t.price.toLocaleString(undefined,{maximumFractionDigits:2}):(t.price>0?'$'+t.price.toPrecision(4):'\u2014');
    const volStr=t.vol>0?fmtVol(t.vol):'';
    const levTag=t.maxLev&&t.maxLev>1?`<span style="font-size:10px;color:var(--text-dim);background:var(--subtle-bg);padding:1px 5px;border-radius:4px">${t.maxLev}\u00d7</span>`:'';
    const subcatLabels={stocks:'Stock',indices:'Index',commodities:'Commodity',fx:'FX'};
    const catLabel=t.subcat?subcatLabels[t.subcat]||catLabels[t.cat]:catLabels[t.cat];
    const catTag=catLabel?`<span style="font-size:10px;color:var(--accent);background:var(--accent-bg);padding:1px 6px;border-radius:4px;font-weight:500">${catLabel}</span>`:'';
    return `<button onclick="addTokenAuto('${coin}')" style="display:flex;align-items:center;justify-content:space-between;width:100%;padding:12px 0;background:none;border:none;border-bottom:1px solid var(--subtle-border);cursor:pointer;transition:border-color 0.15s" onmouseenter="this.style.borderBottomColor='var(--border-h)'" onmouseleave="this.style.borderBottomColor='var(--subtle-border)'">
      <div style="display:flex;align-items:center;gap:8px"><span style="font-size:14px;font-weight:500;color:var(--text)">${pair}</span>${levTag}${catTag}</div>
      <div style="display:flex;align-items:center;gap:16px"><span class="mono" style="font-size:13px;color:var(--text2)">${priceStr}</span>${volStr?`<span class="mono" style="font-size:12px;color:var(--text3)">${volStr}</span>`:''}</div>
    </button>`}).join('');
}
async function addTokenAuto(coin){
  const symbol=coin+'/USDC';
  try{
    await api('/api/add-pair','POST',{coin, symbol});
    addNotification('action','entry',`Added ${coin} to your portfolio`,
      `The bot will now monitor ${coin} in auto mode and will pick the best supported strategy without asking you to choose one. It may take a few minutes for the first signals to appear.`,coin);
    closeAddModal();
  }catch(e){
    console.error(e);
    addNotification('warning','system',`Failed to add ${coin}`,
      `Something went wrong: ${e.message}. Please try again.`,coin);
    closeAddModal();
  }
}
function closeAddModal(){
  document.getElementById('add-modal').classList.remove('open');
  addModalCoin=null;
}

// ── Settings Modal ───────────────────────────────────────────
function getSettingsModalEls(){
  return {
    modal:document.getElementById('settings-modal'),
    title:document.getElementById('settings-modal-title'),
    dailyGroup:document.getElementById('settings-daily-group'),
    saveBtn:document.getElementById('settings-save-btn'),
    lev:document.getElementById('set-lev'),
    levValue:document.getElementById('d-set-lev'),
    risk:document.getElementById('set-risk'),
    riskValue:document.getElementById('d-set-risk'),
    daily:document.getElementById('set-daily'),
    dailyValue:document.getElementById('d-set-daily'),
  };
}
function populateSettingsInputs(vals){
  const els=getSettingsModalEls();
  els.lev.value=vals.lev;
  els.levValue.textContent=vals.lev+'x';
  els.risk.value=vals.risk;
  els.riskValue.textContent=vals.risk+'%';
  if(vals.daily!==null && vals.daily!==undefined){
    els.daily.value=vals.daily;
    els.dailyValue.textContent=vals.daily+'%';
  }
}
function configureSettingsModal(mode, coin=null){
  const els=getSettingsModalEls();
  const pairState=coin && lastState && lastState.pairs ? lastState.pairs[coin] : null;
  if(mode==='pair' && pairState){
    els.title.textContent=`Risk Settings · ${coin}`;
    els.saveBtn.textContent='Save Risk Settings';
    els.dailyGroup.style.display='none';
    populateSettingsInputs({
      lev: pairState.max_leverage||lastState.max_leverage||2,
      risk: pairState.risk_per_trade_pct||lastState.risk_per_trade_pct||0.5,
      daily: null,
    });
    return;
  }
  els.title.textContent='Settings';
  els.saveBtn.textContent='Save Settings';
  els.dailyGroup.style.display='';
  populateSettingsInputs({
    lev: lastState.max_leverage||2,
    risk: lastState.risk_per_trade_pct||0.5,
    daily: lastState.max_daily_loss_pct||5,
  });
}
function toggleSettings(coin=null){
  const {modal}=getSettingsModalEls();
  const willOpen=!modal.classList.contains('open') || coin!==null;
  if(!willOpen){
    modal.classList.remove('open');
    settingsCoin=null;
    return;
  }
  settingsCoin=coin;
  modal.classList.add('open');
  if(lastState){
    configureSettingsModal(settingsCoin ? 'pair' : 'global', settingsCoin);
  }
}
async function saveSettings(){
  const {lev:levInput,risk:riskInput,daily:dailyInput}=getSettingsModalEls();
  const lev=parseFloat(levInput.value);
  const risk=parseFloat(riskInput.value);
  if(settingsCoin){
    await api('/api/pair-settings','POST',{coin:settingsCoin,max_leverage:lev,risk_per_trade_pct:risk});
    addNotification('info','system',`Risk updated for ${settingsCoin}: ${lev}x leverage, ${risk}% risk`,
      `These overrides apply only to ${settingsCoin}. They affect the next trade the bot opens on this pair.`,settingsCoin);
  }else{
    const daily=parseFloat(dailyInput.value);
    await api('/api/settings','POST',{max_leverage:lev,risk_per_trade_pct:risk,max_daily_loss_pct:daily});
    addNotification('system','system',`Settings updated: ${lev}x leverage, ${risk}% risk, ${daily}% daily limit`,
      `These settings apply to all new trades. Leverage caps how much the bot can borrow. Risk per trade limits how much equity is at stake per position. The daily loss limit pauses all trading if losses exceed this threshold.`,null);
  }
  toggleSettings();
}

// ── Trading Toggle ───────────────────────────────────────────
async function toggleTrading(){
  if(!lastState)return;
  if(lastState.trading_active){
    await api('/api/stop','POST');
    addNotification('system','system','Trading stopped','You stopped the trading engine. The bot will no longer open new positions or manage existing ones until you restart.',null);
  }else{
    await api('/api/start','POST');
    addNotification('action','entry','Trading started','The bot is now actively monitoring your tokens and will execute trades when conditions are met.',null);
  }
}

// ── Card Rendering ───────────────────────────────────────────
function esc(s){return s?String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'):''}
function fmt$(v){return v>=0?'+$'+Math.abs(v).toFixed(0):'-$'+Math.abs(v).toFixed(0)}
function fmtPct(v){return (v>=0?'+':'')+v.toFixed(1)+'%'}
function fmtPrice(v){if(!v)return '\u2014';if(v>=1000)return '$'+v.toLocaleString(undefined,{maximumFractionDigits:0});if(v>=1)return '$'+v.toFixed(2);return '$'+v.toPrecision(4)}
function timeAgo(iso){
  if(!iso)return '';
  const diff=Math.max(0,Math.floor((Date.now()-new Date(iso).getTime())/1000));
  if(diff<10)return 'just now';
  if(diff<60)return diff+'s ago';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  return Math.floor(diff/3600)+'h ago';
}

function renderCards(s){
  const grid=document.getElementById('d-card-grid');
  const pairs=s.pairs||{};
  const coins=Object.keys(pairs);
  let html='';
  let activeCount=0,watchingCount=0,totalPnl=0;

  for(const coin of coins){
    const ps=pairs[coin];
    const pos=ps.positions&&ps.positions[0];
    const inTrade=!!pos;
    const isExp=expandedCard===coin;
    const direction=pos?(parseFloat(pos.size||0)>0?'LONG':'SHORT'):null;
    const pnl=parseFloat(pos?.unrealized_pnl||ps.pnl||0);
    const entryPx=parseFloat(pos?.entry_px||ps.plan_entry||0);
    const markPx=ps.last_price||0;
    const leverage=parseFloat(pos?.leverage?.value||ps.max_leverage||2);
    const slPx=ps.plan_sl||null;
    const tpPx=ps.plan_tp||null;
    const displayPack=ps.selected_pack_id||ps.pack_id||'trend_pullback';
    const stratObj=STRATEGIES.find(s=>s.id===displayPack);
    const strategy=ps.plan_strategy||(stratObj?stratObj.name:'Watching');
    const pnlPct=entryPx>0?((markPx-entryPx)/entryPx*100*(direction==='SHORT'?-1:1)):0;
    const cardLive=!!ps.trading_live;
    const modeLabel=ps.auto_strategy?'Auto':'Manual';
    const botNote=ps.bot_note||`${modeLabel} mode is scanning ${coin}.`;
    const botDetails=(ps.bot_details||[]).slice(0,3);
    const hasSignal=!!(ps.last_signals&&ps.last_signals.some(sig=>sig.direction!=='none'));
    const scanAgoCompact=ps.last_scan_ts?timeAgo(ps.last_scan_ts):'waiting';
    const headerSubtitle=inTrade?modeLabel+' mode':strategy;
    const hasCustomBotContext=!!ps.bot_note||botDetails.length>0;

    if(inTrade){activeCount++;totalPnl+=pnl}else{watchingCount++}

    html+=`<div class="card ${isExp?'expanded':''} ${cardLive?'card-live':''} clickable" onclick="toggleCard('${coin}')">`

    // Header
    html+=`<div class="card-head">
      <div class="card-token">
        <div class="token-icon">${tokenIcon(coin)}</div>
        <div><div class="token-name">${coin}${cardLive?'<span class="live-dot"></span>':''}</div><div class="token-strategy">${esc(headerSubtitle)}</div></div>
      </div>
      <div class="card-actions">
        <svg class="chevron ${isExp?'open':''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        <button class="btn-x" onclick="event.stopPropagation();removePair('${coin}')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
      </div>
    </div>`;

    // Status badge
    const badgeBg=inTrade?(direction==='LONG'?'var(--green-bg)':'var(--red-bg)'):'var(--badge-inactive-bg)';
    const badgeColor=inTrade?(direction==='LONG'?'var(--green)':'var(--red)'):'var(--text2)';
    const arrow=direction==='LONG'?'\u2197':direction==='SHORT'?'\u2198':'\u23F1';
    const statusText=inTrade?direction:(hasSignal?'Setup forming':'Watching');
    const summaryValue=inTrade
      ?`${fmt$(pnl)}<span class="pnl-pct">(${fmtPct(pnlPct)})</span>`
      :(hasSignal?`${Math.round((((ps.last_signals||[]).find(sig=>sig.direction!=='none')?.confidence)||0)*100)}%`:'Idle');
    const summarySubvalue=inTrade
      ?esc(strategy)
      :(hasSignal?esc(strategy):`Last scan ${esc(scanAgoCompact)}`);
    html+=`<div class="summary-row">
      <div class="summary-main">
        <span class="status-badge" style="background:${badgeBg};color:${badgeColor}">${arrow} ${statusText}</span>
      </div>
      <div class="summary-side">
        <div class="summary-value mono" style="color:${inTrade?(pnl>=0?'var(--green)':'var(--red)'):'var(--text1)'}">${summaryValue}</div>
        <div class="summary-subvalue">${summarySubvalue}</div>
      </div>
    </div>`;

    // Expanded controls — Watching cards (educational + settings)
    if(isExp&&!inTrade){
      const curPack=displayPack;
      const strat=STRATEGIES.find(s=>s.id===curPack)||STRATEGIES[0];
      const pairLev=ps.max_leverage||2;
      const pairRisk=ps.risk_per_trade_pct||0.5;
      // Pull live signal data for this pair's strategy
      const sigForPack=(ps.last_signals||[]).find(s=>s.pack_id===curPack);
      const sigConf=sigForPack?Math.round((sigForPack.confidence||0)*100):0;
      const sigDir=sigForPack?sigForPack.direction:'none';
      const sigReasons=sigForPack&&sigForPack.reasons?sigForPack.reasons:[];
      const hasScan=sigReasons.length>0;
      const scanTs=ps.last_scan_ts;
      const scanAgo=scanTs?timeAgo(scanTs):'waiting for first scan\u2026';

      // Fallback educational hints when no scan data yet
      const fallbackHints={
        'trend_pullback':['Waiting for first scan \u2014 will check trend direction across multiple timeframes'],
        'compression_breakout':['Waiting for first scan \u2014 will monitor Bollinger Band width for squeeze conditions'],
        'liquidity_sweep_reversal':['Waiting for first scan \u2014 will look for wick rejections below recent swing lows']
      };

      // Build dynamic cues from real reasons
      const liveCues=hasScan?sigReasons:fallbackHints[curPack]||fallbackHints['trend_pullback'];
      const topCues=liveCues.slice(0,3);

      // Confidence bar color
      const confColor=sigConf>=70?'var(--green)':sigConf>=40?'var(--yellow)':sigConf>0?'var(--text3)':'var(--text3)';
      const confLabel=sigConf>=70?'Setup forming \u2014 entry imminent':sigConf>=50?'Most conditions met':sigConf>=25?'Some conditions met':sigConf>0?'Few conditions met':'Scanning\u2026';
      const explainSummary=hasCustomBotContext?botNote:(ps.auto_strategy?`${strategy} is the current best candidate, but the setup still needs confirmation.`:`${strategy} is pinned, but the entry checklist is not complete yet.`);

      html+=`<div class="controls watch-edu">
        <div class="edu-section">
          <div class="explain-block">
            <div class="explain-head">
              <div class="explain-title">Why No Trade Yet</div>
              <span class="scan-ago">${esc(scanAgo)}</span>
            </div>
            <div class="explain-summary">${esc(explainSummary)}</div>
            <div class="conf-bar-wrap">
              <div class="conf-bar"><div class="conf-fill" style="width:${sigConf}%;background:${confColor}"></div></div>
              <div class="conf-meta">
                <span class="conf-label" style="color:${confColor}">${confLabel}</span>
                <span class="conf-pct mono" style="color:${confColor}">${sigConf}%</span>
              </div>
            </div>
            ${sigDir!=='none'?`<div class="sig-direction sig-dir-${sigDir}">
              ${sigDir==='buy'?'\u2197 Leaning long':'\u2198 Leaning short'} \u2014 waiting for confirmation
            </div>`:''}
            <ul class="explain-list">
              ${topCues.map(r=>{
                const neg=r.includes('\u2264')||r.includes('\u2265')||r.includes('< ')||r.includes('> max')||r.includes('not ')||r.includes('Outside')||r.includes('wrong')||r.includes('diverging')||r.includes('ranging')||r.includes('chop')||r.includes('below')||r.includes('insufficient')||r.includes('No ');
                const pos=r.includes('above')||r.includes('bullish')||r.includes('confirms')||r.includes('aligned')||r.includes('Net R')||r.includes('Confidence');
                const dotColor=pos?'var(--green)':neg?'var(--red)':'var(--text3)';
                const kicker=pos?'Ready':'Blocked';
                return `<li class="explain-item"><span class="cue-dot" style="position:static;color:${dotColor}">\u25CF</span><div class="explain-body"><div class="explain-kicker">${kicker}</div>${esc(r)}</div></li>`;
              }).join('')}
              ${botDetails.map(r=>`<li class="explain-item"><span class="cue-dot" style="position:static;color:var(--text3)">\u25CF</span><div class="explain-body"><div class="explain-kicker">Context</div>${esc(r)}</div></li>`).join('')}
            </ul>
          </div>
        </div>
        <div class="edu-section">
          <div class="edu-label">Strategy & Mode</div>
          <div class="edu-strat-desc">${esc(`Hyperbot is managing ${coin} in supervised auto mode with ${strat.name}. No manual strategy choice is needed.`)}</div>
        </div>
        <div class="card-action-row">
          <button class="${cardLive?'btn-stop-trading':'btn-start-trading'}" style="flex:1" onclick="event.stopPropagation();toggleCardTrading('${coin}')">
            ${cardLive?'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg> Stop Trading':'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg> Start Trading'}
          </button>
          <button class="btn-secondary" onclick="event.stopPropagation();toggleSettings('${coin}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            Risk
          </button>
        </div>
      </div>`;
    }

    // Expanded controls — Position cards
    if(isExp&&inTrade){
      html+=`<div class="controls">
        ${hasCustomBotContext?`<div class="bot-view" style="margin-bottom:12px">
          <div class="bot-view-title">Position Note</div>
          <div class="bot-view-note">${esc(botNote)}</div>
          ${botDetails.length?`<ul class="bot-view-list">${botDetails.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:''}
        </div>`:''}
        <div class="info-grid">
          <div class="info-cell"><div class="info-cell-label">Entry</div><div class="info-cell-value mono">${fmtPrice(entryPx)}</div></div>
          <div class="info-cell"><div class="info-cell-label">Mark</div><div class="info-cell-value mono">${fmtPrice(markPx)}</div></div>
          <div class="info-cell"><div class="info-cell-label">Leverage</div><div class="info-cell-value mono">${leverage}x</div></div>
        </div>
        <div class="sl-tp-row">
          <div class="sl-tp-box" style="background:var(--red-bg);border:1px solid rgba(239,68,68,0.12)">
            <div class="sl-tp-header"><span style="color:var(--red);opacity:0.8">Stop Loss</span></div>
            <div class="sl-tp-value mono" style="color:var(--red)">${fmtPrice(slPx)}</div>
            <div class="sl-tp-btns">
              <button class="sl-tp-btn" style="background:rgba(239,68,68,0.08);color:var(--red)" onclick="event.stopPropagation();adjustSl('${coin}','tighter')">Tighter</button>
              <button class="sl-tp-btn" style="background:rgba(239,68,68,0.08);color:var(--red)" onclick="event.stopPropagation();adjustSl('${coin}','wider')">Wider</button>
            </div>
          </div>
          <div class="sl-tp-box" style="background:var(--green-bg);border:1px solid rgba(34,197,94,0.12)">
            <div class="sl-tp-header"><span style="color:var(--green);opacity:0.8">Take Profit</span></div>
            <div class="sl-tp-value mono" style="color:var(--green)">${fmtPrice(tpPx)}</div>
            <div class="sl-tp-btns">
              <button class="sl-tp-btn" style="background:rgba(34,197,94,0.08);color:var(--green)" onclick="event.stopPropagation();adjustTp('${coin}','closer')">Closer</button>
              <button class="sl-tp-btn" style="background:rgba(34,197,94,0.08);color:var(--green)" onclick="event.stopPropagation();adjustTp('${coin}','further')">Further</button>
            </div>
          </div>
        </div>
        <div class="card-action-row">
          <button class="btn-secondary" onclick="event.stopPropagation();toggleSettings('${coin}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            Risk
          </button>
          <button class="btn-close-position" style="flex:1" onclick="event.stopPropagation();closePosition('${coin}')">Close Position</button>
        </div>
      </div>`;
    }

    html+=`</div>`;
  }

  // Add token card
  html+=`<button class="card-add" onclick="openAddModal()">
    <div class="add-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></div>
    <span>Add token</span>
  </button>`;

  grid.innerHTML=html;

  // Empty state
  document.getElementById('d-empty').style.display=coins.length===0?'block':'none';

  // Summary bar
  document.getElementById('d-active-count').textContent=activeCount+' active';
  document.getElementById('d-watching-count').textContent=watchingCount+' watching';
  const pnlEl=document.getElementById('d-open-pnl');
  pnlEl.textContent=fmt$(totalPnl);
  pnlEl.style.color=totalPnl>=0?'var(--green)':'var(--red)';
}

function toggleCard(coin){expandedCard=expandedCard===coin?null:coin;if(lastState)renderCards(lastState)}

async function removePair(coin){
  if(!confirm('Remove '+coin+' from your dashboard?'))return;
  await api('/api/remove-pair','POST',{coin});
  addNotification('system','system',`Removed ${coin} from dashboard`,`${coin} will no longer be monitored. Any open positions remain on your Hyperliquid account.`,coin);
}

// ── Per-card Trading Toggle ──────────────────────────────────
async function toggleCardTrading(coin){
  const ps=lastState&&lastState.pairs?lastState.pairs[coin]:null;
  if(!ps)return;
  const goLive=!ps.trading_live;
  if(goLive&&!confirm(`Go live on ${coin}? The bot will execute real trades when signals fire.`))return;
  try{
    await api('/api/pair-settings','POST',{coin,trading_live:goLive});
    if(goLive){
      addNotification('action','entry',`${coin} is now live`,
        `The bot will actively trade ${coin} when entry conditions are met. You can stop trading from the card at any time.`,coin);
    }else{
      addNotification('system','system',`${coin} trading stopped`,
        `The bot will continue scanning ${coin} but will not open new positions.`,coin);
    }
  }catch(e){console.error(e)}
}

// ── Per-card Risk ────────────────────────────────────────────
async function setCardRisk(coin,lev,risk){
  try{
    const body={coin};
    if(lev!==null)body.max_leverage=lev;
    if(risk!==null)body.risk_per_trade_pct=risk;
    await api('/api/pair-settings','POST',body);
    const parts=[];
    if(lev!==null)parts.push(`leverage ${lev}x`);
    if(risk!==null)parts.push(`risk ${risk}%`);
    addNotification('info','system',`Risk updated for ${coin}: ${parts.join(', ')}`,
      `These settings override the global defaults for ${coin} only. They apply to the next trade the bot opens on this pair.`,coin);
  }catch(e){console.error(e)}
}

// ── SL/TP Adjustments (placeholder — wired to notification) ──
function adjustSl(coin,dir){
  addNotification('info','sl',`Stop loss ${dir==='tighter'?'tightened':'widened'} for ${coin}`,
    `${dir==='tighter'?'Tightening':'Widening'} the stop loss ${dir==='tighter'?'reduces potential loss but increases the chance of being stopped out':'gives the trade more room to breathe but increases potential loss'}. The bot will use the new level for this position.`,coin);
}
function adjustTp(coin,dir){
  addNotification('info','sl',`Take profit ${dir==='closer'?'moved closer':'moved further'} for ${coin}`,
    `${dir==='closer'?'A closer take profit locks in gains sooner but limits upside':'A further take profit aims for larger gains but the trade needs to move more in your favor'}. The bot will use the new level.`,coin);
}
async function closePosition(coin){
  if(!confirm('Close your '+coin+' position at market price?'))return;
  try{
    await api('/api/close-position','POST',{coin});
    addNotification('action','exit',`Position close requested for ${coin}`,
      `A market order to close the full ${coin} position has been submitted. It may take a moment to fill.`,coin);
  }catch(e){
    console.error(e);
    addNotification('warning','system',`Failed to close ${coin} position`,
      `Something went wrong: ${e.message}. Check your Hyperliquid account directly to verify position status.`,coin);
  }
}

// ── Main Poll Loop ───────────────────────────────────────────
let prevLogLen=0;
async function dashPoll(){
  try{
    const s=await api('/api/state');
    lastState=s;

    // Header + Hero
    document.getElementById('d-equity').textContent='$'+(s.equity||0).toLocaleString(undefined,{maximumFractionDigits:0});
    const dayPnl=s.equity&&s.start_of_day_equity?(s.equity-s.start_of_day_equity)/s.start_of_day_equity*100:0;
    const dayPnlEl=document.getElementById('d-daily-pnl');
    dayPnlEl.textContent=fmtPct(dayPnl);
    dayPnlEl.style.color=dayPnl>=0?'var(--green)':'var(--red)';
    updateHero(s);

    // Status badge
    const running=s.trading_active;
    const statusBadge=document.getElementById('d-status-badge');
    statusBadge.style.background=running?'rgba(34,197,94,0.12)':'var(--badge-inactive-bg)';
    statusBadge.style.color=running?'var(--green)':'var(--badge-inactive-color)';
    statusBadge.querySelector('.badge-dot').style.background=running?'var(--green)':'var(--badge-inactive-color)';
    document.getElementById('d-status-text').textContent=running?'Live':'Stopped';

    // Mode badge
    const modeBadge=document.getElementById('d-mode-badge');
    const liveBtn=document.getElementById('d-live-btn');
    const liveText=document.getElementById('d-live-text');
    const liveDot=document.getElementById('d-live-dot');
    if(s.live_enabled){
      modeBadge.style.background='rgba(34,197,94,0.12)';
      modeBadge.style.color='var(--green)';
      modeBadge.textContent='Mainnet';
      if(liveBtn){
        liveBtn.style.background='rgba(239,68,68,0.08)';
        liveBtn.style.border='1px solid rgba(239,68,68,0.2)';
        liveBtn.style.color='var(--red)';
      }
      if(liveText) liveText.textContent='Stop Trading';
      if(liveDot) liveDot.style.background='var(--red)';
    }
    else{
      modeBadge.style.background='rgba(59,130,246,0.12)';
      modeBadge.style.color='#60a5fa';
      modeBadge.textContent='Simulation';
      if(liveBtn){
        liveBtn.style.background='rgba(34,197,94,0.08)';
        liveBtn.style.border='1px solid rgba(34,197,94,0.2)';
        liveBtn.style.color='var(--green)';
      }
      if(liveText) liveText.textContent='Go Live';
      if(liveDot) liveDot.style.background='var(--green)';
    }

    // Per-card trading state is rendered inside renderCards()

    // Market regime indicator
    updateRegimeUI(s);

    // Thinking bar
    const thinkEl=document.getElementById('d-thinking');
    if(s.thinking){thinkEl.classList.add('visible');document.getElementById('d-thinking-text').textContent=s.thinking}
    else{thinkEl.classList.remove('visible')}

    // Error bar
    const errEl=document.getElementById('d-error');
    if(s.error){errEl.classList.add('visible');errEl.textContent=s.error}
    else{errEl.classList.remove('visible')}

    // Generate notifications from trade log
    const log=s.trade_log||[];
    if(log.length>prevLogLen){
      for(let i=prevLogLen;i<log.length;i++){
        const entry=log[i];
        const tag=entry.tag||'';
        const detail=entry.detail||'';
        if(tag==='ORDER'||tag==='SIGNAL'){
          const action=entry.action||'';
          const why=generateWhy(tag,action,detail,entry);
          addNotification('action',action.includes('BUY')||action.includes('ENTRY')?'entry':'exit',
            `${action} ${entry.coin||''} ${detail}`.trim(),why,entry.coin);
        }
      }
      prevLogLen=log.length;
    }

    // Render cards
    renderCards(s);

  }catch(e){
    console.error('Poll error:',e);
  }
}

function generateWhy(tag,action,detail,entry){
  if(tag==='SIGNAL'){
    if(action.includes('LONG'))return `The signal engine detected bullish conditions across multiple timeframes for ${entry.coin||'this token'}. The strategy waits for alignment before suggesting an entry \u2014 this reduces false signals.`;
    if(action.includes('SHORT'))return `The signal engine detected bearish conditions. Price appears overextended or rejecting a key level, suggesting a potential pullback.`;
    return `The signal engine is monitoring conditions. ${detail}`;
  }
  if(action.includes('BUY'))return `A buy order was placed. The strategy identified favorable entry conditions and the position size was calculated based on your risk settings.`;
  if(action.includes('SELL'))return `A sell order was placed to close or reduce the position. ${detail}`;
  return detail||'The trading engine took an action based on current market conditions and your strategy settings.';
}

// ── Wallet Connect (EIP-6963) ────────────────────────────────
let connectedAddress=null;
const discoveredWallets=new Map();

window.addEventListener('eip6963:announceProvider',(event)=>{
  const {info,provider}=event.detail;
  if(!discoveredWallets.has(info.rdns)){
    discoveredWallets.set(info.rdns,{info,provider});
    renderWalletList();
  }
});

function renderWalletList(){
  const el=document.getElementById('wallet-list');
  if(!el)return;
  let html='';
  if(discoveredWallets.size===0){
    html=`<div style="color:var(--text3);text-align:center;padding:1rem;font-size:0.85rem">No browser wallets detected.<br>Install MetaMask, Rabby, or another EVM wallet extension.</div>`;
  }else{
    html+=`<div style="font-size:0.75rem;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem">Browser wallets detected</div>`;
    for(const [rdns,{info}] of discoveredWallets){
      html+=`<button onclick="connectWallet('${rdns}')" style="display:flex;align-items:center;gap:0.75rem;width:100%;padding:0.85rem 1.25rem;background:#1a1a2e;border:1px solid #3a3a4a;border-radius:8px;color:#e0e0e0;font-size:0.95rem;cursor:pointer;margin-bottom:0.5rem;transition:all 0.2s">
        <img src="${info.icon}" width="28" height="28" style="border-radius:6px">
        ${info.name}
      </button>`;
    }
  }
  // Manual address entry
  html+=`<div style="text-align:center;color:var(--text3);font-size:0.8rem;margin:1rem 0;position:relative"><span style="background:var(--overlay-bg);padding:0 0.5rem;position:relative;z-index:1">OR</span><div style="position:absolute;top:50%;left:0;right:0;height:1px;background:var(--overlay-border)"></div></div>`;
  html+=`<div style="display:flex;gap:0.5rem"><input type="text" id="manual-address" placeholder="Paste wallet address (0x...)" style="flex:1;padding:0.65rem 0.75rem;background:#1a1a2a;border:1px solid #3a3a4a;border-radius:8px;color:#e0e0e0;font-family:monospace;font-size:0.8rem;outline:none"><button onclick="connectManualAddress()" style="padding:0.65rem 1rem;background:#4ade80;color:#0a0a0f;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:0.85rem">Go</button></div>`;
  el.innerHTML=html;
}

async function connectWallet(rdns){
  const wallet=discoveredWallets.get(rdns);
  if(!wallet)return;
  const statusEl=document.getElementById('wallet-status');
  try{
    statusEl.style.display='block';
    statusEl.style.background='#15152a';statusEl.style.border='1px solid #20205a';statusEl.style.color='#60a5fa';
    statusEl.textContent='Connecting to '+wallet.info.name+'...';
    const accounts=await wallet.provider.request({method:'eth_requestAccounts'});
    const addr=accounts[0].toLowerCase();
    await setWalletAddress(addr);
  }catch(e){
    statusEl.style.background='#2a1515';statusEl.style.border='1px solid #5a2020';statusEl.style.color='#f87171';
    statusEl.textContent='Error: '+e.message;
  }
}

async function connectManualAddress(){
  const input=document.getElementById('manual-address');
  const addr=(input?.value||'').trim().toLowerCase();
  if(!addr.startsWith('0x')||addr.length!==42){
    const statusEl=document.getElementById('wallet-status');
    statusEl.style.display='block';
    statusEl.style.background='#2a1515';statusEl.style.border='1px solid #5a2020';statusEl.style.color='#f87171';
    statusEl.textContent='Invalid address. Must be 0x... (42 characters)';
    return;
  }
  await setWalletAddress(addr);
}

function skipWalletConnect(){
  document.getElementById('wallet-overlay').style.display='none';
  addNotification('system','system','Welcome to Hyperbot!',
    'No wallet connected. Add tokens manually with the + button, or connect your wallet anytime from the header.',null);
  startPolling();
}

async function setWalletAddress(addr){
  connectedAddress=addr;
  try{
    await api('/api/set-wallet','POST',{address:addr});
  }catch(e){console.error('set-wallet failed:',e)}
  document.getElementById('wallet-overlay').style.display='none';
  // Update header with truncated address
  const walletEl=document.getElementById('d-wallet-addr');
  if(walletEl) walletEl.textContent=addr.substring(0,6)+'...'+addr.substring(38);
  await detectPositions();
  startPolling();
}

async function detectPositions(){
  try{
    const liveData=await api('/api/positions');
    const positions=liveData.positions||[];
    const orders=liveData.orders||[];
    if(positions.length>0||orders.length>0){
      const posCoins=positions.map(p=>p.coin.toUpperCase());
      const orderCoins=orders.map(o=>o.coin.toUpperCase());
      const allCoins=[...new Set([...posCoins,...orderCoins])];
      for(const coin of allCoins){
        try{await api('/api/add-pair','POST',{coin,symbol:coin+'/USDC'})}catch(e){}
      }
      if(positions.length>0){
        addNotification('system','system',
          `Found ${positions.length} open position${positions.length>1?'s':''}`,
          `Detected positions for ${posCoins.join(', ')} on your Hyperliquid account.`,null);
      }
      if(orders.length>0){
        const uniqueOrderCoins=[...new Set(orderCoins)].filter(c=>!posCoins.includes(c));
        if(uniqueOrderCoins.length>0){
          addNotification('info','scan',
            `Found ${orders.length} open order${orders.length>1?'s':''}`,
            `Detected pending orders for ${uniqueOrderCoins.join(', ')}.`,null);
        }
      }
    }else{
      addNotification('system','system','Wallet connected!',
        'No open positions found. Add your first token using the + button below.',null);
    }
  }catch(e){
    console.error('Position detection failed:',e);
  }
}

function startPolling(){
  dashPoll();
  pollTimer=setInterval(dashPoll,3000);
}

// ── Init ─────────────────────────────────────────────────────
async function init(){
  // Check if backend already has a wallet address (e.g. from Keychain)
  try{
    const s=await api('/api/state');
    if(s.master_address){
      connectedAddress=s.master_address;
      document.getElementById('wallet-overlay').style.display='none';
      const walletEl=document.getElementById('d-wallet-addr');
      if(walletEl) walletEl.textContent=s.master_address.substring(0,6)+'...'+s.master_address.substring(38);
      await detectPositions();
      startPolling();
      return;
    }
  }catch(e){}

  // No wallet — show connect overlay
  document.getElementById('wallet-overlay').style.display='flex';
  window.dispatchEvent(new Event('eip6963:requestProvider'));
  // Give wallets 500ms to announce themselves
  setTimeout(renderWalletList,500);
}

init();
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

        elif path == "/api/debrief":
            # Generate morning debrief / trade review
            try:
                import debrief
                lookback = 1  # default: yesterday
                report = debrief.generate_debrief(ROOT, lookback_days=lookback)
                self._json(report)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path == "/api/pairs":
            # Return full categorized market universe
            try:
                markets = hl_client.get_all_markets()
                self._json(markets)
            except Exception as e:
                # Fallback to basic allMids if full market fetch fails
                try:
                    mids = hl_client.get_all_mids()
                    perps = [{"coin": k, "price": v} for k, v in mids.items()]
                    self._json({"perps": perps, "spot": []})
                except Exception as e2:
                    self._json({"error": str(e2)}, 500)

        elif path == "/api/positions":
            # Fetch live positions from Hyperliquid for the connected wallet
            try:
                address = STATE.master_address
                if not address:
                    self._json({"positions": [], "orders": []})
                    return
                ch = hl_client.get_clearinghouse_state(address)
                positions = []
                for p in ch.get("assetPositions", []):
                    info = p.get("position", {})
                    szi = float(info.get("szi", "0"))
                    if szi == 0:
                        continue
                    positions.append({
                        "coin": info.get("coin", ""),
                        "size": szi,
                        "entry_price": info.get("entryPx", "0"),
                        "unrealized_pnl": info.get("unrealizedPnl", "0"),
                        "leverage": (info.get("leverage") or {}).get("value", "1"),
                        "liq_price": info.get("liquidationPx"),
                        "margin_used": info.get("marginUsed", "0"),
                    })
                # Also fetch open orders
                orders = []
                try:
                    open_orders = hl_client.get_open_orders(address)
                    for o in open_orders:
                        orders.append({
                            "coin": o.get("coin", ""),
                            "side": o.get("side", ""),
                            "sz": o.get("sz", "0"),
                            "limit_px": o.get("limitPx", "0"),
                            "oid": o.get("oid"),
                        })
                except Exception:
                    pass
                self._json({"positions": positions, "orders": orders})
            except Exception as e:
                self._json({"error": str(e), "positions": [], "orders": []}, 500)

        elif path == "/api/workspace-status":
            # Check if workspace was pre-built (has pairs in manifest)
            has_manifest = MANIFEST_PATH.exists()
            manifest_pairs = []
            if has_manifest:
                try:
                    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
                    manifest_pairs = manifest.get("pairs", [])
                    # Also check if there's a legacy single-pair setup
                    if not manifest_pairs and manifest.get("coin"):
                        manifest_pairs = [{"coin": manifest["coin"], "symbol": manifest["symbol"], "enabled": True}]
                except Exception:
                    pass
            self._json({
                "has_manifest": has_manifest,
                "pairs": manifest_pairs,
                "setup_complete": STATE.setup_complete,
            })

        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (json.JSONDecodeError, ValueError) as e:
            self._json({"ok": False, "error": f"Invalid JSON: {e}"}, 400)
            return
        path = self.path.split("?")[0]

        if path == "/api/set-wallet":
            address = body.get("address", "").strip().lower()
            if not address or not address.startswith("0x") or len(address) != 42:
                self._json({"ok": False, "error": "Invalid wallet address"}, 400)
                return
            with STATE.lock:
                STATE.master_address = address
            log_trade("WALLET", "operator", 0, 0, f"Wallet connected: {address}")
            self._json({"ok": True, "address": address})

        elif path == "/api/start":
            STATE.live_enabled = True
            STATE.trading_active = True
            log_trade("START", "operator", 0, STATE.last_price or 0, "live trading activated")
            self._json({"ok": True, "trading_active": STATE.trading_active})

        elif path == "/api/stop":
            STATE.trading_active = False
            log_trade("STOP", "operator", 0, STATE.last_price or 0, "trading stopped by operator")
            self._json({"ok": True, "trading_active": False})

        elif path == "/api/settings":
            try:
                STATE.max_leverage = clamp_live_leverage(body.get("max_leverage", STATE.max_leverage))
                STATE.risk_per_trade_pct = body.get("risk_per_trade_pct", STATE.risk_per_trade_pct)
                STATE.max_daily_loss_pct = body.get("max_daily_loss_pct", STATE.max_daily_loss_pct)
                STATE.margin_mode = normalize_margin_mode(body.get("margin_mode", STATE.margin_mode))
                # Persist to policy file
                if POLICY_PATH.exists():
                    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
                    safe = policy.get("auto_apply", {}).get("safe_bands", {})
                    safe["leverage_max"] = STATE.max_leverage
                    safe["risk_per_trade_pct_max"] = STATE.risk_per_trade_pct
                    safe["max_daily_loss_pct"] = STATE.max_daily_loss_pct
                    POLICY_PATH.write_text(json.dumps(policy, indent=2), encoding="utf-8")
                log_trade("SETTINGS", "operator", 0, STATE.last_price or 0,
                          f"lev={STATE.max_leverage}x risk={STATE.risk_per_trade_pct}% daily={STATE.max_daily_loss_pct}% mode={STATE.margin_mode}")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pair-settings":
            coin = body.get("coin", "").upper().strip()
            if not coin:
                self._json({"ok": False, "error": "coin is required"}, 400)
                return
            with STATE.lock:
                ps = STATE.pairs.get(coin)
                if not ps:
                    self._json({"ok": False, "error": f"Unknown pair: {coin}"}, 400)
                    return
                if "enabled" in body:
                    ps.enabled = bool(body["enabled"])
                if "max_leverage" in body:
                    ps.max_leverage = clamp_live_leverage(body["max_leverage"])
                if "risk_per_trade_pct" in body:
                    ps.risk_per_trade_pct = float(body["risk_per_trade_pct"])
                if "margin_mode" in body:
                    ps.margin_mode = normalize_margin_mode(body["margin_mode"], ps.margin_mode)
                if "auto_strategy" in body:
                    ps.auto_strategy = bool(body["auto_strategy"])
                if "pack_id" in body:
                    ps.pack_id = str(body["pack_id"])
                    ps.plan_strategy = str(body.get("plan_strategy", ps.plan_strategy))
                    if "auto_strategy" not in body:
                        ps.auto_strategy = False
                if "trading_live" in body:
                    effective_pack = ps.selected_pack_id if ps.auto_strategy else ps.pack_id
                    if coin == "TAO" and effective_pack == "trend_pullback" and bool(body["trading_live"]):
                        self._json({"ok": False, "error": "TAO trend_pullback live trading is disabled pending better sample quality"}, 400)
                        return
                    ps.trading_live = bool(body["trading_live"])
                    # Auto-enable global live trading when first card goes live
                    if ps.trading_live:
                        STATE.trading_active = True
                        if not STATE.live_enabled:
                            STATE.live_enabled = True
                            log_trade("SETTINGS", "operator", 0, 0,
                                      "Live trading auto-enabled (first card went live)")
            log_trade("SETTINGS", "operator", 0, 0,
                      f"{coin}: enabled={ps.enabled} lev={ps.max_leverage}x risk={ps.risk_per_trade_pct}% mode={ps.margin_mode} auto={ps.auto_strategy}")
            self._json({"ok": True, "coin": coin})

        elif path == "/api/switch-pair":
            coin = body.get("coin", "")
            with STATE.lock:
                known_pair = coin in STATE.pairs
                if known_pair:
                    STATE.active_coin = coin
                    STATE._sync_legacy()
            if known_pair:
                self._json({"ok": True, "active_coin": coin})
            else:
                self._json({"ok": False, "error": f"Unknown pair: {coin}"}, 400)

        elif path == "/api/add-pair":
            coin = body.get("coin", "").upper().strip()
            symbol = body.get("symbol", "").upper().strip()
            if not coin or not symbol:
                self._json({"ok": False, "error": "coin and symbol are required"}, 400)
                return
            with STATE.lock:
                if coin in STATE.pairs:
                    self._json({"ok": False, "error": f"{coin} is already added"}, 400)
                    return
                ps_new = STATE.add_pair(coin, symbol)
                req_pack = body.get("pack_id", "").strip()
                ps_new.auto_strategy = not req_pack or req_pack == "auto"
                if ps_new and req_pack and req_pack != "auto":
                    ps_new.pack_id = req_pack
                if ps_new and ps_new.auto_strategy:
                    ps_new.selected_pack_id = "scalp_v2"
                ps_new.margin_mode = normalize_margin_mode(body.get("margin_mode", STATE.margin_mode))
                ps_new.max_leverage = clamp_live_leverage(body.get("max_leverage", STATE.max_leverage))
                if not STATE.setup_complete:
                    STATE.setup_complete = True
            # Install strategy configs for the new pair
            config_dir = ROOT / "config" / "strategies"
            config_dir.mkdir(parents=True, exist_ok=True)
            installed_packs = []
            # Use the pack_id from the request if provided, otherwise scan existing
            requested_pack = body.get("pack_id", "").strip()
            if requested_pack and requested_pack != "auto":
                target_pack_ids: set[str] = {requested_pack}
            else:
                target_pack_ids = set(AUTO_STRATEGY_PACK_IDS)
            if not target_pack_ids:
                target_pack_ids = {"trend_pullback"}
            for pack_id in sorted(target_pack_ids):
                new_id = f"{coin.lower()}_{pack_id}"
                if pack_id == "scalp_v2":
                    cfg = _scalp_v2_config_payload(coin, symbol)
                    cfg["strategy_id"] = new_id
                else:
                    cfg = {
                        "strategy_id": new_id,
                        "display_name": f"{coin} {pack_id.replace('_', ' ').title()}",
                        "enabled": True,
                        "pack_id": pack_id,
                        "market": {"symbol": symbol, "coin": coin, "market_type": "perpetual"},
                        "runner": {"source": "hyperliquid_candles", "anchor_timeframe": "1D", "trigger_timeframe": "4H", "confirmation_timeframe": "1H"},
                        "entry": {"sma_period": 10, "pullback_zone_pct": 5.0},
                        "filters": {"overextension_max_pct": 20.0, "min_pullback_pct": 3.0},
                        "risk": {"invalidation_below_sma_pct": 3.0, "position_sizing": {"risk_per_trade_pct": 0.5, "max_leverage": ps_new.max_leverage, "margin_mode": ps_new.margin_mode}},
                        "take_profit": {"tp1_r_multiple": 1.0, "tp2_r_multiple": 2.0},
                    }
                (config_dir / f"{new_id}.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                installed_packs.append(new_id)
            # Update manifest
            if MANIFEST_PATH.exists():
                try:
                    pairs_list = _read_manifest_pairs()
                    pairs_list = [p for p in pairs_list if str(p.get("coin", "")).upper() != coin]
                    pairs_list.append({"symbol": symbol, "coin": coin, "enabled": True, "strategies": []})
                    _write_manifest_pairs(pairs_list)
                except Exception:
                    pass
            log_trade("ADD_PAIR", "operator", 0, 0, f"Added {coin} ({symbol}) with {len(installed_packs)} strategy configs")
            self._json({"ok": True, "coin": coin, "symbol": symbol, "configs": installed_packs})

        elif path == "/api/remove-pair":
            coin = body.get("coin", "").upper().strip()
            if not coin:
                self._json({"ok": False, "error": "coin is required"}, 400)
                return
            with STATE.lock:
                if coin not in STATE.pairs:
                    self._json({"ok": False, "error": f"{coin} not found"}, 400)
                    return
                if len(STATE.pairs) <= 1:
                    self._json({"ok": False, "error": "Cannot remove the last pair"}, 400)
                    return
                del STATE.pairs[coin]
                if STATE.active_coin == coin:
                    STATE.active_coin = next(iter(STATE.pairs))
                    STATE._sync_legacy()
            config_dir = ROOT / "config" / "strategies"
            if config_dir.exists():
                for cfg in config_dir.glob(f"{coin.lower()}_*.json"):
                    try:
                        cfg.unlink()
                    except Exception:
                        pass
            if MANIFEST_PATH.exists():
                try:
                    pairs_list = [
                        p for p in _read_manifest_pairs()
                        if str(p.get("coin", "")).upper() != coin
                    ]
                    _write_manifest_pairs(pairs_list)
                except Exception:
                    pass
            log_trade("REMOVE_PAIR", "operator", 0, 0, f"Removed {coin}")
            self._json({"ok": True, "removed": coin})

        elif path == "/api/close-position":
            coin = body.get("coin", "").upper().strip()
            if not coin:
                self._json({"ok": False, "error": "coin is required"}, 400)
                return
            with STATE.lock:
                ps = STATE.pairs.get(coin)
                if not ps:
                    self._json({"ok": False, "error": f"Unknown pair: {coin}"}, 400)
                    return
            try:
                # Get current position size to close
                address = STATE.master_address
                if not address:
                    self._json({"ok": False, "error": "No wallet connected"}, 400)
                    return
                ch = hl_client.get_clearinghouse_state(address)
                pos_size = 0.0
                is_long = True
                for p in ch.get("assetPositions", []):
                    info = p.get("position", {})
                    if info.get("coin", "").upper() == coin:
                        pos_size = abs(float(info.get("szi", "0")))
                        is_long = float(info.get("szi", "0")) > 0
                        break
                if pos_size == 0:
                    self._json({"ok": False, "error": f"No open position for {coin}"}, 400)
                    return
                # Close by placing opposite market order with reduce_only
                result = hl_client.place_order(coin, not is_long, pos_size, order_type="market", reduce_only=True)
                if result.ok:
                    log_trade("CLOSE", "operator", pos_size, STATE.last_price or 0,
                              f"Closed {coin} position, oid={result.order_id}")
                    self._json({"ok": True, "coin": coin, "closed_size": pos_size})
                else:
                    log_trade("CLOSE_FAIL", "operator", pos_size, STATE.last_price or 0,
                              f"Close failed: {result.error}")
                    self._json({"ok": False, "error": f"Close order failed: {result.error}"}, 500)
            except Exception as e:
                traceback.print_exc()
                self._json({"ok": False, "error": str(e)}, 500)

        elif path == "/api/backtest":
            coin = body.get("coin", "").upper().strip() or STATE.coin
            pack_id = body.get("pack_id", "trend_pullback")
            days = int(body.get("days", 90))
            if not coin:
                self._json({"ok": False, "error": "No coin specified"}, 400)
                return
            try:
                import backtest as bt
                from datetime import datetime, timedelta, timezone as tz
                end_dt = datetime.now(tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                start_dt = end_dt - timedelta(days=days - 1)
                start_ms = int(start_dt.timestamp() * 1000)
                end_ms = int((end_dt + timedelta(days=1)).timestamp() * 1000) - 1
                summary = bt.run_backtest(coin, pack_id, start_ms, end_ms, hl_client.HL_MAINNET)
                self._json({"ok": True, "summary": summary})
            except SystemExit as e:
                self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"ok": False, "error": str(e)}, 500)

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
    if args.live:
        STATE.trading_active = True  # Auto-start when launched in live mode
    else:
        print("[hyperbot] VIEW-ONLY STARTUP: no live orders will be placed.", flush=True)
        print("[hyperbot]   Relaunch with --live --confirm-risk if you want the dashboard to trade.", flush=True)

    # Auto-setup: load pairs from manifest if it exists
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest_pairs = manifest.get("pairs", [])
            if not manifest_pairs and manifest.get("coin"):
                manifest_pairs = [{"coin": manifest["coin"], "symbol": manifest["symbol"], "enabled": True}]
            for mp in manifest_pairs:
                coin = mp.get("coin", "")
                symbol = mp.get("symbol", coin)
                if coin and coin not in STATE.pairs:
                    STATE.add_pair(coin, symbol)
            if STATE.pairs:
                first = next(iter(STATE.pairs))
                STATE.coin = first
                STATE.symbol = STATE.pairs[first].symbol
                STATE.setup_complete = True
                print(f"[hyperbot] Auto-loaded {len(STATE.pairs)} pair(s): {', '.join(STATE.pairs.keys())}", flush=True)
        except Exception as e:
            print(f"[hyperbot] Warning: could not read manifest: {e}", flush=True)

    # Load risk settings from policy if available
    if POLICY_PATH.exists() and not STATE.setup_complete:
        try:
            policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            safe = policy.get("auto_apply", {}).get("safe_bands", {})
            STATE.max_leverage = safe.get("leverage_max", STATE.max_leverage)
            STATE.risk_per_trade_pct = safe.get("risk_per_trade_pct_max", STATE.risk_per_trade_pct)
            STATE.max_daily_loss_pct = safe.get("max_daily_loss_pct", STATE.max_daily_loss_pct)
        except Exception:
            pass

    # If no manifest but we have credentials, still mark setup complete
    # so the trading loop starts and pairs can be added via the UI
    if not STATE.setup_complete:
        creds = hl_client.get_credentials()
        if creds.get("master_address"):
            STATE.setup_complete = True
            print("[hyperbot] No manifest but credentials found — ready for pairs via UI", flush=True)
        else:
            # No credentials either — still allow the UI, just mark setup complete
            # so the trading loop can start and show the empty state
            STATE.setup_complete = True
            print("[hyperbot] Fresh start — add tokens via the dashboard", flush=True)

    # Start trading loop in background
    thread = threading.Thread(target=trading_loop, daemon=True)
    thread.start()

    port = args.port or find_free_port()
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)

    url = f"http://127.0.0.1:{port}"
    mode = "LIVE TRADING" if args.live else "VIEW ONLY"
    print(f"[hyperbot] {mode} — {url}", flush=True)
    if not args.live:
        print("[hyperbot] WARNING: live trading is disabled until you explicitly enable it from the UI or restart with --live --confirm-risk.", flush=True)
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
