# Multi-Assistant Task Protocol

## Purpose

Use `.tasks/` for assistant-specific handoffs, verification requests, and bounded follow-up work.

Use the other collaboration files for different jobs:

- `AGENTS.md` for stable repo rules
- `STATUS.md` for current state
- `NEXT.md` for project-level next actions
- `LEARNINGS.md` for durable lessons

## Files

- `codex.md` — Codex inbox
- `claude.md` — Claude inbox
- `gemini.md` — Gemini inbox
- `_log.md` — append-only handoff log

## Task Template

Use this exact structure:

```md
- [ ] Short task title
  Owner: codex
  Context: why this task exists
  Files: some/path.py, another/path.md
  Acceptance: concrete definition of done
  Result:
```

Rules:

- One task, one owner.
- Keep `Acceptance:` testable.
- Keep `Result:` empty until the task is completed or cancelled.
- If the task is cancelled, leave it in place and explain why in `Result:`.

## Session Start

1. Read your inbox file.
2. Review `STATUS.md`, `NEXT.md`, and `LEARNINGS.md`.
3. Complete pending tasks before unrelated work unless the user explicitly overrides.

## Session End

1. Mark finished tasks `[x]`.
2. Fill in the `Result:` line with what happened and the date.
3. Add any new follow-up tasks to the correct inbox.
4. Append one line to `.tasks/_log.md` using:

```text
YYYY-MM-DDTHH:MM | assistant | scope | summary
```

## Scope Rules

- Use `.tasks/` for assigned handoffs and verification work.
- Use `NEXT.md` for broad project priorities.
- Use `LEARNINGS.md` only for durable truths that should matter in future sessions.
- Do not silently delete completed tasks; move them under `## Completed` when the inbox gets long.
