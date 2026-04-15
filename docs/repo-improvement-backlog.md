# Repo Improvement Backlog

Last updated: 2026-04-10

This backlog is based on the current repo state, not generic repo advice.

## Repo Improvements

1. Add stronger ignore rules for local tool state and vendored output.
Evidence: `.claude/`, `.seo77/`, and nested `.netlify/` content add search and test noise without being part of the product.
Status: started by tightening `.gitignore`.

2. Decide on one active activity log.
Evidence: root `_ACTIVITY.log` already exists, while the restored task system now uses `.tasks/_log.md`.
Recommendation: treat `_ACTIVITY.log` as legacy history and use `.tasks/_log.md` for live handoffs unless there is a strong reason to keep both active.

3. Add a repo-scoped validation command for collaboration files.
Evidence: the repo had `AGENTS.md` references to `.tasks/` without the queue actually existing.
Recommendation: extend `scripts/release_readiness.py` to verify `AGENTS.md`, `STATUS.md`, `NEXT.md`, `LEARNINGS.md`, and `.tasks/` stay internally consistent.

## App And Product Improvements

1. Persist strategy attribution at the moment a decision is made.
Evidence: weekly reporting can reconstruct fills, but it cannot reliably explain why a trade happened when the dashboard was not running.
Primary files: `templates/hyperbot-multi/scripts/dashboard.py`, `templates/hyperbot-multi/scripts/trade_journal.py`.

2. Record decision logs outside the UI process.
Evidence: the current learning loop weakens when Hyperliquid fills exist but local decision logs do not.
Recommendation: move the decision journal closer to the execution loop so bot reasoning is captured even without an open dashboard tab.

3. Separate signal quality from execution quality in the weekly report.
Evidence: realized PnL alone does not reveal whether losses came from bad entries, poor exits, spread, fees, or risk sizing.
Recommendation: extend weekly output to bucket misses into setup quality, execution quality, and risk management quality.

## Workflow And Automation Improvements

1. Use `.tasks/` only for assigned handoffs, not broad planning.
Evidence: `NEXT.md` and `.tasks/` serve different purposes and drift when both contain general TODOs.
Recommendation: keep project-level priorities in `NEXT.md` and assistant-owned follow-ups in `.tasks/`.

2. Add a scheduled self-check for weekly delivery health.
Evidence: SimpleX delivery now depends on a local profile, a resolved contact name, and report regeneration before send.
Primary file: `scripts/send_simplex_report.py`.

3. Make test and search commands ignore vendored and worktree paths by default.
Evidence: naive test discovery currently explodes into `.claude/worktrees` and nested vendor trees, which obscures the real project tests.
Recommendation: document or script a canonical project test command that targets only repo-owned tests.

## Open Risks And Data Gaps

- Strategy attribution remains the biggest learning gap in the trading loop.
- The repo now has a consistent collaboration contract, but it still has two historical activity-log concepts.
- Generated workspace documentation is clearer than before, but repo-level validation does not yet enforce that clarity.
