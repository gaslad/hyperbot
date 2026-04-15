# Stop-Loss Oscillation Bug Fix — April 15, 2026

## Problem Summary

Trades were becoming erratic with continuous opens and closes, causing money loss due to:
1. **Stop-loss replacement failures** cascading into rapid retries
2. **Double-rounding precision errors** when prices were sent to Hyperliquid exchange
3. **Uncontrolled SL widening loops** in volatile markets (ZEC, NEAR)

## Root Cause Analysis

### 1. Double-Rounding Bug
- `position_manager.py` rounded prices with `_sig5()` (5 significant figures)
- `hl_client.py` then rounded the same prices **again** with `round_price()`
- This caused precision loss: `332.43636996 → 332.44 → 332.44` (losing detail)
- **Result**: Hyperliquid SDK rejected the imprecise values with "float_to_wire causes rounding" error

### 2. Flawed Significance-Figure Algorithm
Both functions used: `factor = 10 ** (sig_figs - 1 - magnitude)`

For a price like `332.43636996`:
- `log10(332.436...) ≈ 2.52`, floor = 2
- `factor = 10 ** (5 - 1 - 2) = 10^2 = 100`
- Result: `332.43636996 * 100 / 100 = 332.44` (truncated to 2 decimals)

This loses the meaningful precision needed for Hyperliquid's wire format.

### 3. SL Management Cascade
Once an SL replacement failed:
- Position manager re-triggered the same management action every 15 seconds
- Dashboard logged the failure but kept the old SL (not cancelled)
- Next cycle tried again → same failure → infinite retry loop
- **Result**: Rapid "SL_MOVE" → "MANAGE_FAIL" → "SL_MOVE" pattern

Example from logs (2026-04-15 04:13-04:20):
```
[SL_MOVE] ... widening SL to $0.91
[MANAGE_FAIL] SL replace failed: ('float_to_wire causes rounding', -0.119...)
[SL_MOVE] ... widening SL to $0.91
[MANAGE_FAIL] SL replace failed: ('float_to_wire causes rounding', -0.119...)
[CLOSE] ... position forced closed due to cascade
```

## Solutions Implemented

### 1. Remove Double-Rounding (position_manager.py)
**Changed**: `_sig5()` now returns prices **unrounded**
```python
def _sig5(price: float) -> float:
    """Return price as-is for rounding to be done by hl_client.round_price()."""
    return price
```

**Why**: Position manager shouldn't round. Let the exchange client handle all precision.

### 2. Fix round_price() to Use Safe Precision (hl_client.py)
**Changed**: From 5-sig-fig algorithm to **8 decimal places**
```python
def round_price(price: float, coin: str, base_url: str = HL_MAINNET) -> float:
    """Round price to 8 decimal places for Hyperliquid wire serialization."""
    if price <= 0:
        return price
    return round(price, 8)  # Safe for Hyperliquid SDK's float-to-wire conversion
```

**Why**: Hyperliquid SDK uses 8-decimal precision for safe float serialization.
- Avoids the "float_to_wire causes rounding" error
- Preserves actual price details for small-value coins (ZEC, NEAR)

### 3. Add SL Replacement Cooldown (dashboard.py)
**Added**: 10-second cooldown after SL replacement failure
```python
# Safeguard: skip if we just failed an SL replacement in the last 10 seconds
last_sl_fail_ts = managed.get("_last_sl_fail_ts", 0)
if time.time() - last_sl_fail_ts < 10:
    continue
```

**Why**: Prevents cascading failures from hammering the exchange with the same failing order.

## Affected Coins and Impact

This bug affected **all trading pairs**, but was most visible in:
- **NEAR** ($1.36): Tight prices, high multiplier in widening calculations
- **ZEC** ($350): Volatile, triggered frequent ATR-based widening
- **ETH, BTC**: Less obvious due to larger prices, but same root cause

## Testing & Verification

Verify the fix works:
1. Monitor trade journal for absence of consecutive "MANAGE_FAIL" entries
2. Check that SL adjustments succeed on first attempt (no "SL_MOVE" → "MANAGE_FAIL" chains)
3. Confirm positions don't cascade-close from management failures

## Future Improvements

1. **Validate prices server-side** before sending to exchange (prevent malformed orders)
2. **Add unit tests** for round_price() with edge cases
3. **Track SL management success rates** in daily metrics
4. **Consider fixed-decimal format** (using `decimal.Decimal`) for financial calculations instead of float

## Files Changed

- `templates/hyperbot-multi/scripts/position_manager.py` — Removed double-rounding
- `templates/hyperbot-multi/scripts/hl_client.py` — Fixed round_price() algorithm
- `templates/hyperbot-multi/scripts/dashboard.py` — Added SL cooldown safeguard
