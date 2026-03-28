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
- `scripts/create_workspace.py`
- strategy-pack templates under `strategy-packs/`
- workspace template material under `templates/workspace/`
- `templates/workspace/scripts/apply_revision.py`
- most of `templates/workspace/scripts/profile_symbol_strategy.py`

Still external or approval-heavy:
- market data fetch in `profile_symbol_strategy.py`
- explicit operator merge/apply decision for revisions
- future live-trading enablement decisions

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

## Questions For Claude To Push On

1. Which current approval points are actually safety-critical, and which are just workflow friction?
2. What deterministic rules can replace human confirmation for routine revision adoption?
3. What minimum policy schema would let Hyperbot auto-run safely for setup and revision work?
4. How should local market-data caching work so the repo is practical even without external model services?
5. What repo changes would make the plugin optional and the CLI primary?
5. What remaining non-local dependencies should be isolated so the local CLI stays the default product path?

## Expected Outcome

Claude should propose a repo plan that:
- preserves safety boundaries
- removes most routine human approvals
- keeps assistant usage optional
- makes the trading workspace usable as a local deterministic system
