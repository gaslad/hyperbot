from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import trade_journal


class TradeJournalReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "reports" / "weekly").mkdir(parents=True, exist_ok=True)
        (self.root / "hyperbot.workspace.json").write_text(
            json.dumps({"workspace_name": "hyperbot-test"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_record(self, record: dict) -> None:
        journal_path = self.root / "data" / "trade_journal.jsonl"
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_build_weekly_report_writes_markdown(self) -> None:
        now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        week_start_ms = int((now.timestamp() * 1000) - 3 * 24 * 60 * 60 * 1000)
        previous_week_ms = int((now.timestamp() * 1000) - 10 * 24 * 60 * 60 * 1000)

        self._write_record(
            {
                "kind": "fill",
                "ts_ms": week_start_ms,
                "ts": "2026-04-07T12:00:00Z",
                "coin": "BTC",
                "closed_pnl": 12.5,
                "fee": 0.2,
                "price": 100.0,
                "size": 0.5,
                "fill_key": "hash:abc",
            }
        )
        self._write_record(
            {
                "kind": "fill",
                "ts_ms": week_start_ms + 1000,
                "ts": "2026-04-07T12:00:01Z",
                "coin": "SOL",
                "closed_pnl": -4.0,
                "fee": 0.1,
                "price": 50.0,
                "size": 1.0,
                "fill_key": "hash:def",
            }
        )
        self._write_record(
            {
                "kind": "trade_log",
                "ts_ms": week_start_ms + 2000,
                "ts": "2026-04-07T12:00:02Z",
                "action": "SKIP",
                "strategy": "scalp_v2",
                "size": 0,
                "price": 50.0,
                "note": "confidence 0.4 below live threshold",
                "bucket": "filtering",
            }
        )
        self._write_record(
            {
                "kind": "trade_log",
                "ts_ms": week_start_ms + 2500,
                "ts": "2026-04-07T12:00:02.500000Z",
                "action": "FILLED",
                "strategy": "scalp_v2",
                "order_id": 777,
                "size": 1.5,
                "price": 101.0,
                "note": "oid=777",
                "bucket": "execution",
            }
        )
        self._write_record(
            {
                "kind": "fill",
                "ts_ms": previous_week_ms,
                "ts": "2026-03-31T12:00:00Z",
                "coin": "BTC",
                "closed_pnl": 2.0,
                "fee": 0.05,
                "price": 90.0,
                "size": 0.25,
                "fill_key": "hash:ghi",
            }
        )
        self._write_record(
            {
                "kind": "fill",
                "ts_ms": week_start_ms + 3000,
                "ts": "2026-04-07T12:00:03Z",
                "coin": "ETH",
                "closed_pnl": 6.0,
                "fee": 0.12,
                "price": 101.0,
                "size": 1.5,
                "fill_key": "hash:jkl",
                "order_id": 777,
                "strategy": "scalp_v2",
                "strategy_source": "order_id",
            }
        )

        journal = trade_journal.TradeJournal(self.root)
        report = journal.build_weekly_report(now=now)

        self.assertIsNotNone(report)
        assert report is not None
        report_path = report["report_path"]
        self.assertTrue(report_path.exists())
        self.assertTrue(report["latest_simplex_path"].exists())

        markdown = report_path.read_text(encoding="utf-8")
        self.assertIn("Weekly Trading Report", markdown)
        self.assertIn("Closed fills: 3 (2W / 1L / 0 flat)", markdown)
        self.assertIn("What Hyperbot Learned", markdown)
        self.assertIn("Suggestions", markdown)
        self.assertIn("What Changed This Week", markdown)
        self.assertIn("Strategy Review", markdown)
        self.assertIn("Fill Attribution", markdown)
        self.assertIn("Per-Coin Actions", markdown)

        simplex_text = report["latest_simplex_path"].read_text(encoding="utf-8")
        self.assertIn("weekly report", simplex_text)
        self.assertIn("Closed PnL: +14.5000 USDC", simplex_text)


if __name__ == "__main__":
    unittest.main()
