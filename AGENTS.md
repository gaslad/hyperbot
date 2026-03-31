# Hyperbot — Agent Instructions

This file provides context for any AI coding assistant working on this repo.
It is the assistant-agnostic equivalent of CLAUDE.md.

## What This Repo Is

Hyperbot is a local-first CLI for generating and running Hyperliquid perpetual
futures trading workspaces. It scaffolds a self-contained workspace per trading
pair (e.g. `hyperbot-SOL/`) with deterministic signal detection, risk management,
order execution, and a live web dashboard.

No LLM API tokens are required for any core functionality.

## Primary Objectives

- Reduce required human approvals by at least 95%
- Preserve safe defaults for live trading
- Make the generated workspace runnable 100% local without LLM API tokens

## Architecture

- `scripts/hyperbot.py` — CLI entrypoint (`dashboard`, `connect`, `run`, `validate`, `list-packs`). Auto-syncs template scripts into workspace on every launch.
- `scripts/create_workspace.py` — workspace generator (template path: `templates/hyperbot-multi`)
- `scripts/connect/` — wallet connect server + browser UI (see Wallet Connect Flow below)
- `scripts/deploy.sh` — Netlify deploy script for landing page
- `strategy-packs/` — installable strategy-pack definitions with `pack_id` in each config
  - `trend_pullback/`, `compression_breakout/`, `liquidity_sweep_reversal/` — legacy 1D/4H strategies
  - `scalp_v2/` — 5-minute breakout scalping strategy (see Scalp Strategy v2 below)
- `templates/hyperbot-multi/` — workspace skeleton copied into generated workspaces
  - `scripts/dashboard.py` — card-based web dashboard with inline HTML/CSS/JS + trading loop
  - `scripts/hl_client.py` — Hyperliquid API client (orders, trigger orders, portfolio, candles, L2 book)
  - `scripts/signals.py` — legacy signal engine (SMA, Bollinger, ATR, wick analysis) for 1D/4H strategies
  - `scripts/scalp_strategy_v2.py` — standalone scalp strategy module (5m/15m, self-contained)
  - `scripts/scalp_strategy_v2_prompt.py` — full strategy specification / rules reference
  - `scripts/blaze_scalp.py` — ultra-fast 1m test scalper for pipeline verification
- `config/policy/operator-policy.json` — risk policy for auto-apply decisions
- `install.sh` — one-command installer for new users (installs to ~/Desktop/Hyperbot)
- `docs/wallet-integration-guide.md` — full EIP-6963/WalletConnect/EIP-712 reference
- `docs/local-first-roadmap.md` — roadmap and design direction

## Design Principles

1. **Local-first**: all core logic is deterministic Python, no cloud model dependency
2. **Assistant-agnostic**: the generated workspace must work without any specific AI tool
3. **Safety by default**: live trading is always opt-in (`--live --confirm-risk`)
4. **Policy-driven**: safe actions auto-apply within operator-policy bounds; only genuinely
   risky transitions (enabling live orders, raising leverage above caps) require human approval
5. **Simple by default**: the dashboard is a card grid, not a trading terminal — complexity is progressive
6. **Educational transparency**: every bot action explains what happened and why in plain English

## What To Look For When Making Changes

1. Places where a human must approve a safe action that could be policy-gated instead
2. Places where a model does work that deterministic code, rules, or templates can do
3. Clean separation between: local execution / optional assistant guidance / high-risk actions
4. Keeping the whole workflow runnable from the CLI without cloud model dependency

## Key Technical Details

- Hyperliquid SDK: `hyperliquid-python-sdk` with EIP-712 phantom agent signing
- Order formatting: `szDecimals` per asset for size, 5 significant figures for price
- API wallet flow: `wallet=agent_wallet, account_address=master_address`
- Portfolio: perps clearinghouse + spot clearinghouse combined for full equity
- Signal dispatch: `pack_id` field in strategy config maps to detector functions in `signals.py` (legacy) or `scalp_strategy_v2.py` (scalp_v2)
- Credentials: macOS Keychain via `security` CLI, fallback to `~/.hyperbot/credentials/`

## Wallet Connect Flow

The wallet connect system lives in `scripts/connect/` and provides the onboarding
flow for linking a user's Hyperliquid wallet to Hyperbot.

### Files

- `scripts/connect/wallet_connect.html` — browser UI with three connection paths:
  1. **EIP-6963 discovered wallets** — auto-detects installed browser extensions
     (Talisman, MetaMask, Rabby, etc.) without Brave hijacking `window.ethereum`
  2. **WalletConnect** — QR code / deep link for mobile wallets (lazy-loads SDK from CDN)
  3. **Manual API key** — fallback for pasting Hyperliquid API wallet credentials
- `scripts/connect/server.py` — local HTTP server that:
  - Serves the HTML page at `http://127.0.0.1:{random_port}/`
  - Provides `/nonce`, `/complete`, `/save-credentials`, `/status` endpoints
  - Submits signed `approveAgent` to Hyperliquid's exchange API
  - Stores credentials in macOS Keychain (master_address, agent_address, agent_private_key)
  - Auto-launches `hyperbot dashboard` after successful connection

### Hyperliquid approveAgent (CRITICAL details)

- **EIP-712 domain chainId**: `42161` (Arbitrum One) — NOT 1337 (HL L1)
- **Action `signatureChainId`**: `"0xa4b1"` (mainnet) or `"0x66eee"` (testnet)
- **`vaultAddress: null`** must be present in the exchange API payload
- Primary type: `HyperliquidTransaction:ApproveAgent`
- Agent wallet is ephemeral (generated via `ethers.Wallet.createRandom()`), can trade but cannot withdraw
- Full reference: `docs/wallet-integration-guide.md`

### New User Install → Connect → Dashboard Flow

```
curl install.sh | bash
  → installs to ~/Desktop/Hyperbot
  → creates venv, installs deps
  → auto-configures PATH
  → auto-launches: hyperbot dashboard
    → if no credentials: redirects to hyperbot connect
    → user connects wallet (browser extension or WalletConnect)
    → credentials saved to Keychain
    → dashboard launches automatically
```

## Website (Landing Page)

- `website/index.html` — single-file marketing landing page ("Brutalist Signal" aesthetic)
- `website/_redirects` — Netlify SPA routing
- Hosted on Netlify: `hyperbot-landing` (site ID: `7a8e6d3d-4864-4c60-8928-6593a8e3429b`)
- Live URL: `https://hyperbot-landing.netlify.app`
- Deploy: `./scripts/deploy.sh` or drag-drop `website/` at Netlify dashboard

## Dashboard

- `templates/hyperbot-multi/scripts/dashboard.py` — serves the trading dashboard as inline HTML (`DASHBOARD_HTML` string)
- All vanilla HTML/CSS/JS — no React, no build step
- `dashPoll()` hits `/api/state` every 3s
- To edit the UI: modify the `DASHBOARD_HTML` string (starts ~line 619), then verify with `python3 -c "compile(open('dashboard.py').read(), 'dashboard.py', 'exec')"`
- Prototype for new design: `prototype-dashboard.jsx` (React, for visual reference only — not used in production)

### Dashboard Architecture (v2 — Card-Based)

The dashboard is being redesigned from a 3-column professional trading terminal to a simplified, educational card-based interface.

**Layout: responsive card grid (not columns)**
- Each active token = one card showing: symbol, strategy, status, P&L
- `+` card at the end opens add-token modal (pick token → pick strategy)
- Clicking an active trade card expands it to reveal: entry/mark/leverage, TP/SL controls (Tighter/Wider, Closer/Further), close position button

**Unmanaged position detection**
- Dashboard pulls all open positions from Hyperliquid via `/info` clearinghouseState
- Positions not opened by Hyperbot appear as "Unmanaged" cards with:
  - Risk rating (poor/decent/good) based on current conditions
  - Issues list (missing stop loss, excessive leverage, etc.)
  - Actionable suggestions (add SL at $X, reduce leverage, let Hyperbot manage)

**Activity & Insights (notification center)**
- Slide-out right panel, triggered by bell icon in header
- Chronological feed of every bot action and observation
- Each notification expands to show a "Why this happened" educational card
- Replaces the old signal checklist, trade thesis, and trade log panels

**Header (simplified)**
- Logo + mode badges (Live/Stopped, Simulation/Mainnet)
- Equity and daily P&L
- Bell icon (notification center), Settings, Start/Stop

**What was removed from v1:**
- 3-column layout
- SVG line chart (no chart by default — adds no value for the target user)
- Logic Engine panel (bias, confidence, condition checklist)
- Exposure & Risk panel (raw liquidation prices, margin utilization)
- Bottom tabbed panel (Logs, Pairs, Config, Backtest) — logs retained but collapsed/hidden

**Design philosophy:**
- Simplicity over density — this is not a pro trading terminal
- Educational transparency — every action explains what and why in plain English
- Progressive disclosure — simple status on cards, controls on expand, education in notifications

## Blaze Scalp (1-Minute Test Strategy)

Ultra-fast test strategy for verifying the execution pipeline. NOT designed for profitability.

- `blaze_scalp.py` — standalone module, same interface as scalp_v2
- Uses 1m candles, EMA(8)/EMA(21) crossover for direction
- Minimal filters: RVOL ≥ 0.5x, spread < 0.1%, micro-breakout over last 5 candles
- 1:1 R:R (1 ATR stop, 1 ATR target) — resolves in minutes
- No time-of-day filter, no ADX/VWAP/CVD/choppiness gates
- Single TP (no partials) — simplest possible execution flow
- Risk: 0.2% equity per trade, max 5x leverage
- Pack ID: `blaze_scalp`

## Scalp Strategy v2 (5-Minute Breakout)

The primary active strategy, designed for 3–5 trades/day on liquid Hyperliquid perps.

### Architecture

- `scalp_strategy_v2.py` is a standalone module exposing `ScalpStrategy.evaluate(symbol, market_data) -> TradeSignal`
- The trading loop in `dashboard.py` branches on `pack_id == "scalp_v2"` — calls the scalp strategy directly instead of the legacy `signals.detect_all_signals()`
- A shared `SCALP_STRATEGY` instance tracks consecutive losses and rolling performance across the session

### How It Works

1. **Regime filter** (all 8 must pass): 15m EMA alignment, ADX > 20, Choppiness < 55, VWAP side, ATR above median, RVOL ≥ 1.5x, CVD confirming, time-of-day window
2. **Setup detection**: 5m breakout/breakdown beyond recent range, not overextended, minimum 1.5R to next structure
3. **Entry**: ALO (maker) limit at retest level preferred, IOC (taker) for strong momentum
4. **Exit**: SL with explicit limit price (wider of structural or 1.0–1.5 ATR), partial TP at 1R (30%), final TP at 1.8R (70%)
5. **Risk**: 0.3% equity per trade, max 10x leverage (derived from stop distance), 1.5% daily loss halt, 3 consecutive loss cooldown

### Execution on Hyperliquid

- Entry → `place_order()` (ALO or IOC)
- SL → `place_trigger_order(tp_or_sl="sl")` with explicit limit price (0.3% buffer)
- TP1 → `place_trigger_order(tp_or_sl="tp")` for 30% partial
- TP final → `place_trigger_order(tp_or_sl="tp")` for remaining 70%
- Failsafe: if SL placement fails after entry, position is immediately flattened via market IOC

### hl_client.py Extensions (for scalp_v2)

- `get_best_bid_ask(coin)` — L2 book top-of-book for spread checks
- `update_leverage(coin, leverage)` — sets cross leverage before entry
- `place_trigger_order(coin, is_buy, size, trigger_price, limit_price, tp_or_sl)` — TP/SL with explicit limit prices

### Workspace Script Sync

`hyperbot.py dashboard` now auto-syncs template `.py` files into the workspace on every launch, comparing file contents. This means edits to template scripts take effect on the next dashboard restart without needing to delete and recreate the workspace.

## Build & Test

```bash
python3 scripts/release_readiness.py          # repo/workspace readiness checks
python3 scripts/validate_apply_revision.py    # revision adoption path validation
python3 scripts/hyperbot.py dashboard         # launch web dashboard (view-only)
```

## Multi-Assistant Task Queue

This repo is worked on by multiple AI assistants (Claude, Codex, Gemini).
Coordination happens through `.tasks/` — read `.tasks/PROTOCOL.md` for the full spec.

**On every session start:**
1. Read `.tasks/codex.md` for any pending tasks assigned to you
2. Complete pending tasks before starting new work (unless the user overrides)
3. When done, mark tasks `[x]`, fill in the `Result:` line, and log in `_log.md`

**After completing work:**
If your changes need verification, testing, or follow-up by another assistant,
append a task to their inbox (`.tasks/claude.md` or `.tasks/gemini.md`).

## Branch

Active development is on `main`.

## Install Script

- `install.sh` — downloads repo, creates Python venv, installs deps, auto-configures PATH
- Default install path: `~/Desktop/Hyperbot` (override with `HYPERBOT_INSTALL_ROOT`)
- Bin symlinks go to `~/.local/bin` (auto-added to shell RC)
- Auto-launches `hyperbot dashboard` at the end of install via `exec`
- Source URL: `https://raw.githubusercontent.com/gastonmorixe/hyperbot/main/install.sh`
