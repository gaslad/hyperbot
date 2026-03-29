# Hyperbot Local-First Roadmap

## Goal

Move Hyperbot toward a workflow where:

- workspace generation is fully local
- strategy revision remains deterministic and local
- model usage is optional
- human approvals are reserved for genuinely risky transitions, not routine setup

Target:
- reduce operator approval interrupts by at least 95%
- make normal workspace creation and iteration runnable without LLM API tokens

## Current State

Already local:
- `scripts/create_workspace.py` — workspace generation
- `scripts/hyperbot.py` — CLI entrypoint with `dashboard`, `run`, `validate` commands
- strategy-pack templates under `strategy-packs/` (all include `pack_id` for signal dispatch)
- workspace template material under `templates/workspace/`
- `templates/workspace/scripts/apply_revision.py` — revision adoption
- `templates/workspace/scripts/profile_symbol_strategy.py` — 90-day symbol profiling
- `templates/workspace/scripts/hl_client.py` — full Hyperliquid API client (orders, portfolio, candles)
- `templates/workspace/scripts/signals.py` — deterministic signal detection (SMA, Bollinger, ATR, wick)
- `templates/workspace/scripts/dashboard.py` — single-page web dashboard (wizard + live trading)

Phase 4 (web dashboard) completed:
- 5-step onboarding wizard: pair → strategies → risk → credentials → build
- Live dashboard with real-time price, equity (perps+spot), signals, positions, trade log
- Order execution with proper `szDecimals` rounding and 5-sig-fig price formatting
- macOS Keychain credential storage
- Workspace auto-rename to `hyperbot-{COIN}`
- Strategy config installation with `pack_id` for signal dispatcher routing
- Daily loss limit enforcement, position sizing, leverage caps
- Thinking ticker showing system status

Still external or approval-heavy:
- market data fetch (Hyperliquid API — no alternative for live candles)
- live trading enablement (`--live --confirm-risk` required)

## Design Direction

### 1. Separate low-risk from high-risk actions

Low-risk actions should become policy-driven and default-allow:
- scaffold workspace
- install packs
- generate docs/runbooks
- run local validations
- produce deterministic revision recommendations
- apply revisions automatically when they stay inside defined safe bands

High-risk actions should remain explicit:
- enabling unattended live trading
- increasing leverage above policy limits
- changing risk caps beyond local thresholds
- authorizing real order submission

### 2. Add local policy files

Introduce a workspace policy file, for example:
- `config/policy/operator-policy.json`

It should define:
- approval-free actions
- safe parameter ranges for auto-apply
- max leverage ceilings
- allowed symbols or venues
- whether unattended mode can ever be enabled automatically

### 3. Make revision adoption deterministic

Today `apply_revision.py` is safe but still manual by design.

Add an optional path such as:
- `--auto-apply-safe`
- `--policy config/policy/operator-policy.json`

Only auto-apply changes when they stay within approved local bounds, for example:
- leverage unchanged or lower
- risk-per-trade unchanged or lower
- stop-loss logic tightened, not loosened
- filter changes within bounded ranges

### 4. Add a fully local CLI workflow

The repo should support a local-first path like:

```bash
python3 scripts/create_workspace.py ...
python3 scripts/validate_apply_revision.py
python3 scripts/profile_symbol_strategy.py --days 90
python3 scripts/apply_revision.py --auto-apply-safe --policy config/policy/operator-policy.json
```

This should work without any model token requirement.

### 5. Keep model usage optional

If assistants are used later, limit them to:
- operator explanations
- summaries
- comparison writeups
- suggested next actions

Do not make them required for:
- workspace creation
- config generation
- validation
- pack scoring
- revision merging

## Suggested Implementation Order

1. ~~Add local operator policy schema and sample file.~~ **Done** — `config/policy/operator-policy.json` ships in every workspace.
2. ~~Extend `apply_revision.py` with policy-aware safe auto-apply mode.~~ **Done** — `--auto-apply-safe --policy` flags added.
3. ~~Add a single local command that runs scaffold -> validate -> review -> safe-apply.~~ **Done** — `hyperbot run` pipeline command.
4. ~~Cache market data snapshots locally so repeated runs do not require refetch.~~ **Done** — `research/cache/` with configurable max age.
5. ~~Add an optional local-only mode flag that disables any future model-dependent features.~~ **Done** — `hyperbot --local-only` and `--offline` on the profiler.
6. ~~Add web dashboard with onboarding wizard and live trading.~~ **Done** — `hyperbot dashboard` launches single-page app.
7. ~~Fix order execution (szDecimals rounding, price formatting, error handling).~~ **Done** — orders confirmed on mainnet.
8. ~~Fix portfolio value (perps + spot combined).~~ **Done** — full equity displayed correctly.
9. ~~Fix signal detection (pack_id routing in strategy configs).~~ **Done** — all three strategies returning real analysis.

## Next Priorities

1. ~~Multi-pair support — run multiple `hyperbot-{COIN}` workspaces simultaneously with a unified dashboard~~ **Done** — single workspace now supports multiple pairs via `--symbol` flag (repeatable), dashboard has pair tabs, trading loop iterates all enabled pairs, manifest includes backward-compatible `pairs` array
2. Backtesting — local replay of signal detection against historical candle data
3. Notifications — optional alerts (local push, webhook) when signals fire or positions change
4. Performance tracking — trade history with win rate, R-multiple, drawdown metrics
5. Strategy tuning UI — adjust strategy parameters from the dashboard without editing JSON

## Design Principles

- Preserve safety boundaries: live trading is always opt-in (`--live --confirm-risk`)
- Remove routine human approvals: policy-driven auto-apply for safe parameter ranges
- Keep assistant usage optional: all core logic is deterministic local Python
- Make the trading workspace usable as a local deterministic system
