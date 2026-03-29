from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hl_client


class RoundFunctionsTests(unittest.TestCase):
    def test_round_size_various_decimals(self) -> None:
        cases = [
            (1.6, 0, 2),
            (1.26, 1, 1.3),
            (1.234, 2, 1.23),
            (1.234567, 5, 1.23457),
        ]
        for size, decimals, expected in cases:
            with self.subTest(size=size, decimals=decimals):
                self.assertEqual(hl_client.round_size(size, decimals), expected)

    def test_round_price_uses_five_significant_figures(self) -> None:
        cases = [
            (12345.6, 12346.0),
            (1234.56, 1234.6),
            (12.3456, 12.346),
            (0.0123456, 0.012346),
        ]
        for price, expected in cases:
            with self.subTest(price=price):
                self.assertEqual(hl_client.round_price(price, "SOL"), expected)

    @mock.patch("hl_client.get_meta")
    def test_get_asset_info_returns_matching_coin(self, mock_get_meta: mock.Mock) -> None:
        mock_get_meta.return_value = {
            "universe": [
                {"name": "BTC", "szDecimals": 3},
                {"name": "SOL", "szDecimals": 2},
            ]
        }
        self.assertEqual(hl_client.get_asset_info("SOL"), {"name": "SOL", "szDecimals": 2})
        self.assertIsNone(hl_client.get_asset_info("ETH"))


class PlaceOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exchange_cls = self._build_exchange_class()
        self.module_patches = self._patch_sdk_modules(self.exchange_cls)
        self.module_patches.start()
        self.addCleanup(self.module_patches.stop)

    @staticmethod
    def _build_exchange_class():
        class FakeExchange:
            response: dict = {"status": "ok", "response": {"data": {"statuses": []}}}

            def __init__(self, wallet, base_url, account_address=None) -> None:
                self.wallet = wallet
                self.base_url = base_url
                self.account_address = account_address

            def order(self, coin, is_buy, size, price, order_spec, reduce_only=False):
                return self.__class__.response

        return FakeExchange

    def _patch_sdk_modules(self, exchange_cls):
        hyperliquid_mod = types.ModuleType("hyperliquid")
        hyperliquid_exchange_mod = types.ModuleType("hyperliquid.exchange")
        hyperliquid_exchange_mod.Exchange = exchange_cls
        hyperliquid_mod.exchange = hyperliquid_exchange_mod

        eth_account_mod = types.ModuleType("eth_account")

        class FakeAccount:
            @staticmethod
            def from_key(key):
                return types.SimpleNamespace(address="0xagent")

        eth_account_mod.Account = FakeAccount

        return mock.patch.dict(
            sys.modules,
            {
                "hyperliquid": hyperliquid_mod,
                "hyperliquid.exchange": hyperliquid_exchange_mod,
                "eth_account": eth_account_mod,
            },
        )

    def _call_place_order(self, response: dict) -> hl_client.OrderResult:
        self.exchange_cls.response = response
        with mock.patch("hl_client.get_credentials", return_value={"master_address": "0xmaster", "agent_private_key": "0xkey"}), \
             mock.patch("hl_client.get_asset_info", return_value={"szDecimals": 2, "maxLeverage": 5}), \
             mock.patch("hl_client.get_mid_price", return_value=100.0):
            return hl_client.place_order("SOL", True, 1.234, order_type="market")

    def test_place_order_success_parses_filled_oid(self) -> None:
        result = self._call_place_order(
            {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 12345}}]}}}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.order_id, 12345)
        self.assertIsNone(result.error)

    def test_place_order_collects_status_errors(self) -> None:
        result = self._call_place_order(
            {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"error": "size too small"},
                            "WouldCrossMakerOrder",
                        ]
                    }
                },
            }
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "size too small; WouldCrossMakerOrder")
        self.assertIsNone(result.order_id)

    def test_place_order_handles_missing_oid_in_ok_response(self) -> None:
        result = self._call_place_order({"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}})
        self.assertFalse(result.ok)
        self.assertIsNone(result.order_id)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
