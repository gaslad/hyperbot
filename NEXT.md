# Next — Hyperbot Session Handoff

Last updated: 2026-03-31

## Just Completed
- Confirmed the BTC card `Go Live` path now auto-enables both `STATE.live_enabled` and `STATE.trading_active`, and the trade log records `Live trading auto-enabled (first card went live)`.
- Fixed a deadlock in [templates/hyperbot-multi/scripts/dashboard.py](/Users/gaston/Projects/hyperbot/templates/hyperbot-multi/scripts/dashboard.py) at line 99 by changing the global state lock to `threading.RLock()`. The `trading_live` toggle was hanging because `/api/pair-settings` logged while already holding the lock.
- Fixed trigger-order payload formatting in [templates/hyperbot-multi/scripts/hl_client.py](/Users/gaston/Projects/hyperbot/templates/hyperbot-multi/scripts/hl_client.py) at line 629 by sending `triggerPx` as a float instead of a string.
- A real blaze signal fired during verification on mainnet: the entry filled, SL placement failed on the pre-fix code path, and the failsafe flatten logic immediately closed the position. This proved the live execution branch was reachable and exposed the trigger bug.
- Hyperbot was launched successfully in explicit live mode with `python3 scripts/hyperbot.py dashboard /tmp/hyperbot-workspace --live --confirm-risk --port 8765`.

## Pending — Ready to Pick Up
1. Re-run one real blaze trade after the `triggerPx` fix and confirm both SL and TP trigger orders are accepted by Hyperliquid. Start from `/tmp/hyperbot-workspace` or recreate a clean workspace, then watch the next live BTC blaze signal.
2. Add position monitoring to `scalp_v2` so partial fills and the `move SL to breakeven after TP1` flow are actually managed instead of only placing entry + TP/SL orders.
3. Update the dashboard Live Analysis to show passing conditions as well as rejection reasons.
4. Backtest `scalp_v2`; the current `backtest.py` only supports legacy strategies.

## Blockers
- Real end-to-end verification depends on live market conditions. Blaze must emit another `TRADE` signal before the fixed trigger-order path can be confirmed on-chain.
- Default workspace creation targets `/Users/gaston/Projects/hyperbot-workspace`, which was outside the writable sandbox in this session. I used `/tmp/hyperbot-workspace` for testing.
- Launching the local dashboard server required escalation because binding `127.0.0.1:8765` was denied inside the sandbox.

## Decisions Made
- Blaze RVOL is currently back at `rvol_min = 0.5`; the next live verification should confirm whether this suppresses trades too aggressively again.
- Keep dashboard per-pair risk % as the source of truth for blaze sizing.
- Keep live auto-enable on the first card `Go Live` action, but rely on the per-card confirmation dialog as the safety gate.
- Use `threading.RLock()` for dashboard state because handlers can legitimately log while mutating state.
