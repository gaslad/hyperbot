#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import hl_client
import signals

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFEST = ROOT / "hyperbot.workspace.json"

DAY_MS = 24 * 60 * 60 * 1000
DAILY_WARMUP_DAYS = 120
H4_WARMUP_DAYS = 30


@dataclass(slots=True)
class Trade:
    strategy_id: str
    pack_id: str
    direction: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    return_pct: float
    r_multiple: float
    exit_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a deterministic workspace backtest from existing signal detectors.")
    parser.add_argument("--coin", help="Hyperliquid coin symbol, e.g. SOL")
    parser.add_argument("--days", type=int, help="Lookback window ending today in UTC")
    parser.add_argument("--start-date", help="Backtest start date in YYYY-MM-DD (UTC)")
    parser.add_argument("--end-date", help="Backtest end date in YYYY-MM-DD (UTC)")
    parser.add_argument("--pack", dest="pack_id", help="Strategy pack id to backtest")
    parser.add_argument("--pack-id", dest="pack_id_legacy", help=argparse.SUPPRESS)
    parser.add_argument("--api-url", default=hl_client.HL_MAINNET)
    parser.add_argument("--json-only", action="store_true", help="Print only the JSON summary")
    args = parser.parse_args()

    args.pack_id = args.pack_id or args.pack_id_legacy
    if not args.pack_id:
        raise SystemExit("--pack is required")
    if args.days is not None and (args.start_date or args.end_date):
        raise SystemExit("use either --days or --start-date/--end-date, not both")
    if args.days is None and not (args.start_date and args.end_date):
        raise SystemExit("either --days or both --start-date and --end-date are required")
    if args.days is not None and args.days <= 0:
        raise SystemExit("--days must be greater than zero")
    return args


def load_workspace() -> dict[str, Any]:
    if not WORKSPACE_MANIFEST.exists():
        return {}
    return json.loads(WORKSPACE_MANIFEST.read_text(encoding="utf-8"))


def infer_default_coin(workspace: dict[str, Any]) -> str | None:
    symbol = workspace.get("symbol")
    if not symbol:
        return None
    return hl_client.infer_coin(symbol)


def parse_date_utc(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"invalid date '{value}', expected YYYY-MM-DD") from exc


def resolve_window(args: argparse.Namespace) -> tuple[int, int]:
    if args.days is not None:
        end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=args.days - 1)
    else:
        start_dt = parse_date_utc(args.start_date)
        end_dt = parse_date_utc(args.end_date)
        if end_dt < start_dt:
            raise SystemExit("end date must be on or after start date")
    start_ms = to_ms(start_dt)
    end_ms = to_ms(end_dt + timedelta(days=1)) - 1
    return start_ms, end_ms


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def candle_timestamp(raw: dict[str, Any]) -> int:
    return int(raw.get("t", 0))


def candle_open(raw: dict[str, Any]) -> float:
    return float(raw.get("o", raw.get("open", 0.0)))


def candle_close(raw: dict[str, Any]) -> float:
    return float(raw.get("c", raw.get("close", 0.0)))


def candle_high(raw: dict[str, Any]) -> float:
    return float(raw.get("h", raw.get("high", 0.0)))


def candle_low(raw: dict[str, Any]) -> float:
    return float(raw.get("l", raw.get("low", 0.0)))


def format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def signal_direction_value(signal: Any) -> str:
    direction = getattr(signal, "direction", signals.Direction.NONE)
    return getattr(direction, "value", str(direction)).lower()


def ceil_days_between(start_ms: int, end_ms: int) -> int:
    if end_ms <= start_ms:
        return 1
    return max(1, math.ceil((end_ms - start_ms) / DAY_MS))


def fetch_candles(
    coin: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    warmup_days: int,
    base_url: str,
) -> list[dict[str, Any]]:
    fetch_start_ms = start_ms - warmup_days * DAY_MS
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lookback_days = ceil_days_between(fetch_start_ms, now_ms)
    candles = hl_client.get_candles(coin, interval, lookback_days, base_url=base_url)
    candles = sorted(candles, key=candle_timestamp)
    if not candles:
        return []
    return [candle for candle in candles if candle_timestamp(candle) <= end_ms]


def load_installed_configs() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for config_path in sorted(signals.CONFIG_DIR.glob("*.json")):
        if config_path.name == "README.md":
            continue
        configs.append(json.loads(config_path.read_text(encoding="utf-8")))
    return configs


def workspace_pack_ids(workspace: dict[str, Any]) -> set[str]:
    return {
        str(item.get("pack_id"))
        for item in workspace.get("strategy_packs", [])
        if item.get("pack_id")
    }


def ensure_pack_installed(pack_id: str, workspace: dict[str, Any]) -> None:
    config_pack_ids = {cfg.get("pack_id") for cfg in load_installed_configs() if cfg.get("pack_id")}
    installed = config_pack_ids | workspace_pack_ids(workspace)
    if pack_id not in installed:
        raise SystemExit(f"pack not installed in workspace: {pack_id}")


def pick_signal(pack_id: str, detected: list[Any]) -> Any | None:
    candidates = [
        sig for sig in detected
        if getattr(sig, "pack_id", None) == pack_id
        and signal_direction_value(sig) != signals.Direction.NONE.value
        and getattr(sig, "entry_price", None) is not None
        and getattr(sig, "stop_loss", None) is not None
        and getattr(sig, "take_profit", None) is not None
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda sig: (
            float(getattr(sig, "confidence", 0.0)),
            str(getattr(sig, "strategy_id", "")),
        ),
        reverse=True,
    )
    return candidates[0]


def resolve_same_bar_exit(direction: str, stop: float, take_profit: float, bar: dict[str, Any]) -> tuple[float, str]:
    bar_open = candle_open(bar)
    if direction == signals.Direction.BUY.value:
        stop_distance = abs(bar_open - stop)
        take_profit_distance = abs(take_profit - bar_open)
        if bar_open <= stop or stop_distance <= take_profit_distance:
            return stop, "stop_loss_same_bar"
        return take_profit, "take_profit_same_bar"

    stop_distance = abs(stop - bar_open)
    take_profit_distance = abs(bar_open - take_profit)
    if bar_open >= stop or stop_distance <= take_profit_distance:
        return stop, "stop_loss_same_bar"
    return take_profit, "take_profit_same_bar"


def evaluate_exit(
    signal: Any,
    entry_time_ms: int,
    bars_4h: list[dict[str, Any]],
    end_ms: int,
    fallback_price: float,
) -> Trade:
    direction = signal_direction_value(signal)
    entry = float(signal.entry_price)
    stop = float(signal.stop_loss)
    take_profit = float(signal.take_profit)
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError(f"invalid risk for signal {signal.strategy_id}: entry={entry} stop={stop}")

    exit_price = fallback_price
    exit_time = end_ms
    exit_reason = "end_of_range"

    for bar in bars_4h:
        ts = candle_timestamp(bar)
        if ts < entry_time_ms:
            continue
        if ts > end_ms:
            break

        high = candle_high(bar)
        low = candle_low(bar)

        if direction == signals.Direction.BUY.value:
            stop_hit = low <= stop
            tp_hit = high >= take_profit
        else:
            stop_hit = high >= stop
            tp_hit = low <= take_profit

        if stop_hit and tp_hit:
            exit_price, exit_reason = resolve_same_bar_exit(direction, stop, take_profit, bar)
            exit_time = ts
            break
        if stop_hit:
            exit_price = stop
            exit_time = ts
            exit_reason = "stop_loss"
            break
        if tp_hit:
            exit_price = take_profit
            exit_time = ts
            exit_reason = "take_profit"
            break

    signed_move = exit_price - entry if direction == signals.Direction.BUY.value else entry - exit_price
    return_pct = (signed_move / entry) * 100.0 if entry else 0.0
    r_multiple = signed_move / risk
    return Trade(
        strategy_id=str(signal.strategy_id),
        pack_id=str(signal.pack_id),
        direction=direction,
        entry_time=entry_time_ms,
        exit_time=exit_time,
        entry_price=entry,
        exit_price=exit_price,
        stop_loss=stop,
        take_profit=take_profit,
        return_pct=return_pct,
        r_multiple=r_multiple,
        exit_reason=exit_reason,
    )


def compute_max_drawdown_pct(equity_curve: list[float]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
    return max_drawdown


def build_summary(
    coin: str,
    pack_id: str,
    start_ms: int,
    end_ms: int,
    daily_bars_in_range: int,
    trades: list[Trade],
) -> dict[str, Any]:
    wins = sum(1 for trade in trades if trade.r_multiple > 0)
    total_r = sum(trade.r_multiple for trade in trades)
    avg_r = total_r / len(trades) if trades else 0.0

    equity = 1.0
    equity_curve = [equity]
    for trade in trades:
        equity *= (1.0 + trade.return_pct / 100.0)
        equity_curve.append(equity)

    return {
        "coin": coin,
        "pack_id": pack_id,
        "start_date": format_ts(start_ms),
        "end_date": format_ts(end_ms),
        "bars_evaluated": daily_bars_in_range,
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round((wins / len(trades) * 100.0) if trades else 0.0, 3),
        "average_r_multiple": round(avg_r, 4),
        "total_r_multiple": round(total_r, 4),
        "max_drawdown_pct": round(compute_max_drawdown_pct(equity_curve), 3),
        "total_return_pct": round((equity - 1.0) * 100.0, 3),
        "trade_log": [
            {
                **asdict(trade),
                "entry_time": format_ts(trade.entry_time),
                "exit_time": format_ts(trade.exit_time),
                "return_pct": round(trade.return_pct, 4),
                "r_multiple": round(trade.r_multiple, 4),
            }
            for trade in trades
        ],
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        f"Backtest: {summary['coin']} / {summary['pack_id']}",
        f"Window:   {summary['start_date']} -> {summary['end_date']}",
        f"Bars:     {summary['bars_evaluated']}",
        f"Trades:   {summary['trades']} ({summary['wins']}W / {summary['losses']}L, win rate {summary['win_rate_pct']}%)",
        f"Avg R:    {summary['average_r_multiple']}",
        f"Total R:  {summary['total_r_multiple']}",
        f"Return:   {summary['total_return_pct']}%",
        f"Max DD:   {summary['max_drawdown_pct']}%",
    ]
    if summary["trade_log"]:
        lines.append("")
        lines.append("Recent trades:")
        for trade in summary["trade_log"][-5:]:
            lines.append(
                f"- {trade['entry_time']} {trade['direction']} {trade['strategy_id']} -> "
                f"{trade['exit_time']} {trade['exit_reason']} "
                f"(R={trade['r_multiple']}, return={trade['return_pct']}%)"
            )
    return "\n".join(lines)


def run_backtest(
    coin: str,
    pack_id: str,
    start_ms: int,
    end_ms: int,
    base_url: str,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = workspace or load_workspace()
    ensure_pack_installed(pack_id, workspace)

    daily = fetch_candles(coin, start_ms, end_ms, "1d", DAILY_WARMUP_DAYS, base_url)
    h4 = fetch_candles(coin, start_ms, end_ms, "4h", H4_WARMUP_DAYS, base_url)
    if not daily:
        raise SystemExit(f"no daily candles returned for {coin}")
    if not h4:
        raise SystemExit(f"no 4H candles returned for {coin}")

    daily_in_range = [bar for bar in daily if start_ms <= candle_timestamp(bar) <= end_ms]
    if not daily_in_range:
        raise SystemExit("no daily candles inside requested date range")

    trades: list[Trade] = []
    day_index = 0
    while day_index < len(daily_in_range):
        day_bar = daily_in_range[day_index]
        current_ts = candle_timestamp(day_bar)
        candles_1d = [bar for bar in daily if candle_timestamp(bar) <= current_ts]
        signal_time_ms = current_ts + DAY_MS - 1
        candles_4h = [bar for bar in h4 if candle_timestamp(bar) <= signal_time_ms]
        current_price = candle_close(day_bar)
        detected = signals.detect_all_signals(candles_1d, candles_4h, current_price, coin=coin)
        signal = pick_signal(pack_id, detected)
        if signal is None:
            day_index += 1
            continue

        entry_time_ms = signal_time_ms
        remaining_h4 = [bar for bar in h4 if candle_timestamp(bar) >= entry_time_ms]
        fallback_daily = [bar for bar in daily_in_range if candle_timestamp(bar) >= current_ts]
        fallback_price = candle_close(fallback_daily[-1]) if fallback_daily else current_price
        trade = evaluate_exit(signal, entry_time_ms, remaining_h4, end_ms, fallback_price)
        trades.append(trade)

        next_index = day_index + 1
        while next_index < len(daily_in_range) and (candle_timestamp(daily_in_range[next_index]) + DAY_MS - 1) <= trade.exit_time:
            next_index += 1
        day_index = next_index

    return build_summary(coin, pack_id, start_ms, end_ms, len(daily_in_range), trades)


def main() -> int:
    args = parse_args()
    workspace = load_workspace()
    coin = args.coin or infer_default_coin(workspace)
    if not coin:
        raise SystemExit("--coin is required when the workspace manifest has no symbol")

    start_ms, end_ms = resolve_window(args)
    summary = run_backtest(coin, args.pack_id, start_ms, end_ms, args.api_url, workspace=workspace)
    if not args.json_only:
        print(render_report(summary))
        print("")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
