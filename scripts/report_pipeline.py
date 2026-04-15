#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import send_simplex_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = ROOT.parent / "hyperbot-workspace"
DEFAULT_KIND = "daily"
DEFAULT_LOOKBACK_HOURS = {"daily": 24, "weekly": 24 * 7}


class PipelinePaths:
    def __init__(
        self,
        *,
        workspace: Path,
        report_dir: Path,
        latest_markdown: Path,
        latest_simplex: Path,
        report_path: Path,
        state_path: Path,
        log_path: Path,
        used_staging: bool,
    ) -> None:
        self.workspace = workspace
        self.report_dir = report_dir
        self.latest_markdown = latest_markdown
        self.latest_simplex = latest_simplex
        self.report_path = report_path
        self.state_path = state_path
        self.log_path = log_path
        self.used_staging = used_staging


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _resolve_pipeline_paths(workspace: Path, kind: str) -> PipelinePaths:
    report_dir = workspace / "docs" / "reports" / kind
    data_dir = workspace / "data"
    report_name = f"{kind}-report_{_utc_now().date().isoformat()}"
    primary_markdown = report_dir / f"{report_name}.md"
    primary_simplex = report_dir / f"{report_name}-simplex.txt"
    latest_markdown = report_dir / f"latest-{kind}.md"
    latest_simplex = report_dir / f"latest-{kind}-simplex.txt"

    staging_root = Path(tempfile.gettempdir()) / "hyperbot-reports" / workspace.name / kind
    staging_markdown = staging_root / f"{report_name}.md"
    staging_simplex = staging_root / f"{report_name}-simplex.txt"
    staging_latest_markdown = staging_root / f"latest-{kind}.md"
    staging_latest_simplex = staging_root / f"latest-{kind}-simplex.txt"

    report_dir_writable = _is_writable_dir(report_dir)
    data_dir_writable = _is_writable_dir(data_dir)
    report_base = report_dir if report_dir_writable else staging_root
    state_base = data_dir if data_dir_writable else staging_root
    report_path = primary_markdown if report_dir_writable else staging_markdown
    latest_md = latest_markdown if report_dir_writable else staging_latest_markdown
    latest_txt = latest_simplex if report_dir_writable else staging_latest_simplex

    return PipelinePaths(
        workspace=workspace,
        report_dir=report_base,
        latest_markdown=latest_md,
        latest_simplex=latest_txt,
        report_path=report_path,
        state_path=state_base / "report_pipeline_state.json",
        log_path=state_base / "report_pipeline.log",
        used_staging=not report_dir_writable or not data_dir_writable,
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().isoformat().replace("+00:00", "Z")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {message}\n")


def _load_workspace_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load workspace module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_workspace_modules(workspace: Path):
    scripts_dir = workspace / "scripts"
    hl_client_path = scripts_dir / "hl_client.py"
    trade_journal_path = scripts_dir / "trade_journal.py"
    if not hl_client_path.exists() or not trade_journal_path.exists():
        raise FileNotFoundError(f"workspace scripts are missing under {scripts_dir}")
    hl_client = _load_workspace_module("hl_client", hl_client_path)
    trade_journal = _load_workspace_module("trade_journal", trade_journal_path)
    return hl_client, trade_journal


def _window_bounds(now_ms: int, lookback_hours: int) -> tuple[int, int, int, int]:
    current_end = now_ms
    current_start = current_end - lookback_hours * 60 * 60 * 1000
    previous_end = current_start - 1
    previous_start = previous_end - lookback_hours * 60 * 60 * 1000
    return current_start, current_end, previous_start, previous_end


def _within(ts_ms: int, start_ms: int, end_ms: int) -> bool:
    return start_ms <= ts_ms <= end_ms


def _entry_actions_for_direction(direction: str) -> set[str]:
    normalized = direction.lower()
    if "short" in normalized:
        return {"SELL"}
    return {"BUY"}


def _log_coin(log: dict[str, Any]) -> str:
    return str(log.get("coin", "")).strip().upper()


def _is_open_fill(fill: dict[str, Any]) -> bool:
    direction = str(fill.get("direction", "")).lower()
    closed_pnl = _safe_float(fill.get("closed_pnl"))
    start_position = _safe_float(fill.get("start_position"))
    return direction.startswith("open") or (closed_pnl == 0.0 and start_position == 0.0)


def _is_close_fill(fill: dict[str, Any]) -> bool:
    direction = str(fill.get("direction", "")).lower()
    closed_pnl = _safe_float(fill.get("closed_pnl"))
    return direction.startswith("close") or closed_pnl != 0.0


def _entry_worse_bps(direction: str, planned: float, actual: float) -> float:
    normalized = direction.lower()
    if "short" in normalized:
        return round((planned - actual) / planned * 10000.0, 2)
    return round((actual - planned) / planned * 10000.0, 2)


def _parse_numeric(note: str, key: str) -> float | None:
    match = re.search(rf"{re.escape(key)}=([0-9]+(?:\.[0-9]+)?)", note or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_leverage(note: str) -> float | None:
    match = re.search(r"lev=([0-9]+(?:\.[0-9]+)?)x", note or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_order_type(note: str) -> str:
    note = note or ""
    match = re.search(r"\b(ALO|IOC|maker|taker)\b", note, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _new_trade_review(coin: str, direction: str, open_fill: dict[str, Any] | None) -> dict[str, Any]:
    size = abs(_safe_float(open_fill.get("size"))) if open_fill else 0.0
    return {
        "coin": coin,
        "direction": direction,
        "strategy": str(open_fill.get("strategy", "")) if open_fill else "",
        "open_fill": open_fill,
        "close_fills": [],
        "trade_logs": [],
        "remaining_size": size,
        "planned_entry": None,
        "planned_stop": None,
        "planned_tp1": None,
        "planned_tp2": None,
        "leverage": None,
        "entry_order_type": "",
        "entry_slippage_bps": None,
        "net_pnl": 0.0,
        "fees": _safe_float(open_fill.get("fee")) if open_fill else 0.0,
        "status_note": "",
        "tp_note": "",
        "entry_note": "",
        "orphan_close": open_fill is None,
        "last_ts_ms": _safe_int(open_fill.get("ts_ms")) if open_fill else 0,
    }


def _attach_close_fill(review: dict[str, Any], fill: dict[str, Any]) -> None:
    review["close_fills"].append(fill)
    review["net_pnl"] += _safe_float(fill.get("closed_pnl"))
    review["fees"] += _safe_float(fill.get("fee"))
    review["remaining_size"] = max(0.0, review["remaining_size"] - abs(_safe_float(fill.get("size"))))
    review["last_ts_ms"] = max(review["last_ts_ms"], _safe_int(fill.get("ts_ms")))


def _load_records_for_window(workspace: Path, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    records = _read_jsonl(workspace / "data" / "trade_journal.jsonl")
    return [record for record in records if start_ms <= _safe_int(record.get("ts_ms")) <= end_ms]


def _build_trade_reviews(window_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills = sorted([r for r in window_records if r.get("kind") == "fill"], key=lambda item: _safe_int(item.get("ts_ms")))
    trade_logs = sorted([r for r in window_records if r.get("kind") == "trade_log"], key=lambda item: _safe_int(item.get("ts_ms")))
    active_by_coin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reviews: list[dict[str, Any]] = []

    for fill in fills:
        coin = str(fill.get("coin", "")).strip()
        direction = str(fill.get("direction", "")).strip()
        if not coin:
            continue
        if _is_open_fill(fill):
            review = _new_trade_review(coin, direction, fill)
            reviews.append(review)
            active_by_coin[coin].append(review)
            continue
        if not _is_close_fill(fill):
            continue

        active_list = active_by_coin[coin]
        review = None
        for candidate in reversed(active_list):
            if candidate["remaining_size"] > 1e-12 or candidate["open_fill"] is None:
                review = candidate
                break
        if review is None:
            review = _new_trade_review(coin, direction, None)
            reviews.append(review)
            active_list.append(review)

        _attach_close_fill(review, fill)
        if review["remaining_size"] <= 1e-12 and review in active_list:
            active_list.remove(review)

    for review in reviews:
        start_ms = _safe_int(review["open_fill"].get("ts_ms")) if review["open_fill"] else 0
        if not start_ms and review["close_fills"]:
            start_ms = min(_safe_int(close.get("ts_ms")) for close in review["close_fills"])
        end_ms = max([_safe_int(close.get("ts_ms")) for close in review["close_fills"]] or [start_ms])
        relevant_logs = [
            log
            for log in trade_logs
            if _within(_safe_int(log.get("ts_ms")), max(0, start_ms - 5 * 60 * 1000), end_ms + 30 * 60 * 1000)
            and (not str(log.get("coin", "")).strip() or str(log.get("coin", "")).strip() == review["coin"])
        ]
        if review["open_fill"] and review["open_fill"].get("order_id"):
            order_id = _safe_int(review["open_fill"].get("order_id"))
            order_logs = [log for log in relevant_logs if _safe_int(log.get("order_id")) == order_id]
            if order_logs:
                rest = [log for log in relevant_logs if _safe_int(log.get("order_id")) != order_id]
                relevant_logs = order_logs + rest
        review["trade_logs"] = sorted(relevant_logs, key=lambda item: _safe_int(item.get("ts_ms")))
        _derive_review_metadata(review)

    return reviews


def _select_entry_log(review: dict[str, Any], logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not review["open_fill"]:
        return None

    entry_oid = _safe_int(review["open_fill"].get("order_id"))
    if entry_oid:
        exact = [log for log in logs if _safe_int(log.get("order_id")) == entry_oid]
        if exact:
            return sorted(exact, key=lambda item: _safe_int(item.get("ts_ms")))[0]

    anchor_ts = _safe_int(review["open_fill"].get("ts_ms"))
    if not anchor_ts:
        return None

    entry_actions = _entry_actions_for_direction(review["direction"])
    candidate_start = max(0, anchor_ts - 2 * 60 * 1000)
    candidate_end = anchor_ts + 10 * 60 * 1000
    candidates = [
        log
        for log in logs
        if str(log.get("action", "")).upper() in entry_actions | {"FILLED"}
        and _within(_safe_int(log.get("ts_ms")), candidate_start, candidate_end)
    ]
    if not candidates:
        return None

    preferred_strategy = str(review.get("strategy", "")).strip()
    if preferred_strategy:
        strategy_matches = [log for log in candidates if str(log.get("strategy", "")).strip() == preferred_strategy]
        if strategy_matches:
            candidates = strategy_matches

    return sorted(candidates, key=lambda item: _safe_int(item.get("ts_ms")))[0]


def _derive_review_metadata(review: dict[str, Any]) -> None:
    logs = review["trade_logs"]
    if review["open_fill"]:
        review["strategy"] = review["strategy"] or str(review["open_fill"].get("strategy", ""))

    if not logs and review["open_fill"]:
        review["status_note"] = "No matching trade-intent logs were captured, so slippage and chase quality could not be measured directly."
        return

    entry_log = _select_entry_log(review, logs)
    if not entry_log:
        if review["open_fill"]:
            review["status_note"] = "Entry intent was not captured in the local logs, so slippage and chase quality could not be measured directly."
        return

    anchor_ts = _safe_int(entry_log.get("ts_ms")) or (_safe_int(review["open_fill"].get("ts_ms")) if review["open_fill"] else 0)
    context_start = max(0, anchor_ts - 2 * 60 * 1000)
    context_end = anchor_ts + 15 * 60 * 1000
    context_logs = [
        log
        for log in logs
        if _within(_safe_int(log.get("ts_ms")), context_start, context_end)
        and (
            not _log_coin(log)
            or not review["coin"].strip()
            or _log_coin(log) == review["coin"].strip().upper()
        )
    ]
    if not context_logs:
        context_logs = [entry_log]

    if entry_log:
        review["strategy"] = review["strategy"] or str(entry_log.get("strategy", ""))
        review["planned_entry"] = _safe_float(entry_log.get("planned_entry"), default=None) if entry_log.get("planned_entry") is not None else _safe_float(entry_log.get("price"), default=None)
        review["entry_order_type"] = _parse_order_type(str(entry_log.get("note", "")))

    settings_logs = [
        log
        for log in context_logs
        if str(log.get("action", "")).upper() == "SETTINGS" and _safe_int(log.get("ts_ms")) <= anchor_ts + 2 * 60 * 1000
    ]
    if settings_logs:
        for log in settings_logs:
            note = str(log.get("note", ""))
            leverage = _parse_leverage(note)
            if leverage is not None:
                review["leverage"] = leverage
                break

    stop_logs = [
        log
        for log in context_logs
        if str(log.get("action", "")).upper() in {"SL_SET", "SL_MOVE"} and _safe_int(log.get("ts_ms")) >= anchor_ts
    ]
    tp1_logs = [
        log
        for log in context_logs
        if str(log.get("action", "")).upper() == "TP1_SET" and _safe_int(log.get("ts_ms")) >= anchor_ts
    ]
    tp2_logs = [
        log
        for log in context_logs
        if str(log.get("action", "")).upper() == "TP2_SET" and _safe_int(log.get("ts_ms")) >= anchor_ts
    ]
    tp_logs = [
        log
        for log in context_logs
        if str(log.get("action", "")).upper() == "TP_SET" and _safe_int(log.get("ts_ms")) >= anchor_ts
    ]

    if stop_logs:
        review["planned_stop"] = _safe_float(stop_logs[0].get("planned_stop"), default=None) if stop_logs[0].get("planned_stop") is not None else _safe_float(stop_logs[0].get("price"), default=None)
        if review["planned_stop"] is None:
            review["planned_stop"] = _parse_numeric(str(stop_logs[0].get("note", "")), "SL")

    if tp1_logs:
        review["planned_tp1"] = _safe_float(tp1_logs[0].get("planned_tp1"), default=None) if tp1_logs[0].get("planned_tp1") is not None else _safe_float(tp1_logs[0].get("price"), default=None)
        if review["planned_tp1"] is None:
            review["planned_tp1"] = _parse_numeric(str(tp1_logs[0].get("note", "")), "TP1")

    if tp2_logs:
        review["planned_tp2"] = _safe_float(tp2_logs[0].get("planned_tp2"), default=None) if tp2_logs[0].get("planned_tp2") is not None else _safe_float(tp2_logs[0].get("price"), default=None)
        if review["planned_tp2"] is None:
            review["planned_tp2"] = _parse_numeric(str(tp2_logs[0].get("note", "")), "TP2")

    if not review["planned_tp2"] and tp_logs:
        review["planned_tp2"] = _safe_float(tp_logs[0].get("planned_tp2"), default=None) if tp_logs[0].get("planned_tp2") is not None else _safe_float(tp_logs[0].get("price"), default=None)

    if review["open_fill"] and review["planned_entry"] is not None:
        actual = _safe_float(review["open_fill"].get("price"))
        planned = _safe_float(review["planned_entry"])
        if planned > 0:
            review["entry_slippage_bps"] = _entry_worse_bps(review["direction"], planned, actual)
            if abs(review["entry_slippage_bps"]) <= 5:
                review["entry_note"] = f"entry matched plan within {abs(review['entry_slippage_bps']):.2f} bps"
            elif review["entry_slippage_bps"] > 20:
                review["entry_note"] = f"entry was {review['entry_slippage_bps']:.2f} bps worse than plan, which looks like possible chase"
            else:
                review["entry_note"] = f"entry moved {review['entry_slippage_bps']:.2f} bps relative to plan"

    if review["planned_entry"] is None and review["open_fill"]:
        review["status_note"] = "Entry intent was not captured in the local logs, so slippage and chase quality could not be measured directly."
    elif review["entry_slippage_bps"] is not None:
        if review["entry_slippage_bps"] > 20:
            review["status_note"] = f"Entry was {review['entry_slippage_bps']:.2f} bps worse than plan; possible chase."
        elif review["entry_slippage_bps"] < -20:
            review["status_note"] = f"Entry improved by {abs(review['entry_slippage_bps']):.2f} bps versus plan."
        else:
            review["status_note"] = f"Entry stayed close to plan ({review['entry_slippage_bps']:.2f} bps worse)."

    if review["planned_tp1"] is not None and review["close_fills"]:
        first_close = review["close_fills"][0]
        first_price = _safe_float(first_close.get("price"))
        if "short" in review["direction"].lower():
            hit_tp1 = first_price <= _safe_float(review["planned_tp1"])
        else:
            hit_tp1 = first_price >= _safe_float(review["planned_tp1"])
        if hit_tp1:
            review["tp_note"] = "TP1 behaved well on the first partial."
        else:
            review["tp_note"] = "First exit did not fully reach TP1."

    if len(review["close_fills"]) > 1:
        first_close = review["close_fills"][0]
        final_close = review["close_fills"][-1]
        first_price = _safe_float(first_close.get("price"))
        final_price = _safe_float(final_close.get("price"))
        if "short" in review["direction"].lower():
            gave_back = final_price > first_price
        else:
            gave_back = final_price < first_price
        if gave_back:
            if review["tp_note"]:
                review["tp_note"] += " The remainder gave back part of the first partial."
            else:
                review["tp_note"] = "The remainder gave back part of the first partial."


def _trade_label(review: dict[str, Any]) -> str:
    direction = review["direction"].replace("Open ", "").strip()
    return f"{review['coin']} {direction.lower()}"


def _format_trade_summary(review: dict[str, Any]) -> str:
    entry = review["open_fill"]
    first_close = review["close_fills"][0] if review["close_fills"] else None
    final_close = review["close_fills"][-1] if review["close_fills"] else None
    parts = [f"{_trade_label(review)}: {review['net_pnl']:+.4f} USDC"]
    if entry and review["planned_entry"] is not None and review["entry_slippage_bps"] is not None:
        parts.append(f"entry {_safe_float(entry.get('price')):.4f} vs {_safe_float(review['planned_entry']):.4f}")
        parts.append(f"{review['entry_slippage_bps']:+.2f} bps")
    elif entry:
        parts.append(f"entry {_safe_float(entry.get('price')):.4f}")
    if review["leverage"] is not None:
        parts.append(f"{review['leverage']:.1f}x leverage")
    if review["planned_stop"] is not None:
        parts.append(f"stop {review['planned_stop']:.2f}")
    if review["planned_tp1"] is not None:
        parts.append(f"TP1 {review['planned_tp1']:.2f}")
    if review["planned_tp2"] is not None:
        parts.append(f"TP2 {review['planned_tp2']:.2f}")
    if review["tp_note"]:
        parts.append(review["tp_note"])
    if review["status_note"] and not review["planned_entry"]:
        parts.append(review["status_note"])
    if first_close and final_close and len(review["close_fills"]) > 1:
        parts.append(f"first exit {_safe_float(first_close.get('price')):.4f}, final exit {_safe_float(final_close.get('price')):.4f}")
    parts.append(f"fees {review['fees']:.4f} USDC")
    return "; ".join(parts)


def _render_report_body(
    *,
    workspace_name: str,
    kind: str,
    window_label: str,
    summary: dict[str, Any],
    trade_reviews: list[dict[str, Any]],
    sync_note: str,
    generated_at: datetime,
) -> tuple[str, str]:
    closed_reviews = [review for review in trade_reviews if review["close_fills"] or abs(review["net_pnl"]) > 0.0]
    positive = sorted([review for review in closed_reviews if review["net_pnl"] > 0], key=lambda item: item["net_pnl"], reverse=True)
    negative = sorted([review for review in closed_reviews if review["net_pnl"] < 0], key=lambda item: item["net_pnl"])

    markdown_lines = [
        f"# {workspace_name} {kind.title()} Report",
        "",
        f"Window: {window_label} UTC",
        f"Generated: {generated_at.isoformat().replace('+00:00', 'Z')}",
        "",
        "## Status",
        f"- {sync_note}",
        f"- Fills reviewed: {summary['fill_count']}",
        f"- Closed fills: {summary['closed_fill_count']} ({summary['wins']}W / {summary['losses']}L / {summary['flat']} flat)",
        f"- Closed PnL: {summary['closed_pnl']:+.4f} USDC",
        f"- Fees: {summary['fees']:.4f} USDC",
    ]

    if summary["trade_log_count"] == 0:
        markdown_lines.append("- No local strategy event logs were present for the review window, so attribution is fill-only.")

    markdown_lines.extend(["", "## What Worked"])
    if positive:
        for review in positive[:3]:
            markdown_lines.append(f"- {_format_trade_summary(review)}")
    else:
        markdown_lines.append("- No closed trade finished positive in this window.")

    markdown_lines.extend(["", "## What Lost Money"])
    if negative:
        for review in negative[:3]:
            markdown_lines.append(f"- {_format_trade_summary(review)}")
    else:
        markdown_lines.append("- No closed trade finished negative in this window.")

    markdown_lines.extend(["", "## Execution Review"])
    crossed_close_count = 0
    for review in closed_reviews:
        crossed_close_count += sum(1 for fill in review["close_fills"] if fill.get("crossed"))
    markdown_lines.append(f"- Observed fees on reviewed trades: {sum(review['fees'] for review in closed_reviews):.4f} USDC")
    markdown_lines.append(f"- Crossed closes on reviewed trades: {crossed_close_count}/{sum(len(review['close_fills']) for review in closed_reviews)}")

    for review in closed_reviews[:5]:
        notes = []
        if review["status_note"]:
            notes.append(review["status_note"])
        if review["tp_note"]:
            notes.append(review["tp_note"])
        if review["planned_stop"] is not None:
            notes.append(f"stop staged at {review['planned_stop']:.2f}" if review["trade_logs"] else f"stop plan {review['planned_stop']:.2f}")
        if review["planned_tp1"] is not None:
            notes.append(f"TP1 {review['planned_tp1']:.2f}")
        if review["planned_tp2"] is not None:
            notes.append(f"TP2 {review['planned_tp2']:.2f}")
        if review["leverage"] is not None:
            notes.append(f"{review['leverage']:.1f}x leverage")
        if review["entry_order_type"]:
            notes.append(f"{review['entry_order_type']} entry")
        if review["planned_entry"] is not None and review["entry_slippage_bps"] is not None:
            notes.append(f"entry drift {review['entry_slippage_bps']:+.2f} bps")
        elif review["open_fill"] and review["planned_entry"] is None:
            notes.append("slippage and chase not directly measurable")
        if notes:
            markdown_lines.append(f"- {_trade_label(review)}: " + "; ".join(notes))

    markdown_lines.extend(["", "## Small Changes"])
    suggestions: list[str] = []
    if any(review["planned_entry"] is None and review["open_fill"] for review in closed_reviews):
        suggestions.append("Keep journaling intended entry, stop, TP ladder, leverage, and order type at placement time so slippage and chase stay measurable.")
    if any(review["entry_slippage_bps"] is not None and review["entry_slippage_bps"] > 20 for review in closed_reviews):
        suggestions.append("Prefer maker/ALO entries when the spread allows and cap obvious entry drift before it grows into a bad fill.")
    if any(len(review["close_fills"]) > 1 and review["tp_note"] and "gave back" in review["tp_note"].lower() for review in closed_reviews):
        suggestions.append("Add a harder max-hold rule or faster trail on remainder legs so strong first partials do not fade back into the market.")
    if not suggestions:
        suggestions.append("Keep the current filters and size stable; the sample is too small for a larger strategy change.")
    for suggestion in suggestions[:3]:
        markdown_lines.append(f"- {suggestion}")

    markdown_lines.append("")

    simplex_lines = [
        f"{workspace_name} {kind} report",
        f"Window: {window_label} UTC",
        f"Sync: {sync_note}",
        f"Closed PnL: {summary['closed_pnl']:+.4f} USDC",
        f"Fees: {summary['fees']:.4f} USDC",
    ]
    if positive:
        simplex_lines.append(f"Worked: {_format_trade_summary(positive[0])}")
    if negative:
        simplex_lines.append(f"Lost: {_format_trade_summary(negative[0])}")
    if suggestions:
        simplex_lines.append(f"Change: {suggestions[0]}")
    return "\n".join(markdown_lines), "\n".join(simplex_lines) + "\n"


def _render_failure_note(
    *,
    workspace_name: str,
    kind: str,
    window_label: str,
    sync_note: str,
    reason: str,
) -> tuple[str, str]:
    markdown = "\n".join(
        [
            f"# {workspace_name} {kind.title()} Report",
            "",
            f"Window: {window_label} UTC",
            "",
            "## Status",
            f"- {sync_note}",
            f"- Report generation failed: {reason}",
            "",
            "## What Worked",
            "- No completed fills were available to review.",
            "",
            "## What Lost Money",
            "- No completed fills were available to review.",
            "",
            "## Execution Review",
            "- The pipeline recovered far enough to preserve a failure note instead of inventing results.",
            "",
            "## Small Changes",
            "- Fix the root failure noted above, then rerun the pipeline.",
            "",
        ]
    )
    simplex = "\n".join(
        [
            f"{workspace_name} {kind} report",
            f"Window: {window_label} UTC",
            f"Status: FAILED - {reason}",
            f"Sync: {sync_note}",
        ]
    ) + "\n"
    return markdown, simplex


def _validate_report(markdown: str, simplex: str, *, failure_note: bool) -> list[str]:
    errors: list[str] = []
    if not markdown.strip():
        errors.append("markdown report is empty")
    if not simplex.strip():
        errors.append("SimpleX report is empty")
    if failure_note:
        if "FAILED" not in simplex and "failed" not in markdown.lower():
            errors.append("failure note does not include a failure marker")
        return errors

    required_sections = ("What Worked", "What Lost Money", "Execution Review", "Small Changes")
    for section in required_sections:
        if section not in markdown:
            errors.append(f"missing section: {section}")
    if "Closed PnL:" not in markdown:
        errors.append("markdown does not contain a closed PnL line")
    return errors


def _sync_user_fills(
    *,
    workspace: Path,
    hl_client,
    trade_journal,
    lookback_hours: int,
    log_path: Path,
) -> tuple[int, str]:
    data_dir = workspace / "data"
    if not _is_writable_dir(data_dir):
        return 0, "workspace data directory is not writable here, so fill sync was skipped and the local journal was reused"

    master_address = hl_client.read_credential("master_address")
    if not master_address:
        return 0, "no master wallet credential was available, so fill sync was skipped and the local journal was reused"

    journal = trade_journal.TradeJournal(workspace)
    try:
        processed = journal.sync_user_fills(
            master_address,
            hl_client.HL_MAINNET,
            lookback_days=max(1, lookback_hours // 24),
        )
        return processed, "fills synced successfully"
    except Exception as exc:
        _append_log(log_path, f"sync-error | {exc}")
        return 0, f"fill sync failed ({exc}); the local journal was reused"


def run_pipeline(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists():
        print(f"ERROR: workspace does not exist: {workspace}", file=sys.stderr)
        return 2

    kind = str(args.kind).lower()
    if kind not in {"daily", "weekly"}:
        print(f"ERROR: unsupported report kind: {kind}", file=sys.stderr)
        return 2

    lookback_hours = int(args.lookback_hours or DEFAULT_LOOKBACK_HOURS[kind])
    now = _utc_now()
    now_ms = _to_ms(now)
    start_ms, end_ms, previous_start_ms, previous_end_ms = _window_bounds(now_ms, lookback_hours)
    window_label = f"{datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()} to {datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat()}"

    hl_client, trade_journal = _load_workspace_modules(workspace)
    paths = _resolve_pipeline_paths(workspace, kind)
    _append_log(paths.log_path, f"start | kind={kind} workspace={workspace} lookback_hours={lookback_hours}")

    synced_count = 0
    sync_note = "local journal only"
    if not args.no_sync:
        synced_count, sync_note = _sync_user_fills(
            workspace=workspace,
            hl_client=hl_client,
            trade_journal=trade_journal,
            lookback_hours=lookback_hours,
            log_path=paths.log_path,
        )
        _append_log(paths.log_path, f"sync | {sync_note}")
    else:
        sync_note = "sync skipped by request; local journal only"

    records = _read_jsonl(workspace / "data" / "trade_journal.jsonl")
    review_records = [record for record in records if _safe_int(record.get("ts_ms")) <= end_ms]
    current_records = [record for record in records if _within(_safe_int(record.get("ts_ms")), start_ms, end_ms)]
    previous_records = [record for record in records if _within(_safe_int(record.get("ts_ms")), previous_start_ms, previous_end_ms)]

    trade_reviews = _build_trade_reviews(review_records)
    window_trade_reviews = [
        review
        for review in trade_reviews
        if any(_within(_safe_int(fill.get("ts_ms")), start_ms, end_ms) for fill in review["close_fills"])
    ]
    current_summary = {
        "fill_count": sum(1 for record in current_records if record.get("kind") == "fill"),
        "closed_fill_count": sum(1 for record in current_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) != 0.0),
        "wins": sum(1 for record in current_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) > 0),
        "losses": sum(1 for record in current_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) < 0),
        "flat": sum(1 for record in current_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) == 0.0),
        "closed_pnl": round(sum(_safe_float(record.get("closed_pnl")) for record in current_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) != 0.0), 4),
        "fees": round(sum(review["fees"] for review in window_trade_reviews if review["close_fills"]), 4),
        "trade_log_count": sum(1 for record in current_records if record.get("kind") == "trade_log"),
        "closed_trade_count": sum(1 for review in window_trade_reviews if review["close_fills"]),
        "crossed_close_count": sum(1 for review in window_trade_reviews for fill in review["close_fills"] if fill.get("crossed")),
        "crossed_close_total": sum(len(review["close_fills"]) for review in window_trade_reviews),
    }
    previous_summary = {
        "fill_count": sum(1 for record in previous_records if record.get("kind") == "fill"),
        "closed_pnl": round(sum(_safe_float(record.get("closed_pnl")) for record in previous_records if record.get("kind") == "fill" and _safe_float(record.get("closed_pnl")) != 0.0), 4),
    }

    if current_summary["fill_count"] == 0 and current_summary["trade_log_count"] == 0:
        markdown, simplex = _render_failure_note(
            workspace_name=workspace.name,
            kind=kind,
            window_label=window_label,
            sync_note=sync_note,
            reason="no fills or trade logs were available in the requested window",
        )
        failure_note = True
    else:
        sync_note_display = sync_note if synced_count <= 0 else f"{sync_note} (+{synced_count} new fills)"
        markdown, simplex = _render_report_body(
            workspace_name=workspace.name,
            kind=kind,
            window_label=window_label,
            summary=current_summary,
            trade_reviews=window_trade_reviews,
            sync_note=sync_note_display,
            generated_at=now,
        )
        if previous_summary["fill_count"] > 0:
            markdown += f"\n## Comparison\n- Prior {kind} window closed PnL: {previous_summary['closed_pnl']:+.4f} USDC\n"
        failure_note = False

    validation_errors = _validate_report(markdown, simplex, failure_note=failure_note)
    if validation_errors and not failure_note:
        markdown, simplex = _render_failure_note(
            workspace_name=workspace.name,
            kind=kind,
            window_label=window_label,
            sync_note=sync_note,
            reason="; ".join(validation_errors),
        )
        failure_note = True
        validation_errors = _validate_report(markdown, simplex, failure_note=True)

    if validation_errors:
        for error in validation_errors:
            _append_log(paths.log_path, f"validation-error | {error}")
        print("\n".join(validation_errors), file=sys.stderr)
        return 1

    try:
        _write_text_atomic(paths.report_path, markdown)
        _write_text_atomic(paths.latest_markdown, markdown)
        _write_text_atomic(paths.latest_simplex, simplex)
    except Exception as exc:
        fallback_root = Path(tempfile.gettempdir()) / "hyperbot-reports" / workspace.name / kind
        fallback_root.mkdir(parents=True, exist_ok=True)
        fallback_markdown = fallback_root / paths.report_path.name
        fallback_latest_md = fallback_root / paths.latest_markdown.name
        fallback_simplex = fallback_root / paths.latest_simplex.name
        _write_text_atomic(fallback_markdown, markdown)
        _write_text_atomic(fallback_latest_md, markdown)
        _write_text_atomic(fallback_simplex, simplex)
        _append_log(paths.log_path, f"write-fallback | {exc}")
        paths = PipelinePaths(
            workspace=workspace,
            report_dir=fallback_root,
            latest_markdown=fallback_latest_md,
            latest_simplex=fallback_simplex,
            report_path=fallback_markdown,
            state_path=fallback_root / "report_pipeline_state.json",
            log_path=paths.log_path,
            used_staging=True,
        )

    state = {
        "workspace": str(workspace),
        "kind": kind,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "synced_count": synced_count,
        "sync_note": sync_note,
        "report_path": str(paths.report_path),
        "latest_markdown": str(paths.latest_markdown),
        "latest_simplex": str(paths.latest_simplex),
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "failure_note": failure_note,
    }
    try:
        _write_text_atomic(paths.state_path, json.dumps(state, indent=2, sort_keys=True))
    except Exception as exc:
        _append_log(paths.log_path, f"state-write-failed | {exc}")

    _append_log(paths.log_path, f"generated | report={paths.report_path} failure_note={failure_note}")

    if args.no_send:
        print(f"Report generated at {paths.report_path}", flush=True)
        return 0

    outcome = send_simplex_report.send_report(
        paths.latest_simplex,
        db_prefix=args.db_prefix,
        contact=args.contact,
        binary=args.binary,
        timeout_seconds=args.timeout_seconds,
        retry_count=args.send_retries,
        retry_delay_seconds=args.send_retry_delay_seconds,
    )
    _append_log(paths.log_path, f"send | ok={outcome.ok} attempts={outcome.attempts} code={outcome.returncode}")
    state["delivery_ok"] = outcome.ok
    state["delivery_attempts"] = outcome.attempts
    state["delivery_returncode"] = outcome.returncode
    state["delivery_message"] = outcome.message
    try:
        _write_text_atomic(paths.state_path, json.dumps(state, indent=2, sort_keys=True))
    except Exception as exc:
        _append_log(paths.log_path, f"state-write-failed | send-status | {exc}")
    if outcome.ok:
        print(f"Report sent via SimpleX from {paths.latest_simplex}", flush=True)
        return 0

    print(f"SimpleX delivery failed: {outcome.message}", file=sys.stderr, flush=True)
    return outcome.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate, validate, and send Hyperbot reports.")
    parser.add_argument(
        "--workspace",
        default=os.environ.get("HYPERBOT_WORKSPACE", str(DEFAULT_WORKSPACE)),
        help="Workspace directory that contains data/ and scripts/",
    )
    parser.add_argument("--kind", choices=("daily", "weekly"), default=DEFAULT_KIND, help="Report cadence to build")
    parser.add_argument("--lookback-hours", type=int, default=0, help="Window size in hours; defaults to the report cadence")
    parser.add_argument("--no-sync", action="store_true", help="Skip Hyperliquid fill sync and reuse the local journal")
    parser.add_argument("--no-send", action="store_true", help="Generate and validate only")
    parser.add_argument("--db-prefix", default=os.environ.get("HYPERBOT_SIMPLEX_DB_PREFIX", send_simplex_report.DEFAULT_DB_PREFIX), help="SimpleX database prefix")
    parser.add_argument("--contact", default=os.environ.get("HYPERBOT_SIMPLEX_CONTACT", send_simplex_report.DEFAULT_CONTACT), help="SimpleX contact name")
    parser.add_argument("--binary", default=os.environ.get("HYPERBOT_SIMPLEX_BINARY", "simplex-chat"), help="SimpleX CLI binary")
    parser.add_argument("--timeout-seconds", type=int, default=8, help="Seconds to wait after sending")
    parser.add_argument("--send-retries", type=int, default=3, help="How many times to retry transient SimpleX send failures")
    parser.add_argument("--send-retry-delay-seconds", type=float, default=2.0, help="Base delay between SimpleX retries")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
