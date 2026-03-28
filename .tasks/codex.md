# Codex Inbox

<!-- Check .tasks/PROTOCOL.md for task format and rules. -->

- [ ] **TASK-001** | from:claude | priority:high | 2026-03-28
  > Build `templates/workspace/scripts/backtest.py` — walk-forward backtesting module.
  > Read `templates/workspace/scripts/signals.py` for the `detect_all_signals()` interface.
  > Read `templates/workspace/scripts/hl_client.py` for `get_candles()` and asset info helpers.
  > Requirements:
  >   - CLI: `python3 backtest.py --coin SOL --days 90 --pack trend_pullback`
  >   - Walk forward through daily candles, calling detect_all_signals() at each step
  >   - Simulate entries/exits using signal's entry_price, stop_loss, take_profit
  >   - Track: win rate, avg R-multiple, max drawdown, total return, trade count
  >   - Output: JSON summary to stdout + human-readable table
  >   - No external deps beyond what's already in the workspace
  >   - No model calls — fully deterministic
  > Output: `templates/workspace/scripts/backtest.py`
  > Verify: `python3 -c "import backtest; print('ok')"` in the scripts dir
  > Result:

- [ ] **TASK-002** | from:claude | priority:normal | 2026-03-28
  > Add unit tests for `hl_client.py` order formatting functions.
  > Read `templates/workspace/scripts/hl_client.py` — focus on `round_size()`, `round_price()`,
  > `get_asset_info()`, and the `place_order()` response parsing logic.
  > Requirements:
  >   - Test round_size with various szDecimals (0, 1, 2, 5)
  >   - Test round_price with 5-sig-fig rounding edge cases
  >   - Test place_order response parsing: success, error in statuses, missing oid
  >   - Mock the HTTP calls — don't hit real Hyperliquid
  >   - Use only stdlib unittest (no pytest dependency)
  > Output: `templates/workspace/scripts/test_hl_client.py`
  > Verify: `python3 -m unittest test_hl_client -v`
  > Result:
