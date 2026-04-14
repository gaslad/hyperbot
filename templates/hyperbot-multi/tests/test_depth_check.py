"""Tests for the hl_client.check_depth() function.

Covers:
- Sufficient depth
- Insufficient depth
- Empty order book
- Edge cases
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hl_client


class TestCheckDepth(unittest.TestCase):

    def _mock_book(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict:
        """Create a mock L2 book response."""
        return {
            "levels": [
                [{"px": str(px), "sz": str(sz)} for px, sz in bids],
                [{"px": str(px), "sz": str(sz)} for px, sz in asks],
            ]
        }

    @mock.patch.object(hl_client, "get_l2_book")
    def test_sufficient_depth(self, mock_book):
        # BTC at $100,000, 0.1% threshold = within $100 of mid
        # Mid = (99990 + 100010) / 2 = 100000
        # Threshold = 100000 * 0.001 = 100
        mock_book.return_value = self._mock_book(
            bids=[
                (99990, 0.5),   # $49,995 depth
                (99950, 0.3),   # $29,985 depth  (within $100)
                (99900, 0.2),   # outside threshold
            ],
            asks=[
                (100010, 0.5),  # $50,005 depth
                (100050, 0.3),  # $30,015 depth  (within $100)
                (100200, 0.5),  # outside threshold
            ],
        )
        result = hl_client.check_depth("BTC", order_size_usd=10000)

        self.assertTrue(result["sufficient"])
        self.assertGreater(result["bid_depth_usd"], 10000)
        self.assertGreater(result["ask_depth_usd"], 10000)
        self.assertGreater(result["depth_ratio"], 2.0)

    @mock.patch.object(hl_client, "get_l2_book")
    def test_insufficient_depth(self, mock_book):
        # Thin book
        mock_book.return_value = self._mock_book(
            bids=[(99990, 0.01)],  # $999.9 depth
            asks=[(100010, 0.01)],  # $1000.1 depth
        )
        result = hl_client.check_depth("BTC", order_size_usd=10000)

        self.assertFalse(result["sufficient"])
        self.assertLess(result["depth_ratio"], 2.0)

    @mock.patch.object(hl_client, "get_l2_book")
    def test_empty_book(self, mock_book):
        mock_book.return_value = {"levels": [[], []]}
        result = hl_client.check_depth("BTC", order_size_usd=10000)

        self.assertFalse(result["sufficient"])
        self.assertEqual(result["bid_depth_usd"], 0.0)
        self.assertEqual(result["ask_depth_usd"], 0.0)

    @mock.patch.object(hl_client, "get_l2_book")
    def test_zero_order_size(self, mock_book):
        mock_book.return_value = self._mock_book(
            bids=[(99990, 0.5)],
            asks=[(100010, 0.5)],
        )
        result = hl_client.check_depth("BTC", order_size_usd=0)
        # Zero order should report 0 ratio
        self.assertEqual(result["depth_ratio"], 0.0)

    @mock.patch.object(hl_client, "get_l2_book")
    def test_custom_depth_pct(self, mock_book):
        # Wider threshold (0.5%)
        mock_book.return_value = self._mock_book(
            bids=[
                (99990, 0.5),
                (99800, 0.3),
                (99500, 0.2),  # $500 from mid, within 0.5%
            ],
            asks=[
                (100010, 0.5),
                (100200, 0.3),
                (100500, 0.2),
            ],
        )
        result = hl_client.check_depth("BTC", order_size_usd=5000, depth_pct=0.005)
        self.assertGreater(result["bid_depth_usd"], 0)


class TestCheckDepthError(unittest.TestCase):

    @mock.patch.object(hl_client, "get_l2_book")
    def test_api_error_returns_insufficient(self, mock_book):
        mock_book.side_effect = Exception("API timeout")
        result = hl_client.check_depth("BTC", order_size_usd=10000)

        self.assertFalse(result["sufficient"])
        self.assertEqual(result["depth_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
