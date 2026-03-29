# Hyperbot — Repo Structure Assessment

_Generated 2026-03-29_

---

## Repo Type Assessment

- **Primary repo type:** Automation/tooling — a CLI-driven workspace generator and runtime for algorithmic trading
- **Secondary characteristics:** Has a web app embedded inside a template (dashboard.py is a full SPA), strategy-pack plugin system, and multi-agent coordination layer
- **Confidence:** High
- **Evidence:** `scripts/hyperbot.py` CLI entrypoint, `scripts/create_workspace.py` generator, `templates/workspace/` skeleton that gets copied into standalone workspaces, `strategy-packs/` plugin directory, `.tasks/` multi-agent queue, no package.json or pyproject.toml (pure scripts, no package distribution)

---

## Current Structure Summary

```
hyperbot/
├── .github/workflows/         # CI triggers for Codex + Gemini task automation
├── .tasks/                    # Multi-assistant task queue (Claude, Codex, Gemini)
├── assets/                    # Empty directory (dead)
├── docs/                      # Roadmap, architecture, release-readiness
├── scripts/                   # CLI entrypoint + workspace generator + validators
│   └── connect/               # Web3 wallet connection module
├── skills/                    # ChatGPT Skill package (1 skill)
├── strategy-packs/            # 3 installable strategy definitions
│   ├── trend_pullback/
│   ├── compression_breakout/
│   └── liquidity_sweep_reversal/
├── templates/workspace/       # Workspace skeleton (copied per workspace)
│   ├── config/                # Markets, policy, env, strategies
│   ├── data/                  # Empty (cache placeholder)
│   ├── docs/                  # Strategy selection guide
│   ├── research/              # Research artifacts placeholder
│   ├── scripts/               # Runtime Python (dashboard, signals, hl_client, backtest, etc.)
│   ├── strategies/            # Strategy template skeleton
│   └── system/                # Context, contracts, memory for assistants
├── AGENTS.md                  # Agent-agnostic instructions
├── CLAUDE.md                  # Claude-specific brief
├── GEMINI.md                  # Gemini-specific brief
├── README.md                  # User-facing docs
└── install.sh                 # curl-pipe installer
```

**88 files total. ~6,400 lines of code.** Dashboard.py alone is 2,181 lines (34% of the codebase).

---

## Structural Problems Found

### 1. dashboard.py is a monolith (Critical)

At 2,181 lines, `dashboard.py` contains the full HTTP server, all API endpoints, the trading loop, state management, the complete HTML/CSS/JS single-page app, the wizard flow, the add-pair modal, the backtest integration, and the settings panel. This is the hardest file in the repo to work on for any agent — every edit requires loading 2K+ lines of context, and changes to CSS can break Python string escaping.

**Impact:** Every agent touching the dashboard burns tokens re-reading 2K lines. Merge conflicts are likely when multiple agents edit it. A single syntax error in an f-string kills the whole dashboard.

### 2. `__pycache__` committed to repo

Three `__pycache__/` directories exist despite `.gitignore` listing them. They were likely committed before the gitignore rule was added. These are dead weight — bytecode is machine-specific and should never be in version control.

### 3. Dead/empty directories

- `assets/` — empty, no references anywhere
- `templates/workspace/data/` — empty placeholder
- `templates/workspace/strategies/_template/risk/` — empty

### 4. Three agent instruction files with overlapping content

`CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` all describe the same project objectives, architecture, and design principles with slight variations. When the architecture changes, three files need updating. Drift is inevitable.

### 5. `templates/workspace/system/` is over-engineered

The `system/context/`, `system/contracts/`, and `system/memory/` subdirectories add 3 levels of nesting for what amounts to 3 small markdown files. These are assistant-context files that could live flat in the workspace root or a single `system/` directory.

### 6. No dependency manifest

No `requirements.txt`, `pyproject.toml`, or `setup.py`. The repo depends on `hyperliquid-python-sdk` and standard library modules, but there's no way for a new contributor or agent to know what to install. `install.sh` exists but only handles the GitHub clone, not Python dependencies.

### 7. Tests live inside the template, not the repo

`test_hl_client.py` and `test_signals.py` are inside `templates/workspace/scripts/` — meaning they're shipped into every generated workspace. Tests for the generator itself (`create_workspace.py`, `hyperbot.py`) don't exist.

### 8. No clear separation between "repo code" and "workspace runtime"

`scripts/hyperbot.py` is repo code. `templates/workspace/scripts/dashboard.py` is workspace runtime. But `hyperbot.py` imports and calls into the workspace scripts at runtime, blurring the boundary. When a workspace is generated, the template files are copied — but the running dashboard is a live copy, not the template. This causes the "can't see updates on site" problem (edits to template don't propagate to existing workspaces).

### 9. Strategy packs have redundant README nesting

Each strategy pack has `README.md` at the pack root AND `templates/strategy/README.md`. The inner README describes the same strategy the outer one does.

### 10. `.gitignore` is minimal

Only ignores `__pycache__/`, `*.pyc`, and `.DS_Store`. Missing: `.env`, `*.log`, `data/`, credential files, generated workspaces (`hyperbot-*/`), IDE configs.

---

## Recommended Target Structure

```
hyperbot/
├── .github/workflows/
│   ├── codex-inbox.yml
│   └── gemini-inbox.yml
├── .tasks/
│   ├── PROTOCOL.md
│   ├── _log.md
│   ├── claude.md
│   ├── codex.md
│   └── gemini.md
├── docs/
│   ├── architecture.md
│   ├── local-first-roadmap.md
│   └── release-readiness.md
├── scripts/                          # Repo-level CLI tools
│   ├── hyperbot.py
│   ├── create_workspace.py
│   ├── release_readiness.py
│   ├── validate_apply_revision.py
│   └── connect/
│       ├── __init__.py
│       └── server.py
├── strategy-packs/
│   ├── manifest.json
│   ├── trend_pullback/
│   │   ├── pack.json
│   │   ├── README.md
│   │   └── templates/
│   ├── compression_breakout/
│   │   ├── pack.json
│   │   ├── README.md
│   │   └── templates/
│   └── liquidity_sweep_reversal/
│       ├── pack.json
│       ├── README.md
│       └── templates/
├── templates/workspace/
│   ├── config/
│   │   ├── env/.env.example
│   │   ├── markets/hyperliquid_perps.json
│   │   ├── policy/operator-policy.json
│   │   └── strategies/
│   ├── scripts/
│   │   ├── dashboard/                # ← SPLIT: dashboard becomes a package
│   │   │   ├── __init__.py           #    (server.py, html.py, api.py, trading.py, state.py)
│   │   │   ├── server.py             #    HTTP server + main()
│   │   │   ├── state.py              #    TradingState, PairState
│   │   │   ├── trading.py            #    trading_loop()
│   │   │   ├── api.py                #    API endpoint handlers
│   │   │   └── ui.py                 #    HTML/CSS/JS generation
│   │   ├── hl_client.py
│   │   ├── signals.py
│   │   ├── backtest.py
│   │   ├── apply_revision.py
│   │   └── profile_symbol_strategy.py
│   ├── system/
│   │   ├── project-context.md
│   │   ├── signal-contract.md
│   │   └── project-memory.md
│   └── README.md
├── tests/                            # ← MOVED: repo-level tests
│   ├── test_hl_client.py
│   ├── test_signals.py
│   ├── test_create_workspace.py      # ← NEW
│   └── test_hyperbot_cli.py          # ← NEW
├── AGENTS.md                         # Single source of truth for all agents
├── README.md
├── requirements.txt                  # ← NEW
├── install.sh
└── .gitignore                        # ← EXPANDED
```

### Key changes:
1. **Split dashboard.py** into a `dashboard/` package (5 files ~400-500 lines each instead of 1 file at 2,181)
2. **Consolidate agent docs** into one `AGENTS.md` — move Claude/Gemini-specific quirks into `.tasks/` protocol or inline comments
3. **Move tests** to repo-level `tests/` directory
4. **Add `requirements.txt`**
5. **Flatten `system/`** — remove unnecessary `context/`, `contracts/`, `memory/` subdirectories
6. **Remove dead directories** (`assets/`, empty placeholders)
7. **Remove committed `__pycache__/`**
8. **Expand `.gitignore`**

---

## Why This Structure Fits

**Dashboard split** is the highest-leverage change. The file is edited more than any other, by all three agents, and it's 34% of the codebase in one file. Splitting by responsibility (state, trading loop, API handlers, UI generation) means agents only load the module they need. A CSS change doesn't require reading the trading loop.

**Single AGENTS.md** because the three current files (CLAUDE.md, AGENTS.md, GEMINI.md) have 80%+ overlap. The per-agent differences are minor (Claude's repo path hint, Gemini's context style). These should be in the task protocol, not duplicated across three root-level files.

**Repo-level tests** because test files inside `templates/workspace/` get shipped to every generated workspace where they serve no purpose. Tests should validate the repo code and live where agents expect them.

**requirements.txt** because `hyperliquid-python-sdk` is a hard dependency that isn't documented anywhere. An agent spinning up a fresh env can't run anything without guessing the deps.

---

## Minimal Required Changes

These are ordered by impact-to-effort ratio. Do them first:

1. **Remove committed `__pycache__/` directories** — `git rm -r --cached` on all three locations
2. **Expand `.gitignore`** — add `.env`, `*.log`, `data/`, `hyperbot-*/`, `.vscode/`, `.idea/`, `credentials/`
3. **Add `requirements.txt`** — even if it's just `hyperliquid-python-sdk`
4. **Delete empty `assets/` directory**
5. **Consolidate CLAUDE.md + GEMINI.md into AGENTS.md** — keep CLAUDE.md only for the repo-path hint and task-queue section (which are already in AGENTS.md)
6. **Add `hyperbot update` command** — copies template files into an existing workspace so edits to the template actually propagate (this is the root cause of "can't see updates on site")

---

## Optional Future Additions

These become relevant only if the repo grows:

- **Split dashboard.py into a package** — worth doing when the file next needs a major feature addition (it's the highest-value refactor but also the most disruptive)
- **Move tests to `tests/`** — do this when adding CLI tests for hyperbot.py and create_workspace.py
- **`docs/decisions/`** — if major architectural trade-offs start recurring (e.g., "why local-only", "why no WebSocket"), capture them as decision records
- **Strategy pack SDK docs** — if third-party pack authors ever become a thing, document the pack.json schema and template token system
- **`scripts/sync_workspace.py`** — a dedicated tool to diff template vs. workspace and patch selectively (more robust than the `cp` workaround)

---

## Multi-LLM Collaboration Recommendation

**Use: `AGENTS.md` only.** Remove `CLAUDE.md` and `GEMINI.md` as separate root files.

**Why:**
- The repo already has a mature multi-agent system (`.tasks/PROTOCOL.md`, per-agent inboxes, CI triggers). That system is the right place for agent-specific instructions.
- Three overlapping root docs means three files to update when architecture changes. All three agents read all three files anyway — they're just burning tokens on redundant context.
- The only Claude-specific content worth keeping is the repo path (`~/Library/CloudStorage/Dropbox/...`) — that belongs in a `.tasks/claude.md` header or `AGENTS.md` footnote, not a whole separate file.
- `SKILL.md` exists in `skills/bootstrap-trading-workspace/` and is fine there — it's a ChatGPT skill definition, not a repo-level doc.

---

## Risk Flags

1. **dashboard.py monolith** — This is the single biggest structural risk. At 2,181 lines in a string-embedded SPA, one misplaced quote breaks the entire dashboard. Every agent session that touches it loads 2K lines of context. As features keep getting added (add-pair modal, backtest UI, strategy tuning), this file will grow past 3K lines and become genuinely painful.

2. **Template-vs-workspace divergence** — There is no mechanism to sync template changes into existing workspaces. Every template edit creates a silent divergence. The user already hit this ("can't see updates on site"). This will keep happening.

3. **No CI tests** — The GitHub workflows only trigger agent task queues. There are no automated tests that run on push/PR. The two test files exist but aren't wired into any CI pipeline.

4. **Credential handling in a Dropbox-synced repo** — The repo lives in Dropbox. While credentials are stored in Keychain (good), any accidental commit of credential files would sync to Dropbox and potentially other devices. The `.gitignore` should explicitly exclude credential patterns.

5. **Single-branch development** — Active work is on `feature/web3-wallet-connect` but all recent changes (multi-pair, dashboard, backtest) are also landing there. This branch name no longer describes what it contains, which will confuse agents looking at git history.
