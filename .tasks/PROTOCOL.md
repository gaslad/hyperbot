# Multi-Assistant Task Protocol

This directory is an async task queue shared between AI coding assistants
working on this repo. Each assistant has an inbox file. Any assistant can
read any inbox, but only writes tasks to OTHER assistants' inboxes.

## How It Works

1. **On session start**: read your own inbox, pick up any pending tasks
2. **On task completion**: mark the task `[x]` in the inbox, append a
   one-line result summary, and log it in `_log.md`
3. **When you have work for another assistant**: append a task to their inbox
4. **Self-tasks**: if you discover work you can't finish now, add it to
   your own inbox for next session

## Inboxes

| File | Assistant | Strengths |
|------|-----------|-----------|
| `claude.md` | Claude (Cowork / Claude Code) | Live integration, dashboard UI, Hyperliquid API, real-time debugging, multi-file edits with context |
| `codex.md` | Codex (OpenAI) | Self-contained modules, test writing, isolated functions, sandboxed execution |
| `gemini.md` | Gemini (Antigravity / Google) | Large context window, whole-repo audits, cross-file refactoring, pattern analysis |

## Task Format

```markdown
- [ ] **TASK-001** | from:claude | priority:high | 2026-03-28
  > Build backtest.py module — walk-forward simulation using signals.py interface.
  > Context: read templates/workspace/scripts/signals.py for detect_all_signals() API.
  > Output: templates/workspace/scripts/backtest.py
  > Result:
```

Fields:
- `TASK-NNN` — sequential ID (check _log.md for the latest number)
- `from:` — which assistant created the task
- `priority:` — `high` (do first), `normal`, `low` (when you have time)
- Date — when the task was created
- `>` lines — description, context pointers, expected output
- `Result:` — filled in by the assistant that completes it

## Rules

1. **Read before you write.** Check your inbox before starting any other work.
2. **One task, one concern.** Keep tasks atomic. If it's two things, make two tasks.
3. **Point to files, not paste code.** Use relative paths so context stays fresh.
4. **Don't duplicate.** Check if a task already exists before adding one.
5. **Verify before closing.** Run the relevant validation before marking `[x]`.
6. **Log everything.** Every completed task gets a line in `_log.md`.
7. **Don't modify another assistant's completed tasks.** Only append new ones.
