# Hyperbot

Hyperbot is a local-first repository for generating and configuring Hyperliquid-native automated trading workspaces.

## Quick Install

Install Hyperbot from GitHub with one command:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/gaslad/hyperbot/main/install.sh)
```

That installs a local `hyperbot` CLI under `~/.local/bin`.

## Quick Start

```bash
hyperbot list-packs
hyperbot create-workspace btc-workspace --output-dir ~/Documents/Codex --symbol BTCUSDT --strategy-pack trend_pullback
hyperbot validate
```

To update an existing install, run the same install command again.

## Scope

Hyperbot is not the live trading workspace itself.
It should:
- scaffold a clean trading repo
- install selected strategy packs
- configure Hyperliquid connectivity
- prepare automation, notifications, and operator commands
- validate the path to unattended trading

The primary interface is the local CLI installed from GitHub.

## Strategy Packs

Current v1 packs:
- `trend_pullback`
- `compression_breakout`
- `liquidity_sweep_reversal`

These are baseline packs. They are meant to install good defaults quickly.

## Token-Specific Revision

Hyperbot keeps a second-stage workflow for symbol-specific optimization.

That means:
- baseline pack defaults are one-size-fits-most
- token-specific revision is separate
- the generated workspace can run a 90-day symbol review without overwriting pack defaults globally
- `scripts/profile_symbol_strategy.py` in generated workspaces fetches real Hyperliquid candles and writes profile + revision artifacts
- workspace creation now runs the 90-day revision automatically for the selected symbol and each installed strategy pack unless `--skip-profile` is used
- `scripts/apply_revision.py` then lets the user preview, validate, back up, and merge approved overrides into the installed strategy config

This is tied to the actual selected pair, not a hardcoded token label. Artifacts are named from the selected symbol and strategy ids.

## Current Layout

- `strategy-packs/`: installable strategy-pack definitions and templates
- `templates/workspace/`: starter Hyperliquid-only workspace skeleton
- `scripts/hyperbot.py`: local CLI entrypoint
- `scripts/create_workspace.py`: generator entry point
- `scripts/validate_apply_revision.py`: local end-to-end validation for revision preview/apply flows
- `scripts/release_readiness.py`: repo and workspace readiness checks
- `install.sh`: one-command GitHub installer
- `docs/`: product architecture and generation plan

## Validation

Validate the revision-adoption path locally:

```bash
python3 scripts/validate_apply_revision.py
```

Run the repo/workspace readiness check:

```bash
python3 scripts/release_readiness.py
```

This verifies:
- Python scripts compile
- `apply_revision.py` passes its generated-workspace validation flow
- required repo files are present

## Local-First Direction

The core workspace generator is intended to stay deterministic and local-first.

Planning artifacts for reducing approval friction and avoiding mandatory model-token usage live in:
- [`CLAUDE.md`](/Users/gaston/Library/CloudStorage/Dropbox/--%20PROJECTS/Plugins/hyperbot/CLAUDE.md)
- [`docs/local-first-roadmap.md`](/Users/gaston/Library/CloudStorage/Dropbox/--%20PROJECTS/Plugins/hyperbot/docs/local-first-roadmap.md)
