# Status

Last updated: 2026-04-15

## Done

- **2026-04-15**: Diagnosed and fixed erratic trade cascade (SL oscillation death spiral):
  - Root cause: double-rounding in position_manager + hl_client causing "float_to_wire" errors
  - Fixed `_sig5()` in position_manager.py to return unrounded prices (no double-rounding)
  - Fixed `round_price()` in hl_client.py: changed from flawed 5-sig-fig to safe 8-decimal precision
  - Added 10-second cooldown in dashboard.py to prevent cascading SL retry failures
  - Created diagnostic doc: `docs/SL_OSCILLATION_FIX.md`
- Added Fibonacci retracement strategy pack (`fib_retracement`) with auto Fib level selection and ATR-adaptive parameters.
- Fixed compounding SL bug: ATR-expansion now uses `initial_stop` instead of the drifting `stop_trigger`.
- Migrated landing page from Netlify to Hostinger (`hyperbot.enseris.com`). DNS nameservers updated from Spaceship to Hostinger; propagation pending.
- Added `.hostinger.json` for SSH-based deploys.

## In Progress

- **Growth mode implemented, awaiting first live test.** Launch: `hyperbot dashboard --mode growth --live --confirm-risk`
- Monitor SL management after fixes: verify no cascading "SL_MOVE → MANAGE_FAIL" chains in trade journal over next 24-48h
- DNS propagation for `enseris.com` (nameservers switched to Hostinger 2026-04-15). Once propagated: add subdomain in hPanel, install SSL.
- Validating auto-strategy routing against live realized-trade evidence.

## Blocked

- `hyperbot` CLI not on PATH — user got `command not found`. Needs PATH fix or `install.sh` re-run before growth mode can launch.
- SSL for `hyperbot.enseris.com` — requires DNS propagation to complete.
- Realized fills attribution is incomplete when the dashboard runtime is not active and logging.

## Recent Changes

- `position_manager.py`:
  - `_sig5()` now returns unrounded prices (delegates to hl_client.round_price())
  - Prevents double-rounding that was causing "float_to_wire causes rounding" errors
- `hl_client.py`:
  - `round_price()` changed from 5-significant-figure algorithm to 8-decimal precision
  - Matches Hyperliquid SDK's safe float serialization
  - Preserves detail for small-value coins (ZEC, NEAR, etc.)
- `dashboard.py`:
  - Added `_last_sl_fail_ts` tracking in managed position state
  - 10-second cooldown prevents cascading SL replacement retries
  - Clears cooldown timestamp on successful SL move
- `strategy-packs/fib_retracement/` — new pack: auto-selects nearest Fib level (0.236–0.786)
- Website hosting moved from Netlify to Hostinger. Files deployed to `domains/hyperbot.enseris.com/public_html/`.
- **Growth mode** added across `scalp_strategy_v2.py`, `position_manager.py`, `dashboard.py`, `operator-policy.json`:
  - 2% risk, 4× leverage, 1.2× RVOL, 1.5R TP, 15m cooldowns, 12 default liquid pairs
  - Activated via `--mode growth` flag or `HYPERBOT_MODE=growth` env var
  - Configurable trailing stop params in position_manager (0.3R gap in growth vs 0.5R default)
