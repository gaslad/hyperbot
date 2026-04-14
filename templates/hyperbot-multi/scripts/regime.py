"""Portfolio-level market regime filter.

Determines whether the overall crypto market environment is favorable
for trading. This runs above individual strategy regime checks — if the
portfolio regime says "risk-off", all pairs reduce size or pause entries.

Inputs:
    - BTC candles (1D) — trend anchor for the entire market
    - Funding rates across tracked pairs
    - BTC dominance proxy (BTC price vs altcoin basket)

Outputs:
    - MarketRegime: GREEN / YELLOW / RED
    - Size multiplier (1.0 / 0.5 / 0.0)
    - Reasons for the current regime call

All logic is deterministic. No ML.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any


class RegimeLevel:
    GREEN = "green"    # Full size, all strategies active
    YELLOW = "yellow"  # Reduced size (50%), only high-confidence signals
    RED = "red"        # No new entries, manage existing positions only


@dataclass
class MarketRegime:
    level: str = RegimeLevel.GREEN
    size_multiplier: float = 1.0
    min_confidence: float = 0.0   # minimum confidence to accept signals
    reasons: list[str] = field(default_factory=list)

    def allows_entry(self, confidence: float = 0.0) -> bool:
        if self.level == RegimeLevel.RED:
            return False
        if self.level == RegimeLevel.YELLOW and confidence < self.min_confidence:
            return False
        return True


# ---------------------------------------------------------------------------
# Candle helpers (self-contained, no dependency on signals.py)
# ---------------------------------------------------------------------------

def _closes(candles: list[dict]) -> list[float]:
    return [float(c.get("c") or c.get("close") or 0) for c in candles]


def _highs(candles: list[dict]) -> list[float]:
    return [float(c.get("h") or c.get("high") or 0) for c in candles]


def _lows(candles: list[dict]) -> list[float]:
    return [float(c.get("l") or c.get("low") or 0) for c in candles]


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return statistics.mean(values[-period:])


def _atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].get("h", candles[i].get("high", 0)))
        l = float(candles[i].get("l", candles[i].get("low", 0)))
        pc = float(candles[i - 1].get("c", candles[i - 1].get("close", 0)))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.mean(trs[-period:])


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
# Core regime evaluation
# ---------------------------------------------------------------------------

def evaluate(
    btc_candles_1d: list[dict],
    pair_funding_rates: dict[str, float] | None = None,
    altcoin_prices: dict[str, float] | None = None,
    btc_price: float | None = None,
) -> MarketRegime:
    """Evaluate portfolio-level market regime.

    Args:
        btc_candles_1d: BTC daily candles (minimum 30, ideally 60+)
        pair_funding_rates: {coin: funding_rate} — negative = shorts paying
        altcoin_prices: {coin: current_price} — for dominance proxy
        btc_price: current BTC price (or derived from candles)

    Returns:
        MarketRegime with level, size_multiplier, and reasons
    """
    regime = MarketRegime()
    red_flags = 0
    yellow_flags = 0

    if not btc_candles_1d or len(btc_candles_1d) < 21:
        regime.reasons.append("Insufficient BTC data — defaulting to cautious")
        regime.level = RegimeLevel.YELLOW
        regime.size_multiplier = 0.5
        regime.min_confidence = 0.7
        return regime

    closes = _closes(btc_candles_1d)
    price = btc_price or closes[-1]

    # ---------------------------------------------------------------
    # Check 1: BTC trend (price vs 20-day SMA)
    # ---------------------------------------------------------------
    sma20 = _sma(closes, 20)
    if sma20:
        if price < sma20:
            distance_pct = (sma20 - price) / sma20 * 100
            if distance_pct > 5:
                red_flags += 1
                regime.reasons.append(f"BTC {distance_pct:.1f}% below SMA20 — strong downtrend")
            else:
                yellow_flags += 1
                regime.reasons.append(f"BTC {distance_pct:.1f}% below SMA20 — weakening")
        else:
            regime.reasons.append(f"BTC above SMA20 — trend intact")

    # ---------------------------------------------------------------
    # Check 2: BTC momentum (3-day trend direction)
    # ---------------------------------------------------------------
    if len(closes) >= 4:
        if _falling(closes, 3):
            yellow_flags += 1
            regime.reasons.append("BTC 3-day momentum declining")
        elif _rising(closes, 3):
            regime.reasons.append("BTC 3-day momentum rising")

    # ---------------------------------------------------------------
    # Check 3: BTC volatility regime (ATR vs median)
    # ---------------------------------------------------------------
    atr_val = _atr(btc_candles_1d, 14)
    if atr_val and len(btc_candles_1d) >= 30:
        # Compare current ATR to 30-day median
        recent_atrs = []
        for i in range(max(0, len(btc_candles_1d) - 30), len(btc_candles_1d)):
            if i > 0:
                h = float(btc_candles_1d[i].get("h", btc_candles_1d[i].get("high", 0)))
                l = float(btc_candles_1d[i].get("l", btc_candles_1d[i].get("low", 0)))
                pc = float(btc_candles_1d[i - 1].get("c", btc_candles_1d[i - 1].get("close", 0)))
                recent_atrs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if recent_atrs:
            median_atr = statistics.median(recent_atrs)
            if median_atr > 0 and atr_val > median_atr * 1.5:
                yellow_flags += 1
                regime.reasons.append(f"BTC volatility elevated ({atr_val / median_atr:.1f}x median)")

    # ---------------------------------------------------------------
    # Check 4: Funding rate sentiment
    # ---------------------------------------------------------------
    if pair_funding_rates:
        rates = list(pair_funding_rates.values())
        avg_funding = statistics.mean(rates) if rates else 0
        negative_count = sum(1 for r in rates if r < 0)

        if avg_funding < -0.001:
            yellow_flags += 1
            regime.reasons.append(f"Avg funding negative ({avg_funding:.4f}) — shorts paying, sentiment bearish")
        elif avg_funding < -0.005:
            red_flags += 1
            regime.reasons.append(f"Avg funding deeply negative ({avg_funding:.4f}) — extreme fear")
        elif avg_funding > 0.003:
            yellow_flags += 1
            regime.reasons.append(f"Avg funding elevated ({avg_funding:.4f}) — potential overcrowded longs")
        else:
            regime.reasons.append(f"Funding neutral ({avg_funding:.4f})")

        if negative_count > len(rates) * 0.7:
            yellow_flags += 1
            regime.reasons.append(f"{negative_count}/{len(rates)} pairs have negative funding")

    # ---------------------------------------------------------------
    # Check 5: BTC daily range compression (potential for big move)
    # ---------------------------------------------------------------
    if len(btc_candles_1d) >= 10:
        recent_ranges = []
        for c in btc_candles_1d[-10:]:
            h = float(c.get("h", c.get("high", 0)))
            l = float(c.get("l", c.get("low", 0)))
            if l > 0:
                recent_ranges.append((h - l) / l * 100)
        if recent_ranges and statistics.mean(recent_ranges) < 1.5:
            regime.reasons.append("BTC range compressed — potential breakout imminent (caution)")
            yellow_flags += 1

    # ---------------------------------------------------------------
    # Check 6: Drawdown from recent highs
    # ---------------------------------------------------------------
    if len(closes) >= 14:
        recent_high = max(_highs(btc_candles_1d[-14:]))
        if recent_high > 0:
            dd_pct = (recent_high - price) / recent_high * 100
            if dd_pct > 10:
                red_flags += 1
                regime.reasons.append(f"BTC {dd_pct:.1f}% off 14-day high — significant drawdown")
            elif dd_pct > 5:
                yellow_flags += 1
                regime.reasons.append(f"BTC {dd_pct:.1f}% off 14-day high — moderate pullback")

    # ---------------------------------------------------------------
    # Aggregate into regime level
    # ---------------------------------------------------------------
    if red_flags >= 2:
        regime.level = RegimeLevel.RED
        regime.size_multiplier = 0.0
        regime.min_confidence = 1.0  # nothing passes
    elif red_flags >= 1 or yellow_flags >= 3:
        regime.level = RegimeLevel.RED
        regime.size_multiplier = 0.0
        regime.min_confidence = 1.0
    elif yellow_flags >= 2:
        regime.level = RegimeLevel.YELLOW
        regime.size_multiplier = 0.5
        regime.min_confidence = 0.75
    elif yellow_flags >= 1:
        regime.level = RegimeLevel.YELLOW
        regime.size_multiplier = 0.75
        regime.min_confidence = 0.65
    else:
        regime.level = RegimeLevel.GREEN
        regime.size_multiplier = 1.0
        regime.min_confidence = 0.0

    return regime
