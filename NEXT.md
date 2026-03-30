# Next — Hyperbot Session Handoff

Last updated: 2026-03-30

## Current State

**The v2 card-based dashboard is fully written into `dashboard.py` but has NEVER been tested in a browser.** All changes are uncommitted — nothing from the v2 work has been committed yet (15 modified files, 4 untracked).

The new UI replaces the old 3-column trading terminal with a card-based layout: token cards in a grid, `+` button to add tokens, expandable trade controls, notification center with educational "why" explanations, and unmanaged position detection. The React prototype (`prototype-dashboard.jsx`) was approved by the user as the design target.

## Just Completed (Across Two Sessions)

### Session 1 — Design + Implementation
- Ran trading-dashboard-critic on live dashboard, identified issues
- Defined new dashboard philosophy (card-based, educational, progressive disclosure)
- Built React prototype (`prototype-dashboard.jsx`) — user approved ("cooked Claude")
- Updated all docs (brand-brief, AGENTS.md, roadmap, CLAUDE.md, GEMINI.md)
- Implemented v2 HTML/CSS/JS in `templates/hyperbot-multi/scripts/dashboard.py` (full rewrite: -1733/+709 lines)
- Rewrote `launch_dashboard()` in `scripts/hyperbot.py` for credential-first flow
- Fixed `setup_complete` always-false bug (removed wizard dependency)
- Fixed trading loop crash on empty pairs (`get_candles("")` → 500)
- Fixed repo paths from Dropbox → `~/Projects/hyperbot`
- Removed dead credential-entry endpoints from dashboard

### Session 2 — Workspace Creation Fix
- Added `--empty` flag to `create_workspace.py` (replaces hacky PLACEHOLDER approach)
- Updated `launch_dashboard()` to use `--empty` instead of `--symbol PLACEHOLDER` + cleanup
- Fixed welcome notification JS condition (was gated on `!setup_complete` which is always true)
- Verified dry-run of `create_workspace.py --empty` produces clean manifest

## Key Files Changed (all uncommitted)

| File | What Changed |
|------|-------------|
| `templates/hyperbot-multi/scripts/dashboard.py` | Full HTML/CSS/JS rewrite — old wizard + 3-column layout → card grid + notification panel + add-token modal. Auto-setup logic. Empty-pairs guard. Removed dead endpoints. |
| `scripts/hyperbot.py` | `launch_dashboard()` rewritten: Keychain creds check → `--empty` workspace → launch. |
| `scripts/create_workspace.py` | Added `--empty` flag for bare workspace creation (no pairs, no strategies). |
| `scripts/connect/server.py` | Wallet connect flow for EIP-6963 browser extensions. |
| `scripts/connect/wallet_connect.html` | WalletConnect UI page. |
| `AGENTS.md` | Rewrote dashboard section for v2 architecture + new design principles. |
| `docs/brand-brief.md` | Added "Dashboard Philosophy" section. |
| `docs/local-first-roadmap.md` | Dashboard v2 is priority #2, fixed stale template paths. |
| `CLAUDE.md`, `GEMINI.md` | Paths fixed: Dropbox → `~/Projects/hyperbot`. |
| `.netlify/netlify.toml`, `website/.netlify/netlify.toml` | Paths fixed. |
| `prototype-dashboard.jsx` | NEW — React visual prototype (reference only, not production). |

## Pending — Ready to Pick Up

1. **Test the new-user flow end-to-end in a browser.** This is the #1 priority — the dashboard has never been loaded in a browser since the v2 rewrite.
   - Check Keychain: `security find-generic-password -s hyperbot -a hyperbot.master_address -w`
   - If no creds: `cd ~/Projects/hyperbot && python3 scripts/hyperbot.py connect`
   - Delete any stale workspace: `rm -rf ~/Projects/hyperbot-workspace`
   - Launch: `cd ~/Projects/hyperbot && python3 scripts/hyperbot.py dashboard`
   - Expected: browser → empty card grid → `+` button → welcome notification → add token → card appears → bot watches
   - Fix whatever breaks, re-test.

2. **Fix runtime issues found during testing.** Known risks:
   - `hl_client.get_all_mids()` needs Hyperliquid SDK installed in workspace Python env
   - Connect module import relies on Python adding `scripts/` to sys.path
   - Strategy pack installation in `/api/add-pair` assumes pack files exist relative to workspace `ROOT`

3. **Commit all v2 changes.** Suggest: one commit for docs/config path fixes, one for the dashboard v2 rewrite.

4. **WalletConnect projectId** — still using default. Need one from cloud.reown.com.

5. **Testnet support** — wallet connect hardcodes Mainnet.

## Blockers

- **No browser testing done yet.** Dashboard HTML was written blind — layout, JS errors, API connectivity are all unverified.
- Cowork sandbox blocks npm/GitHub — deploy/install testing must happen in user's terminal.

## Decisions Made

- **Card-based dashboard approved** — user loved the prototype, don't revert to 3-column
- **`--empty` workspace** — `create_workspace.py --empty` creates clean workspace with no pairs/strategies
- **`setup_complete` always True** — trading loop starts immediately, empty pairs handled gracefully (3s wait + retry)
- **Welcome notification on empty pairs** — triggers when `pairs.length === 0` regardless of setup state
- **Credentials from Keychain only** — `launch_dashboard()` refuses without creds, no manual entry UI
- **Educational "why" explanations** are the primary UX differentiator
