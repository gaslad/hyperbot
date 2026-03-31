# Next — Hyperbot Session Handoff

Last updated: 2026-03-31

## Just Completed (This Session)

### Blaze Scalp — RVOL Filter Removed
- **Problem:** BTC was never triggering because RVOL (current candle vol / 20-candle avg) was consistently 0.02x–0.37x, failing the 0.5x threshold
- **Fix:** Set `rvol_min = 0.0` in `blaze_scalp.py:50` — RVOL filter effectively disabled, price action alone decides
- **Also fixed:** Hardcoded `{0.5}` in log message at `blaze_scalp.py:96` — was showing wrong threshold in rejection logs

### Live Trading Auto-Enable (No More --live Flag Required)
- **Problem:** Dashboard required `--live --confirm-risk` CLI flags for orders to execute. Without them, `STATE.live_enabled = False` and all orders were silently dropped
- **Fix:** Two changes in `dashboard.py`:
  - `/api/pair-settings` handler (~line 2318): When a card's "Go Live" toggle is activated, auto-sets `STATE.live_enabled = True` with a log message
  - `/api/start` handler (~line 2267): Start Trading button also auto-enables `STATE.live_enabled`
- The per-card confirm dialog ("Go live on X? The bot will execute real trades...") still fires as a safety check

### Position Sizing — Dashboard Risk % Passthrough
- **Problem:** Blaze strategy used its own hardcoded `risk_per_trade_pct = 0.002` (0.2%) instead of the dashboard's per-pair risk setting (0.5–1%). With $99.80 equity, 0.2% = $0.20 risk → BTC position size rounded to zero
- **Fix:**
  - `blaze_scalp.py:321-323`: Now reads `market_data["risk_per_trade_pct"]` (from dashboard, as percentage) and converts to decimal. Falls back to config default if not present
  - `blaze_scalp.py:332`: Same passthrough for `max_leverage`
  - `dashboard.py:466-474`: `_run_blaze_cycle()` now passes `pair_risk` and `pair_leverage` from the dashboard's per-pair settings into `market_data`

## Key Files Changed

| File | Lines | What Changed |
|------|-------|-------------|
| `templates/hyperbot-multi/scripts/blaze_scalp.py` | 50, 96, 321-323, 332 | RVOL disabled, log fix, risk/leverage passthrough |
| `templates/hyperbot-multi/scripts/dashboard.py` | 466-474, 2267-2270, 2318-2323 | Risk passthrough to blaze, auto-enable live trading |

## Pending — Ready to Pick Up

1. **Verify the "Go Live" → auto-enable → order execution flow end-to-end.** The three fixes above haven't been tested together yet. Rebuild workspace (`rm -rf ~/Projects/hyperbot-workspace`) and restart dashboard. Add BTC, set risk to 0.5% or 1%, click "Go Live" on the BTC card. Confirm:
   - Log shows "Live trading auto-enabled (first card went live)"
   - Next blaze signal with action=TRADE actually submits to Hyperliquid
   - Trade appears in HL order/trade history
   - Check for "notional < $10 min" skip at `dashboard.py:529` — with 0.5% risk on $99.80, notional should be ~$830 for BTC which clears it

2. **Add position monitoring to scalp_v2 cycle.** Currently places entry + TP/SL but doesn't monitor for partial fills or "move SL to breakeven after TP1" flow.

3. **Dashboard: show passing conditions too.** Currently only rejection reasons shown in Live Analysis.

4. **Backtest scalp_v2.** Existing `backtest.py` only supports legacy strategies.

## Blockers

- **Workspace rebuild required after every template edit.** The sync logic (hyperbot.py:141-154) correctly copies on byte mismatch, but if the workspace was created from the same template before the edit, files match and nothing syncs. Workaround: `rm -rf ~/Projects/hyperbot-workspace` before restart. Long-term: add a `--force-sync` flag or template version hash.

## Decisions Made

- **RVOL filter disabled entirely (0.0)** — BTC 1m volume is too thin/inconsistent relative to its own 20-candle average. Let breakout logic alone gate entries.
- **`--live --confirm-risk` CLI flags no longer required** — live trading auto-enables when the first card "Go Live" button is clicked. The per-card confirm dialog remains as the safety gate.
- **Dashboard per-pair risk % is now source of truth for blaze sizing** — the strategy's hardcoded `risk_per_trade_pct` is only a fallback. This means changing risk in the UI takes effect on the next signal evaluation without a restart.
