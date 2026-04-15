# LEARNINGS.md

Durable findings from hyperbot development that matter beyond this session.

## Float Precision in Crypto Exchange APIs (2026-04-15)

**Finding**: Double-rounding of prices in position management → exchange API rejection.

**The Bug**: `position_manager.py` rounded prices with `_sig5()` (5-significant-figure algorithm), then `hl_client.py` rounded them **again** with `round_price()`. This cascading precision loss caused Hyperliquid SDK to reject prices with "float_to_wire causes rounding" errors.

**The Fix**: Removed rounding from position_manager. Let the exchange client handle all precision once, using 8 decimal places (safe for Hyperliquid's wire serialization).

**Why It Matters**: Exchange APIs have strict serialization requirements. Crypto prices especially need careful handling because:
- Small-value coins (ZEC @ $350, NEAR @ $1.36) have tighter precision windows than large-caps
- Double-rounding in a calculation pipeline is invisible but deadly
- Significance-figure algorithms can destroy precision for mid-range values (100–1000 range)

**Action**: When building position management logic that feeds exchange APIs:
1. Calculate prices without rounding
2. Round **exactly once** at the exchange client boundary
3. Use fixed-decimal precision (8 decimals for most exchanges) not significance figures
4. Consider `decimal.Decimal` instead of float for financial math to avoid accumulation errors

---

## SL Management Cascade Failure Pattern (2026-04-15)

**Finding**: Repeated SL replacement failures cascade into rapid position opens/closes and money loss.

**The Pattern**: When an SL order fails:
1. Dashboard logs failure but keeps old SL (not cancelled)
2. Next cycle (15s later) position_manager regenerates the same SL move action
3. Dashboard tries again → same failure
4. Repeat until position forced closed or SL succeeds by luck

This happened 50+ times in 7 minutes, visible in trade_journal as "SL_MOVE" → "MANAGE_FAIL" → "SL_MOVE" chains.

**Why**: No cooldown between retry attempts. Position manager has no memory of recent failures, so it always suggests the same action.

**The Fix**: Track last-failure timestamp in managed position state. Skip SL replacement attempts for 10 seconds after a failure. Clear the timestamp on success.

**Action**: For any system that retries flaky operations:
- Add failure tracking (timestamp, retry count)
- Implement exponential backoff or cooldown periods
- Clear retry state on success
- Log the decision to skip (so debugging doesn't miss it)
