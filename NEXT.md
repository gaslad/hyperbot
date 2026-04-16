# Next

Last updated: 2026-04-16

## Just Completed

- **Fixed root cause of 0 trades overnight**: `round_price()` produced >5 significant figures on slippage-adjusted prices. Hyperliquid rejected all 226 attempts with "Order has invalid price." Now enforces 5 sig figs.
- **Growth/preservation toggle** in dashboard header — clickable badge switches mode live, reconfigures risk params, adds missing liquid pairs.
- **Dynamic liquid pairs** — `fetch_top_liquid_pairs()` replaces hardcoded list, fetches top 12 by 24h volume from Hyperliquid API.
- **Enhanced daily report** — Suggestions section now includes rejection diagnosis with counts, underperforming coin flags, downtime estimation, fee drag warnings.
- **Fixed CLI** — `~/.local/bin/hyperbot` wrapper created, `hyperbot --help` works.

## Pending — Ready to Pick Up

1. **Restart dashboard to pick up `round_price` fix** — the running instance (PID 82779, port 53488) still has the old 8-decimal code. Kill stale instances (PIDs 11388, 13439) and restart:
   ```
   kill 11388 13439 82779
   hyperbot dashboard --mode growth --live --confirm-risk
   ```
   Then monitor for 2-4 hours. Orders should now succeed. Watch for:
   - First successful FILLED entry in trade log
   - SL placement succeeding (not "invalid price")
   - Growth mode toggle badge showing correctly in header

2. **Verify DNS for `hyperbot.enseris.com`** — still not resolving. Check Hostinger hPanel: ensure subdomain exists and A record points to 82.197.83.159. If NS propagation is stuck, try adding A record manually.

3. **Remove ZEC from active pairs** — daily report flagged 0W/5L, -$10.24 net. Either remove it or tighten its filters. Check if it's being auto-added by `fetch_top_liquid_pairs()` (it was #5 by volume yesterday).

4. **[improvement] Add report sections proposed but not implemented**:
   - Missed Opportunities (signals that passed filters but were exchange-rejected)
   - System Health (API latency, error rate, uptime %)
   - Risk Exposure (peak notional, distance to daily halt)
   - Hourly Heatmap (which hours produce best P&L)
   - Slippage Report (entry price vs signal price delta)

5. **[improvement] Strategy attribution gap** — fills that happen while dashboard is restarting can't be attributed to a strategy. Consider persisting pending-order state to disk so attribution survives restarts.

## Blockers

- Dashboard must be restarted for price fix to take effect — until then, orders will continue to be rejected
- DNS for `hyperbot.enseris.com` still not resolving

## Decisions Made

- `round_price()` uses 5 significant figures (not 8 decimal places) — this matches Hyperliquid's documented price requirements. The previous "8 decimal" approach was a workaround for double-rounding that masked the real issue.
- Dynamic liquid pairs replaces hardcoded list — pairs rotate based on real 24h volume, not a static curated list. $5M daily volume minimum filters out illiquid assets.
- Growth mode toggle is a UI badge, not a settings panel item — keeps it visible and one-click accessible since it changes fundamental risk parameters.
- Three stale dashboard instances accumulated overnight because new launches don't kill old ones. Consider adding a PID file or port lock.

## Assumptions

- `AGENTS.md` is the single source of truth for repo instructions
- Workspace script sync pushes template changes to active workspaces on restart
- The `round_price` fix will resolve the majority of order rejections (226/239 were "invalid price")
