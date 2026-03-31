"""
scalp_strategy_v2.py
====================
Hyperliquid 5-minute scalping strategy module — v2 (professional-grade)

Drop-in strategy module. Designed to plug into an existing bot.
Exposes a single entry point: ScalpStrategy.evaluate(symbol, market_data) -> TradeSignal

Dependencies:
    pip install pandas numpy ta hyperliquid-python-sdk

Assumes your bot provides a MarketData object (or dict) with:
    - candles_5m:  list of OHLCV dicts, most recent last, minimum 60 candles
    - candles_15m: list of OHLCV dicts, most recent last, minimum 60 candles
    - account_equity: float (USDC)
    - session_daily_loss: float (USDC, positive = loss)
    - session_consecutive_losses: int
    - session_trade_count: int
    - mark_price: float
    - best_bid: float
    - best_ask: float
    - open_position: dict or None  ({"side": "long"/"short", "entry": float, "size": float})

Wire up:
    from scalp_strategy_v2 import ScalpStrategy, StrategyConfig
    strategy = ScalpStrategy(config=StrategyConfig())
    signal = strategy.evaluate("BTC", market_data)
    if signal.action == "TRADE":
        # pass signal.order_params to your Hyperliquid order executor
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
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    # Risk
    risk_per_trade_pct: float = 0.003        # 0.30% of equity per trade
    max_daily_loss_pct: float = 0.015        # 1.5%
    max_leverage: float = 10.0
    max_consecutive_losses: int = 3
    post_loss_size_multiplier: float = 0.5   # 50% size after 3 losses
    max_session_losses: int = 5              # full halt after 5 losses

    # Regime
    adx_min: float = 20.0
    choppiness_max: float = 55.0
    rvol_min: float = 1.5
    atr_period: int = 10                     # ATR(10) not ATR(14)
    ema_fast_15m: int = 20
    ema_slow_15m: int = 50
    ema_fast_5m: int = 20
    vwap_exclusion_hours: int = 4            # hours post-midnight UTC

    # Entry
    min_r_distance: float = 1.5             # minimum reward in R before entry
    max_chase_atr: float = 0.75             # max extension beyond trigger
    max_chase_atr_high_rvol: float = 1.0   # extended chase if RVOL > 2.5x
    high_rvol_chase_threshold: float = 2.5

    # Stop
    stop_atr_min: float = 1.0
    stop_atr_max: float = 1.5
    sl_limit_buffer_pct: float = 0.003      # 0.3% beyond SL trigger for limit price
    tp_limit_buffer_pct: float = 0.001      # 0.1% buffer on TP limit price
    mark_price_buffer_pct: float = 0.001    # 0.1% mark-to-last buffer in stop placement

    # Take profit
    partial_exit_r: float = 1.0            # first partial at 1R
    partial_exit_pct: float = 0.30         # close 30% at first target
    final_target_r: float = 1.8            # final target

    # Fees
    taker_fee: float = 0.00045             # 0.045% Hyperliquid taker
    maker_fee: float = 0.00015             # 0.015% Hyperliquid maker (ALO)
    estimated_slippage: float = 0.0010     # 0.10% estimated round-trip slippage

    # Execution
    ioc_price_offset_pct: float = 0.001    # 0.1% offset for IOC market-like orders
    max_spread_pct: float = 0.0005         # 0.05% max acceptable spread
    max_latency_ms: int = 500

    # Time filters (UTC hours, inclusive)
    # Adjusted for AEST (UTC+10) operator — covers London open through NY close
    session_windows: list = field(default_factory=lambda: [
        (8, 17),    # London open → NY afternoon (18:00–03:00 AEST)
        (22, 24),   # Asia morning session (08:00–10:00 AEST)
        (0, 6),     # Asia/early London (10:00–16:00 AEST)
    ])
    blocked_hours: list = field(default_factory=lambda: [6, 7])  # 16:00–18:00 AEST dead zone

    # Spread / breakout
    breakout_lookback: int = 8              # candles to look back for high/low range
    breakout_lookback_min: int = 3

    # CVD lookback
    cvd_lookback: int = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    ema_aligned: bool = False
    adx_value: float = 0.0
    adx_ok: bool = False
    choppiness: float = 0.0
    choppiness_ok: bool = False
    price_above_vwap: bool = False
    vwap_ok: bool = False
    atr_above_median: bool = False
    rvol: float = 0.0
    rvol_ok: bool = False
    cvd_confirming: bool = False
    time_ok: bool = False
    vwap_exclusion_active: bool = False

    @property
    def passes(self) -> bool:
        return all([
            self.ema_aligned,
            self.adx_ok,
            self.choppiness_ok,
            self.vwap_ok,
            self.atr_above_median,
            self.rvol_ok,
            self.cvd_confirming,
            self.time_ok,
        ])

    def rejection_reasons(self) -> list[str]:
        reasons = []
        if not self.ema_aligned:       reasons.append("15m EMA not aligned")
        if not self.adx_ok:            reasons.append(f"ADX {self.adx_value:.1f} ≤ 20 (ranging)")
        if not self.choppiness_ok:     reasons.append(f"Choppiness {self.choppiness:.1f} ≥ 55 (chop)")
        if not self.vwap_ok:           reasons.append("Price on wrong side of VWAP")
        if not self.atr_above_median:  reasons.append("ATR below median threshold")
        if not self.rvol_ok:           reasons.append(f"RVOL {self.rvol:.2f}x < 1.5x")
        if not self.cvd_confirming:    reasons.append("CVD diverging from breakout")
        if not self.time_ok:           reasons.append("Outside valid trading window")
        return reasons


@dataclass
class SetupState:
    direction: Optional[str] = None        # "long" or "short"
    breakout_level: Optional[float] = None
    structure_stop: Optional[float] = None # structural stop level
    atr_stop: Optional[float] = None       # ATR-based stop level
    stop_price: Optional[float] = None     # final stop (wider of two)
    entry_price: Optional[float] = None
    tp1_price: Optional[float] = None
    tp_final_price: Optional[float] = None
    r_distance: float = 0.0
    atr: float = 0.0
    extended_beyond_trigger: float = 0.0
    valid: bool = False
    rejection_reasons: list = field(default_factory=list)


@dataclass
class OrderParams:
    """Ready-to-submit order parameters for Hyperliquid."""
    symbol: str
    side: str                              # "buy" or "sell"
    size: float
    entry_price: float
    entry_order_type: str                  # "Alo" (maker) or "Ioc" (taker)
    stop_trigger: float
    stop_limit: float                      # explicit limit, not trigger-only
    tp1_trigger: float
    tp1_limit: float
    tp1_size: float                        # partial (30%)
    tp_final_trigger: float
    tp_final_limit: float
    tp_final_size: float
    leverage: float
    reduce_only_exits: bool = True


@dataclass
class TradeSignal:
    action: str                            # "TRADE" or "NO_TRADE"
    symbol: str
    timestamp: str
    direction: Optional[str] = None
    regime: Optional[RegimeState] = None
    setup: Optional[SetupState] = None
    order_params: Optional[OrderParams] = None
    confidence: int = 0
    effective_r_net: float = 0.0
    rejection_reasons: list = field(default_factory=list)

    def summary(self) -> str:
        if self.action == "NO_TRADE":
            return f"NO TRADE [{self.symbol}] — {'; '.join(self.rejection_reasons)}"
        op = self.order_params
        return (
            f"TRADE [{self.symbol}] {self.direction.upper()} | "
            f"Entry {op.entry_price:.4f} ({op.entry_order_type}) | "
            f"SL {op.stop_trigger:.4f} (limit {op.stop_limit:.4f}) | "
            f"TP1 {op.tp1_trigger:.4f} | TP_final {op.tp_final_trigger:.4f} | "
            f"Lev {op.leverage:.1f}x | Net R ≈ {self.effective_r_net:.2f} | "
            f"Confidence {self.confidence}/10"
        )


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    # Hyperliquid API returns short keys: t, o, h, l, c, v, n
    # Normalize to long names the strategy expects
    rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp", "n": "trades"}
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            raise KeyError(f"Missing required column '{col}' after rename — raw keys were: {list(candles[0].keys()) if candles else '[]'}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed ADX."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    dm_plus = np.where((high - prev_high) > (prev_low - low), np.maximum(high - prev_high, 0), 0)
    dm_minus = np.where((prev_low - low) > (high - prev_high), np.maximum(prev_low - low, 0), 0)

    tr_s = pd.Series(
        pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    )
    tr_smooth = tr_s.ewm(span=period, adjust=False).mean()
    di_plus = 100 * pd.Series(dm_plus).ewm(span=period, adjust=False).mean() / tr_smooth
    di_minus = 100 * pd.Series(dm_minus).ewm(span=period, adjust=False).mean() / tr_smooth
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


def choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Choppiness Index: 100 × log10(sum_ATR(1, n) / (n-period high - n-period low)) / log10(n)"""
    atr1 = atr(df, 1)
    rolling_atr_sum = atr1.rolling(period).sum()
    rolling_high = df["high"].rolling(period).max()
    rolling_low = df["low"].rolling(period).min()
    range_ = (rolling_high - rolling_low).replace(0, np.nan)
    ci = 100 * np.log10(rolling_atr_sum / range_) / np.log10(period)
    return ci


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets at start of df (caller slices to session)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumvol = df["volume"].cumsum()
    cumtp_vol = (typical_price * df["volume"]).cumsum()
    return cumtp_vol / cumvol.replace(0, np.nan)


def relative_volume(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    avg = df["volume"].rolling(lookback).mean().shift(1)
    return df["volume"] / avg.replace(0, np.nan)


def cvd(df: pd.DataFrame) -> pd.Series:
    """
    Approximate CVD using close position within candle as a proxy for buy/sell pressure.
    True CVD requires tick data. This is a reasonable candle-based approximation:
        delta ≈ volume × ((close - low) - (high - close)) / (high - low)
    """
    hl = (df["high"] - df["low"]).replace(0, np.nan)
    delta = df["volume"] * ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl
    return delta.cumsum()


# ---------------------------------------------------------------------------
# Core strategy class
# ---------------------------------------------------------------------------

class ScalpStrategy:
    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self._performance: list[float] = []  # rolling R per trade
        self._consecutive_losses: int = 0
        self._daily_loss: float = 0.0

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def evaluate(self, symbol: str, market_data: dict) -> TradeSignal:
        """
        Evaluate one symbol for a trade signal.

        Args:
            symbol: e.g. "BTC"
            market_data: dict with keys described in module docstring

        Returns:
            TradeSignal with action "TRADE" or "NO_TRADE"
        """
        ts = datetime.now(timezone.utc).isoformat()
        rejections = []

        # --- Session risk guards ---
        risk_ok, risk_reason = self._session_risk_check(market_data)
        if not risk_ok:
            return TradeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=[risk_reason])

        # --- Pre-flight: data quality ---
        if not self._data_ok(market_data):
            return TradeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=["Stale or insufficient market data"])

        # --- Spread check ---
        spread = (market_data["best_ask"] - market_data["best_bid"]) / market_data["best_ask"]
        if spread > self.config.max_spread_pct:
            return TradeSignal(action="NO_TRADE", symbol=symbol, timestamp=ts,
                               rejection_reasons=[f"Spread {spread*100:.3f}% > max {self.config.max_spread_pct*100:.3f}%"])

        # --- Build dataframes ---
        df5 = _df(market_data["candles_5m"])
        df15 = _df(market_data["candles_15m"])

        # --- Compute regime ---
        regime = self._compute_regime(df5, df15)
        if not regime.passes:
            return TradeSignal(
                action="NO_TRADE", symbol=symbol, timestamp=ts,
                regime=regime,
                rejection_reasons=regime.rejection_reasons(),
            )

        # --- Detect setup ---
        direction = "long" if regime.ema_aligned and df5["close"].iloc[-1] > df5["close"].mean() else None
        # Determine direction from 15m EMA alignment
        ema20_15 = ema(df15["close"], self.config.ema_fast_15m).iloc[-1]
        ema50_15 = ema(df15["close"], self.config.ema_slow_15m).iloc[-1]
        direction = "long" if ema20_15 > ema50_15 else "short"

        setup = self._detect_setup(df5, direction, market_data)
        if not setup.valid:
            return TradeSignal(
                action="NO_TRADE", symbol=symbol, timestamp=ts,
                direction=direction, regime=regime, setup=setup,
                rejection_reasons=setup.rejection_reasons,
            )

        # --- Size and order params ---
        equity = market_data["account_equity"]
        size_multiplier = self.config.post_loss_size_multiplier if self._consecutive_losses >= 3 else 1.0
        order_params = self._build_order_params(symbol, direction, setup, equity, market_data, size_multiplier)
        if order_params is None:
            return TradeSignal(
                action="NO_TRADE", symbol=symbol, timestamp=ts,
                rejection_reasons=["Position sizing failed — leverage would exceed cap"],
            )

        # --- Effective R calculation ---
        round_trip_fees = (self.config.maker_fee + self.config.taker_fee) + self.config.estimated_slippage
        risk_unit = abs(setup.entry_price - setup.stop_price) / setup.entry_price
        fee_r_drag = round_trip_fees / risk_unit if risk_unit > 0 else 0
        partial_r = (self.config.partial_exit_pct * self.config.partial_exit_r +
                     (1 - self.config.partial_exit_pct) * self.config.final_target_r)
        effective_r_net = partial_r - fee_r_drag

        # --- Confidence score ---
        confidence = self._score_confidence(regime, setup)

        signal = TradeSignal(
            action="TRADE",
            symbol=symbol,
            timestamp=ts,
            direction=direction,
            regime=regime,
            setup=setup,
            order_params=order_params,
            confidence=confidence,
            effective_r_net=effective_r_net,
        )
        logger.info(signal.summary())
        return signal

    # ------------------------------------------------------------------
    # Regime computation
    # ------------------------------------------------------------------

    def _compute_regime(self, df5: pd.DataFrame, df15: pd.DataFrame) -> RegimeState:
        cfg = self.config
        r = RegimeState()

        # 15m EMA alignment
        ema20 = ema(df15["close"], cfg.ema_fast_15m)
        ema50 = ema(df15["close"], cfg.ema_slow_15m)
        r.ema_aligned = (ema20.iloc[-1] > ema50.iloc[-1]) or (ema20.iloc[-1] < ema50.iloc[-1])
        # (True always — actual directional check is in setup detection)

        # 15m ADX
        adx_series = adx(df15, 14)
        r.adx_value = adx_series.iloc[-1] if not np.isnan(adx_series.iloc[-1]) else 0.0
        r.adx_ok = r.adx_value > cfg.adx_min

        # 5m Choppiness
        ci = choppiness_index(df5, 14)
        r.choppiness = ci.iloc[-1] if not np.isnan(ci.iloc[-1]) else 100.0
        r.choppiness_ok = r.choppiness < cfg.choppiness_max

        # VWAP — check exclusion window
        now_utc = datetime.now(timezone.utc)
        r.vwap_exclusion_active = now_utc.hour < cfg.vwap_exclusion_hours
        vwap_series = vwap(df5)
        current_close = df5["close"].iloc[-1]
        ema20_15 = ema(df15["close"], cfg.ema_fast_15m).iloc[-1]
        ema50_15 = ema(df15["close"], cfg.ema_slow_15m).iloc[-1]
        bullish = ema20_15 > ema50_15
        if r.vwap_exclusion_active:
            # During exclusion: use 5m EMA20 as proxy anchor
            ema20_5m = ema(df5["close"], cfg.ema_fast_5m).iloc[-1]
            r.price_above_vwap = current_close > ema20_5m
        else:
            r.price_above_vwap = current_close > vwap_series.iloc[-1]
        r.vwap_ok = (bullish and r.price_above_vwap) or (not bullish and not r.price_above_vwap)

        # ATR vs median
        atr_series = atr(df5, cfg.atr_period)
        current_atr = atr_series.iloc[-1]
        atr_median = atr_series.iloc[-20:].median()
        r.atr_above_median = current_atr > atr_median * 0.9  # allow 10% below median

        # RVOL
        rvol_series = relative_volume(df5, 20)
        r.rvol = rvol_series.iloc[-1] if not np.isnan(rvol_series.iloc[-1]) else 0.0
        r.rvol_ok = r.rvol >= cfg.rvol_min

        # CVD
        cvd_series = cvd(df5)
        cvd_delta = cvd_series.iloc[-1] - cvd_series.iloc[-1 - cfg.cvd_lookback]
        if bullish:
            r.cvd_confirming = cvd_delta >= 0  # rising or flat for longs
        else:
            r.cvd_confirming = cvd_delta <= 0  # declining or flat for shorts

        # Time filter
        r.time_ok = self._time_filter_ok(now_utc)

        return r

    # ------------------------------------------------------------------
    # Setup detection
    # ------------------------------------------------------------------

    def _detect_setup(self, df5: pd.DataFrame, direction: str, market_data: dict) -> SetupState:
        cfg = self.config
        setup = SetupState(direction=direction)
        close = df5["close"]
        high = df5["high"]
        low = df5["low"]
        current_close = close.iloc[-1]
        atr_val = atr(df5, cfg.atr_period).iloc[-1]
        setup.atr = atr_val

        # Identify breakout range (last 3–8 candles excluding current)
        lookback = cfg.breakout_lookback
        range_candles = df5.iloc[-1-lookback:-1]
        breakout_high = range_candles["high"].max()
        breakout_low = range_candles["low"].min()

        if direction == "long":
            # Breakout: current close above range high
            if current_close <= breakout_high:
                setup.rejection_reasons.append("No breakout: close did not exceed range high")
                return setup

            setup.breakout_level = breakout_high
            # Extension check
            extension = (current_close - breakout_high) / atr_val if atr_val > 0 else 999
            rvol = relative_volume(df5, 20).iloc[-1]
            max_chase = cfg.max_chase_atr_high_rvol if rvol >= cfg.high_rvol_chase_threshold else cfg.max_chase_atr
            if extension > max_chase:
                setup.rejection_reasons.append(
                    f"Extended {extension:.2f} ATR beyond trigger (max {max_chase:.2f})")
                return setup

            # Stop placement — wider of structural or ATR
            structural_stop = range_candles["low"].min()
            atr_stop = market_data["best_ask"] - cfg.stop_atr_max * atr_val
            setup.structure_stop = structural_stop
            setup.atr_stop = atr_stop
            setup.stop_price = min(structural_stop, atr_stop)  # wider = lower for longs

            # Entry
            setup.entry_price = market_data["best_ask"]

        else:  # short
            if current_close >= breakout_low:
                setup.rejection_reasons.append("No breakdown: close did not breach range low")
                return setup

            setup.breakout_level = breakout_low
            extension = (breakout_low - current_close) / atr_val if atr_val > 0 else 999
            rvol = relative_volume(df5, 20).iloc[-1]
            max_chase = cfg.max_chase_atr_high_rvol if rvol >= cfg.high_rvol_chase_threshold else cfg.max_chase_atr
            if extension > max_chase:
                setup.rejection_reasons.append(
                    f"Extended {extension:.2f} ATR beyond trigger (max {max_chase:.2f})")
                return setup

            structural_stop = range_candles["high"].max()
            atr_stop = market_data["best_bid"] + cfg.stop_atr_max * atr_val
            setup.structure_stop = structural_stop
            setup.atr_stop = atr_stop
            setup.stop_price = max(structural_stop, atr_stop)  # wider = higher for shorts

            setup.entry_price = market_data["best_bid"]

        # R distance check
        risk = abs(setup.entry_price - setup.stop_price)
        if risk <= 0:
            setup.rejection_reasons.append("Zero risk distance")
            return setup

        # Estimate next resistance/support
        if direction == "long":
            next_resistance = high.iloc[-20:].max()
            r_distance = (next_resistance - setup.entry_price) / risk
        else:
            next_support = low.iloc[-20:].min()
            r_distance = (setup.entry_price - next_support) / risk

        setup.r_distance = r_distance
        if r_distance < cfg.min_r_distance:
            setup.rejection_reasons.append(
                f"Insufficient reward distance: {r_distance:.2f}R < {cfg.min_r_distance:.2f}R")
            return setup

        # TP levels
        if direction == "long":
            setup.tp1_price = setup.entry_price + cfg.partial_exit_r * risk
            setup.tp_final_price = setup.entry_price + cfg.final_target_r * risk
        else:
            setup.tp1_price = setup.entry_price - cfg.partial_exit_r * risk
            setup.tp_final_price = setup.entry_price - cfg.final_target_r * risk

        setup.valid = True
        return setup

    # ------------------------------------------------------------------
    # Order parameter construction
    # ------------------------------------------------------------------

    def _build_order_params(
        self,
        symbol: str,
        direction: str,
        setup: SetupState,
        equity: float,
        market_data: dict,
        size_multiplier: float,
    ) -> Optional[OrderParams]:
        cfg = self.config
        side = "buy" if direction == "long" else "sell"

        # Position sizing
        risk_amount = equity * cfg.risk_per_trade_pct * size_multiplier
        stop_distance_pct = abs(setup.entry_price - setup.stop_price) / setup.entry_price
        if stop_distance_pct <= 0:
            return None

        position_value = risk_amount / stop_distance_pct
        leverage = position_value / equity
        if leverage > cfg.max_leverage:
            # Reduce size to respect leverage cap
            position_value = equity * cfg.max_leverage
            leverage = cfg.max_leverage
        leverage = round(leverage, 1)
        size = position_value / setup.entry_price

        # Entry order type: prefer maker (ALO) for retest entries
        entry_order_type = "Alo"  # switch to "Ioc" in executor if using continuation

        # SL with explicit limit price (Hyperliquid requires this)
        if direction == "long":
            sl_trigger = setup.stop_price
            sl_limit = sl_trigger * (1 - cfg.sl_limit_buffer_pct)
            tp1_trigger = setup.tp1_price
            tp1_limit = tp1_trigger * (1 - cfg.tp_limit_buffer_pct)
            tp_final_trigger = setup.tp_final_price
            tp_final_limit = tp_final_trigger * (1 - cfg.tp_limit_buffer_pct)
        else:
            sl_trigger = setup.stop_price
            sl_limit = sl_trigger * (1 + cfg.sl_limit_buffer_pct)
            tp1_trigger = setup.tp1_price
            tp1_limit = tp1_trigger * (1 + cfg.tp_limit_buffer_pct)
            tp_final_trigger = setup.tp_final_price
            tp_final_limit = tp_final_trigger * (1 + cfg.tp_limit_buffer_pct)

        tp1_size = size * cfg.partial_exit_pct
        tp_final_size = size * (1 - cfg.partial_exit_pct)

        return OrderParams(
            symbol=symbol,
            side=side,
            size=round(size, 6),
            entry_price=_sig5(setup.entry_price),
            entry_order_type=entry_order_type,
            stop_trigger=_sig5(sl_trigger),
            stop_limit=_sig5(sl_limit),
            tp1_trigger=_sig5(tp1_trigger),
            tp1_limit=_sig5(tp1_limit),
            tp1_size=round(tp1_size, 6),
            tp_final_trigger=_sig5(tp_final_trigger),
            tp_final_limit=_sig5(tp_final_limit),
            tp_final_size=round(tp_final_size, 6),
            leverage=leverage,
        )

    # ------------------------------------------------------------------
    # Session risk
    # ------------------------------------------------------------------

    def _session_risk_check(self, market_data: dict) -> tuple[bool, str]:
        cfg = self.config
        equity = market_data.get("account_equity", 0)
        daily_loss = market_data.get("session_daily_loss", 0)
        consec = market_data.get("session_consecutive_losses", self._consecutive_losses)

        if equity > 0 and daily_loss / equity >= cfg.max_daily_loss_pct:
            return False, f"Daily loss limit hit: {daily_loss/equity*100:.2f}% ≥ {cfg.max_daily_loss_pct*100:.1f}%"
        if consec >= cfg.max_session_losses:
            return False, f"Session halted: {consec} consecutive losses ≥ {cfg.max_session_losses}"
        if market_data.get("open_position") is not None:
            return False, "Position already open — max 1 concurrent position"
        return True, ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _time_filter_ok(self, now: datetime) -> bool:
        cfg = self.config
        h = now.hour
        if h in cfg.blocked_hours:
            return False
        for start, end in cfg.session_windows:
            if start <= h < end:
                return True
        return False

    def _data_ok(self, market_data: dict) -> bool:
        if not market_data.get("candles_5m") or len(market_data["candles_5m"]) < 60:
            return False
        if not market_data.get("candles_15m") or len(market_data["candles_15m"]) < 60:
            return False
        if not market_data.get("mark_price"):
            return False
        return True

    def _score_confidence(self, regime: RegimeState, setup: SetupState) -> int:
        score = 5  # base
        if regime.adx_value > 30: score += 1
        if regime.rvol >= 2.0: score += 1
        if regime.choppiness < 45: score += 1
        if setup.r_distance >= 2.0: score += 1
        if not regime.vwap_exclusion_active: score += 1
        # Penalise
        if regime.adx_value < 22: score -= 1
        if setup.extended_beyond_trigger > 0.5 * setup.atr: score -= 1
        return max(1, min(10, score))

    def record_result(self, result_r: float):
        """Call after each trade closes to update rolling performance tracking."""
        self._performance.append(result_r)
        if result_r < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        # Keep last 50
        if len(self._performance) > 50:
            self._performance.pop(0)

    def performance_summary(self) -> dict:
        if not self._performance:
            return {}
        wins = [r for r in self._performance if r > 0]
        return {
            "trades": len(self._performance),
            "win_rate": len(wins) / len(self._performance),
            "avg_r": sum(self._performance) / len(self._performance),
            "profit_factor": (sum(wins) / abs(sum(r for r in self._performance if r < 0)))
                             if any(r < 0 for r in self._performance) else float("inf"),
            "consecutive_losses": self._consecutive_losses,
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _sig5(price: float) -> float:
    """Round to 5 significant figures — Hyperliquid price precision requirement."""
    if price == 0:
        return 0.0
    digits = 5 - int(math.floor(math.log10(abs(price)))) - 1
    return round(price, digits)


# ---------------------------------------------------------------------------
# Hyperliquid order executor stub
# ---------------------------------------------------------------------------

class HyperliquidExecutor:
    """
    Stub executor — replace with your actual Hyperliquid SDK calls.
    Shows correct order construction patterns for v2 strategy.
    """

    def __init__(self, info_client, exchange_client):
        self.info = info_client
        self.exchange = exchange_client

    def submit_trade(self, signal: TradeSignal) -> dict:
        """Submit entry + attach TP/SL orders."""
        op = signal.order_params
        results = {}

        # 1. Set leverage
        self.exchange.update_leverage(op.leverage, op.symbol)

        # 2. Entry order (ALO = maker, IOC = aggressive taker)
        entry_order = {
            "coin": op.symbol,
            "is_buy": op.side == "buy",
            "sz": op.size,
            "limit_px": op.entry_price,
            "order_type": {"limit": {"tif": op.entry_order_type}},
            "reduce_only": False,
        }
        results["entry"] = self.exchange.order(**entry_order)

        # 3. Stop loss — trigger + explicit limit price (CRITICAL: not trigger-only)
        sl_order = {
            "coin": op.symbol,
            "is_buy": op.side != "buy",  # opposite side for exit
            "sz": op.size,
            "limit_px": op.stop_limit,   # explicit limit — do not omit
            "order_type": {
                "trigger": {
                    "triggerPx": op.stop_trigger,
                    "isMarket": False,   # False = limit trigger, not market trigger
                    "tpsl": "sl",
                }
            },
            "reduce_only": True,
        }
        results["sl"] = self.exchange.order(**sl_order)

        # 4. TP1 — partial exit at 1R
        tp1_order = {
            "coin": op.symbol,
            "is_buy": op.side != "buy",
            "sz": op.tp1_size,
            "limit_px": op.tp1_limit,
            "order_type": {
                "trigger": {
                    "triggerPx": op.tp1_trigger,
                    "isMarket": False,
                    "tpsl": "tp",
                }
            },
            "reduce_only": True,
        }
        results["tp1"] = self.exchange.order(**tp1_order)

        # 5. TP final — remainder
        tp_final_order = {
            "coin": op.symbol,
            "is_buy": op.side != "buy",
            "sz": op.tp_final_size,
            "limit_px": op.tp_final_limit,
            "order_type": {
                "trigger": {
                    "triggerPx": op.tp_final_trigger,
                    "isMarket": False,
                    "tpsl": "tp",
                }
            },
            "reduce_only": True,
        }
        results["tp_final"] = self.exchange.order(**tp_final_order)

        return results

    def emergency_exit(self, symbol: str, size: float, is_long: bool):
        """
        Flatten position immediately using IOC limit with offset.
        Called by failsafe logic (connectivity loss, flash crash, etc.)
        """
        # Get current best price
        # In real code: fetch order book snapshot
        # Using aggressive offset to guarantee fill
        offset_pct = 0.001  # 0.1%
        # For a real implementation, fetch live price here
        # Placeholder — wire to your data feed
        logger.critical(f"EMERGENCY EXIT: {symbol} {'SELL' if is_long else 'BUY'} {size}")
        # self.exchange.order(coin=symbol, is_buy=not is_long, sz=size,
        #     limit_px=mark_price * (0.999 if is_long else 1.001),
        #     order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)


# ---------------------------------------------------------------------------
# Quick self-test (run: python scalp_strategy_v2.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    logging.basicConfig(level=logging.INFO)

    def _fake_candle(base, i):
        o = base + random.uniform(-5, 5)
        c = o + random.uniform(-3, 3)
        h = max(o, c) + random.uniform(0, 2)
        l = min(o, c) - random.uniform(0, 2)
        v = random.uniform(100, 1000)
        return {"open": o, "high": h, "low": l, "close": c, "volume": v}

    base_price = 65000.0
    # Create a trending market (slowly climbing)
    candles_5m = [_fake_candle(base_price + i * 10, i) for i in range(80)]
    candles_15m = [_fake_candle(base_price + i * 30, i) for i in range(80)]

    fake_data = {
        "candles_5m": candles_5m,
        "candles_15m": candles_15m,
        "account_equity": 10_000.0,
        "session_daily_loss": 0.0,
        "session_consecutive_losses": 0,
        "session_trade_count": 0,
        "mark_price": candles_5m[-1]["close"],
        "best_bid": candles_5m[-1]["close"] - 1,
        "best_ask": candles_5m[-1]["close"] + 1,
        "open_position": None,
    }

    strategy = ScalpStrategy()
    signal = strategy.evaluate("BTC", fake_data)
    print("\n" + "="*60)
    print(signal.summary())
    print("="*60)
    if signal.action == "TRADE":
        op = signal.order_params
        print(f"\nOrder params:")
        print(f"  Entry:      {op.entry_price} ({op.entry_order_type})")
        print(f"  SL trigger: {op.stop_trigger}  SL limit: {op.stop_limit}")
        print(f"  TP1:        {op.tp1_trigger} ({op.tp1_size:.4f} units)")
        print(f"  TP final:   {op.tp_final_trigger} ({op.tp_final_size:.4f} units)")
        print(f"  Leverage:   {op.leverage}x")
        print(f"  Net R est:  {signal.effective_r_net:.2f}R")
        print(f"  Confidence: {signal.confidence}/10")
