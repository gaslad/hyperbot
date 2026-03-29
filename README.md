# Hyperbot

Local-first CLI for generating and running Hyperliquid perpetual futures trading workspaces. One command takes you from zero to a live dashboard with real-time signals, position tracking, and automated order execution.

## Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/gaslad/hyperbot/main/install.sh)
```

Installs the `hyperbot` CLI under `~/.local/bin`. Run the same command again to update.

## Quick Start — Web Dashboard

The fastest path from nothing to live trading:

```bash
hyperbot dashboard                          # view-only mode (safe, no orders)
hyperbot dashboard --live --confirm-risk    # live mode (real orders with real money)
```

This launches a single-page web app with a 5-step wizard:

1. Pick a trading pair (top Hyperliquid perps by volume)
2. Select strategies (trend pullback, compression breakout, liquidity sweep reversal)
3. Set risk parameters (leverage, risk per trade, daily loss limit)
4. Enter wallet credentials (stored in macOS Keychain / encrypted file)
5. Build workspace (fetches candles, installs configs, runs initial signal scan)

After setup, the dashboard shows live price, account equity (perps + spot combined), strategy signals, open positions, trade log, and a thinking ticker that explains what the bot is doing.

## Quick Start — CLI Only

```bash
python3 -m pip install -r requirements.txt
hyperbot list-packs
hyperbot create-workspace btc-workspace --output-dir ~/Documents --symbol BTCUSDT --strategy-pack trend_pullback
hyperbot validate
```

## How It Works

Hyperbot generates a self-contained trading workspace per pair (e.g. `hyperbot-SOL/`, `hyperbot-LINK/`). Each workspace contains:

- Strategy configs with `pack_id` for signal dispatcher routing
- A deterministic signal engine (SMA, Bollinger Bands, ATR, wick analysis)
- Risk management (position sizing, daily loss limits, leverage caps)
- Operator policy files for unattended-safe auto-apply
- A live dashboard server with real-time polling

No LLM API tokens are required. All signal detection, risk checks, and order sizing are deterministic local Python.

## Strategy Packs

Current v1 packs:

- `trend_pullback` — SMA trend following with pullback entries
- `compression_breakout` — Bollinger Band compression into expansion
- `liquidity_sweep_reversal` — Wick analysis for sweep-and-reverse setups

These are baseline packs with good defaults. Token-specific revision (90-day profile) can further tune them per pair.

## Token-Specific Revision

Hyperbot keeps a second-stage workflow for symbol-specific optimization:

- Baseline pack defaults are one-size-fits-most
- `scripts/profile_symbol_strategy.py` in generated workspaces fetches real Hyperliquid candles and writes profile + revision artifacts
- Workspace creation runs the 90-day revision automatically unless `--skip-profile` is used
- `scripts/apply_revision.py` lets the user preview, validate, back up, and merge approved overrides
- Safe revisions (within policy bands) can auto-apply without operator confirmation

## Hyperliquid Integration

Orders use the Hyperliquid Python SDK with proper asset-specific formatting:

- `szDecimals` lookup per asset for size rounding
- 5 significant figures for price rounding
- IOC (Immediate-or-Cancel) market orders with configurable slippage
- API wallet flow: `wallet=agent_wallet, account_address=master_address`
- Full portfolio value: perps clearinghouse + spot clearinghouse combined
- Credential storage via macOS Keychain (`security` CLI) or encrypted file

## Current Layout

- `strategy-packs/`: installable strategy-pack definitions and templates
- `templates/workspace/`: starter Hyperliquid-only workspace skeleton
  - `scripts/dashboard.py`: single-page web app (wizard + live dashboard)
  - `scripts/hl_client.py`: Hyperliquid API client with order execution
  - `scripts/signals.py`: deterministic signal detection engine
- `scripts/hyperbot.py`: local CLI entrypoint
- `scripts/create_workspace.py`: generator entry point
- `scripts/validate_apply_revision.py`: local end-to-end validation
- `scripts/release_readiness.py`: repo and workspace readiness checks
- `install.sh`: one-command GitHub installer
- `requirements.txt`: Python dependency manifest
- `docs/`: product architecture and roadmap

## Validation

```bash
python3 scripts/validate_apply_revision.py   # revision adoption path
python3 scripts/release_readiness.py         # repo/workspace readiness
```

## Local-First Direction

The core workspace generator is deterministic and local-first. No cloud model dependency is required for workspace generation, signal detection, risk management, or order execution.

Planning artifacts for reducing approval friction live in:

- [`CLAUDE.md`](CLAUDE.md)
- [`docs/local-first-roadmap.md`](docs/local-first-roadmap.md)
