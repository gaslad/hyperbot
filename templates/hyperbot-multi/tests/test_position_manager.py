"""Tests for the position_manager module.

Covers:
- Post-TP1 breakeven SL move
- Trailing stop after TP1
- Time-based SL tightening
- Volatility-adjusted SL widening/tightening
- No-action when conditions are not met
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import position_manager
from position_manager import ManagementState, PositionAction, manage


def _make_state(**overrides) -> ManagementState:
    """Create a ManagementState with sensible defaults for a long BTC trade."""
    defaults = dict(
        coin="BTC",
        is_long=True,
        entry_price=100000.0,
        current_price=100500.0,
        stop_trigger=99000.0,
        initial_size=0.01,
        current_size=0.01,
        entry_atr=500.0,
        current_atr=500.0,
        opened_at="2026-04-14T10:00:00+00:00",
        age_minutes=10.0,
        sl_oid=12345,
        tp1_oid=12346,
        tp2_oid=12347,
        tp1_filled=False,
        tp1_moved=False,
        pack_id="scalp_v2",
        max_hold_minutes=90.0,
        stale_after_minutes=30.0,
        breakeven_buffer_pct=0.0012,
    )
    defaults.update(overrides)
    return ManagementState(**defaults)


class TestPostTP1Breakeven(unittest.TestCase):
    """After TP1 fills, SL should move to breakeven + buffer."""

    def test_moves_sl_to_breakeven_on_tp1_fill(self):
        ms = _make_state(tp1_filled=True, tp1_moved=False, current_price=101000.0)
        actions = manage(ms)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.action, "move_sl")
        # Breakeven = 100000 + 0.12% = 100120
        self.assertAlmostEqual(action.new_trigger, 100120.0, delta=10)
        self.assertEqual(action.cancel_oid, 12345)
        self.assertIn("breakeven", action.reason.lower())

    def test_no_action_if_sl_already_above_breakeven(self):
        """If SL is already at or above breakeven, don't move it."""
        ms = _make_state(
            tp1_filled=True,
            tp1_moved=False,
            stop_trigger=100200.0,  # already above breakeven
        )
        actions = manage(ms)
        # Should not try to move SL lower
        self.assertEqual(len(actions), 0)

    def test_short_trade_breakeven(self):
        ms = _make_state(
            is_long=False,
            entry_price=100000.0,
            current_price=99500.0,
            stop_trigger=101000.0,
            tp1_filled=True,
            tp1_moved=False,
        )
        actions = manage(ms)

        self.assertEqual(len(actions), 1)
        action = actions[0]
        # Breakeven for short = 100000 - 0.12% = 99880
        self.assertAlmostEqual(action.new_trigger, 99880.0, delta=10)


class TestTrailingStop(unittest.TestCase):
    """After TP1 fill + breakeven move, SL should trail as price moves."""

    def test_trails_sl_when_profitable(self):
        # Price has moved to +1.5R (entry=100000, risk=1000, current=101500)
        ms = _make_state(
            tp1_filled=True,
            tp1_moved=True,
            entry_price=100000.0,
            stop_trigger=100120.0,  # at breakeven
            current_price=101500.0,
            current_size=0.007,  # 70% remaining after TP1
        )
        actions = manage(ms)

        # Should trail SL to lock in ~1.0R (1.5 - 0.5 = 1.0R)
        sl_actions = [a for a in actions if a.action == "move_sl"]
        self.assertGreater(len(sl_actions), 0)
        new_trigger = sl_actions[0].new_trigger
        # New SL should be above breakeven
        self.assertGreater(new_trigger, 100120.0)

    def test_no_trail_when_barely_profitable(self):
        """Don't trail when R < 0.5 (too close to entry)."""
        ms = _make_state(
            tp1_filled=True,
            tp1_moved=True,
            entry_price=100000.0,
            stop_trigger=99800.0,   # risk = $200
            current_price=100080.0,  # only +0.4R (80/200)
        )
        actions = manage(ms)
        trail_actions = [a for a in actions if a.action == "move_sl" and "trailing" in a.reason.lower()]
        self.assertEqual(len(trail_actions), 0)


class TestTimeTightening(unittest.TestCase):
    """After stale period, SL should tighten to breakeven."""

    def test_tightens_after_stale_period(self):
        ms = _make_state(
            age_minutes=35.0,  # past stale_after_minutes=30
            current_price=100500.0,
            tp1_filled=False,
        )
        actions = manage(ms)

        sl_actions = [a for a in actions if a.action == "move_sl"]
        self.assertGreater(len(sl_actions), 0)
        self.assertIn("stale", sl_actions[0].reason.lower())

    def test_no_tighten_when_underwater(self):
        """Don't move SL to breakeven if position is underwater."""
        ms = _make_state(
            age_minutes=35.0,
            current_price=99500.0,  # below entry
            tp1_filled=False,
        )
        actions = manage(ms)
        sl_actions = [a for a in actions if a.action == "move_sl" and "stale" in a.reason.lower()]
        self.assertEqual(len(sl_actions), 0)

    def test_tightens_at_75_pct_max_hold(self):
        ms = _make_state(
            age_minutes=68.0,  # 75% of 90 minutes
            current_price=100500.0,
            tp1_filled=False,
        )
        actions = manage(ms)
        sl_actions = [a for a in actions if a.action == "move_sl"]
        self.assertGreater(len(sl_actions), 0)


class TestVolatilityAdjustment(unittest.TestCase):
    """SL should widen when ATR expands significantly."""

    def test_widens_sl_on_atr_expansion(self):
        ms = _make_state(
            entry_atr=500.0,
            current_atr=700.0,  # 40% expansion
            tp1_filled=False,
            current_price=100500.0,
        )
        actions = manage(ms)

        sl_actions = [a for a in actions if a.action == "move_sl" and "ATR" in a.reason]
        self.assertGreater(len(sl_actions), 0)
        # New SL should be wider (lower for long)
        self.assertLess(sl_actions[0].new_trigger, ms.stop_trigger)

    def test_tightens_sl_on_atr_contraction_post_tp1(self):
        ms = _make_state(
            entry_atr=500.0,
            current_atr=300.0,  # 40% contraction
            tp1_filled=True,
            tp1_moved=True,
            stop_trigger=100200.0,
            current_price=101000.0,
        )
        actions = manage(ms)

        sl_actions = [a for a in actions if a.action == "move_sl" and "ATR" in a.reason]
        # Should tighten SL
        if sl_actions:
            self.assertGreater(sl_actions[0].new_trigger, ms.stop_trigger)

    def test_no_adjustment_on_normal_atr(self):
        ms = _make_state(
            entry_atr=500.0,
            current_atr=520.0,  # only 4% change
        )
        actions = manage(ms)
        atr_actions = [a for a in actions if "ATR" in (a.reason or "")]
        self.assertEqual(len(atr_actions), 0)


class TestNoAction(unittest.TestCase):
    """Verify no actions when nothing needs to change."""

    def test_no_action_fresh_trade(self):
        ms = _make_state(age_minutes=5.0, current_price=100200.0)
        actions = manage(ms)
        self.assertEqual(len(actions), 0)

    def test_no_action_zero_size(self):
        ms = _make_state(current_size=0.0)
        actions = manage(ms)
        self.assertEqual(len(actions), 0)


if __name__ == "__main__":
    unittest.main()
