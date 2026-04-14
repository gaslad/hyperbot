"""Morning Debrief — trade replay and what-if analysis engine.

Generates a structured review of yesterday's (or any period's) trades:
- Timeline of entries/exits with P&L
- What-if analysis: "what if SL was tighter/wider", "what if TP was closer"
- Pattern detection: time-of-day clusters, strategy underperformance
- One-click adjustment suggestions

The output is JSON suitable for rendering in the dashboard or as Markdown.

Usage:
    from debrief import generate_debrief
    report = generate_debrief(workspace_path, lookback_days=1)
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Trade reconstruction from journal
# ---------------------------------------------------------------------------

def _load_journal(journal_path: Path) -> list[dict]:
    entries = []
    if not journal_path.exists():
        return entries
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _load_candle_cache(candle_dir: Path, coin: str, interval: str) -> list[dict]:
    """Load cached candles if available."""
    path = candle_dir / f"{coin.lower()}_{interval}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def reconstruct_trades(
    entries: list[dict],
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Reconstruct trade round-trips from journal entries.

    Returns a list of trade dicts, each containing:
    - strategy, coin, direction
    - entry_time, entry_price, size
    - exit_time, exit_price, closed_pnl
    - sl_price, tp_price (planned)
    - duration_minutes
    - outcome: "win", "loss", "flat"
    """
    # Collect fills and trade_log entries in the time window
    fills = []
    trade_logs = {}

    for entry in entries:
        ts_ms = entry.get("ts_ms", 0)
        if ts_ms < start_ms or ts_ms > end_ms:
            continue

        if entry.get("kind") == "fill":
            fills.append(entry)
        elif entry.get("kind") == "trade_log":
            oid = entry.get("order_id")
            if oid:
                trade_logs[oid] = entry

    # Group fills by coin + strategy
    groups: dict[str, list[dict]] = defaultdict(list)
    for fill in fills:
        coin = fill.get("coin", "")
        strategy = fill.get("strategy", "unknown")
        key = f"{coin}:{strategy}"
        groups[key].append(fill)

    trades = []
    for key, group_fills in groups.items():
        sorted_fills = sorted(group_fills, key=lambda f: f.get("ts_ms", 0))

        # Pair up entries and exits
        current_trade: dict | None = None
        for fill in sorted_fills:
            pnl = float(fill.get("closed_pnl", 0))
            side = fill.get("side", "").lower()
            price = float(fill.get("price", 0))
            size = float(fill.get("size", 0))
            ts = fill.get("ts", "")
            ts_ms = fill.get("ts_ms", 0)

            if current_trade is None:
                # Start a new trade
                coin, strategy = key.split(":", 1)
                oid = fill.get("order_id")
                log_entry = trade_logs.get(oid, {})

                current_trade = {
                    "coin": coin,
                    "strategy": strategy,
                    "direction": "long" if side == "buy" else "short",
                    "entry_time": ts,
                    "entry_time_ms": ts_ms,
                    "entry_price": price,
                    "size": size,
                    "sl_price": log_entry.get("stop_loss") or log_entry.get("sl"),
                    "tp_price": log_entry.get("take_profit") or log_entry.get("tp"),
                    "fills": [fill],
                }
            else:
                current_trade["fills"].append(fill)
                # Check if this is a closing fill (has closed_pnl or reduces position)
                if pnl != 0:
                    current_trade["exit_time"] = ts
                    current_trade["exit_time_ms"] = ts_ms
                    current_trade["exit_price"] = price
                    current_trade["closed_pnl"] = pnl
                    current_trade["fee"] = float(fill.get("fee", 0))

                    # Calculate duration
                    entry_ms = current_trade.get("entry_time_ms", 0)
                    exit_ms = ts_ms
                    current_trade["duration_minutes"] = (exit_ms - entry_ms) / 60000 if entry_ms else 0

                    # Classify outcome
                    if pnl > 0:
                        current_trade["outcome"] = "win"
                    elif pnl < 0:
                        current_trade["outcome"] = "loss"
                    else:
                        current_trade["outcome"] = "flat"

                    trades.append(current_trade)
                    current_trade = None

        # Orphaned entries (still open)
        if current_trade:
            current_trade["outcome"] = "open"
            current_trade["exit_price"] = None
            current_trade["closed_pnl"] = 0
            trades.append(current_trade)

    return sorted(trades, key=lambda t: t.get("entry_time_ms", 0))


# ---------------------------------------------------------------------------
# What-If analysis
# ---------------------------------------------------------------------------

def what_if_analysis(trade: dict, candles: list[dict] | None = None) -> list[dict]:
    """Generate what-if scenarios for a single trade.

    Each scenario is: {description, adjusted_pnl, delta, actionable}
    """
    scenarios = []
    entry = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    sl = trade.get("sl_price")
    tp = trade.get("tp_price")
    pnl = trade.get("closed_pnl", 0)
    direction = trade.get("direction", "long")
    size = trade.get("size", 0)
    outcome = trade.get("outcome", "")

    if not entry or not exit_price or outcome == "open":
        return scenarios

    risk = abs(entry - float(sl)) if sl else abs(entry * 0.02)

    # Scenario 1: What if SL was 0.5 ATR tighter?
    if sl and outcome == "loss":
        tighter_sl = float(sl) + risk * 0.25 if direction == "long" else float(sl) - risk * 0.25
        saved = abs(float(sl) - tighter_sl) * size
        scenarios.append({
            "description": f"If SL was 25% tighter (${tighter_sl:.2f} vs ${float(sl):.2f})",
            "adjusted_pnl": pnl + saved,
            "delta": saved,
            "param": "strategy.stop_atr_max",
            "direction": "reduce",
            "actionable": True,
        })

    # Scenario 2: What if TP was closer (0.8R instead of 1R)?
    if tp and outcome == "loss":
        if direction == "long":
            closer_tp = entry + risk * 0.8
            would_have_hit = candles and any(
                float(c.get("h", c.get("high", 0))) >= closer_tp
                for c in (candles or [])
            )
        else:
            closer_tp = entry - risk * 0.8
            would_have_hit = candles and any(
                float(c.get("l", c.get("low", 0))) <= closer_tp
                for c in (candles or [])
            )

        if would_have_hit:
            estimated_gain = risk * 0.8 * size
            scenarios.append({
                "description": f"If TP1 was at 0.8R (${closer_tp:.2f}), it would have hit",
                "adjusted_pnl": estimated_gain,
                "delta": estimated_gain - pnl,
                "param": "strategy.partial_exit_r",
                "direction": "reduce",
                "actionable": True,
            })

    # Scenario 3: What if entry was delayed 1 candle?
    if candles and len(candles) >= 2:
        # Find the candle after entry
        entry_ms = trade.get("entry_time_ms", 0)
        for i, c in enumerate(candles):
            c_ts = c.get("t", c.get("timestamp", 0))
            if isinstance(c_ts, str):
                continue
            if c_ts > entry_ms and i > 0:
                delayed_entry = float(candles[i].get("o", candles[i].get("open", entry)))
                if direction == "long" and delayed_entry < entry:
                    improvement = (entry - delayed_entry) * size
                    scenarios.append({
                        "description": f"If entry delayed 1 candle (${delayed_entry:.2f} vs ${entry:.2f})",
                        "adjusted_pnl": pnl + improvement,
                        "delta": improvement,
                        "param": None,
                        "direction": None,
                        "actionable": False,
                    })
                elif direction == "short" and delayed_entry > entry:
                    improvement = (delayed_entry - entry) * size
                    scenarios.append({
                        "description": f"If entry delayed 1 candle (${delayed_entry:.2f} vs ${entry:.2f})",
                        "adjusted_pnl": pnl + improvement,
                        "delta": improvement,
                        "param": None,
                        "direction": None,
                        "actionable": False,
                    })
                break

    return scenarios


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

def detect_patterns(trades: list[dict]) -> list[dict]:
    """Detect recurring patterns in trade outcomes.

    Returns a list of pattern dicts:
    - {pattern, description, severity, suggestion}
    """
    patterns = []

    if not trades:
        return patterns

    # Pattern 1: Time-of-day loss clusters
    hour_results: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        ts = trade.get("entry_time", "")
        try:
            dt = datetime.fromisoformat(ts)
            hour_results[dt.hour].append(trade.get("closed_pnl", 0))
        except (ValueError, TypeError):
            pass

    for hour, pnls in hour_results.items():
        if len(pnls) >= 2:
            avg_pnl = statistics.mean(pnls)
            loss_rate = sum(1 for p in pnls if p < 0) / len(pnls)
            if loss_rate >= 0.75 and avg_pnl < 0:
                patterns.append({
                    "pattern": "time_cluster",
                    "description": f"Hour {hour}:00 UTC: {loss_rate:.0%} loss rate across {len(pnls)} trades (avg ${avg_pnl:.2f})",
                    "severity": "high" if loss_rate >= 0.9 else "medium",
                    "suggestion": f"Consider blocking hour {hour} in strategy.blocked_hours",
                    "data": {"hour": hour, "loss_rate": loss_rate, "avg_pnl": avg_pnl, "count": len(pnls)},
                })

    # Pattern 2: Strategy underperformance
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        by_strategy[trade.get("strategy", "unknown")].append(trade)

    for strategy, strat_trades in by_strategy.items():
        pnls = [t.get("closed_pnl", 0) for t in strat_trades if t.get("outcome") != "open"]
        if len(pnls) >= 3:
            win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
            total_pnl = sum(pnls)
            if win_rate < 0.35 and total_pnl < 0:
                patterns.append({
                    "pattern": "strategy_underperformance",
                    "description": f"{strategy}: {win_rate:.0%} win rate, ${total_pnl:.2f} total P&L",
                    "severity": "high",
                    "suggestion": f"Review {strategy} parameters or pause it temporarily",
                    "data": {"strategy": strategy, "win_rate": win_rate, "total_pnl": total_pnl},
                })

    # Pattern 3: SL-then-reversal (stops too tight)
    sl_reversals = 0
    for trade in trades:
        if trade.get("outcome") == "loss" and trade.get("sl_price"):
            # If exit was at SL and price later exceeded TP, stop was too tight
            # (simplified: check if exit_price is close to SL)
            exit_price = trade.get("exit_price", 0)
            sl_price = float(trade["sl_price"])
            if exit_price and abs(exit_price - sl_price) / sl_price < 0.005:
                sl_reversals += 1

    if sl_reversals >= 2 and len(trades) >= 3:
        patterns.append({
            "pattern": "tight_stops",
            "description": f"{sl_reversals}/{len(trades)} losing trades exited at SL then likely reversed",
            "severity": "medium",
            "suggestion": "Consider widening stop_atr_max by 10-15%",
            "data": {"sl_reversals": sl_reversals, "total_trades": len(trades)},
        })

    # Pattern 4: Consecutive losses (revenge trading signal)
    max_streak = 0
    current_streak = 0
    for trade in sorted(trades, key=lambda t: t.get("entry_time_ms", 0)):
        if trade.get("outcome") == "loss":
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    if max_streak >= 3:
        patterns.append({
            "pattern": "loss_streak",
            "description": f"Max consecutive loss streak: {max_streak}",
            "severity": "high" if max_streak >= 5 else "medium",
            "suggestion": "Circuit breakers should have kicked in. Verify max_consecutive_losses config.",
            "data": {"max_streak": max_streak},
        })

    # Pattern 5: Average hold time for winners vs losers
    win_durations = [t.get("duration_minutes", 0) for t in trades if t.get("outcome") == "win"]
    loss_durations = [t.get("duration_minutes", 0) for t in trades if t.get("outcome") == "loss"]
    if win_durations and loss_durations:
        avg_win_dur = statistics.mean(win_durations)
        avg_loss_dur = statistics.mean(loss_durations)
        if avg_loss_dur > avg_win_dur * 1.5:
            patterns.append({
                "pattern": "slow_losers",
                "description": f"Avg losing trade held {avg_loss_dur:.0f}m vs {avg_win_dur:.0f}m for winners",
                "severity": "medium",
                "suggestion": "Reduce stale_after_minutes to cut losers faster",
                "data": {"avg_win_duration": avg_win_dur, "avg_loss_duration": avg_loss_dur},
            })

    return patterns


# ---------------------------------------------------------------------------
# Main debrief generator
# ---------------------------------------------------------------------------

def generate_debrief(
    workspace_path: Path,
    lookback_days: int = 1,
    include_candles: bool = False,
) -> dict[str, Any]:
    """Generate a full morning debrief report.

    Returns a dict suitable for JSON serialization:
    {
        "period": {"start": str, "end": str},
        "summary": {trade_count, win_count, loss_count, total_pnl, win_rate},
        "trades": [...],
        "what_ifs": {trade_index: [scenarios]},
        "patterns": [...],
        "suggestions": [...],
    }
    """
    journal_path = workspace_path / "data" / "trade_journal.jsonl"
    entries = _load_journal(journal_path)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=lookback_days)).timestamp() * 1000)

    trades = reconstruct_trades(entries, start_ms, end_ms)

    # Compute summary
    closed_trades = [t for t in trades if t.get("outcome") in ("win", "loss", "flat")]
    wins = [t for t in closed_trades if t["outcome"] == "win"]
    losses = [t for t in closed_trades if t["outcome"] == "loss"]
    total_pnl = sum(t.get("closed_pnl", 0) for t in closed_trades)

    summary = {
        "trade_count": len(closed_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "total_pnl": round(total_pnl, 2),
        "win_rate": len(wins) / len(closed_trades) if closed_trades else 0,
        "avg_win": round(statistics.mean([t["closed_pnl"] for t in wins]), 2) if wins else 0,
        "avg_loss": round(statistics.mean([t["closed_pnl"] for t in losses]), 2) if losses else 0,
        "best_trade": max((t.get("closed_pnl", 0) for t in closed_trades), default=0),
        "worst_trade": min((t.get("closed_pnl", 0) for t in closed_trades), default=0),
    }

    # What-if analysis for each trade
    what_ifs = {}
    for i, trade in enumerate(trades):
        scenarios = what_if_analysis(trade)
        if scenarios:
            what_ifs[i] = scenarios

    # Pattern detection
    patterns = detect_patterns(trades)

    # Aggregate suggestions from patterns and what-ifs
    suggestions = []
    for pattern in patterns:
        if pattern.get("suggestion"):
            suggestions.append({
                "source": f"pattern:{pattern['pattern']}",
                "text": pattern["suggestion"],
                "severity": pattern.get("severity", "medium"),
            })

    # Count what-if themes
    param_deltas: dict[str, float] = defaultdict(float)
    for scenarios in what_ifs.values():
        for scenario in scenarios:
            if scenario.get("param") and scenario.get("actionable"):
                param_deltas[scenario["param"]] += scenario.get("delta", 0)

    for param, total_delta in param_deltas.items():
        if total_delta > 0:
            suggestions.append({
                "source": f"what_if:{param}",
                "text": f"Adjusting {param} could have improved P&L by ${total_delta:.2f}",
                "severity": "medium",
            })

    return {
        "period": {
            "start": (now - timedelta(days=lookback_days)).isoformat(),
            "end": now.isoformat(),
            "lookback_days": lookback_days,
        },
        "summary": summary,
        "trades": trades,
        "what_ifs": what_ifs,
        "patterns": patterns,
        "suggestions": sorted(suggestions, key=lambda s: {"high": 0, "medium": 1, "low": 2}.get(s["severity"], 2)),
    }


def format_markdown(debrief: dict) -> str:
    """Format a debrief dict as Markdown for terminal/report output."""
    lines = []
    period = debrief.get("period", {})
    summary = debrief.get("summary", {})

    lines.append(f"# Morning Debrief — {period.get('end', '')[:10]}")
    lines.append("")
    lines.append(f"**Period**: last {period.get('lookback_days', 1)} day(s)")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Trades: {summary.get('trade_count', 0)} "
                 f"({summary.get('win_count', 0)}W / {summary.get('loss_count', 0)}L)")
    wr = summary.get('win_rate', 0)
    lines.append(f"- Win rate: {wr:.0%}")
    lines.append(f"- Total P&L: ${summary.get('total_pnl', 0):+.2f}")
    lines.append(f"- Best trade: ${summary.get('best_trade', 0):+.2f}")
    lines.append(f"- Worst trade: ${summary.get('worst_trade', 0):+.2f}")
    lines.append("")

    # Trades timeline
    trades = debrief.get("trades", [])
    if trades:
        lines.append("## Trade Timeline")
        lines.append("")
        for i, trade in enumerate(trades):
            icon = {"win": "+", "loss": "-", "flat": "=", "open": "~"}.get(trade.get("outcome", ""), "?")
            pnl = trade.get("closed_pnl", 0)
            dur = trade.get("duration_minutes", 0)
            lines.append(
                f"{icon} **{trade.get('coin', '?')}** {trade.get('direction', '?')} "
                f"({trade.get('strategy', '?')}) "
                f"— entry ${trade.get('entry_price', 0):.2f}, "
                f"exit ${trade.get('exit_price', 0) or 0:.2f}, "
                f"P&L ${pnl:+.2f}, {dur:.0f}m"
            )

            # What-ifs for this trade
            what_ifs = debrief.get("what_ifs", {}).get(i, [])
            for scenario in what_ifs:
                lines.append(f"  - _What if_: {scenario['description']} → ${scenario['adjusted_pnl']:+.2f} "
                           f"(${scenario['delta']:+.2f})")
        lines.append("")

    # Patterns
    patterns = debrief.get("patterns", [])
    if patterns:
        lines.append("## Patterns Detected")
        lines.append("")
        for pattern in patterns:
            severity_icon = {"high": "!!!", "medium": "!!", "low": "!"}.get(pattern.get("severity", ""), "")
            lines.append(f"- {severity_icon} **{pattern['pattern']}**: {pattern['description']}")
            if pattern.get("suggestion"):
                lines.append(f"  - Suggestion: {pattern['suggestion']}")
        lines.append("")

    # Suggestions
    suggestions = debrief.get("suggestions", [])
    if suggestions:
        lines.append("## Actionable Suggestions")
        lines.append("")
        for sug in suggestions:
            lines.append(f"- [{sug['severity'].upper()}] {sug['text']}")
        lines.append("")

    return "\n".join(lines)
