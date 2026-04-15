# Next

Last updated: 2026-04-15

## Just Completed

- Implemented **growth mode** — a switchable aggressive trading preset for fast account growth
- `scalp_strategy_v2.py`: added `growth_config()` and `preservation_config()` factory functions
  - Growth: 2% risk, 4× max leverage, 1.2× RVOL filter, 1.5R final target, 5% daily halt, 7 session losses
  - Preservation: unchanged original defaults (0.5% risk, 2× leverage, 1.8R target)
- `position_manager.py`: made trailing stop configurable via `ManagementState` fields
  - New fields: `trail_gap_r`, `trail_min_lock_r`, `trail_activation_r`, `trail_ratchet_threshold_r`
  - Defaults unchanged (backwards-compatible); growth mode passes tighter values (0.3R gap vs 0.5R)
- `dashboard.py`: full growth mode integration
  - `--mode growth|preservation` CLI flag + `HYPERBOT_MODE` env var
  - Mode-dependent: 15m cooldown (was 30m), 45m reentry lockout (was 2hr), 6 scan batch (was 3), 3× default leverage
  - `DEFAULT_LIQUID_PAIRS`: BTC, ETH, SOL, DOGE, SUI, PEPE, WIF, LINK, AVAX, ARB, HYPE, XRP — auto-registered in growth mode
  - Growth mode overrides STATE risk params and prints mode banner on startup
- `operator-policy.json`: added `growth_bands` alongside `safe_bands`, `default_liquid_pairs` list, bumped to version 2
- `AGENTS.md`: documented Trading Modes section with full parameter comparison table

## Pending — Ready to Pick Up

1. **Fix `hyperbot` CLI path** — user got `command not found: hyperbot` when trying to launch growth mode. Either `~/.local/bin` isn't on PATH or the symlink was never created by `install.sh`. Check `~/.local/bin/hyperbot` exists and `echo $PATH` includes it. May need `source ~/.zshrc` or re-run `install.sh`.
2. **First live growth mode test** — once CLI works, launch with `hyperbot dashboard --mode growth --live --confirm-risk` and monitor the first 4-6 hours. Watch for:
   - Correct pair auto-loading (should see 12 pairs registered on startup)
   - Strategy using 2% risk and 4× leverage cap
   - Tighter trailing stops activating at 0.4R instead of 0.5R
   - 15-minute cooldowns between trades on the same pair
3. **Monitor SL fixes from previous session** — the SL oscillation fix is still untested in production. Growth mode's higher frequency will stress-test it faster.
4. Verify `hyperbot.enseris.com` DNS propagation (from previous session — may be resolved by now).
5. Consider adding a dashboard UI toggle for growth/preservation mode so it can be switched without restarting.

## Blockers & Warnings

- `hyperbot` CLI not on user's PATH — blocks all launch attempts until resolved
- Growth mode is code-complete but **untested in live trading** — first session should be monitored closely
- All growth mode changes are in templates; existing workspaces need a dashboard restart to pick up the sync

## Decisions Made

- Growth mode is a separate preset, not a modification of defaults — `preservation_config()` returns the exact original settings, so switching back is lossless
- RVOL was the only regime filter relaxed (1.5× → 1.2×); all other quality filters (ADX, choppiness, EMA, CVD) stay strict in both modes
- Default liquid pairs list is curated for Hyperliquid specifically: all 12 have tight spreads and deep L2 books as of April 2026
- Trail parameters are passed through `ManagementState` rather than module-level constants, keeping position_manager stateless and testable

## Assumptions

- `AGENTS.md` is the single source of truth for repo instructions
- Workspace script sync (`hyperbot.py dashboard`) will push template changes to active workspaces on next restart
- The 12 default pairs remain liquid on Hyperliquid — should be re-validated if market conditions change significantly
