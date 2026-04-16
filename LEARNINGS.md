# LEARNINGS.md

Durable findings from hyperbot development that matter beyond this session.

## Hyperliquid Requires 5 Significant Figures for Prices (2026-04-16)

**Finding**: `round_price()` rounding to 8 decimal places does NOT satisfy Hyperliquid's price requirements. The exchange requires ≤5 significant figures, and rejects orders silently with "Order has invalid price."

**The Bug**: Market orders add 0.5% slippage to the mid price. SOL at $83.81 → $84.22905 (7 sig figs). The SDK's `float_to_wire()` doesn't enforce sig fig limits — it just formats and sends. The exchange API rejects it. This caused **226 rejections overnight** with zero fills on attempted orders.

**Why It Was Hard to Find**: The previous session (2026-04-15) changed `round_price()` from a broken 5-sig-fig algorithm to "safe 8 decimal" to fix double-rounding in position_manager. The double-rounding fix was correct, but switching to 8 decimals introduced a new failure mode: prices with >5 sig figs. The error message "Order has invalid price" doesn't mention significant figures.

**The Fix**: `round_price()` now uses `min(5 - 1 - floor(log10(abs(price))), 8)` to compute the exact decimal count for 5 significant figures, capped at 8 for SDK compatibility.

**Action**: When working with Hyperliquid order prices:
1. Always enforce ≤5 significant figures, not just decimal places
2. Test with slippage-adjusted prices, not just raw mid prices
3. The SDK's `float_to_wire()` is NOT a safety net — it only checks round-trip precision, not sig fig limits
4. Verify with: `len(str(price).replace('.','').lstrip('0')) <= 5`

---

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
