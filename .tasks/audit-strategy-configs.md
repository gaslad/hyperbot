# Strategy Config Audit Report

## Overview
A review of the strategy config templates under `strategy-packs/*/templates/config.json` was conducted to ensure consistency across naming conventions, alignment with the deterministic logic in `templates/workspace/scripts/signals.py`, and adherence to required fields.

## General Checks
- **Pack IDs Context**: All configs correctly denote matching `pack_id` fields for their respective directories. 
- **Required Fields**: All configs possess the core root-level elements (`strategy_id`, `display_name`, `enabled`, `market`, `runner`). 
- **Sensible Defaults**: 
  - Risk parameters inside `position_sizing` allocate safely (`1%`, `0.75%`, `1.5%` per trade and reasonably bounded leverage `max_leverage` 3-4x).
  - Take profits exist and stagger gracefully (`50%` close initially, `50%` on TP2).

## Inconsistencies and Issues Found

### 1. `compression_breakout` Config
- **Missing `entry` Object**: `signals.py` extracts `bb_period` and `compression_threshold` from the `"entry"` block, but the config template strictly defines a `"filters"` block. The actual risk and entry thresholds defined in `signals.py` default to `20` and `0.04` and ignore the JSON completely.
- **Unused `filters` Key**: Keys such as `compression_lookback_bars`, `atr_percentile_max` do not correspond to the properties fetched by the strategy rules engine.
- **Runner Timeframes Misalignment**: `signals.py` operates strictly on the `candles_1d` variable to calculate Bollinger Bands for compression, yet the runner specifies `{"anchor_timeframe": "4H", "trigger_timeframe": "1H"}`. This implies an expectation to run on 4H candles which the Python code natively circumvents.

### 2. `liquidity_sweep_reversal` Config
- **Missing `entry` Object**: `signals.py` looks to extract `sweep_lookback_bars` and `wick_rejection_ratio` from the `"entry"` block.
- **Misnamed and Unused Parameters**: Config provides `"filters": {"swing_lookback_bars": 12, ...}` which is ignored. `signals.py` looks for `sweep_lookback_bars` instead inside `entry`. 
- **Runner Timeframes Misalignment**: Strategy engine uses `candles_1d` to extract daily high/lows and closes. The `runner` configuration dictates `4H` as the anchor.

### 3. `trend_pullback` Config
- **Clean Alignment**: The keys directly align with how `signals.py` parses parameters. `entry` contains `sma_period` and `pullback_zone_pct`. `filters` contains `min_pullback_pct`. It is the most robustly aligned config.
- **Runner Configuration**: Anchor is `1D`, trigger is `4H`, confirming its alignment with `signals.py` checking `candles_1d` and `candles_4h`. No issues found.

## Recommended Fixes
To harmonize logic, the config templates for `compression_breakout` and `liquidity_sweep_reversal` should be rewritten. 
1. Convert `runner` on both to `"anchor_timeframe": "1D", "trigger_timeframe": "4H"` to mirror execution in `signals.py`.
2. Move key structural entry rules from `"filters"` to an `"entry"` object.
3. Rename keys to exactly match what `signals.py` uses.
