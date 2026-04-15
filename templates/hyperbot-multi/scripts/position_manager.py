"""Position Manager — active management of open positions.

Handles:
- Trailing stop-loss after TP1 fills (move SL to breakeven + buffer)
- Time-based SL tightening (after stale period, tighten to breakeven)
- Volatility-adjusted SL/TP (if ATR drifts significantly from entry ATR)
- Breakeven enforcement once position has been open long enough

This module is stateless — call manage() each cycle with current state.
The caller (trading loop) is responsible for executing returned actions
via hl_client.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Action types returned to the trading loop
# ---------------------------------------------------------------------------

@dataclass
class PositionAction:
    """An action the trading loop should execute via hl_client."""
    action: str              # "move_sl", "move_tp", "close", "noop"
    coin: str
    reason: str
    # For move_sl / move_tp
    new_trigger: float | None = None
    new_limit: float | None = None
    cancel_oid: int | None = None  # order to cancel before placing new one
    # For close
    size: float | None = None
    is_buy: bool | None = None     # side for the close order


@dataclass
class ManagementState:
    """Snapshot of everything the manager needs to evaluate one position."""
    coin: str
    is_long: bool
    entry_price: float
    current_price: float
    stop_trigger: float
    initial_size: float
    current_size: float
    # ATR at entry vs now
    entry_atr: float
    current_atr: float
    # Time
    opened_at: str              # ISO timestamp
    age_minutes: float
    # TP/SL order IDs
    sl_oid: int | None = None
    tp1_oid: int | None = None
    tp2_oid: int | None = None
    # Partial fill state
    tp1_filled: bool = False
    tp1_moved: bool = False     # whether we already moved SL to breakeven
    # Strategy config
    pack_id: str = ""
    # Exit management config
    max_hold_minutes: float = 90.0
    stale_after_minutes: float = 30.0
    breakeven_buffer_pct: float = 0.0012  # 0.12% buffer above/below entry
    # Trailing stop config (post-TP1)
    trail_gap_r: float = 0.5              # trail this many R behind current price
    trail_min_lock_r: float = 0.3         # minimum R profit to lock in
    trail_activation_r: float = 0.5       # start trailing when profit exceeds this R
    trail_ratchet_threshold_r: float = 0.1  # min improvement to move SL
    # Original stop at entry (used for ATR expansion calc so it doesn't compound)
    initial_stop: float = 0.0


def _risk(entry: float, stop: float) -> float:
    return abs(entry - stop) if entry and stop else 0.0


def _current_r(ms: ManagementState) -> float:
    risk = _risk(ms.entry_price, ms.stop_trigger)
    if risk <= 0:
        return 0.0
    if ms.is_long:
        return (ms.current_price - ms.entry_price) / risk
    return (ms.entry_price - ms.current_price) / risk


def _breakeven_price(ms: ManagementState) -> float:
    """Entry price + small buffer to cover fees."""
    buf = ms.entry_price * ms.breakeven_buffer_pct
    return ms.entry_price + buf if ms.is_long else ms.entry_price - buf


def _sig5(price: float) -> float:
    """Return price as-is for rounding to be done by hl_client.round_price().

    position_manager should NOT round — let the exchange client handle precision.
    This avoids double-rounding when hl_client.round_price() is called again.
    """
    return price


# ---------------------------------------------------------------------------
# Core management logic
# ---------------------------------------------------------------------------

def manage(ms: ManagementState) -> list[PositionAction]:
    """Evaluate one managed position and return a list of actions.

    Actions are ordered by priority. The caller should execute them
    sequentially (each may depend on the previous cancelling an order).

    Returns an empty list if no action is needed.
    """
    actions: list[PositionAction] = []
    r = _current_r(ms)
    risk = _risk(ms.entry_price, ms.stop_trigger)

    if ms.current_size <= 0 or risk <= 0:
        return actions

    # ---------------------------------------------------------------
    # 1. Post-TP1: trail SL to breakeven + buffer
    # ---------------------------------------------------------------
    if ms.tp1_filled and not ms.tp1_moved:
        be_price = _breakeven_price(ms)
        should_move = (
            (ms.is_long and be_price > ms.stop_trigger) or
            (not ms.is_long and be_price < ms.stop_trigger)
        )
        if should_move:
            # SL limit sits slightly worse than trigger for fill safety
            if ms.is_long:
                new_limit = _sig5(be_price * (1 - 0.003))
            else:
                new_limit = _sig5(be_price * (1 + 0.003))

            actions.append(PositionAction(
                action="move_sl",
                coin=ms.coin,
                reason=f"TP1 filled — moving SL to breakeven + fees (${be_price:.2f})",
                new_trigger=_sig5(be_price),
                new_limit=new_limit,
                cancel_oid=ms.sl_oid,
            ))
            return actions  # execute this before anything else

    # ---------------------------------------------------------------
    # 2. Trailing stop after TP1 — ratchet SL higher on each new high
    # ---------------------------------------------------------------
    if ms.tp1_filled and ms.tp1_moved and r > ms.trail_activation_r:
        # Trail: move SL to lock in at least trail_min_lock_r profit
        trail_level_r = max(ms.trail_min_lock_r, r - ms.trail_gap_r)
        if ms.is_long:
            trail_price = ms.entry_price + trail_level_r * risk
        else:
            trail_price = ms.entry_price - trail_level_r * risk

        # Only move SL if the new level is tighter (more protective)
        should_move = (
            (ms.is_long and trail_price > ms.stop_trigger + risk * ms.trail_ratchet_threshold_r) or
            (not ms.is_long and trail_price < ms.stop_trigger - risk * ms.trail_ratchet_threshold_r)
        )
        if should_move:
            if ms.is_long:
                new_limit = _sig5(trail_price * (1 - 0.003))
            else:
                new_limit = _sig5(trail_price * (1 + 0.003))

            actions.append(PositionAction(
                action="move_sl",
                coin=ms.coin,
                reason=f"Trailing SL to lock in {trail_level_r:.1f}R profit (${trail_price:.2f})",
                new_trigger=_sig5(trail_price),
                new_limit=new_limit,
                cancel_oid=ms.sl_oid,
            ))

    # ---------------------------------------------------------------
    # 3. Time-based tightening — move SL to breakeven after stale period
    # ---------------------------------------------------------------
    if not ms.tp1_filled and ms.age_minutes >= ms.stale_after_minutes and r > 0:
        be_price = _breakeven_price(ms)
        should_move = (
            (ms.is_long and be_price > ms.stop_trigger) or
            (not ms.is_long and be_price < ms.stop_trigger)
        )
        if should_move:
            if ms.is_long:
                new_limit = _sig5(be_price * (1 - 0.003))
            else:
                new_limit = _sig5(be_price * (1 + 0.003))

            actions.append(PositionAction(
                action="move_sl",
                coin=ms.coin,
                reason=f"Stale trade ({ms.age_minutes:.0f}m) — tightening SL to breakeven",
                new_trigger=_sig5(be_price),
                new_limit=new_limit,
                cancel_oid=ms.sl_oid,
            ))

    # ---------------------------------------------------------------
    # 4. At 75% of max hold time: move SL to breakeven regardless
    # ---------------------------------------------------------------
    if not ms.tp1_filled and ms.age_minutes >= ms.max_hold_minutes * 0.75 and r > 0:
        be_price = _breakeven_price(ms)
        should_move = (
            (ms.is_long and be_price > ms.stop_trigger) or
            (not ms.is_long and be_price < ms.stop_trigger)
        )
        if should_move and not actions:  # don't duplicate if #3 already covered
            if ms.is_long:
                new_limit = _sig5(be_price * (1 - 0.003))
            else:
                new_limit = _sig5(be_price * (1 + 0.003))

            actions.append(PositionAction(
                action="move_sl",
                coin=ms.coin,
                reason=f"Approaching max hold ({ms.age_minutes:.0f}/{ms.max_hold_minutes:.0f}m) — SL to breakeven",
                new_trigger=_sig5(be_price),
                new_limit=new_limit,
                cancel_oid=ms.sl_oid,
            ))

    # ---------------------------------------------------------------
    # 5. Volatility drift — if current ATR is 30%+ different from entry
    #    Skip if stale-trade tightening already queued (avoids oscillation).
    #    Use initial_stop (not current stop_trigger) so risk doesn't compound.
    # ---------------------------------------------------------------
    base_stop = ms.initial_stop if ms.initial_stop else ms.stop_trigger
    if ms.entry_atr > 0 and ms.current_atr > 0 and not actions:
        atr_ratio = ms.current_atr / ms.entry_atr
        original_risk = _risk(ms.entry_price, base_stop)
        if atr_ratio > 1.3 and not ms.tp1_filled and original_risk > 0:
            # Volatility expanded: widen SL by the same ratio to avoid
            # getting stopped on normal noise
            if ms.is_long:
                new_sl = ms.entry_price - original_risk * atr_ratio
                # Don't widen past 2x original risk
                min_sl = ms.entry_price - original_risk * 2.0
                new_sl = max(new_sl, min_sl)
                # Only act if this would actually widen the stop
                if new_sl < ms.stop_trigger:
                    actions.append(PositionAction(
                        action="move_sl",
                        coin=ms.coin,
                        reason=f"ATR expanded {atr_ratio:.1f}x — widening SL to ${new_sl:.2f} to avoid noise stop",
                        new_trigger=_sig5(new_sl),
                        new_limit=_sig5(new_sl * (1 - 0.003)),
                        cancel_oid=ms.sl_oid,
                    ))
            else:
                new_sl = ms.entry_price + original_risk * atr_ratio
                max_sl = ms.entry_price + original_risk * 2.0
                new_sl = min(new_sl, max_sl)
                if new_sl > ms.stop_trigger:
                    actions.append(PositionAction(
                        action="move_sl",
                        coin=ms.coin,
                        reason=f"ATR expanded {atr_ratio:.1f}x — widening SL to ${new_sl:.2f} to avoid noise stop",
                        new_trigger=_sig5(new_sl),
                        new_limit=_sig5(new_sl * (1 + 0.003)),
                        cancel_oid=ms.sl_oid,
                    ))

        elif atr_ratio < 0.7 and ms.tp1_filled:
            # Volatility contracted after TP1: tighten SL to capture more
            tighter_risk = _risk(ms.entry_price, ms.stop_trigger) * atr_ratio
            if ms.is_long:
                new_sl = ms.current_price - tighter_risk
                if new_sl > ms.stop_trigger:
                    actions.append(PositionAction(
                        action="move_sl",
                        coin=ms.coin,
                        reason=f"ATR contracted {atr_ratio:.1f}x post-TP1 — tightening SL to ${new_sl:.2f}",
                        new_trigger=_sig5(new_sl),
                        new_limit=_sig5(new_sl * (1 - 0.003)),
                        cancel_oid=ms.sl_oid,
                    ))
            else:
                new_sl = ms.current_price + tighter_risk
                if new_sl < ms.stop_trigger:
                    actions.append(PositionAction(
                        action="move_sl",
                        coin=ms.coin,
                        reason=f"ATR contracted {atr_ratio:.1f}x post-TP1 — tightening SL to ${new_sl:.2f}",
                        new_trigger=_sig5(new_sl),
                        new_limit=_sig5(new_sl * (1 + 0.003)),
                        cancel_oid=ms.sl_oid,
                    ))

    return actions
