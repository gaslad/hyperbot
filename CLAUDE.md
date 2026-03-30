# Claude Brief

Use [AGENTS.md](/Users/gaston/Library/CloudStorage/Dropbox/-- PROJECTS/hyperbot/AGENTS.md) as the single source of truth for repo context, architecture, objectives, and coordination rules.

Claude-specific note:

- When giving shell commands to the user, include the repo `cd` explicitly:
  `cd ~/Library/CloudStorage/Dropbox/--\ PROJECTS/hyperbot && <command>`

## Session Start Checklist

On every session start, do these automatically (no user prompt needed):

1. Read `NEXT.md` if it exists — this is the handoff from the previous session
2. Run a pre-flight check (read the `pre-flight-check` skill) to surface sandbox/network constraints before committing to any approach that needs external services
3. Check if `.task-scoper-map.md` is stale (older than most recent file changes) — if so, update it using the `codebase-snapshot` skill

## Session End Checklist

Before ending any session:

1. Update `NEXT.md` with what was done, what's pending, and any blockers (use the `session-handoff` skill)
2. Update `AGENTS.md` if any new files/areas were created
