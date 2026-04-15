---
name: repo-improvement-loop
description: >
  End-of-session retrospective that captures durable learnings and suggests
  evidence-based improvements. Use when the user says "improvement loop",
  "what did we learn", "save learnings", "session review", "what went well",
  "what went wrong", "improvement pass", "capture this", "don't lose this",
  "save what we learned", "repo improvements", "optimise the repo", or any
  request to review the session's work and extract lasting value from it.
  Also trigger when a session covered significant ground (bug fixes, new
  features, workflow changes, production incidents) and the user wants to
  make sure nothing valuable gets lost. This skill does NOT restructure
  collaboration files or manage AGENTS.md — use repo-llm-agnostic for that.
---

# Repo Improvement Loop

A focused end-of-session pass that answers three questions:

1. **What did we get right?** — Wins, patterns, and decisions worth repeating.
2. **What went wrong or could be better?** — Failures, friction, and missed
   opportunities — with enough detail to act on next time.
3. **What should the repo remember?** — Durable learnings that belong in the
   codebase so they survive beyond this conversation.

## When to run this

After any session that involved meaningful work: shipping a feature, debugging
a production issue, refactoring code, setting up infrastructure, running an
audit, or any workflow that produced knowledge worth keeping. The value is in
the *extraction* — turning ephemeral session context into permanent repo value.

## What this skill does NOT do

This skill stays in its lane:

- **Does not restructure collaboration files.** No touching AGENTS.md,
  CLAUDE.md, GEMINI.md, or their relationships. That's `repo-llm-agnostic`.
- **Does not reorganise the repo.** No moving files, renaming directories, or
  changing folder structure. That's `repo-rescue` or `repo-structure-analyst`.
- **Does not rewrite STATUS.md or NEXT.md from scratch.** It may *suggest*
  updates to these files, but it presents suggestions for the user to approve
  rather than overwriting them.

## The pass — step by step

### 1. Review what happened

Look at what was done in this session. Sources of evidence:

- Files created, modified, or deleted
- Git history if available (`git log --oneline -20`, `git diff`)
- STATUS.md, NEXT.md, or equivalent state files
- Conversation context (what was discussed, what was tried, what failed)
- Any error messages, workarounds, or unexpected discoveries

Build a clear picture of the session's work before making judgments.

### 2. Identify wins

What worked well? Look for:

- **Good decisions** — choices that saved time, avoided bugs, or simplified
  the codebase. Note *why* they worked so the pattern can be repeated.
- **Effective workflows** — tools, scripts, or processes that made the work
  smoother. If something was built this session that could be reused, flag it.
- **Knowledge gained** — things the team now knows that it didn't before. API
  quirks, dependency behavior, platform constraints, user patterns.
- **Problems avoided** — near-misses or risks that were caught early. What
  caught them? Can that check be automated?

### 3. Identify failures and friction

What went wrong or took longer than it should have? Look for:

- **Repeated mistakes** — the same error happening more than once in a session
  is a strong signal that something needs documenting or automating.
- **Wasted time** — dead ends, wrong assumptions, missing documentation that
  forced guesswork. What would have prevented the waste?
- **Manual steps that should be automated** — anything done by hand more than
  twice is a candidate for a script.
- **Missing guardrails** — bugs or issues that could have been caught by
  tests, linting, validation, or CI checks but weren't.
- **Knowledge gaps** — places where the team had to guess because data,
  documentation, or telemetry was missing.

Be specific. "Tests could be better" is useless. "The swatch upload had no
validation that images met Amazon's 1500px minimum — we found out only after
rejection" is actionable.

### 4. Save durable learnings

Write to `LEARNINGS.md` (or the repo's existing durable-memory file). A
learning is durable if it's likely to matter next month, not just later today.

**Passes the bar:**
- Repeated production failure modes and their root causes
- Environment or dependency quirks (e.g., "Homebrew Python 3.14 blocks
  system-wide pip — always use venv")
- API behavior that isn't well-documented (rate limits, undocumented error
  codes, propagation delays)
- Workflow rules that reduced confusion across sessions or tools
- Product heuristics supported by real usage data
- Architecture decisions and *why* they were made

**Does not pass the bar:**
- Temporary TODO items (use NEXT.md)
- Session narration ("we tried X then Y then Z")
- Speculative ideas with no supporting evidence
- Anything already in code comments or commit messages

Format each learning as a standalone entry that makes sense without
conversation context. Include the date, the specific finding, and enough
detail that a cold-start session can act on it.

### 5. Suggest improvements

Generate a prioritized list grounded in evidence from this session. Separate
into categories so the user can triage by domain:

**Repo improvements** — missing tests, documentation gaps, dead code, file
organisation issues, CI/CD gaps, developer experience friction.

**App / product improvements** — bugs found, UX issues surfaced, performance
problems, feature gaps that came up during the work.

**Workflow / automation improvements** — manual steps that could be scripted,
processes that break across sessions, tool integrations that would save time.

**Open risks / data gaps** — things you suspect are problems but can't confirm
from the evidence. Be explicit about what's uncertain and what data would
resolve it.

For each suggestion, include:
- **What** — the specific improvement
- **Why** — the evidence from this session that supports it
- **Impact** — how much it would help (and what kind of help: speed, safety,
  quality, reliability)

## Writing rules

- **Evidence over opinion.** Every suggestion should point to something
  specific that happened. "We should add tests" is weak. "The swatch upload
  broke because there was no validation that the image was 1500px — a test
  for image dimensions would have caught this" is strong.
- **Concrete over vague.** Prefer commands, paths, and examples. If you're
  suggesting a script, sketch what it would do.
- **Honest about uncertainty.** If the evidence is weak, say "suspected" or
  "needs investigation." Don't present hunches as facts.
- **Compact.** Learnings should be scannable. One paragraph per entry, not an
  essay. Bullet points for improvement lists.

## Deliverables

Present to the user:

1. **Session review** — what went well, what didn't, with specifics.
2. **Durable learnings** — saved to LEARNINGS.md (or noted that nothing
   qualified this session).
3. **Prioritised improvement list** — repo, app, workflow, and open risks.
4. **Suggested updates** to STATUS.md and NEXT.md if the session's work
   changed what's current or what's next — presented as suggestions, not
   applied automatically.
