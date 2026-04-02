# Next — Hyperbot Session Handoff

Last updated: 2026-04-02

## Just Completed
- Added pair-level auto strategy routing in [dashboard.py](/Users/gaston/Projects/hyperbot/templates/hyperbot-multi/scripts/dashboard.py): cards can now run in `auto` mode, evaluate `scalp_v2` plus the legacy packs every cycle, and prefer the strongest live setup instead of relying on a stale manual preselection.
- Added educational card explainers in [dashboard.py](/Users/gaston/Projects/hyperbot/templates/hyperbot-multi/scripts/dashboard.py): each card now shows a collapsed footnote plus an expanded `Bot View` with what the bot sees, why it is acting or waiting, and the active risk guardrails.
- Updated the add-token modal to recommend `Auto-pick best setup`, while manual override still exists per card for pinning a specific strategy.
- Relaunched the live dashboard from `/tmp/hyperbot-workspace` on `http://127.0.0.1:8765`; the current launcher session in this Codex run is `73959`.

## Pending — Ready to Pick Up
1. Verify the new auto router on live scans by watching `/api/state` or the dashboard UI and confirming each pair’s `selected_pack_id`, `bot_note`, and `last_signals` are coherent across BTC, ETH, SOL, HYPE, and TAO.
2. Decide whether `compression_breakout` and `liquidity_sweep_reversal` are good enough to keep in auto mode, or whether the router should be limited to `scalp_v2` plus one legacy strategy until there is better realized-trade evidence.
3. Commit the daily review loop into a persistent automation if the UI keeps failing to create it from the suggested directive. The intended schedule is daily at 8:00 AM Brisbane time.
4. Consider adding an explicit net-edge ranking model for legacy strategies so auto mode does more than compare raw confidence.

## Blockers
- There is no reliable realized-trade sample yet for the new auto selector. The ranking logic is heuristic and needs live observation before treating it as trustworthy.
- The automation UI has been flaky: the user reported the `Open` action did not create the automation even after regenerating the directive.
- Dashboard relaunch still requires escalation because binding the local server is restricted inside the sandbox.

## Decisions Made
- `Blaze Scalp` is excluded from auto selection and remains manual-only because it is still a pipeline/test strategy, not a candidate for live routing preference.
- Auto mode currently considers `scalp_v2`, `trend_pullback`, `compression_breakout`, and `liquidity_sweep_reversal`, then selects the strongest current signal using a simple rank based on direction, confidence, and pack preference.
- Each pair still obeys hard guardrails from the operator: max leverage stays capped at `2x`, risk stays capped per card, and margin mode remains explicit.
- Educational transparency is now part of the card surface, not hidden only in logs or the notification center.
