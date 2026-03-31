# Next — Hyperbot Session Handoff

Last updated: 2026-03-31

## Current State

**Scalp strategy v2 is integrated and running live on the dashboard.** BTC, ETH, SOL, HYPE tested — strategy evaluates every 15s, shows live regime analysis (ADX, VWAP, RVOL, CVD, choppiness), signal strength proximity bar, and rejection reasons. No trades have fired yet (correct — market conditions haven't met all 8 regime filters simultaneously).

Dashboard v2 (card-based UI) is fully functional. Workspace script sync is in place — template changes auto-propagate on dashboard restart.

## Just Completed (This Session)

### Scalp Strategy v2 Integration
- Added `scalp_strategy_v2.py` to workspace template — standalone 5m/15m breakout strategy (~890 lines)
- Added `scalp_strategy_v2_prompt.py` — full strategy rules reference
- Created `strategy-packs/scalp_v2/` with pack.json and config template
- Extended `hl_client.py`: `get_best_bid_ask()`, `update_leverage()`, `place_trigger_order()` (TP/SL with explicit limit prices)
- Added `_run_scalp_v2_cycle()` to dashboard trading loop — branches on `pack_id == "scalp_v2"`
- Full execution flow: set leverage → entry (ALO/IOC) → SL trigger → TP1 partial (30% at 1R) → TP final (70% at 1.8R)
- Failsafe: if SL placement fails, position is immediately flattened

### Dashboard Improvements
- Signal strength now shows regime proximity (0–70%) even on NO_TRADE signals
- Live analysis dot colors: red for failing conditions, green for passing
- "5m Scalper" appears first in strategy picker

### Blaze Scalp (Test Strategy)
- Added `blaze_scalp.py` — ultra-fast 1m test scalper for verifying execution pipeline
- Minimal filters (RVOL ≥ 0.5x, spread < 0.1%, 5-candle micro-breakout), no time/ADX/VWAP gates
- 1:1 R:R with 1 ATR stop/target — should fire and resolve within minutes
- Wired into dashboard with `pack_id == "blaze_scalp"`, appears first in strategy picker

### Infrastructure
- `hyperbot.py` auto-syncs template scripts into workspace on every launch (no more stale copies)
- Fixed candle column mapping: Hyperliquid returns `o,h,l,c,v` → renamed to `open,high,low,close,volume`
- Adjusted time windows for AEST operator (UTC+10): nearly 24h coverage, only 06:00–08:00 UTC blocked

### Docs
- Updated AGENTS.md with scalp_v2 architecture, hl_client extensions, workspace sync docs
- Fixed branch reference (was `feature/web3-wallet-connect`, now `main`)

## Key Files Changed

| File | What Changed |
|------|-------------|
| `templates/hyperbot-multi/scripts/scalp_strategy_v2.py` | NEW — full scalp strategy module |
| `templates/hyperbot-multi/scripts/scalp_strategy_v2_prompt.py` | NEW — strategy rules reference |
| `templates/hyperbot-multi/scripts/blaze_scalp.py` | NEW — 1m test scalper for pipeline verification |
| `templates/hyperbot-multi/scripts/dashboard.py` | Scalp v2 integration, signal proximity, improved live analysis |
| `templates/hyperbot-multi/scripts/hl_client.py` | Trigger orders, leverage, bid/ask |
| `scripts/hyperbot.py` | Workspace script sync on launch |
| `strategy-packs/scalp_v2/` | NEW — pack definition + config |
| `AGENTS.md` | Scalp v2 docs, branch fix |

## Pending — Next Session

1. **Run live with `--live --confirm-risk` and monitor first trade execution.** Strategy is evaluating correctly but no trade has fired yet. Need to observe during peak hours (08:00–17:00 UTC / 18:00–03:00 AEST) when RVOL and ADX conditions are more likely to be met.

2. **Add position monitoring to scalp_v2 cycle.** Currently the strategy places entry + TP/SL but doesn't monitor for partial fills, SL adjustments after TP1 hit (move SL to breakeven), or trailing stop logic. The TP/SL triggers handle basic exits but the "move stop to breakeven after TP1" flow needs implementation.

3. **Dashboard: show passing conditions too.** Currently only rejection reasons are shown in Live Analysis. When 5/8 conditions pass, show the 5 green ones alongside the 3 red ones for better transparency.

4. **Backtest scalp_v2.** The existing `backtest.py` only supports legacy strategies. Needs a parallel path for scalp_v2 using 5m/15m candle data.

5. **Commit the 2 unpushed commits + this session's work, push to GitHub.**

## Decisions Made

- **RVOL threshold stays at 1.5x** — operator decision, no reduction for testing
- **Time windows expanded for AEST** — nearly 24/7, only 06:00–08:00 UTC dead zone
- **Template script sync** — workspace scripts are always overwritten from templates on launch (user config in workspace manifest is preserved)
- **Signal proximity score** — counts passing regime conditions / 8, scaled to 70% max (setup validation needed for higher)
