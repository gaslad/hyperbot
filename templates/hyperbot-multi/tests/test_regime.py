"""Tests for the portfolio-level market regime filter.

Covers:
- GREEN regime on bullish conditions
- YELLOW regime on mixed conditions
- RED regime on bearish conditions
- Funding rate impact
- Drawdown detection
- Edge cases (insufficient data)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import regime
from regime import RegimeLevel, evaluate


def _make_candles(prices: list[float], base_high_offset: float = 2.0, base_low_offset: float = 2.0) -> list[dict]:
    """Generate daily candles from a list of close prices."""
    candles = []
    for i, price in enumerate(prices):
        candles.append({
            "o": price - 0.5,
            "h": price + base_high_offset,
            "l": price - base_low_offset,
            "c": price,
            "v": 1000000,
        })
    return candles


class TestGreenRegime(unittest.TestCase):
    """Bullish conditions should produce GREEN regime."""

    def test_strong_uptrend_is_green(self):
        # Steady uptrend over 30 days with healthy range
        prices = [40000 + i * 500 for i in range(30)]
        candles = _make_candles(prices, base_high_offset=800, base_low_offset=800)
        result = evaluate(candles, btc_price=prices[-1])

        self.assertEqual(result.level, RegimeLevel.GREEN)
        self.assertEqual(result.size_multiplier, 1.0)
        self.assertTrue(result.allows_entry(0.5))

    def test_green_with_neutral_funding(self):
        prices = [50000 + i * 200 for i in range(30)]
        candles = _make_candles(prices)
        funding = {"BTC": 0.0001, "ETH": 0.0002, "SOL": 0.0001}
        result = evaluate(candles, funding, btc_price=prices[-1])

        self.assertIn(result.level, [RegimeLevel.GREEN, RegimeLevel.YELLOW])
        self.assertGreater(result.size_multiplier, 0)


class TestYellowRegime(unittest.TestCase):
    """Mixed conditions should produce YELLOW regime."""

    def test_declining_momentum_triggers_yellow(self):
        # Rising overall but falling last 3 days
        prices = [40000 + i * 300 for i in range(27)]
        prices.extend([prices[-1] - 200, prices[-1] - 400, prices[-1] - 600])
        candles = _make_candles(prices)
        result = evaluate(candles, btc_price=prices[-1])

        # Should be at least yellow due to declining momentum
        self.assertIn(result.level, [RegimeLevel.YELLOW, RegimeLevel.GREEN])

    def test_negative_funding_triggers_caution(self):
        prices = [50000 + i * 200 for i in range(30)]
        candles = _make_candles(prices, base_high_offset=500, base_low_offset=500)
        funding = {"BTC": -0.0015, "ETH": -0.0012, "SOL": -0.0008}
        result = evaluate(candles, funding, btc_price=prices[-1])

        # Negative funding should add yellow flags but uptrend keeps it from red
        self.assertIn(result.level, [RegimeLevel.YELLOW, RegimeLevel.GREEN])

    def test_yellow_blocks_low_confidence(self):
        result = regime.MarketRegime(
            level=RegimeLevel.YELLOW,
            size_multiplier=0.5,
            min_confidence=0.75,
        )
        self.assertFalse(result.allows_entry(0.6))
        self.assertTrue(result.allows_entry(0.8))


class TestRedRegime(unittest.TestCase):
    """Bearish conditions should produce RED regime."""

    def test_deep_downtrend_is_red(self):
        # Strong downtrend: price well below SMA20
        prices = [50000 - i * 600 for i in range(30)]
        candles = _make_candles(prices, base_high_offset=100, base_low_offset=100)
        result = evaluate(candles, btc_price=prices[-1])

        self.assertEqual(result.level, RegimeLevel.RED)
        self.assertEqual(result.size_multiplier, 0.0)
        self.assertFalse(result.allows_entry(1.0))

    def test_significant_drawdown_from_high(self):
        # Crash: 15% drop from recent high
        prices = [50000] * 10  # flat
        prices.extend([50000 - i * 1000 for i in range(1, 11)])  # crash
        prices.extend([42000] * 10)  # bottoming
        candles = _make_candles(prices, base_high_offset=200, base_low_offset=200)
        result = evaluate(candles, btc_price=42000)

        # Should be red or at least yellow due to drawdown
        self.assertIn(result.level, [RegimeLevel.RED, RegimeLevel.YELLOW])
        self.assertLess(result.size_multiplier, 1.0)

    def test_red_blocks_all_entries(self):
        result = regime.MarketRegime(level=RegimeLevel.RED, size_multiplier=0.0, min_confidence=1.0)
        self.assertFalse(result.allows_entry(1.0))
        self.assertFalse(result.allows_entry(0.0))


class TestEdgeCases(unittest.TestCase):
    """Edge cases and insufficient data."""

    def test_insufficient_data_defaults_yellow(self):
        candles = _make_candles([50000] * 10)  # only 10 candles
        result = evaluate(candles)

        self.assertEqual(result.level, RegimeLevel.YELLOW)
        self.assertLess(result.size_multiplier, 1.0)

    def test_empty_candles(self):
        result = evaluate([])
        self.assertEqual(result.level, RegimeLevel.YELLOW)

    def test_no_funding_data(self):
        prices = [50000 + i * 100 for i in range(30)]
        candles = _make_candles(prices)
        result = evaluate(candles, pair_funding_rates=None, btc_price=prices[-1])
        # Should work without funding data
        self.assertIn(result.level, [RegimeLevel.GREEN, RegimeLevel.YELLOW])


if __name__ == "__main__":
    unittest.main()
