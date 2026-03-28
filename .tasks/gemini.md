# Gemini Inbox

<!-- Check .tasks/PROTOCOL.md for task format and rules. -->

- [ ] **TASK-003** | from:claude | priority:high | 2026-03-28
  > Audit all Python files for error handling gaps and silent failures.
  > Scope: every `.py` file under `templates/workspace/scripts/` and `scripts/`.
  > Look for:
  >   - Bare `except:` or `except Exception:` that swallow errors silently
  >   - Missing None checks on API responses before accessing .get() or float()
  >   - float() calls on potentially None values
  >   - HTTP/API failures that could crash the trading loop
  >   - Any place where dashboard.py's dashPoll-style bug could repeat
  >     (JS referencing an element ID that doesn't exist in the HTML)
  > For each finding, report: file, line, what's swallowed, suggested fix.
  > Output: `.tasks/audit-error-handling.md` (markdown checklist)
  > Then append follow-up tasks to `claude.md` or `codex.md` for the actual fixes.
  > Result:

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
