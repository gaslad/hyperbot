#!/usr/bin/env python3
"""Nightly Adaptive Tuner — closes the feedback loop between trade outcomes
and strategy parameters.

Reads the trade journal, computes per-strategy performance metrics, and
adjusts strategy config parameters within operator-policy safe bands.

Run nightly (e.g., via cron or manual invocation):
    python3 scripts/nightly_tuner.py [workspace_path]

All adjustments are:
- Logged with before/after values and rationale
- Constrained within operator-policy.json safe_bands
- Backed up before applying
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum number of trades before we trust metrics enough to adjust
MIN_TRADES_FOR_ADJUSTMENT = 5

# How many days of history to analyze
LOOKBACK_DAYS = 14

# Maximum parameter change per night (relative to current value)
MAX_CHANGE_PCT = 0.20  # 20% max adjustment per parameter per night

# Parameters we can tune and their adjustment rules
TUNABLE_PARAMS = {
    # Format: param_path -> (direction, description)
    # direction: "tighter" = reduce to be more conservative, "looser" = increase
    "strategy.adx_min": {
        "path": ["strategy", "adx_min"],
        "min": 15.0,
        "max": 30.0,
        "direction": "up_on_loss",  # raise threshold when losing
        "description": "ADX minimum threshold for regime filter",
    },
    "strategy.choppiness_max": {
        "path": ["strategy", "choppiness_max"],
        "min": 40.0,
        "max": 65.0,
        "direction": "down_on_loss",  # lower threshold (stricter) when losing
        "description": "Choppiness Index maximum",
    },
    "strategy.rvol_min": {
        "path": ["strategy", "rvol_min"],
        "min": 1.0,
        "max": 2.5,
        "direction": "up_on_loss",  # require more volume confirmation when losing
        "description": "Relative volume minimum",
    },
    "strategy.risk_per_trade_pct": {
        "path": ["strategy", "risk_per_trade_pct"],
        "min": 0.002,
        "max": 0.01,
        "direction": "down_on_loss",  # reduce size when losing
        "description": "Risk per trade (% of equity)",
    },
    "strategy.min_r_distance": {
        "path": ["strategy", "min_r_distance"],
        "min": 1.0,
        "max": 2.5,
        "direction": "up_on_loss",  # require better R when losing
        "description": "Minimum reward-to-risk distance",
    },
    "exit_management.stale_after_minutes": {
        "path": ["exit_management", "stale_after_minutes"],
        "min": 15.0,
        "max": 60.0,
        "direction": "down_on_loss",  # cut losers faster when losing
        "description": "Stale trade timeout",
    },
}


# ---------------------------------------------------------------------------
# Journal reader
# ---------------------------------------------------------------------------

def load_journal(journal_path: Path) -> list[dict]:
    """Load JSONL trade journal entries."""
    entries = []
    if not journal_path.exists():
        return entries
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def extract_fills(entries: list[dict], lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Extract fill entries within the lookback window."""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000)
    fills = []
    for entry in entries:
        if entry.get("kind") != "fill":
            continue
        ts_ms = entry.get("ts_ms", 0)
        if ts_ms >= cutoff_ms:
            fills.append(entry)
    return fills


def compute_metrics(fills: list[dict]) -> dict[str, dict[str, Any]]:
    """Compute per-strategy performance metrics.

    Returns: {strategy_id: {win_rate, profit_factor, avg_r, trade_count, ...}}
    """
    # Group fills by strategy using trade_log correlation
    by_strategy: dict[str, list[dict]] = defaultdict(list)

    for fill in fills:
        strategy = fill.get("strategy", fill.get("coin", "unknown"))
        by_strategy[strategy].append(fill)

    results = {}
    for strategy_id, strategy_fills in by_strategy.items():
        wins = [f for f in strategy_fills if float(f.get("closed_pnl", 0)) > 0]
        losses = [f for f in strategy_fills if float(f.get("closed_pnl", 0)) < 0]
        flat = [f for f in strategy_fills if float(f.get("closed_pnl", 0)) == 0]

        total_win = sum(float(f.get("closed_pnl", 0)) for f in wins)
        total_loss = abs(sum(float(f.get("closed_pnl", 0)) for f in losses))

        win_rate = len(wins) / len(strategy_fills) if strategy_fills else 0
        profit_factor = total_win / total_loss if total_loss > 0 else float("inf") if total_win > 0 else 0
        avg_pnl = statistics.mean([float(f.get("closed_pnl", 0)) for f in strategy_fills]) if strategy_fills else 0

        # Time-of-day analysis
        hour_pnl: dict[int, list[float]] = defaultdict(list)
        for fill in strategy_fills:
            ts = fill.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts)
                hour_pnl[dt.hour].append(float(fill.get("closed_pnl", 0)))
            except (ValueError, TypeError):
                pass

        worst_hours = sorted(
            ((h, statistics.mean(pnls)) for h, pnls in hour_pnl.items() if len(pnls) >= 2),
            key=lambda x: x[1]
        )[:3]

        # Consecutive loss streaks
        max_consecutive_losses = 0
        current_streak = 0
        for fill in sorted(strategy_fills, key=lambda f: f.get("ts_ms", 0)):
            if float(fill.get("closed_pnl", 0)) < 0:
                current_streak += 1
                max_consecutive_losses = max(max_consecutive_losses, current_streak)
            else:
                current_streak = 0

        results[strategy_id] = {
            "trade_count": len(strategy_fills),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_pnl": avg_pnl,
            "total_pnl": total_win - total_loss,
            "max_consecutive_losses": max_consecutive_losses,
            "worst_hours": worst_hours,
            "largest_loss": min((float(f.get("closed_pnl", 0)) for f in strategy_fills), default=0),
        }

    return results


# ---------------------------------------------------------------------------
# Parameter adjustment engine
# ---------------------------------------------------------------------------

def _get_nested(d: dict, path: list[str]) -> Any | None:
    """Get a value from a nested dict using a path list."""
    current = d
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _set_nested(d: dict, path: list[str], value: Any) -> None:
    """Set a value in a nested dict using a path list."""
    current = d
    for key in path[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[path[-1]] = value


def compute_adjustments(
    config: dict,
    metrics: dict[str, Any],
    policy_bands: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute parameter adjustments based on metrics.

    Returns a list of adjustment dicts:
        [{param, old_value, new_value, reason, description}]
    """
    adjustments = []

    if metrics.get("trade_count", 0) < MIN_TRADES_FOR_ADJUSTMENT:
        return adjustments

    win_rate = metrics.get("win_rate", 0.5)
    profit_factor = metrics.get("profit_factor", 1.0)
    max_consecutive_losses = metrics.get("max_consecutive_losses", 0)

    # Determine adjustment direction: are we losing?
    is_underperforming = (
        win_rate < 0.40 or
        profit_factor < 1.0 or
        max_consecutive_losses >= 4 or
        metrics.get("total_pnl", 0) < 0
    )

    is_performing_well = (
        win_rate > 0.55 and
        profit_factor > 1.5 and
        max_consecutive_losses < 3 and
        metrics.get("total_pnl", 0) > 0
    )

    for param_key, param_info in TUNABLE_PARAMS.items():
        path = param_info["path"]
        current_value = _get_nested(config, path)
        if current_value is None:
            continue

        current_value = float(current_value)
        param_min = float(param_info["min"])
        param_max = float(param_info["max"])
        direction = param_info["direction"]

        # Determine adjustment magnitude (5-15% based on severity)
        if is_underperforming:
            if win_rate < 0.30 or max_consecutive_losses >= 5:
                adjust_pct = 0.15  # aggressive
                severity = "significant underperformance"
            else:
                adjust_pct = 0.08  # moderate
                severity = "underperformance"
        elif is_performing_well:
            adjust_pct = 0.05  # gentle loosening
            severity = "strong performance — cautiously loosening"
        else:
            continue  # no adjustment needed

        adjust_pct = min(adjust_pct, MAX_CHANGE_PCT)

        # Apply direction
        if is_underperforming:
            if direction == "up_on_loss":
                new_value = current_value * (1 + adjust_pct)
            elif direction == "down_on_loss":
                new_value = current_value * (1 - adjust_pct)
            else:
                continue
        elif is_performing_well:
            # Reverse: gently loosen constraints
            if direction == "up_on_loss":
                new_value = current_value * (1 - adjust_pct * 0.5)
            elif direction == "down_on_loss":
                new_value = current_value * (1 + adjust_pct * 0.5)
            else:
                continue

        # Clamp to param bounds
        new_value = max(param_min, min(param_max, new_value))

        # Clamp to policy safe bands
        policy_max = policy_bands.get(f"{param_key}_max")
        if policy_max is not None:
            new_value = min(new_value, float(policy_max))

        # Round appropriately
        if current_value >= 10:
            new_value = round(new_value, 1)
        elif current_value >= 1:
            new_value = round(new_value, 2)
        else:
            new_value = round(new_value, 4)

        if new_value != current_value:
            adjustments.append({
                "param": param_key,
                "path": path,
                "old_value": current_value,
                "new_value": new_value,
                "reason": severity,
                "description": param_info["description"],
                "metrics": {
                    "win_rate": round(win_rate, 3),
                    "profit_factor": round(profit_factor, 2),
                    "trade_count": metrics["trade_count"],
                    "max_consecutive_losses": max_consecutive_losses,
                },
            })

    # Special: block worst hours if they're consistently losing
    worst_hours = metrics.get("worst_hours", [])
    current_blocked = _get_nested(config, ["strategy", "blocked_hours"]) or []
    new_blocked = list(current_blocked)
    for hour, avg_pnl in worst_hours:
        if avg_pnl < 0 and hour not in new_blocked:
            new_blocked.append(hour)
            adjustments.append({
                "param": "strategy.blocked_hours",
                "path": ["strategy", "blocked_hours"],
                "old_value": current_blocked,
                "new_value": sorted(new_blocked),
                "reason": f"Hour {hour}:00 UTC has avg P&L ${avg_pnl:.2f} — blocking",
                "description": "Blocked trading hours",
                "metrics": {"hour": hour, "avg_pnl": round(avg_pnl, 2)},
            })

    return adjustments


def apply_adjustments(
    config: dict,
    adjustments: list[dict[str, Any]],
) -> dict:
    """Apply adjustments to a config dict (returns modified copy)."""
    config = json.loads(json.dumps(config))  # deep copy
    for adj in adjustments:
        _set_nested(config, adj["path"], adj["new_value"])
    return config


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    metrics_by_strategy: dict[str, dict],
    adjustments_by_strategy: dict[str, list[dict]],
) -> str:
    """Generate a human-readable Markdown report."""
    lines = [
        f"# Nightly Tuner Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    for strategy_id, metrics in metrics_by_strategy.items():
        lines.append(f"## {strategy_id}")
        lines.append("")
        lines.append(f"- Trades: {metrics['trade_count']} ({metrics['win_count']}W / {metrics['loss_count']}L)")
        lines.append(f"- Win rate: {metrics['win_rate']:.1%}")
        pf = metrics['profit_factor']
        lines.append(f"- Profit factor: {pf:.2f}" if pf != float('inf') else "- Profit factor: inf (no losses)")
        lines.append(f"- Total P&L: ${metrics['total_pnl']:+.2f}")
        lines.append(f"- Largest loss: ${metrics['largest_loss']:.2f}")
        lines.append(f"- Max consecutive losses: {metrics['max_consecutive_losses']}")
        lines.append("")

        adjs = adjustments_by_strategy.get(strategy_id, [])
        if adjs:
            lines.append("### Adjustments Applied")
            lines.append("")
            for adj in adjs:
                lines.append(f"- **{adj['description']}** (`{adj['param']}`): "
                           f"{adj['old_value']} → {adj['new_value']} — _{adj['reason']}_")
            lines.append("")
        else:
            lines.append("_No adjustments needed._")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(workspace_path: Path, dry_run: bool = False) -> str:
    """Run the nightly tuner on a workspace.

    Returns the report as a string.
    """
    journal_path = workspace_path / "data" / "trade_journal.jsonl"
    config_dir = workspace_path / "config" / "strategies"
    policy_path = workspace_path / "config" / "policy" / "operator-policy.json"
    report_dir = workspace_path / "data" / "tuner_reports"

    # Load policy safe bands
    policy_bands = {}
    if policy_path.exists():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy_bands = policy.get("auto_apply", {}).get("safe_bands", {})

    # Load and analyze journal
    entries = load_journal(journal_path)
    fills = extract_fills(entries, LOOKBACK_DAYS)

    if not fills:
        return "No fills in the last 14 days. Nothing to tune."

    metrics_by_strategy = compute_metrics(fills)
    adjustments_by_strategy: dict[str, list[dict]] = {}

    # For each strategy config, compute and apply adjustments
    for config_path in sorted(config_dir.glob("*.json")):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        strategy_id = config.get("strategy_id", config_path.stem)
        pack_id = config.get("pack_id", "")

        # Find matching metrics (try exact match, then pack_id match)
        metrics = metrics_by_strategy.get(strategy_id)
        if not metrics:
            metrics = metrics_by_strategy.get(pack_id)
        if not metrics:
            continue

        adjustments = compute_adjustments(config, metrics, policy_bands)
        adjustments_by_strategy[strategy_id] = adjustments

        if adjustments and not dry_run:
            # Backup
            backup_path = config_path.with_suffix(".json.bak")
            shutil.copy2(config_path, backup_path)

            # Apply
            new_config = apply_adjustments(config, adjustments)
            config_path.write_text(json.dumps(new_config, indent=2), encoding="utf-8")

            print(f"  [tuner] Applied {len(adjustments)} adjustment(s) to {strategy_id}", flush=True)
        elif adjustments:
            print(f"  [tuner] DRY RUN: Would apply {len(adjustments)} adjustment(s) to {strategy_id}", flush=True)

    # Generate and save report
    report = generate_report(metrics_by_strategy, adjustments_by_strategy)

    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"  [tuner] Report saved: {report_path}", flush=True)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Nightly Adaptive Tuner")
    parser.add_argument("workspace", nargs="?", default=".", help="Path to workspace")
    parser.add_argument("--dry-run", action="store_true", help="Preview adjustments without applying")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    report = run(workspace, dry_run=args.dry_run)
    print(report)


if __name__ == "__main__":
    main()
