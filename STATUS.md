# Status

Last updated: 2026-04-16

## Done

- **2026-04-16**: Fixed `round_price()` — root cause of 226 overnight "Order has invalid price" rejections.
  - Bug: `round_price()` rounded to 8 decimal places, but Hyperliquid requires ≤5 significant figures. Market order slippage produced prices like 84.22905 (7 sig figs) → rejected.
  - Fix: Now uses `floor(log10(price))` to compute exact decimal count for 5 sig figs, capped at 8 for SDK wire compat.
  - Verified against all price ranges: BTC $84K, ETH $2.3K, SOL $84, kPEPE $0.008.
- **2026-04-16**: Enhanced daily email report suggestions section — now includes rejection diagnosis with counts, underperforming coin flags, downtime estimation, fee drag warnings, strong performer callouts.
- **2026-04-16**: Added growth/preservation mode toggle in dashboard header — clickable badge, `POST /api/mode` endpoint, reconfigures all risk params live without restart.
- **2026-04-16**: Replaced hardcoded `DEFAULT_LIQUID_PAIRS` with dynamic `fetch_top_liquid_pairs()` — fetches top 12 perps by 24h notional volume ($5M min) from Hyperliquid API. Falls back to BTC/ETH/SOL/DOGE/SUI/PEPE.
- **2026-04-16**: Fixed `hyperbot` CLI — created wrapper at `~/.local/bin/hyperbot` pointing to dev repo.
- **2026-04-16**: Pushed all pending changes (growth mode, SL fix, reporting, website) and deployed website to Hostinger.
- **2026-04-15**: Implemented growth mode, SL oscillation fix, Fib strategy pack, SimpleX reporting pipeline. See previous STATUS entries for detail.

## In Progress

- **Growth mode live trading** — bot is running on port 53488 in growth mode, but needs dashboard restart to pick up the `round_price` fix. After restart, orders should succeed.
- Monitor SL management post-fix: verify no cascading retry chains in trade journal.
- DNS propagation for `hyperbot.enseris.com` — still not resolving as of 2026-04-16. Files deployed on server and ready.

## Blocked

- SSL for `hyperbot.enseris.com` — requires DNS propagation to complete.
- Realized fills attribution incomplete when dashboard wasn't running (gap in trade journal coverage).

## Recent Changes

- `hl_client.py`: `round_price()` rewritten from 8-decimal to 5-significant-figure algorithm
- `dashboard.py`: growth/preservation toggle badge in header, `POST /api/mode` endpoint, dynamic liquid pairs via `fetch_top_liquid_pairs()`
- `trade_journal.py`: `_learning_notes()` rewritten with rejection diagnosis, downtime estimation, fee drag, per-coin performance analysis
- Workspace scripts synced: `hl_client.py`, `dashboard.py`, `scalp_strategy_v2.py`, `position_manager.py` all copied to `hyperbot-workspace/scripts/`
