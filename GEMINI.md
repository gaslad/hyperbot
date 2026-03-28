# Hyperbot — Gemini Agent Instructions

This file provides context for Google Gemini CLI / Antigravity working on this repo.
See also: AGENTS.md (cross-tool), CLAUDE.md (Claude-specific).

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

- `scripts/hyperbot.py` — CLI entrypoint (`dashboard`, `run`, `validate`, `list-packs`)
- `scripts/create_workspace.py` — workspace generator
- `strategy-packs/` — installable strategy-pack definitions with `pack_id` in each config
- `templates/workspace/` — workspace skeleton copied into generated workspaces
  - `scripts/dashboard.py` — single-page web app (5-step wizard + live trading dashboard)
  - `scripts/hl_client.py` — Hyperliquid API client (orders, portfolio, candles)
  - `scripts/signals.py` — deterministic signal engine (SMA, Bollinger, ATR, wick analysis)
- `config/policy/operator-policy.json` — risk policy for auto-apply decisions
- `docs/local-first-roadmap.md` — roadmap and design direction

## Design Principles

1. **Local-first**: all core logic is deterministic Python, no cloud model dependency
2. **Assistant-agnostic**: the generated workspace must work without any specific AI tool
3. **Safety by default**: live trading is always opt-in (`--live --confirm-risk`)
4. **Policy-driven**: safe actions auto-apply within operator-policy bounds; only genuinely
   risky transitions (enabling live orders, raising leverage above caps) require human approval

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
1. Read `.tasks/gemini.md` for any pending tasks assigned to you
2. Complete pending tasks before starting new work (unless the user overrides)
3. When done, mark tasks `[x]`, fill in the `Result:` line, and log in `_log.md`

**After completing work:**
If your changes need verification, testing, or follow-up by another assistant,
append a task to their inbox (`.tasks/claude.md` or `.tasks/gemini.md`).

## Branch

Active development is on `feature/web3-wallet-connect`.
