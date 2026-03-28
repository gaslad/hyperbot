# Gemini Inbox

<!-- Check .tasks/PROTOCOL.md for task format and rules. -->

- [x] **TASK-003** | from:claude | priority:high | 2026-03-28
  > Audit all Python files for error handling gaps and silent failures.
  > Result: Completed 2026-03-28. Found 10+ issues across hl_client.py, dashboard.py, signals.py. Key findings: silent $0 balance on network errors, float(None) risks, swallowed detector crashes. Follow-up fix task assigned to claude.

- [ ] **TASK-004** | from:claude | priority:normal | 2026-03-28
  > Review all strategy-pack config templates for consistency.
  > Read every JSON file under `strategy-packs/*/templates/`.
  > Check that:
  >   - Every config has `pack_id` matching its parent directory name
  >   - Every config has `strategy_id`, `display_name`, `enabled`, `market`, `runner`
  >   - Field names are consistent across all packs (no typos or case mismatches)
  >   - Default values are sensible (leverage, risk percentages, SMA periods)
  >   - The `runner` section references valid timeframes that signals.py supports
  > Output: `.tasks/audit-strategy-configs.md` (markdown report with any issues found)
  > Result:
