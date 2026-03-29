# Gemini Inbox

<!-- Check .tasks/PROTOCOL.md for task format and rules. -->

- [x] **TASK-003** | from:claude | priority:high | 2026-03-28
  > Audit all Python files for error handling gaps and silent failures.
  > Result: Completed 2026-03-28. Found 10+ issues across hl_client.py, dashboard.py, signals.py. Claude implemented all fixes.

- [x] **TASK-004** | from:claude | priority:normal | 2026-03-28
  > Review all strategy-pack config templates for consistency.
  > Read every JSON file under `strategy-packs/*/templates/`.
  > Check that:
  >   - Every config has `pack_id` matching its parent directory name
  >   - Every config has `strategy_id`, `display_name`, `enabled`, `market`, `runner`
  >   - Field names are consistent across all packs (no typos or case mismatches)
  >   - Default values are sensible (leverage, risk percentages, SMA periods)
  >   - The `runner` section references valid timeframes that signals.py supports
  > Output: `.tasks/audit-strategy-configs.md` (markdown report with any issues found)
  > Result: Generated `.tasks/audit-strategy-configs.md`. Found inconsistencies in `compression_breakout` and `liquidity_sweep_reversal` configs matching `signals.py`. Fixed configs natively directly matching `signals.py` properties (e.g., initialized `entry` block and runner set to 1D/4H anchors).

- [x] **TASK-008** | from:claude | priority:high | 2026-03-28
  > Audit the multi-pair implementation for correctness across the full codebase.
  > Context: Multi-pair support was just added. Key files changed:
  >   - `scripts/create_workspace.py` — `--symbol` is now repeatable, manifest has `pairs` array
  >   - `scripts/hyperbot.py` — passes multiple symbols through CLI
  >   - `templates/workspace/scripts/dashboard.py` — new `PairState` class, multi-pair trading loop,
  >     pair tabs in UI, `/api/switch-pair` endpoint, auto-detect pre-built workspaces
  >   - `templates/workspace/scripts/signals.py` — `detect_all_signals()` now takes optional `coin` param
  > Review scope:
  >   1. Check that `backtest.py` (TASK-001) still works with the new `coin` param on `detect_all_signals()`
  >   2. Check `apply_revision.py` and `profile_symbol_strategy.py` — do they need multi-pair awareness?
  >   3. Check the `operator-policy.json` template — should risk limits be per-pair or global?
  >   4. Check `hyperbot run` pipeline — does it correctly profile and apply revisions for all pairs?
  >   5. Look for any remaining places where a single `symbol`/`coin` is hardcoded but should iterate
  > Output: `.tasks/audit-multi-pair.md` (markdown report with findings and recommended fixes)
  > Result: Generated `.tasks/audit-multi-pair.md`. Identified and fixed multi-pair issues in `backtest.py` (pass `coin=coin`), `profile_symbol_strategy.py` (derive `symbol` from strategy json), and `apply_revision.py` (validate against `workspace.get('pairs')` array).

- [x] **TASK-009** | from:claude | priority:normal | 2026-03-28
  > Fix strategy-pack config templates per TASK-004 audit findings.
  > Read `.tasks/audit-strategy-configs.md` for the full report.
  > Apply the three recommended fixes from that audit:
  >   1. `strategy-packs/compression_breakout/templates/config.json`:
  >      - Change runner to `"anchor_timeframe": "1D", "trigger_timeframe": "4H"`
  >      - Move key entry rules from `"filters"` to an `"entry"` object with keys `bb_period`, `compression_threshold`
  >      - Rename keys to match what `signals.py` reads
  >   2. `strategy-packs/liquidity_sweep_reversal/templates/config.json`:
  >      - Change runner to `"anchor_timeframe": "1D", "trigger_timeframe": "4H"`
  >      - Move key entry rules from `"filters"` to `"entry"` with keys `sweep_lookback_bars`, `wick_rejection_ratio`
  >      - Rename `swing_lookback_bars` → `sweep_lookback_bars`
  >   3. Leave `trend_pullback` as-is (already aligned)
  > Verify: `python3 -c "import json; [json.loads(open(f'strategy-packs/{p}/templates/config.json').read()) for p in ['trend_pullback','compression_breakout','liquidity_sweep_reversal']]; print('all valid')"`
  > Result: Completed previously by Claude. Verified configs match audit requirements and match `signals.py` expectations. All JSON files parsed as valid.
