# Claude Brief

Primary objective for this repo:

- reduce required human approvals by at least 95%
- preserve safe defaults for live trading
- make the generated workspace runnable 100% local without LLM API tokens when possible

## What Matters

- the local CLI should be the primary interface
- the generated trading workspace should stay assistant-agnostic
- core generation and revision logic should prefer deterministic local code over model calls
- any model-assisted step should become optional, not required

## Current Reality

- workspace generation is already local Python
- strategy-pack installation is local file templating
- token-specific revision is already heuristic code plus market data fetches
- the repo does not currently require OpenAI or Anthropic API tokens for its main scripts

## What To Look For

1. Places where a human must approve a safe action that could instead be gated by explicit local policy.
2. Places where a model is doing work that deterministic local code, rules, scoring, or templates can do.
3. A clean separation between:
   - local deterministic execution
   - optional assistant guidance
   - explicitly high-risk live-trading actions
4. A path to run the whole workflow from a local CLI without cloud model dependency.

## Preferred Direction

- local-first CLI as the primary execution surface
- policy files for unattended-safe actions
- deterministic revisions, scoring, and validations
- optional model overlay only for explanation, summarization, or operator UX
- cached or local market-data workflows where practical

## Deliverable Style

When proposing changes, prioritize:

1. concrete architecture changes
2. repo-level implementation steps
3. risk controls that keep live trading opt-in
4. ways to remove approval friction without weakening safety

Start from [`docs/local-first-roadmap.md`](/Users/gaston/Library/CloudStorage/Dropbox/--%20PROJECTS/Plugins/hyperbot/docs/local-first-roadmap.md).
