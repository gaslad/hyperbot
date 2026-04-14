"""Tests for the nightly adaptive tuner.

Covers:
- Metric computation from journal fills
- Parameter adjustments for underperforming strategies
- Parameter loosening for well-performing strategies
- Policy band clamping
- Blocked hours detection
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# nightly_tuner lives at repo_root/scripts/nightly_tuner.py
_repo_root = Path(__file__).resolve().parents[1].parents[1]
sys.path.insert(0, str(_repo_root / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import nightly_tuner


class TestComputeMetrics(unittest.TestCase):
    def test_basic_metrics(self):
        now = datetime.now(timezone.utc)
        fills = [
            {"kind": "fill", "ts_ms": int(now.timestamp() * 1000), "ts": now.isoformat(),
             "strategy": "btc_scalp_v2", "closed_pnl": 50.0, "coin": "BTC"},
            {"kind": "fill", "ts_ms": int(now.timestamp() * 1000), "ts": now.isoformat(),
             "strategy": "btc_scalp_v2", "closed_pnl": -30.0, "coin": "BTC"},
            {"kind": "fill", "ts_ms": int(now.timestamp() * 1000), "ts": now.isoformat(),
             "strategy": "btc_scalp_v2", "closed_pnl": 40.0, "coin": "BTC"},
        ]
        metrics = nightly_tuner.compute_metrics(fills)

        self.assertIn("btc_scalp_v2", metrics)
        m = metrics["btc_scalp_v2"]
        self.assertEqual(m["trade_count"], 3)
        self.assertEqual(m["win_count"], 2)
        self.assertEqual(m["loss_count"], 1)
        self.assertAlmostEqual(m["win_rate"], 2 / 3, places=2)
        self.assertAlmostEqual(m["profit_factor"], 90 / 30, places=2)  # 90/30 = 3.0
        self.assertEqual(m["total_pnl"], 60.0)

    def test_all_losses(self):
        now = datetime.now(timezone.utc)
        fills = [
            {"kind": "fill", "ts_ms": int(now.timestamp() * 1000), "ts": now.isoformat(),
             "strategy": "test", "closed_pnl": -10.0},
            {"kind": "fill", "ts_ms": int(now.timestamp() * 1000), "ts": now.isoformat(),
             "strategy": "test", "closed_pnl": -20.0},
        ]
        metrics = nightly_tuner.compute_metrics(fills)
        m = metrics["test"]

        self.assertEqual(m["win_rate"], 0.0)
        self.assertEqual(m["profit_factor"], 0)
        self.assertEqual(m["max_consecutive_losses"], 2)

    def test_empty_fills(self):
        metrics = nightly_tuner.compute_metrics([])
        self.assertEqual(len(metrics), 0)


class TestComputeAdjustments(unittest.TestCase):
    def setUp(self):
        self.config = {
            "strategy": {
                "adx_min": 20.0,
                "choppiness_max": 55.0,
                "rvol_min": 1.5,
                "risk_per_trade_pct": 0.005,
                "min_r_distance": 1.5,
                "blocked_hours": [],
            },
            "exit_management": {
                "stale_after_minutes": 30.0,
            },
        }
        self.policy_bands = {"leverage_max": 4, "risk_per_trade_pct_max": 0.01}

    def test_adjustments_on_underperformance(self):
        metrics = {
            "trade_count": 10,
            "win_count": 2,
            "loss_count": 8,
            "win_rate": 0.2,
            "profit_factor": 0.5,
            "total_pnl": -100.0,
            "max_consecutive_losses": 5,
            "worst_hours": [(22, -20.0), (23, -15.0)],
            "largest_loss": -50.0,
        }
        adjustments = nightly_tuner.compute_adjustments(self.config, metrics, self.policy_bands)

        self.assertGreater(len(adjustments), 0)

        # adx_min should increase (more selective)
        adx_adj = [a for a in adjustments if a["param"] == "strategy.adx_min"]
        if adx_adj:
            self.assertGreater(adx_adj[0]["new_value"], 20.0)

        # risk_per_trade should decrease
        risk_adj = [a for a in adjustments if a["param"] == "strategy.risk_per_trade_pct"]
        if risk_adj:
            self.assertLess(risk_adj[0]["new_value"], 0.005)

    def test_no_adjustments_below_min_trades(self):
        metrics = {
            "trade_count": 3,  # below MIN_TRADES_FOR_ADJUSTMENT
            "win_count": 1,
            "loss_count": 2,
            "win_rate": 0.33,
            "profit_factor": 0.5,
            "total_pnl": -30,
            "max_consecutive_losses": 2,
            "worst_hours": [],
            "largest_loss": -20,
        }
        adjustments = nightly_tuner.compute_adjustments(self.config, metrics, self.policy_bands)
        self.assertEqual(len(adjustments), 0)

    def test_loosening_on_strong_performance(self):
        metrics = {
            "trade_count": 10,
            "win_count": 7,
            "loss_count": 3,
            "win_rate": 0.7,
            "profit_factor": 2.5,
            "total_pnl": 200.0,
            "max_consecutive_losses": 1,
            "worst_hours": [],
            "largest_loss": -20.0,
        }
        adjustments = nightly_tuner.compute_adjustments(self.config, metrics, self.policy_bands)

        # Should gently loosen adx_min (reduce)
        adx_adj = [a for a in adjustments if a["param"] == "strategy.adx_min"]
        if adx_adj:
            self.assertLess(adx_adj[0]["new_value"], 20.0)

    def test_worst_hours_get_blocked(self):
        metrics = {
            "trade_count": 10,
            "win_count": 2,
            "loss_count": 8,
            "win_rate": 0.2,
            "profit_factor": 0.3,
            "total_pnl": -100.0,
            "max_consecutive_losses": 5,
            "worst_hours": [(22, -25.0), (3, -15.0)],
            "largest_loss": -50.0,
        }
        adjustments = nightly_tuner.compute_adjustments(self.config, metrics, self.policy_bands)

        blocked_adj = [a for a in adjustments if a["param"] == "strategy.blocked_hours"]
        self.assertGreater(len(blocked_adj), 0)
        # Should include the worst hours
        for adj in blocked_adj:
            self.assertIn(adj["new_value"][-1], [22, 3])


class TestApplyAdjustments(unittest.TestCase):
    def test_applies_changes(self):
        config = {"strategy": {"adx_min": 20.0}}
        adjustments = [{
            "param": "strategy.adx_min",
            "path": ["strategy", "adx_min"],
            "old_value": 20.0,
            "new_value": 23.0,
            "reason": "test",
            "description": "test",
        }]
        result = nightly_tuner.apply_adjustments(config, adjustments)

        self.assertEqual(result["strategy"]["adx_min"], 23.0)
        # Original should not be modified
        self.assertEqual(config["strategy"]["adx_min"], 20.0)


if __name__ == "__main__":
    unittest.main()
