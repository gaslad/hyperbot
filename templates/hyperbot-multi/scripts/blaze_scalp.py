"""
blaze_scalp.py
==============
Ultra-fast test scalping strategy — designed to trigger within 15 minutes.

PURPOSE: Verify the full execution pipeline (entry → SL → TP) actually works
on Hyperliquid. NOT designed for profitability — filters are deliberately loose.

Uses 1m candles for fastest signal generation. Enters on any micro-breakout
with minimal confirmation. Tight SL/TP (1 ATR each) for fast resolution.

Wire up (same interface as scalp_strategy_v2):
    from blaze_scalp import BlazeScalp, BlazeConfig
    strategy = BlazeScalp(config=BlazeConfig())
    signal = strategy.evaluate("ETH", market_data)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration — deliberately loose for fast triggering
# ---------------------------------------------------------------------------

@dataclass
class BlazeConfig:
    # Risk — tiny size for testing
    risk_per_trade_pct: float = 0.002       # 0.20% of equity (small for test)
    max_daily_loss_pct: float = 0.02        # 2% daily halt
    max_leverage: float = 5.0               # conservative leverage
    max_consecutive_losses: int = 5
    max_session_losses: int = 10

    # Regime — VERY loose filters
    ema_fast_1m: int = 8
    ema_slow_1m: int = 21
    atr_period: int = 10
    rvol_min: float = 0.5                   # require at least 0.5x recent relative volume
    max_spread_pct: float = 0.001           # 0.1% spread tolerance (2x normal)

    # Entry — trigger on tiny breakouts
    breakout_lookback: int = 5              # only 5 candles
    min_r_distance: float = 1.0             # 1R minimum (not 1.5R)
    max_chase_atr: float = 1.5              # allow more chasing

    # Stop/TP — tight for fast resolution
    stop_atr_mult: float = 1.0              # 1 ATR stop
    tp_atr_mult: float = 1.0               # 1 ATR take profit (1:1 R:R)
    sl_limit_buffer_pct: float = 0.005      # 0.5% buffer on SL limit
    tp_limit_buffer_pct: float = 0.002      # 0.2% buffer on TP limit

    # Fees
    taker_fee: float = 0.00045
    maker_fee: float = 0.00015
    estimated_slippage: float = 0.0010

    # Execution
    ioc_price_offset_pct: float = 0.002     # 0.2% offset for IOC


# ---------------------------------------------------------------------------
# Data structures (reuse same interface as scalp_v2)
# ---------------------------------------------------------------------------

@dataclass
class BlazeRegime:
    ema_aligned: bool = False
    ema_fast_val: float = 0.0
    ema_slow_val: float = 0.0
    atr_val: float = 0.0
    rvol: float = 0.0
    rvol_ok: bool = False
    spread_ok: bool = False

    @property
    def passes(self) -> bool:
        return self.ema_aligned and self.rvol_ok and self.spread_ok

    def rejection_reasons(self) -> list[str]:
        reasons = []
        if not self.ema_aligned:
            reasons.append(f"1m EMAs flat (fast={self.ema_fast_val:.2f} slow={self.ema_slow_val:.2f})")
        if not self.rvol_ok:
            reasons.append(f"RVOL {self.rvol:.2f}x < threshold")
        if not self.spread_ok:
            reasons.append("Spread too wide")
        return reasons


@dataclass
class BlazeSetup:
    direction: Optional[str] = None
    breakout_level: Optional[float] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    atr: float = 0.0
    r_distance: float = 0.0
    valid: bool = False
    rejection_reasons: list = field(default_factory=list)


@dataclass
class BlazeOrderParams:
    symbol: str
    side: str
    size: float
    entry_price: float
    entry_order_type: str           # "Ioc" — always taker for speed
    stop_trigger: float
    stop_limit: float
    tp_trigger: float
    tp_limit: float
    leverage: float
    reduce_only_exits: bool = True


@dataclass
class BlazeSignal:
    action: str                     # "TRADE" or "NO_TRADE"
    symbol: str
    timestamp: str
    direction: Optional[str] = None
    regime: Optional[BlazeRegime] = None
    setup: Optional[BlazeSetup] = None
    order_params: Optional[BlazeOrderParams] = None
    confidence: int = 0
    effective_r_net: float = 0.0
    rejection_reasons: list = field(default_factory=list)

    def summary(self) -> str:
        if self.action == "NO_TRADE":
            return f"NO TRADE [{self.symbol}] — {'; '.join(self.rejection_reasons)}"
        op = self.order_params
        return (
            f"TRADE [{self.symbol}] {self.direction.upper()} | "
            f"Entry {op.entry_price:.4f} (IOC) | "
            f"SL {op.stop_trigger:.4f} | TP {op.tp_trigger:.4f} | "
            f"Lev {op.leverage:.1f}x | Size {op.size:.6f}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp", "n": "trades"}
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            raise KeyError(f"Missing column '{col}' — keys: {list(candles[0].keys()) if candles else '[]'}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _sig5(price: float) -> float:
    if price == 0:
        return 0.0
    digits = 5 - int(math.floor(math.log10(abs(price)))) - 1
    return round(price, digits)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class BlazeScalp:
    def __init__(self, config: BlazeConfig = None):
        self.config = config or BlazeConfig()
        self._consecutive_losses: int = 0
        self._performance: list[float] = []

    def evaluate(self, symbol: str, market_data: dict) -> BlazeSignal:
        """Evaluate one symbol for a blaze scalp signal using 1m candles."""
        ts = datetime.now(timezone.utc).isoformat()
        cfg = self.config

        # --- Session risk guards ---
        equity = market_data.get("account_equity", 0)
        daily_loss = market_data.get("session_daily_loss", 0)
        if equity > 0 and daily_loss / equity >= cfg.max_daily_loss_pct:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Daily loss limit hit"])
        if market_data.get("open_position") is not None:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Position already open"])
        if self._consecutive_losses >= cfg.max_session_losses:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Session loss limit hit"])

        # --- Data check ---
        candles_1m = market_data.get("candles_1m", [])
        if not candles_1m or len(candles_1m) < 30:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Need ≥30 1m candles"])

        best_bid = market_data.get("best_bid")
        best_ask = market_data.get("best_ask")
        mark_price = market_data.get("mark_price", 0)
        if not best_bid or not best_ask or not mark_price:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["No price data"])

        # --- Build dataframe ---
        df = _df(candles_1m)

        # --- Regime (minimal) ---
        regime = BlazeRegime()
        ema_fast = _ema(df["close"], cfg.ema_fast_1m)
        ema_slow = _ema(df["close"], cfg.ema_slow_1m)
        regime.ema_fast_val = ema_fast.iloc[-1]
        regime.ema_slow_val = ema_slow.iloc[-1]
        regime.ema_aligned = abs(ema_fast.iloc[-1] - ema_slow.iloc[-1]) > 0  # any divergence = aligned

        # RVOL
        avg_vol = df["volume"].iloc[-21:-1].mean()
        current_vol = df["volume"].iloc[-1]
        regime.rvol = (current_vol / avg_vol) if avg_vol > 0 else 0
        regime.rvol_ok = regime.rvol >= cfg.rvol_min

        # Spread
        spread = (best_ask - best_bid) / best_ask if best_ask > 0 else 1
        regime.spread_ok = spread <= cfg.max_spread_pct

        if not regime.passes:
            return BlazeSignal(
                action="NO_TRADE", symbol=symbol, timestamp=ts,
                regime=regime, rejection_reasons=regime.rejection_reasons(),
            )

        # --- Direction: just follow 1m EMA crossover ---
        direction = "long" if ema_fast.iloc[-1] > ema_slow.iloc[-1] else "short"

        # --- Setup: micro-breakout on last N candles ---
        setup = BlazeSetup(direction=direction)
        atr_series = _atr(df, cfg.atr_period)
        atr_val = atr_series.iloc[-1]
        setup.atr = atr_val

        if atr_val <= 0:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               regime=regime, rejection_reasons=["ATR is zero"])

        lookback = cfg.breakout_lookback
        recent = df.iloc[-1 - lookback:-1]  # exclude current candle
        current_close = df["close"].iloc[-1]

        if direction == "long":
            breakout_high = recent["high"].max()
            if current_close <= breakout_high:
                return BlazeSignal(
                    action="NO_TRADE", symbol=symbol, timestamp=ts,
                    direction=direction, regime=regime,
                    rejection_reasons=[f"No breakout: close {current_close:.2f} ≤ range high {breakout_high:.2f}"],
                )

            setup.breakout_level = breakout_high
            setup.entry_price = best_ask
            setup.stop_price = _sig5(best_ask - cfg.stop_atr_mult * atr_val)
            setup.tp_price = _sig5(best_ask + cfg.tp_atr_mult * atr_val)

        else:  # short
            breakout_low = recent["low"].min()
            if current_close >= breakout_low:
                return BlazeSignal(
                    action="NO_TRADE", symbol=symbol, timestamp=ts,
                    direction=direction, regime=regime,
                    rejection_reasons=[f"No breakdown: close {current_close:.2f} ≥ range low {breakout_low:.2f}"],
                )

            setup.breakout_level = breakout_low
            setup.entry_price = best_bid
            setup.stop_price = _sig5(best_bid + cfg.stop_atr_mult * atr_val)
            setup.tp_price = _sig5(best_bid - cfg.tp_atr_mult * atr_val)

        # R distance
        risk = abs(setup.entry_price - setup.stop_price)
        reward = abs(setup.tp_price - setup.entry_price)
        setup.r_distance = reward / risk if risk > 0 else 0

        if setup.r_distance < cfg.min_r_distance:
            return BlazeSignal(
                action="NO_TRADE", symbol=symbol, timestamp=ts,
                direction=direction, regime=regime,
                rejection_reasons=[f"R distance {setup.r_distance:.2f} < {cfg.min_r_distance}"],
            )

        setup.valid = True

        # --- Position sizing ---
        size_mult = 0.5 if self._consecutive_losses >= 3 else 1.0
        # Use dashboard per-pair risk if provided (as percentage), else config default (as decimal)
        override_risk = market_data.get("risk_per_trade_pct")
        effective_risk = (override_risk / 100.0) if override_risk is not None else cfg.risk_per_trade_pct
        risk_amount = equity * effective_risk * size_mult
        stop_distance_pct = abs(setup.entry_price - setup.stop_price) / setup.entry_price
        if stop_distance_pct <= 0:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Zero stop distance"])

        position_value = risk_amount / stop_distance_pct
        override_lev = market_data.get("max_leverage")
        max_lev = override_lev if override_lev is not None else cfg.max_leverage
        leverage = position_value / equity if equity > 0 else 0
        if leverage > max_lev:
            position_value = equity * max_lev
            leverage = max_lev
        leverage = round(leverage, 1)
        size = position_value / setup.entry_price if setup.entry_price > 0 else 0

        if size <= 0:
            return BlazeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Size rounds to zero"])

        # --- Build order params ---
        is_long = direction == "long"
        sl_trigger = _sig5(setup.stop_price)
        sl_limit = _sig5(sl_trigger * (1 - cfg.sl_limit_buffer_pct) if is_long else sl_trigger * (1 + cfg.sl_limit_buffer_pct))
        tp_trigger = _sig5(setup.tp_price)
        tp_limit = _sig5(tp_trigger * (1 - cfg.tp_limit_buffer_pct) if is_long else tp_trigger * (1 + cfg.tp_limit_buffer_pct))

        order_params = BlazeOrderParams(
            symbol=symbol,
            side="buy" if is_long else "sell",
            size=round(size, 6),
            entry_price=_sig5(setup.entry_price),
            entry_order_type="Ioc",         # always taker for speed
            stop_trigger=sl_trigger,
            stop_limit=sl_limit,
            tp_trigger=tp_trigger,
            tp_limit=tp_limit,
            leverage=leverage,
        )

        # Confidence (simple — just for display)
        confidence = 6 if regime.rvol >= 1.0 else 5

        signal = BlazeSignal(
            action="TRADE",
            symbol=symbol,
            timestamp=ts,
            direction=direction,
            regime=regime,
            setup=setup,
            order_params=order_params,
            confidence=confidence,
            effective_r_net=setup.r_distance - 0.35,  # rough fee drag estimate
        )
        logger.info(signal.summary())
        return signal

    def record_result(self, result_r: float):
        self._performance.append(result_r)
        if result_r < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        if len(self._performance) > 50:
            self._performance.pop(0)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    logging.basicConfig(level=logging.INFO)

    def _fake(base, i):
        o = base + random.uniform(-2, 2)
        c = o + random.uniform(-1.5, 1.5)
        h = max(o, c) + random.uniform(0, 1)
        l = min(o, c) - random.uniform(0, 1)
        v = random.uniform(50, 500)
        return {"o": o, "h": h, "l": l, "c": c, "v": v}

    # Simulate uptrend
    base = 3000.0
    candles = [_fake(base + i * 0.5, i) for i in range(40)]

    data = {
        "candles_1m": candles,
        "account_equity": 100.0,
        "session_daily_loss": 0.0,
        "mark_price": float(candles[-1]["c"]),
        "best_bid": float(candles[-1]["c"]) - 0.5,
        "best_ask": float(candles[-1]["c"]) + 0.5,
        "open_position": None,
    }

    strat = BlazeScalp()
    sig = strat.evaluate("ETH", data)
    print(f"\n{'='*60}")
    print(sig.summary())
    print(f"{'='*60}")
