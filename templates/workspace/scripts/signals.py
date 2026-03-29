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
    min_pullback_pct = filters_cfg.get("min_pullback_pct", 2.0)
    invalidation_pct = risk_cfg.get("invalidation_below_sma_pct", 2.0)

    daily_closes = closes(candles_1d)
    sma_val = sma(daily_closes, sma_period)

    if sma_val is None:
        return Signal(Direction.NONE, strategy_id, "trend_pullback", 0.0, current_price, ["insufficient data"])

    reasons = []
    score = 0.0

    # Check trend: price above SMA
    if current_price > sma_val:
        reasons.append(f"price above SMA{sma_period}")
        score += 0.3
    else:
        return Signal(Direction.NONE, strategy_id, "trend_pullback", 0.0, current_price, ["price below SMA — no uptrend"])

    # Check pullback: price within pullback zone of SMA
    pullback_pct = ((current_price - sma_val) / sma_val) * 100
    if pullback_pct <= pullback_zone_pct:
        reasons.append(f"pullback {pullback_pct:.1f}% within zone ({pullback_zone_pct}%)")
        score += 0.3
    else:
        reasons.append(f"price {pullback_pct:.1f}% above SMA — not in pullback zone")

    # Check minimum pullback from recent high
    if len(daily_closes) >= 5:
        recent_high = max(daily_closes[-5:])
        drop_from_high = ((recent_high - current_price) / recent_high) * 100
        if drop_from_high >= min_pullback_pct:
            reasons.append(f"pulled back {drop_from_high:.1f}% from recent high")
            score += 0.2

    # 4H trend confirmation
    h4_closes = closes(candles_4h)
    h4_sma = sma(h4_closes, 50)
    if h4_sma and current_price > h4_sma:
        reasons.append("4H price above SMA50")
        score += 0.2

    direction = Direction.BUY if score >= 0.5 else Direction.NONE
    stop_loss = sma_val * (1 - invalidation_pct / 100) if direction == Direction.BUY else None
    take_profit = current_price * 1.04 if direction == Direction.BUY else None  # 4% target

    return Signal(direction, strategy_id, "trend_pullback", min(score, 1.0), current_price,
                  reasons, current_price if direction == Direction.BUY else None, stop_loss, take_profit)


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

    if bb_w is None:
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
            score += 0.4
            direction_hint = Direction.BUY
        elif current_price < lower:
            reasons.append(f"breakout below lower band ({lower:.2f})")
            score += 0.4
            direction_hint = Direction.SELL
        else:
            reasons.append("no breakout yet — price within bands")
            direction_hint = Direction.NONE
    else:
        direction_hint = Direction.NONE

    # Volume/ATR confirmation
    atr_val = atr(candles_1d)
    if atr_val and len(candles_1d) >= 2:
        last_range = float(candles_1d[-1].get("h", candles_1d[-1].get("high", 0))) - float(candles_1d[-1].get("l", candles_1d[-1].get("low", 0)))
        if last_range > atr_val * 1.5:
            reasons.append("expansion candle confirms breakout")
            score += 0.2

    direction = direction_hint if score >= 0.6 else Direction.NONE
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
    wick_ratio_min = entry_cfg.get("wick_rejection_ratio", 2.0)

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
        direction = Direction.BUY
        stop_loss = last_low * 0.995
        take_profit = current_price * 1.05
    elif is_bearish_sweep and score >= 0.5:
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
# Dispatcher
# ---------------------------------------------------------------------------

DETECTORS = {
    "trend_pullback": detect_trend_pullback,
    "compression_breakout": detect_compression_breakout,
    "liquidity_sweep_reversal": detect_liquidity_sweep,
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
