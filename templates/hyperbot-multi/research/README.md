# Research Layer

Hyperbot separates baseline pack installation from token-specific strategy revision.

## Two Modes

1. Baseline pack defaults
- use the installed strategy-pack defaults as-is

2. Token-specific revision
- review the selected symbol separately
- use recent history, such as 90 days, to refine filters, stop logic, and exits
- keep the revision tied to that specific symbol, not the global pack defaults

## Default Behavior

When a workspace is created, Hyperbot automatically runs a token-specific 90-day revision for the selected symbol and each installed strategy pack unless the generator is called with `--skip-profile`.

## Current Tool

Use `scripts/profile_symbol_strategy.py` to:
- rerun a market profile
- rank pack suitability for the current symbol
- generate or refresh token-specific revision files
