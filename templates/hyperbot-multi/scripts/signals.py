#!/usr/bin/env python3
"""Strategy signal detection for Hyperbot workspaces.

Each strategy pack type has a signal detector that reads its config,
checks current market conditions, and returns a Signal (buy/sell/none).

All logic is deterministic — no ML or model calls.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config" / "strategies"


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NONE = "none"


@dataclass
class Signal:
    direction: Direction
    strategy_id: str
    pack_id: str
    confidence: float  # 0.0 - 1.0
    price: float
    reasons: list[str] = field(default_factory=list)
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


def load_strategy_config(strategy_id: str) -> dict:
    path = CONFIG_DIR / f"{strategy_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Strategy config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def _sf(v) -> float:
    """Safe float: handles None and explicit JSON nulls."""
    return float(v) if v is not None else 0.0


def closes(candles: list[dict]) -> list[float]:
    return [_sf(c.get("c") or c.get("close") or 0) for c in candles]


def highs(candles: list[dict]) -> list[float]:
    return [_sf(c.get("h") or c.get("high") or 0) for c in candles]


def lows(candles: list[dict]) -> list[float]:
    return [_sf(c.get("l") or c.get("low") or 0) for c in candles]


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return statistics.mean(values[-period:])


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = statistics.mean(values[:period])
    for val in values[period:]:
        result = (val - result) * multiplier + result
    return result


def atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].get("h", candles[i].get("high", 0)))
        l = float(candles[i].get("l", candles[i].get("low", 0)))
        pc = float(candles[i - 1].get("c", candles[i - 1].get("close", 0)))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.mean(trs[-period:])


def bollinger_width(values: list[float], period: int = 20) -> float | None:
    if len(values) < period:
        return None
    subset = values[-period:]
    mid = statistics.mean(subset)
    std = statistics.stdev(subset)
    if mid == 0:
        return None
    return (std * 2) / mid  # width as fraction of mid


def _rising(values: list[float], lookback: int = 3) -> bool:
    if len(values) < lookback + 1:
        return False
    subset = values[-(lookback + 1):]
    return all(a < b for a, b in zip(subset, subset[1:]))


def _falling(values: list[float], lookback: int = 3) -> bool:
    if len(values) < lookback + 1:
        return False
    subset = values[-(lookback + 1):]
    return all(a > b for a, b in zip(subset, subset[1:]))


# ---------------------------------------------------------------------------
# Strategy: Trend Pullback
# ---------------------------------------------------------------------------

def detect_trend_pullback(config: dict, candles_1d: list[dict], candles_4h: list[dict], current_price: float) -> Signal:
    strategy_id = config.get("strategy_id", "unknown")
    entry_cfg = config.get("entry", {})
    filters_cfg = config.get("filters", {})
    risk_cfg = config.get("risk", {})

    sma_period = entry_cfg.get("sma_period", 20)
    pullback_zone_pct = entry_cfg.get("pullback_zone_pct", 3.0)
    min_pullback_pct = filters_cfg.get("min_pullback_pct", 3.0)
    invalidation_pct = risk_cfg.get("invalidation_below_sma_pct", 2.0)

    daily_closes = closes(candles_1d)
    sma_val = sma(daily_closes, sma_period)
    h4_closes = closes(candles_4h)

    if sma_val is None or len(daily_closes) < sma_period + 2 or len(h4_closes) < 3:
        return Signal(Direction.NONE, strategy_id, "trend_pullback", 0.0, current_price, ["insufficient data"])

    prior_sma = sma(daily_closes[:-1], sma_period)

    # Determine trend direction: bullish (price > rising SMA) or bearish (price < falling SMA)
    is_bullish = current_price > sma_val and prior_sma is not None and sma_val > prior_sma
    is_bearish = current_price < sma_val and prior_sma is not None and sma_val < prior_sma

    if not is_bullish and not is_bearish:
        return Signal(Direction.NONE, strategy_id, "trend_pullback", 0.0, current_price,
                      ["no clear trend — price near SMA or SMA flat"])

    reasons = []
    score = 0.0

    if is_bullish:
        reasons.append(f"price above rising SMA{sma_period}")
        score += 0.3

        pullback_pct = ((current_price - sma_val) / sma_val) * 100
        in_pullback_zone = pullback_pct <= pullback_zone_pct
        if in_pullback_zone:
            reasons.append(f"pullback {pullback_pct:.1f}% within zone ({pullback_zone_pct}%)")
            score += 0.3
        else:
            reasons.append(f"price {pullback_pct:.1f}% above SMA — not in pullback zone")

        pulled_back_enough = False
        if len(daily_closes) >= 5:
            recent_high = max(highs(candles_1d[-5:]))
            drop_from_high = ((recent_high - current_price) / recent_high) * 100
            if drop_from_high >= min_pullback_pct:
                reasons.append(f"pulled back {drop_from_high:.1f}% from recent high")
                score += 0.2
                pulled_back_enough = True
            else:
                reasons.append(f"only {drop_from_high:.1f}% off recent high — needs {min_pullback_pct:.1f}%")

        h4_sma = sma(h4_closes, 50)
        h4_prior_sma = sma(h4_closes[:-1], 50) if len(h4_closes) > 50 else None
        h4_confirmed = bool(h4_sma and h4_prior_sma and current_price > h4_sma
                           and _rising(h4_closes, 2) and h4_sma > h4_prior_sma)
        if h4_confirmed:
            reasons.append("4H price above SMA50 with rising closes")
            score += 0.2
        else:
            reasons.append("4H trend not confirmed — need rising confirmation")

        direction = Direction.BUY if in_pullback_zone and pulled_back_enough and h4_confirmed else Direction.NONE
        stop_loss = sma_val * (1 - invalidation_pct / 100) if direction == Direction.BUY else None
        take_profit = current_price * 1.04 if direction == Direction.BUY else None

    else:  # is_bearish
        reasons.append(f"price below falling SMA{sma_period}")
        score += 0.3

        pullback_pct = ((sma_val - current_price) / sma_val) * 100
        in_pullback_zone = pullback_pct <= pullback_zone_pct
        if in_pullback_zone:
            reasons.append(f"pullback {pullback_pct:.1f}% within zone ({pullback_zone_pct}%)")
            score += 0.3
        else:
            reasons.append(f"price {pullback_pct:.1f}% below SMA — not in pullback zone")

        pulled_back_enough = False
        if len(daily_closes) >= 5:
            recent_low = min(lows(candles_1d[-5:]))
            bounce_from_low = ((current_price - recent_low) / recent_low) * 100
            if bounce_from_low >= min_pullback_pct:
                reasons.append(f"bounced {bounce_from_low:.1f}% from recent low")
                score += 0.2
                pulled_back_enough = True
            else:
                reasons.append(f"only {bounce_from_low:.1f}% off recent low — needs {min_pullback_pct:.1f}%")

        h4_sma = sma(h4_closes, 50)
        h4_prior_sma = sma(h4_closes[:-1], 50) if len(h4_closes) > 50 else None
        h4_confirmed = bool(h4_sma and h4_prior_sma and current_price < h4_sma
                           and _falling(h4_closes, 2) and h4_sma < h4_prior_sma)
        if h4_confirmed:
            reasons.append("4H price below SMA50 with falling closes")
            score += 0.2
        else:
            reasons.append("4H downtrend not confirmed — need falling confirmation")

        direction = Direction.SELL if in_pullback_zone and pulled_back_enough and h4_confirmed else Direction.NONE
        stop_loss = sma_val * (1 + invalidation_pct / 100) if direction == Direction.SELL else None
        take_profit = current_price * 0.96 if direction == Direction.SELL else None

    return Signal(direction, strategy_id, "trend_pullback", min(score, 1.0), current_price,
                  reasons, current_price if direction != Direction.NONE else None, stop_loss, take_profit)


# ---------------------------------------------------------------------------
# Strategy: Compression Breakout
# ---------------------------------------------------------------------------

def detect_compression_breakout(config: dict, candles_1d: list[dict], candles_4h: list[dict], current_price: float) -> Signal:
    strategy_id = config.get("strategy_id", "unknown")
    entry_cfg = config.get("entry", {})
    filters_cfg = config.get("filters", {})
    risk_cfg = config.get("risk", {})

    bb_period = entry_cfg.get("bb_period", 20)
    compression_threshold = entry_cfg.get("compression_threshold", 0.04)

    daily_closes = closes(candles_1d)
    bb_w = bollinger_width(daily_closes, bb_period)
    h4_closes = closes(candles_4h)

    if bb_w is None or len(daily_closes) < bb_period + 2 or len(h4_closes) < 3:
        return Signal(Direction.NONE, strategy_id, "compression_breakout", 0.0, current_price, ["insufficient data"])

    reasons = []
    score = 0.0

    # Check compression
    if bb_w <= compression_threshold:
        reasons.append(f"Bollinger width {bb_w:.4f} <= {compression_threshold} — compressed")
        score += 0.4
    else:
        reasons.append(f"Bollinger width {bb_w:.4f} > {compression_threshold} — not compressed")

    # Check for breakout: price above upper band or below lower
    if len(daily_closes) >= bb_period:
        subset = daily_closes[-bb_period:]
        mid = statistics.mean(subset)
        std = statistics.stdev(subset)
        upper = mid + 2 * std
        lower = mid - 2 * std

        if current_price > upper:
            reasons.append(f"breakout above upper band ({upper:.2f})")
            if _rising(daily_closes, 2) and _rising(h4_closes, 2):
                reasons.append("daily and 4H momentum confirm upside breakout")
                score += 0.2
                score += 0.4
                direction_hint = Direction.BUY
            else:
                reasons.append("breakout lacks rising confirmation")
                direction_hint = Direction.NONE
        elif current_price < lower:
            reasons.append(f"breakout below lower band ({lower:.2f})")
            if _falling(daily_closes, 2) and _falling(h4_closes, 2):
                reasons.append("daily and 4H momentum confirm downside breakout")
                score += 0.2
                score += 0.4
                direction_hint = Direction.SELL
            else:
                reasons.append("breakout lacks falling confirmation")
                direction_hint = Direction.NONE
        else:
            reasons.append("no breakout yet — price within bands")
            direction_hint = Direction.NONE
    else:
        direction_hint = Direction.NONE

    # Volume/ATR confirmation
    atr_val = atr(candles_1d)
    if atr_val and len(candles_1d) >= 2:
        last_range = float(candles_1d[-1].get("h", candles_1d[-1].get("high", 0))) - float(candles_1d[-1].get("l", candles_1d[-1].get("low", 0)))
        if last_range > atr_val * 1.0:  # lowered to 1.0x ATR to catch intraday moves
            reasons.append("expansion candle confirms breakout")
            score += 0.2

    direction = direction_hint if score >= 0.4 else Direction.NONE  # Trigger on breakout alone
    stop_loss = None
    take_profit = None
    if direction == Direction.BUY:
        stop_loss = current_price * 0.98
        take_profit = current_price * 1.06
    elif direction == Direction.SELL:
        stop_loss = current_price * 1.02
        take_profit = current_price * 0.94

    return Signal(direction, strategy_id, "compression_breakout", min(score, 1.0), current_price,
                  reasons, current_price if direction != Direction.NONE else None, stop_loss, take_profit)


# ---------------------------------------------------------------------------
# Strategy: Liquidity Sweep Reversal
# ---------------------------------------------------------------------------

def detect_liquidity_sweep(config: dict, candles_1d: list[dict], candles_4h: list[dict], current_price: float) -> Signal:
    strategy_id = config.get("strategy_id", "unknown")
    entry_cfg = config.get("entry", {})
    filters_cfg = config.get("filters", {})
    risk_cfg = config.get("risk", {})

    lookback = entry_cfg.get("sweep_lookback_bars", 20)
    wick_ratio_min = entry_cfg.get("wick_rejection_ratio", 1.0)  # reduced from 2.0 to catch shorter sweeps

    daily_highs = highs(candles_1d)
    daily_lows = lows(candles_1d)
    daily_closes_list = closes(candles_1d)

    if len(candles_1d) < lookback + 1:
        return Signal(Direction.NONE, strategy_id, "liquidity_sweep_reversal", 0.0, current_price, ["insufficient data"])

    reasons = []
    score = 0.0

    # Find recent swing low and swing high (over lookback period, excluding last bar)
    recent_low = min(daily_lows[-(lookback + 1):-1])
    recent_high = max(daily_highs[-(lookback + 1):-1])

    last = candles_1d[-1]
    last_low = float(last.get("l", last.get("low", 0)))
    last_high = float(last.get("h", last.get("high", 0)))
    last_close = float(last.get("c", last.get("close", 0)))
    last_open = float(last.get("o", last.get("open", 0)))

    body = abs(last_close - last_open) or 0.001

    # Check for bullish sweep: wick below recent low, close above it
    if last_low < recent_low and last_close > recent_low:
        lower_wick = min(last_open, last_close) - last_low
        if lower_wick / body >= wick_ratio_min:
            reasons.append(f"swept below swing low {recent_low:.2f}, wick rejection ratio {lower_wick / body:.1f}")
            score += 0.5
            reasons.append("closed back above sweep level — bullish reversal")
            score += 0.3

    # Check for bearish sweep: wick above recent high, close below it
    if last_high > recent_high and last_close < recent_high:
        upper_wick = last_high - max(last_open, last_close)
        if upper_wick / body >= wick_ratio_min:
            reasons.append(f"swept above swing high {recent_high:.2f}, wick rejection ratio {upper_wick / body:.1f}")
            score += 0.5
            reasons.append("closed back below sweep level — bearish reversal")
            score += 0.3

    # Determine direction
    is_bullish_sweep = last_low < recent_low and last_close > recent_low
    is_bearish_sweep = last_high > recent_high and last_close < recent_high

    if is_bullish_sweep and score >= 0.5:
        if last_close <= last_open:
            reasons.append("bullish sweep needs a confirming green candle")
            return Signal(Direction.NONE, strategy_id, "liquidity_sweep_reversal", 0.0, current_price, reasons)
        if not _rising(daily_closes_list, 2):
            reasons.append("bullish sweep needs improving closes")
            return Signal(Direction.NONE, strategy_id, "liquidity_sweep_reversal", 0.0, current_price, reasons)
        direction = Direction.BUY
        stop_loss = last_low * 0.995
        take_profit = current_price * 1.05
    elif is_bearish_sweep and score >= 0.5:
        if last_close >= last_open:
            reasons.append("bearish sweep needs a confirming red candle")
            return Signal(Direction.NONE, strategy_id, "liquidity_sweep_reversal", 0.0, current_price, reasons)
        if not _falling(daily_closes_list, 2):
            reasons.append("bearish sweep needs weakening closes")
            return Signal(Direction.NONE, strategy_id, "liquidity_sweep_reversal", 0.0, current_price, reasons)
        direction = Direction.SELL
        stop_loss = last_high * 1.005
        take_profit = current_price * 0.95
    else:
        direction = Direction.NONE
        stop_loss = None
        take_profit = None
        if not reasons:
            reasons.append("no sweep detected")

    return Signal(direction, strategy_id, "liquidity_sweep_reversal", min(score, 1.0), current_price,
                  reasons, current_price if direction != Direction.NONE else None, stop_loss, take_profit)


# ---------------------------------------------------------------------------
# Strategy: Fibonacci Retracement
# ---------------------------------------------------------------------------

def _find_swing(candles: list[dict], lookback: int) -> tuple[float, float, int, int]:
    """Find the most recent significant swing high and swing low.

    Returns (swing_low, swing_high, low_idx, high_idx) where indices are
    positions within the *candles* list.  We scan the last *lookback* bars
    (excluding the current bar) so the swing is "confirmed".
    """
    h = highs(candles)
    l = lows(candles)
    # Exclude the last bar (still forming)
    window_h = h[-(lookback + 1):-1]
    window_l = l[-(lookback + 1):-1]
    if not window_h or not window_l:
        return 0.0, 0.0, 0, 0
    offset = len(h) - lookback - 1
    high_val = max(window_h)
    low_val = min(window_l)
    high_idx = offset + window_h.index(high_val)
    low_idx = offset + window_l.index(low_val)
    return low_val, high_val, low_idx, high_idx


_FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)

# Confidence bonus per level — deeper retracements in a trend are higher-conviction entries.
_FIB_CONFIDENCE = {0.236: 0.1, 0.382: 0.2, 0.5: 0.3, 0.618: 0.4, 0.786: 0.25}


def _best_fib_level(price: float, swing_low: float, swing_high: float,
                    swing_range: float, is_upswing: bool,
                    zone_tol: float) -> tuple[float, float, bool]:
    """Pick the Fib level closest to *price* that falls within the tolerance zone.

    Returns (fib_level, fib_price, in_zone).  If no level is in zone, returns
    the nearest one with in_zone=False.
    """
    zone_half = swing_range * zone_tol
    best_level = 0.0
    best_price = 0.0
    best_dist = float("inf")
    best_in_zone = False

    for lvl in _FIB_LEVELS:
        if is_upswing:
            fp = swing_high - swing_range * lvl
        else:
            fp = swing_low + swing_range * lvl
        dist = abs(price - fp)
        in_z = dist <= zone_half
        # Prefer any in-zone hit; among in-zone hits prefer the closest
        if (in_z and not best_in_zone) or (in_z == best_in_zone and dist < best_dist):
            best_level, best_price, best_dist, best_in_zone = lvl, fp, dist, in_z

    return best_level, best_price, best_in_zone


def detect_fib_retracement(config: dict, candles_1d: list[dict], candles_4h: list[dict], current_price: float) -> Signal:
    strategy_id = config.get("strategy_id", "unknown")
    entry_cfg = config.get("entry", {})
    filters_cfg = config.get("filters", {})
    risk_cfg = config.get("risk", {})

    lookback = entry_cfg.get("swing_lookback_bars", 30)
    trend_sma_period = entry_cfg.get("trend_sma_period", 50)
    require_trend = entry_cfg.get("require_trend_alignment", True)
    max_swing_age = filters_cfg.get("max_swing_age_bars", 20)

    daily_closes = closes(candles_1d)

    if len(candles_1d) < lookback + 2:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price, ["insufficient data"])

    # --- ATR-adaptive parameters ---
    # Derive zone tolerance, min swing size, and stop buffer from the asset's
    # own volatility so they scale automatically across BTC vs HYPE vs SOL.
    daily_atr = atr(candles_1d, 14)
    if daily_atr is None or current_price <= 0:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price, ["insufficient data for ATR"])
    atr_pct = daily_atr / current_price  # e.g. 0.03 = 3% daily ATR
    zone_tol = atr_pct * 0.75           # zone = 75% of daily ATR (BTC ~2.3%, HYPE ~5%)
    min_swing_pct = atr_pct * 3.0       # swing must be >= 3x daily ATR to matter
    stop_buffer_pct = atr_pct * 0.5     # stop buffer = half a daily ATR below swing

    # --- 1. Trend filter via SMA on daily ---
    sma_val = sma(daily_closes, trend_sma_period)
    if sma_val is None:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                      [f"need {trend_sma_period} daily bars for SMA"])

    prior_sma = sma(daily_closes[:-1], trend_sma_period)
    trend_bullish = current_price > sma_val and prior_sma is not None and sma_val > prior_sma
    trend_bearish = current_price < sma_val and prior_sma is not None and sma_val < prior_sma

    if require_trend and not trend_bullish and not trend_bearish:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                      ["no clear daily trend — SMA flat or price crossing"])

    # --- 2. Find the swing ---
    swing_low, swing_high, low_idx, high_idx = _find_swing(candles_1d, lookback)
    swing_range = swing_high - swing_low
    if swing_range <= 0:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                      ["no valid swing detected"])

    if swing_range / swing_low < min_swing_pct:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                      [f"swing {swing_range / swing_low * 100:.1f}% too small (need {min_swing_pct * 100:.1f}%)"])

    is_upswing = high_idx > low_idx
    n_bars = len(candles_1d)

    # --- 3. Auto-select the best Fibonacci level ---
    fib_level, fib_price, in_zone = _best_fib_level(
        current_price, swing_low, swing_high, swing_range, is_upswing, zone_tol)

    reasons = []
    score = 0.0

    # Compute all Fib level prices for the signal metadata
    fib_map: dict[float, float] = {}
    for lvl in _FIB_LEVELS:
        if is_upswing:
            fib_map[lvl] = swing_high - swing_range * lvl
        else:
            fib_map[lvl] = swing_low + swing_range * lvl

    # Retracement depth: how far price has pulled back into the swing
    if is_upswing:
        retrace_pct = ((swing_high - current_price) / swing_range * 100) if swing_range > 0 else 0.0
    else:
        retrace_pct = ((current_price - swing_low) / swing_range * 100) if swing_range > 0 else 0.0

    # Distance to nearest Fib level
    dist_to_fib_pct = abs(current_price - fib_price) / current_price * 100 if current_price > 0 else 0.0

    if is_upswing and (trend_bullish or not require_trend):
        swing_age = n_bars - 1 - high_idx
        if swing_age > max_swing_age:
            return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                          [f"swing high is {swing_age} bars old (max {max_swing_age})"])

        reasons.append(f"upswing ${swing_low:.2f} \u2192 ${swing_high:.2f} ({swing_range / swing_low * 100:.1f}%)")
        score += 0.2

        # Fib progress line: which level is being targeted and how far in
        fib_pct_label = f"{fib_level:.3f}".rstrip('0').rstrip('.')
        reasons.append(f"retraced {retrace_pct:.1f}% \u2014 targeting {fib_pct_label} Fib at ${fib_price:.2f}")

        if in_zone:
            reasons.append(f"price in {fib_pct_label} zone ({dist_to_fib_pct:.1f}% away)")
            score += _FIB_CONFIDENCE.get(fib_level, 0.3)
        else:
            reasons.append(f"price {dist_to_fib_pct:.1f}% from {fib_pct_label} level \u2014 not in zone yet")

        # Show all Fib levels as context
        fib_map_str = " | ".join(f"{f:.3f}".rstrip('0').rstrip('.') + f"=${p:.2f}" for f, p in sorted(fib_map.items()))
        reasons.append(f"Fib levels: {fib_map_str}")

        h4_c = closes(candles_4h)
        h4_bounce = len(h4_c) >= 3 and _rising(h4_c, 2)
        if h4_bounce:
            reasons.append("4H closes rising \u2014 bounce confirmation")
            score += 0.2
        else:
            reasons.append("4H bounce not confirmed")

        if trend_bullish:
            reasons.append(f"daily SMA{trend_sma_period} rising \u2014 trend aligned")
            score += 0.2

        if in_zone and h4_bounce:
            direction = Direction.BUY
            stop_loss = swing_low * (1 - stop_buffer_pct)
            risk = current_price - stop_loss
            take_profit = current_price + risk
        else:
            direction = Direction.NONE
            stop_loss = None
            take_profit = None

    elif not is_upswing and (trend_bearish or not require_trend):
        swing_age = n_bars - 1 - low_idx
        if swing_age > max_swing_age:
            return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                          [f"swing low is {swing_age} bars old (max {max_swing_age})"])

        reasons.append(f"downswing ${swing_high:.2f} \u2192 ${swing_low:.2f} ({swing_range / swing_high * 100:.1f}%)")
        score += 0.2

        fib_pct_label = f"{fib_level:.3f}".rstrip('0').rstrip('.')
        reasons.append(f"retraced {retrace_pct:.1f}% \u2014 targeting {fib_pct_label} Fib at ${fib_price:.2f}")

        if in_zone:
            reasons.append(f"price in {fib_pct_label} zone ({dist_to_fib_pct:.1f}% away)")
            score += _FIB_CONFIDENCE.get(fib_level, 0.3)
        else:
            reasons.append(f"price {dist_to_fib_pct:.1f}% from {fib_pct_label} level \u2014 not in zone yet")

        fib_map_str = " | ".join(f"{f:.3f}".rstrip('0').rstrip('.') + f"=${p:.2f}" for f, p in sorted(fib_map.items()))
        reasons.append(f"Fib levels: {fib_map_str}")

        h4_c = closes(candles_4h)
        h4_reject = len(h4_c) >= 3 and _falling(h4_c, 2)
        if h4_reject:
            reasons.append("4H closes falling \u2014 rejection confirmation")
            score += 0.2
        else:
            reasons.append("4H rejection not confirmed")

        if trend_bearish:
            reasons.append(f"daily SMA{trend_sma_period} falling \u2014 trend aligned")
            score += 0.2

        if in_zone and h4_reject:
            direction = Direction.SELL
            stop_loss = swing_high * (1 + stop_buffer_pct)
            risk = stop_loss - current_price
            take_profit = current_price - risk
        else:
            direction = Direction.NONE
            stop_loss = None
            take_profit = None

    else:
        return Signal(Direction.NONE, strategy_id, "fib_retracement", 0.0, current_price,
                      ["swing direction conflicts with trend"])

    return Signal(direction, strategy_id, "fib_retracement", min(score, 1.0), current_price,
                  reasons, current_price if direction != Direction.NONE else None, stop_loss, take_profit)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

DETECTORS = {
    "trend_pullback": detect_trend_pullback,
    "compression_breakout": detect_compression_breakout,
    "liquidity_sweep_reversal": detect_liquidity_sweep,
    "fib_retracement": detect_fib_retracement,
}


def detect_all_signals(candles_1d: list[dict], candles_4h: list[dict], current_price: float,
                       coin: str | None = None) -> list[Signal]:
    """Run installed strategy signal detectors.

    If *coin* is provided, only configs whose strategy_id starts with
    ``{coin.lower()}_`` (or whose market.coin matches) are evaluated.
    This prevents running another pair's detectors on the wrong candles
    in multi-pair workspaces.
    """
    signals = []
    coin_prefix = f"{coin.lower()}_" if coin else None
    for config_path in sorted(CONFIG_DIR.glob("*.json")):
        if config_path.name == "README.md":
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            # Filter by coin when in multi-pair mode
            if coin_prefix:
                sid = config.get("strategy_id", "")
                cfg_coin = config.get("market", {}).get("coin", "")
                if not sid.startswith(coin_prefix) and cfg_coin.upper() != coin.upper():
                    continue
            pack_id = config.get("pack_id", "")
            detector = DETECTORS.get(pack_id)
            if detector:
                sig = detector(config, candles_1d, candles_4h, current_price)
                signals.append(sig)
        except Exception as e:
            print(f"  [signals] Strategy {config_path.name} crashed: {e}", flush=True)
            continue
    return signals
