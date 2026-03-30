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


if __name__ == "__main__":
    unittest.main()
