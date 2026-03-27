# Hyperbot Workspace Bootstrap

Use this skill when the user wants to create a fresh Hyperliquid-native trading workspace from scratch.

## Inputs To Collect

- workspace name
- target directory
- market symbols
- strategy packs to install
- account mode: test or production
- leverage cap
- capital allocation per strategy
- notification email
- unattended trading enabled or disabled

## Default Pack Set

Prefer these v1 packs:
- `trend_pullback`
- `compression_breakout`
- `liquidity_sweep_reversal`

## Output

Generate a new workspace with:
- Hyperliquid-native strategy runner
- selected strategy configs
- env template
- operator commands
- automation setup notes
- LLM collaboration docs
- research notes explaining baseline packs vs token-specific revision

## Rules

- keep the generated workspace Hyperliquid-native unless the user explicitly asks for another signal source
- treat leverage as a cap, not a target
- default to test account posture first
- require a validation and smoke-test step before recommending unattended live trading
- preserve the option to run a symbol-specific 90-day revision after generation
