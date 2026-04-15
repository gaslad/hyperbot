from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import signals


def _fake_detector(config: dict, candles_1d: list[dict], candles_4h: list[dict], current_price: float) -> signals.Signal:
    return signals.Signal(
        direction=signals.Direction.BUY,
        strategy_id=config["strategy_id"],
        pack_id=config["pack_id"],
        confidence=0.8,
        price=current_price,
        reasons=["test"],
        entry_price=current_price,
        stop_loss=current_price * 0.95,
        take_profit=current_price * 1.05,
    )


class DetectAllSignalsCoinFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self._write_config("btc_trend_pullback", "BTC", "test_pack")
        self._write_config("btc_liquidity_sweep", "BTC", "test_pack")
        self._write_config("eth_trend_pullback", "ETH", "test_pack")
        self._write_config("eth_market_alias", "ETH", "test_pack", strategy_id="alias_eth_fallback")
        self._write_config("sol_trend_pullback", "SOL", "test_pack")
        self._write_config("sol_liquidity_sweep", "SOL", "test_pack")
        self._write_file("README.md", "ignored")

        self.config_patch = mock.patch.object(signals, "CONFIG_DIR", self.config_dir)
        self.detectors_patch = mock.patch.dict(signals.DETECTORS, {"test_pack": _fake_detector}, clear=True)
        self.config_patch.start()
        self.detectors_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.detectors_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

        self.candles_1d = [{"o": 100, "h": 110, "l": 95, "c": 105}]
        self.candles_4h = [{"o": 100, "h": 106, "l": 99, "c": 104}]

    def _write_file(self, name: str, contents: str) -> None:
        (self.config_dir / name).write_text(contents, encoding="utf-8")

    def _write_config(self, stem: str, coin: str, pack_id: str, strategy_id: str | None = None) -> None:
        payload = {
            "strategy_id": strategy_id or stem,
            "pack_id": pack_id,
            "market": {"coin": coin},
        }
        (self.config_dir / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_no_coin_filter_returns_all_signals(self) -> None:
        result = signals.detect_all_signals(self.candles_1d, self.candles_4h, 105.0)
        self.assertEqual(len(result), 6)

    def test_btc_coin_filter_returns_only_btc_signals(self) -> None:
        result = signals.detect_all_signals(self.candles_1d, self.candles_4h, 105.0, coin="BTC")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(sig.strategy_id.startswith("btc_") for sig in result))

    def test_eth_coin_filter_uses_market_coin_fallback(self) -> None:
        result = signals.detect_all_signals(self.candles_1d, self.candles_4h, 105.0, coin="ETH")
        self.assertEqual(len(result), 2)
        strategy_ids = {sig.strategy_id for sig in result}
        self.assertEqual(strategy_ids, {"eth_trend_pullback", "alias_eth_fallback"})

    def test_unknown_coin_filter_returns_zero_signals(self) -> None:
        result = signals.detect_all_signals(self.candles_1d, self.candles_4h, 105.0, coin="UNKNOWN")
        self.assertEqual(len(result), 0)


class TrendPullbackRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "strategy_id": "tao_trend_pullback",
            "pack_id": "trend_pullback",
            "entry": {"sma_period": 10, "pullback_zone_pct": 5.0},
            "filters": {"min_pullback_pct": 3.0},
            "risk": {"invalidation_below_sma_pct": 3.0},
        }

    def test_does_not_buy_when_price_is_extended_without_real_pullback(self) -> None:
        candles_1d = [
            {"o": 100, "h": 102, "l": 99, "c": 100},
            {"o": 101, "h": 103, "l": 100, "c": 101},
            {"o": 102, "h": 104, "l": 101, "c": 102},
            {"o": 103, "h": 105, "l": 102, "c": 103},
            {"o": 104, "h": 106, "l": 103, "c": 104},
            {"o": 105, "h": 107, "l": 104, "c": 105},
            {"o": 106, "h": 108, "l": 105, "c": 106},
            {"o": 107, "h": 109, "l": 106, "c": 107},
            {"o": 108, "h": 110, "l": 107, "c": 108},
            {"o": 109, "h": 111, "l": 108, "c": 109},
            {"o": 110, "h": 112, "l": 109, "c": 110},
            {"o": 111, "h": 113, "l": 110, "c": 111},
        ]
        candles_4h = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i} for i in range(60)]

        signal = signals.detect_trend_pullback(self.config, candles_1d, candles_4h, current_price=109.0)

        self.assertEqual(signal.direction, signals.Direction.NONE)
        self.assertTrue(signal.reasons)

    def test_buys_after_pullback_into_zone_with_swing_high_retracement(self) -> None:
        candles_1d = [
            {"o": 100, "h": 101, "l": 99, "c": 100},
            {"o": 101, "h": 102, "l": 100, "c": 101},
            {"o": 102, "h": 103, "l": 101, "c": 102},
            {"o": 103, "h": 104, "l": 102, "c": 103},
            {"o": 104, "h": 105, "l": 103, "c": 104},
            {"o": 105, "h": 106, "l": 104, "c": 105},
            {"o": 106, "h": 107, "l": 105, "c": 106},
            {"o": 107, "h": 112, "l": 106, "c": 107},
            {"o": 106, "h": 108, "l": 104, "c": 106},
            {"o": 104, "h": 106, "l": 103, "c": 104},
            {"o": 105, "h": 107, "l": 104, "c": 105},
            {"o": 106, "h": 108, "l": 105, "c": 106},
        ]
        candles_4h = [{"o": 90 + i * 0.1, "h": 91 + i * 0.1, "l": 89 + i * 0.1, "c": 90 + i * 0.1} for i in range(60)]

        signal = signals.detect_trend_pullback(self.config, candles_1d, candles_4h, current_price=108.0)

        self.assertEqual(signal.direction, signals.Direction.BUY)
        self.assertAlmostEqual(signal.entry_price, 108.0)

    def test_rejects_when_4h_momentum_is_still_falling(self) -> None:
        candles_1d = [
            {"o": 100, "h": 102, "l": 99, "c": 100},
            {"o": 101, "h": 103, "l": 100, "c": 101},
            {"o": 102, "h": 104, "l": 101, "c": 102},
            {"o": 103, "h": 105, "l": 102, "c": 103},
            {"o": 104, "h": 106, "l": 103, "c": 104},
            {"o": 105, "h": 107, "l": 104, "c": 105},
            {"o": 106, "h": 108, "l": 105, "c": 106},
            {"o": 107, "h": 112, "l": 106, "c": 107},
            {"o": 106, "h": 108, "l": 104, "c": 106},
            {"o": 104, "h": 106, "l": 103, "c": 104},
            {"o": 105, "h": 107, "l": 104, "c": 105},
            {"o": 106, "h": 108, "l": 105, "c": 106},
        ]
        candles_4h = [
            {"o": 90 + i * 0.3, "h": 91 + i * 0.3, "l": 89 + i * 0.3, "c": 90 + i * 0.3}
            for i in range(58)
        ]
        candles_4h.extend([
            {"o": 107.0, "h": 108.0, "l": 106.5, "c": 107.0},
            {"o": 106.5, "h": 107.0, "l": 105.0, "c": 105.8},
            {"o": 105.8, "h": 106.0, "l": 103.8, "c": 104.9},
        ])

        signal = signals.detect_trend_pullback(self.config, candles_1d, candles_4h, current_price=108.0)

        self.assertEqual(signal.direction, signals.Direction.NONE)
        self.assertTrue(signal.reasons)


if __name__ == "__main__":
    unittest.main()
