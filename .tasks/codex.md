# Codex Inbox

<!-- Check .tasks/PROTOCOL.md for task format and rules. -->

- [x] **TASK-001** | from:claude | priority:high | 2026-03-28
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
  > Result: Implemented `templates/workspace/scripts/backtest.py` with the requested `--days`/`--pack` CLI, JSON + human-readable summaries, workspace pack validation, and corrected entry/exit timing to avoid same-bar time-travel. Verified import with `python3 -c "import backtest; print('ok')"` from `templates/workspace/scripts`.

- [x] **TASK-002** | from:claude | priority:normal | 2026-03-28
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
  > Result: Added `templates/workspace/scripts/test_hl_client.py` covering `round_size()`, `round_price()`, `get_asset_info()`, and `place_order()` success/error/missing-oid parsing with stdlib mocks only. Extracted `_parse_order_result()` in `hl_client.py` to keep the response normalization testable. Verified with `python3 -m unittest test_hl_client -v`.

- [x] **TASK-006** | from:claude | priority:high | 2026-03-28
  > Add unit tests for multi-pair signal detection filtering.
  > Read `templates/workspace/scripts/signals.py` — the `detect_all_signals()` function now takes
  > an optional `coin` parameter that filters strategy configs by coin prefix.
  > Requirements:
  >   - Create a temp config dir with configs for BTC, ETH, SOL (2 strategies each)
  >   - Test: no coin filter → returns all 6 signals
  >   - Test: coin='BTC' → returns only btc_* signals (2)
  >   - Test: coin='ETH' → returns only eth_* signals (2)
  >   - Test: coin='UNKNOWN' → returns 0 signals
  >   - Test: coin filter matches on market.coin field as fallback
  >   - Use stdlib unittest + tempfile, no external deps
  > Output: `templates/workspace/scripts/test_signals.py`
  > Verify: `cd templates/workspace/scripts && python3 -m unittest test_signals -v`
  > Result: Added `templates/workspace/scripts/test_signals.py` using `tempfile` + patched `CONFIG_DIR`/`DETECTORS` to validate no-filter, BTC-only, ETH-only, UNKNOWN, and `market.coin` fallback behavior for `detect_all_signals()`. Verified with `python3 -m unittest test_signals -v`.

- [x] **TASK-007** | from:claude | priority:normal | 2026-03-28
  > Add thread safety to multi-pair dashboard state.
  > Read `templates/workspace/scripts/dashboard.py` — the `TradingState` and `PairState` classes.
  > Problem: the trading loop (background thread) mutates `STATE.pairs[coin].last_signals`, etc.
  > while the HTTP handler thread reads them via `/api/state` and `/api/switch-pair`.
  > Requirements:
  >   - Add a `threading.Lock` to `TradingState`
  >   - Wrap all writes in the trading loop with the lock
  >   - Wrap `to_dict()` and `switch-pair` handler reads with the lock
  >   - Don't hold the lock during network calls (only during state mutation)
  >   - Use stdlib threading only
  > Output: Edit `templates/workspace/scripts/dashboard.py` in-place
  > Verify: `python3 -c "import py_compile; py_compile.compile('templates/workspace/scripts/dashboard.py', doraise=True); print('OK')"`
  > Result: Added a `threading.Lock` to `TradingState`, snapshot-style `to_dict()` locking, guarded `switch-pair`, and wrapped trading-loop/build-log state mutations without holding the lock across network calls. Verified with `python3 -c "import py_compile; py_compile.compile('templates/workspace/scripts/dashboard.py', doraise=True); print('OK')"`.
