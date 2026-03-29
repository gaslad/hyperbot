# Multi-Pair Implementation Audit

## Overview
A comprehensive review of the multi-pair architecture was conducted following the recent introduction of the `--symbol` repeatable parameter and `pairs` array in the workspace manifest. The core signal generation, trading UI, and client libraries adequately handle multiple active pairs. However, a few lingering edge cases where a single hardcoded symbol was assumed have been identified and patched.

## Findings & Resolutions

### 1. `backtest.py` Processing Irrelevant Signals
- **Issue**: `backtest.py` fetched signals by calling `signals.detect_all_signals(candles_1d, candles_4h, current_price)` without submitting the explicit `coin` parameter. In a multi-pair workspace scenario (where `strategy_id` varies per pair), `detect_all_signals` would apply one pair's candlesticks to another pair's strategy config.
- **Fix**: Passed `coin=coin` to the `detect_all_signals` call on line ~394 so the detector strictly parses matching strategy configs.

### 2. `profile_symbol_strategy.py` Overriding `args.symbol` with Workspace Default 
- **Issue**: During the profiling phase, when evaluating an individual strategy pack (e.g. `eth_trend_pullback`), the script fell back to `workspace.get("symbol")` (the primary pair) if `--symbol` was missing from the CLI. This led to profiling `BTCUSDT` against an `ETHUSDT` strategy.
- **Fix**: Adjusted the script to first attempt to infer the symbol directly from the relevant JSON configuration in `configs[strategy_id]["market"]["symbol"]`. If all else fails, it falls back to the workspace default.

### 3. `apply_revision.py` Validation Check Failure
- **Issue**: Before applying a revision, `apply_revision.py` strictly verified whether the `config_symbol` matches `workspace.get("symbol")`. In a multi-pair workspace, secondary pairs would instantly crash the validation. 
- **Fix**: Altered validation rules to verify whether the `config_symbol` exists within the array collected from `[p["symbol"] for p in workspace.get("pairs", [])]`.

### 4. `operator-policy.json` (Risk Parameters Template)
- **Review Scope**: Should risk limits be per-pair or global?
- **Finding**: Currently, the policy dictates a global account-wide parameter model (e.g. `max_daily_loss_pct`, `leverage_max`). While maintaining global absolute caps provides a critical safety layer, some workflows might benefit from allowing pair-specific leverage bands in the future. At present, preserving a global maximum policy is deemed correct and the safest default to prevent runaway leverage.

### 5. `hyperbot.py run` Pipeline
- **Issue**: The CLI loops through available strategy packs and initiates the profiler automatically for each one.
- **Finding**: The pipeline executes `sys.executable -B profile_symbol_strategy.py --strategy-id ID` successfully now that `profile_symbol_strategy.py` infers the token from the supplied strategy configuration (per fix #2).

## Conclusion
The multi-pair execution framework behaves securely. Revisions to the validation rules and inference endpoints have addressed cross-pair pollution.
