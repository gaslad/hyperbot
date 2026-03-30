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

- `scripts/hyperbot.py` — CLI entrypoint (`dashboard`, `connect`, `run`, `validate`, `list-packs`)
- `scripts/create_workspace.py` — workspace generator (template path: `templates/hyperbot-multi`)
- `scripts/connect/` — wallet connect server + browser UI (see Wallet Connect Flow below)
- `scripts/deploy.sh` — Netlify deploy script for landing page
- `strategy-packs/` — installable strategy-pack definitions with `pack_id` in each config
- `templates/hyperbot-multi/` — workspace skeleton copied into generated workspaces
  - `scripts/dashboard.py` — single-page web app (5-step wizard + live trading dashboard)
  - `scripts/hl_client.py` — Hyperliquid API client (orders, portfolio, candles)
  - `scripts/signals.py` — deterministic signal engine (SMA, Bollinger, ATR, wick analysis)
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
- Signal dispatch: `pack_id` field in strategy config maps to detector functions in `signals.py`
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

Active development is on `feature/web3-wallet-connect`.

## Install Script

- `install.sh` — downloads repo, creates Python venv, installs deps, auto-configures PATH
- Default install path: `~/Desktop/Hyperbot` (override with `HYPERBOT_INSTALL_ROOT`)
- Bin symlinks go to `~/.local/bin` (auto-added to shell RC)
- Auto-launches `hyperbot dashboard` at the end of install via `exec`
- Source URL: `https://raw.githubusercontent.com/gastonmorixe/hyperbot/main/install.sh`
