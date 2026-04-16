#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hl_client

DAY_MS = 24 * 60 * 60 * 1000
DEFAULT_FILL_LOOKBACK_DAYS = 30
WEEK_WINDOW_DAYS = 7
FILL_POLL_MIN_INTERVAL_MS = 60 * 1000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _window_label(start_ms: int, end_ms: int) -> str:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date().isoformat()
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).date().isoformat()
    return f"{start} to {end}"


def _bucket_action(action: str) -> str:
    action = action.upper()
    if action in {"BUY", "SELL", "FILLED"}:
        return "execution"
    if action in {"SL_SET", "TP1_SET", "TP2_SET", "TP_SET", "MANAGE_FAIL", "SL_MOVE"}:
        return "management"
    if action in {"SKIP", "BLOCK", "HALT", "COOLDOWN"}:
        return "filtering"
    if action in {"REJECTED", "LEV_FAIL", "SL_FAIL", "TP1_FAIL", "TP2_FAIL", "CLOSE_FAIL"}:
        return "risk"
    if action in {"START", "STOP", "SETTINGS", "WALLET", "ADD_PAIR", "REMOVE_PAIR", "CLOSE"}:
        return "operator"
    return "other"


def _normalize_note(note: str) -> str:
    note = note.strip()
    if not note:
        return "no note"
    for separator in (" | ", " — ", "; "):
        if separator in note:
            note = note.split(separator, 1)[1].strip()
    return note[:180]


def _extract_order_id(text: str) -> int | None:
    match = re.search(r"\boid=(\d+)\b", text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class TradeJournal:
    def __init__(self, root: Path, manifest_path: Path | None = None) -> None:
        self.root = root
        self.manifest_path = manifest_path or (root / "hyperbot.workspace.json")
        self.data_dir = root / "data"
        self.journal_path = self.data_dir / "trade_journal.jsonl"
        self.state_path = self.data_dir / "trade_journal_state.json"
        self.report_dir = root / "docs" / "reports" / "weekly"
        self.latest_report_path = self.report_dir / "latest-weekly.md"
        self.latest_simplex_path = self.report_dir / "latest-weekly-simplex.txt"
        self._state = self._load_state()
        self._seen_fill_keys = set(self._state.get("seen_fill_keys", []))

    def _ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "seen_fill_keys": [],
                "last_fill_sync_ms": 0,
                "last_fill_poll_ms": 0,
                "last_report_generated_at_ms": 0,
                "last_report_path": "",
            }
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "seen_fill_keys": [],
                "last_fill_sync_ms": 0,
                "last_fill_poll_ms": 0,
                "last_report_generated_at_ms": 0,
                "last_report_path": "",
            }

    def _save_state(self) -> None:
        self._ensure_dirs()
        payload = dict(self._state)
        payload["seen_fill_keys"] = sorted(self._seen_fill_keys)
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _append_record(self, record: dict[str, Any]) -> None:
        self._ensure_dirs()
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")

    def record_trade_log(self, entry: dict[str, Any]) -> None:
        note = str(entry.get("note", ""))
        order_id = _safe_int(entry.get("order_id"))
        if not order_id:
            order_id = _safe_int(_extract_order_id(note))
        record = {
            "kind": "trade_log",
            "ts_ms": _safe_int(entry.get("ts_ms")),
            "ts": str(entry.get("ts") or entry.get("time") or _iso_z(_utc_now())),
            "action": str(entry.get("action", "")),
            "strategy": str(entry.get("strategy", "")),
            "order_id": order_id or None,
            "size": _safe_float(entry.get("size")),
            "price": _safe_float(entry.get("price")),
            "note": note,
            "bucket": _bucket_action(str(entry.get("action", ""))),
        }
        self._append_record(record)

    def _fill_key(self, fill: dict[str, Any]) -> str:
        for key in ("hash", "tid", "oid"):
            value = fill.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        raw = json.dumps(fill, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "raw:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _normalize_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        ts_ms = _safe_int(fill.get("time") or fill.get("timestamp") or fill.get("ts"))
        if not ts_ms:
            ts_ms = _to_ms(_utc_now())
        side = str(fill.get("side", "")).upper()
        direction = str(fill.get("dir", "")).strip()
        if side == "B":
            side = "BUY"
        elif side == "S":
            side = "SELL"
        closed_pnl = _safe_float(fill.get("closedPnl"))
        fee = _safe_float(fill.get("fee"))
        price = _safe_float(fill.get("px"))
        size = _safe_float(fill.get("sz"))
        order_id = _safe_int(fill.get("oid") or fill.get("order_id"))
        return {
            "kind": "fill",
            "fill_key": self._fill_key(fill),
            "ts_ms": ts_ms,
            "ts": _iso_z(datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)),
            "coin": str(fill.get("coin", "")),
            "side": side,
            "direction": direction,
            "price": price,
            "size": size,
            "closed_pnl": closed_pnl,
            "fee": fee,
            "order_id": order_id or None,
            "trade_id": fill.get("tid"),
            "crossed": bool(fill.get("crossed")),
            "start_position": _safe_float(fill.get("startPosition")),
            "bucket": "fill",
            "outcome": "win" if closed_pnl > 0 else "loss" if closed_pnl < 0 else "flat",
            "raw": fill,
        }

    def _build_strategy_lookup(self, records: list[dict[str, Any]]) -> dict[int, str]:
        lookup: dict[int, str] = {}
        for record in records:
            if record.get("kind") != "trade_log":
                continue
            strategy = str(record.get("strategy", "")).strip()
            if not strategy:
                continue
            order_id = _safe_int(record.get("order_id"))
            if order_id:
                lookup[order_id] = strategy
        return lookup

    def _build_coin_strategy_lookup(self, records: list[dict[str, Any]]) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for record in records:
            if record.get("kind") != "trade_log":
                continue
            strategy = str(record.get("strategy", "")).strip()
            coin = str(record.get("coin", "")).strip()
            if strategy and coin and coin not in lookup:
                lookup[coin] = strategy
        return lookup

    def sync_user_fills(
        self,
        address: str,
        base_url: str,
        *,
        lookback_days: int = DEFAULT_FILL_LOOKBACK_DAYS,
    ) -> int:
        if not address:
            return 0

        now_ms = _to_ms(_utc_now())
        last_poll_ms = _safe_int(self._state.get("last_fill_poll_ms"))
        if last_poll_ms and now_ms - last_poll_ms < FILL_POLL_MIN_INTERVAL_MS:
            return 0
        self._state["last_fill_poll_ms"] = now_ms
        last_sync_ms = _safe_int(self._state.get("last_fill_sync_ms"))
        start_ms = last_sync_ms + 1 if last_sync_ms > 0 else now_ms - lookback_days * DAY_MS
        start_ms = max(start_ms, now_ms - lookback_days * DAY_MS)
        existing_records = self._load_records()
        strategy_lookup = self._build_strategy_lookup(existing_records)
        coin_strategy_lookup = self._build_coin_strategy_lookup(existing_records)
        fills = hl_client.get_user_fills_by_time(
            address,
            start_ms=start_ms,
            end_ms=now_ms,
            base_url=base_url,
            aggregate_by_time=True,
        )

        processed = 0
        newest_ts = last_sync_ms
        for fill in sorted(fills or [], key=lambda item: _safe_int(item.get("time"))):
            normalized = self._normalize_fill(fill)
            order_id = _safe_int(normalized.get("order_id"))
            strategy = ""
            if order_id and order_id in strategy_lookup:
                strategy = strategy_lookup[order_id]
            elif normalized.get("coin"):
                strategy = coin_strategy_lookup.get(str(normalized.get("coin", "")), "")
            if strategy:
                normalized["strategy"] = strategy
                normalized["strategy_source"] = "order_id" if order_id and order_id in strategy_lookup else "coin"
            fill_key = normalized["fill_key"]
            if fill_key in self._seen_fill_keys:
                newest_ts = max(newest_ts, normalized["ts_ms"])
                continue
            self._seen_fill_keys.add(fill_key)
            newest_ts = max(newest_ts, normalized["ts_ms"])
            self._append_record(normalized)
            processed += 1

        self._state["last_fill_sync_ms"] = newest_ts
        self._save_state()
        return processed

    def _load_records(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _report_window(self, now_ms: int) -> tuple[int, int, int, int]:
        current_end = now_ms
        current_start = current_end - WEEK_WINDOW_DAYS * DAY_MS
        previous_end = current_start - 1
        previous_start = previous_end - WEEK_WINDOW_DAYS * DAY_MS
        return current_start, current_end, previous_start, previous_end

    def _summary_for_window(self, records: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
        window_records = [r for r in records if start_ms <= _safe_int(r.get("ts_ms")) <= end_ms]
        fills = [r for r in window_records if r.get("kind") == "fill"]
        trade_logs = [r for r in window_records if r.get("kind") == "trade_log"]
        closed_fills = [r for r in fills if _safe_float(r.get("closed_pnl")) != 0.0]

        fills_by_coin: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in closed_fills:
            fills_by_coin[str(item.get("coin", ""))].append(item)

        closed_pnl = sum(_safe_float(r.get("closed_pnl")) for r in closed_fills)
        fees = sum(_safe_float(r.get("fee")) for r in closed_fills)
        gross_volume = sum(_safe_float(r.get("price")) * _safe_float(r.get("size")) for r in fills)
        wins = sum(1 for r in closed_fills if _safe_float(r.get("closed_pnl")) > 0)
        losses = sum(1 for r in closed_fills if _safe_float(r.get("closed_pnl")) < 0)
        flat = sum(1 for r in closed_fills if _safe_float(r.get("closed_pnl")) == 0)
        attributed_fills = [r for r in closed_fills if str(r.get("strategy", "")).strip()]

        action_counts = Counter(str(r.get("action", "")).upper() for r in trade_logs)
        bucket_counts = Counter(str(r.get("bucket", "other")) for r in trade_logs)
        note_counts = Counter(
            _normalize_note(str(r.get("note", "")))
            for r in trade_logs
            if str(r.get("action", "")).upper() in {"SKIP", "REJECTED", "SL_FAIL", "LEV_FAIL", "MANAGE_FAIL", "CLOSE_FAIL"}
        )
        strategy_activity = Counter(str(r.get("strategy", "unknown")) for r in trade_logs if r.get("strategy"))
        strategy_review: dict[str, dict[str, int]] = {}
        for strategy in sorted(strategy_activity):
            strategy_rows = [r for r in trade_logs if str(r.get("strategy", "")) == strategy]
            strategy_actions = Counter(str(r.get("action", "")).upper() for r in strategy_rows)
            strategy_review[strategy] = {
                "events": len(strategy_rows),
                "entries": strategy_actions.get("BUY", 0) + strategy_actions.get("SELL", 0),
                "fills": strategy_actions.get("FILLED", 0),
                "skips": strategy_actions.get("SKIP", 0),
                "rejects": strategy_actions.get("REJECTED", 0),
                "risk_flags": (
                    strategy_actions.get("SL_FAIL", 0)
                    + strategy_actions.get("LEV_FAIL", 0)
                    + strategy_actions.get("MANAGE_FAIL", 0)
                ),
            }
        fill_strategy_review: dict[str, dict[str, Any]] = {}
        fill_strategy_activity = Counter(str(r.get("strategy", "unknown")) for r in attributed_fills if r.get("strategy"))
        for strategy in sorted(fill_strategy_activity):
            strategy_fills = [r for r in attributed_fills if str(r.get("strategy", "")) == strategy]
            fill_strategy_review[strategy] = {
                "fills": len(strategy_fills),
                "wins": sum(1 for r in strategy_fills if _safe_float(r.get("closed_pnl")) > 0),
                "losses": sum(1 for r in strategy_fills if _safe_float(r.get("closed_pnl")) < 0),
                "flat": sum(1 for r in strategy_fills if _safe_float(r.get("closed_pnl")) == 0),
                "net_pnl": round(sum(_safe_float(r.get("closed_pnl")) for r in strategy_fills), 4),
                "fees": round(sum(_safe_float(r.get("fee")) for r in strategy_fills), 4),
            }
        coin_activity = Counter(str(r.get("coin", "")) for r in window_records if r.get("coin"))
        outcome_by_coin = {
            coin: {
                "wins": sum(1 for item in items if _safe_float(item.get("closed_pnl")) > 0),
                "losses": sum(1 for item in items if _safe_float(item.get("closed_pnl")) < 0),
                "net_pnl": round(sum(_safe_float(item.get("closed_pnl")) for item in items), 4),
            }
            for coin, items in fills_by_coin.items()
            if coin
        }

        return {
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "window_label": _window_label(start_ms, end_ms),
            "fill_count": len(fills),
            "closed_fill_count": len(closed_fills),
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate_pct": round((wins / len(closed_fills) * 100.0) if closed_fills else 0.0, 1),
            "closed_pnl": round(closed_pnl, 4),
            "fees": round(fees, 4),
            "gross_volume": round(gross_volume, 4),
            "action_counts": action_counts,
            "bucket_counts": bucket_counts,
            "note_counts": note_counts,
            "strategy_activity": strategy_activity,
            "strategy_review": strategy_review,
            "fill_strategy_review": fill_strategy_review,
            "attributed_fill_count": len(attributed_fills),
            "coin_activity": coin_activity,
            "outcome_by_coin": outcome_by_coin,
            "trade_log_count": len(trade_logs),
        }

    def _learning_notes(self, current: dict[str, Any], previous: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
        learning: list[str] = []
        suggestions: list[str] = []

        action_counts: Counter = current["action_counts"]
        note_counts: Counter = current["note_counts"]
        strategy_activity: Counter = current["strategy_activity"]
        closed_pnl = current["closed_pnl"]
        wins = current["wins"]
        losses = current["losses"]
        win_rate = current["win_rate_pct"]
        rejected = action_counts.get("REJECTED", 0)
        filled = action_counts.get("FILLED", 0)

        # -- Insights --

        if wins > losses and closed_pnl > 0:
            learning.append("Profitable day — current filters are selecting quality setups.")
        elif losses > wins:
            learning.append("More losses than wins today; entry quality should be reviewed in the next tuning pass.")
        else:
            learning.append("Near break-even on closed fills; gains will come from better filtering, not more trades.")

        if action_counts.get("SKIP", 0) > filled:
            learning.append("Most opportunities were skipped — filters are doing real work instead of forcing trades.")

        if action_counts.get("SL_FAIL", 0) > 0:
            learning.append("Stop-loss placement failed at least once — positions may have been briefly unprotected.")
            suggestions.append("Audit SL placement logic; ensure every entry has a confirmed SL before the next scan cycle.")

        if action_counts.get("LEV_FAIL", 0) > 0:
            learning.append("Leverage updates failed — margin or leverage caps may need a preflight check.")
            suggestions.append("Add a margin/leverage preflight check before entry, or reduce target leverage.")

        # Rejection diagnosis
        if rejected > 0:
            reject_rate = rejected / max(rejected + filled, 1) * 100
            learning.append(f"{rejected} order(s) rejected ({reject_rate:.0f}% rejection rate).")
            if note_counts:
                top_note, top_count = note_counts.most_common(1)[0]
                if "invalid price" in top_note.lower():
                    suggestions.append(f"**{top_count}x 'invalid price' rejections** — prices must have ≤5 significant figures on Hyperliquid. Verify round_price() is enforcing this.")
                elif "insufficient" in top_note.lower():
                    suggestions.append(f"**{top_count}x '{top_note}'** — reduce position size or check margin balance.")
                else:
                    suggestions.append(f"Top rejection reason: {top_note} ({top_count}x) — investigate and add a pre-trade check.")

        # Downtime estimation from trade log gaps
        records_in_window = current.get("trade_log_count", 0)
        window_hours = (current["window_end_ms"] - current["window_start_ms"]) / 3_600_000
        if records_in_window > 0 and window_hours > 0:
            events_per_hour = records_in_window / window_hours
            if events_per_hour < 1.0 and window_hours >= 12:
                est_active_hours = min(records_in_window / max(events_per_hour, 0.5), window_hours)
                est_downtime = window_hours - est_active_hours
                if est_downtime > 2:
                    learning.append(f"Estimated ~{est_downtime:.0f}h of inactivity in the last {window_hours:.0f}h window — potential missed opportunities.")
                    suggestions.append("Ensure the bot runs 20+ hours/day; crypto markets produce setups in all sessions.")

        # Per-coin performance suggestions
        outcome = current.get("outcome_by_coin", {})
        losing_coins = [c for c, s in outcome.items() if s["losses"] > s["wins"] and s["net_pnl"] < -5]
        winning_coins = [c for c, s in outcome.items() if s["wins"] > s["losses"] and s["net_pnl"] > 5]
        if losing_coins:
            suggestions.append(f"Consider removing or tightening filters for underperforming coins: {', '.join(losing_coins)}.")
        if winning_coins:
            learning.append(f"Strong performers today: {', '.join(winning_coins)} — validate edge persists before sizing up.")

        # Fee drag
        fees = current.get("fees", 0)
        if closed_pnl != 0 and fees > 0:
            fee_drag = fees / max(abs(closed_pnl), 0.01) * 100
            if fee_drag > 50:
                learning.append(f"Fees consumed {fee_drag:.0f}% of gross P&L — consider using more maker (ALO) orders to reduce costs.")

        if strategy_activity:
            top_strategy, top_count = strategy_activity.most_common(1)[0]
            learning.append(f"{top_strategy} generated the most activity ({top_count} events).")

        if previous:
            delta_win = current["win_rate_pct"] - previous["win_rate_pct"]
            delta_pnl = current["closed_pnl"] - previous["closed_pnl"]
            if delta_win > 0.5:
                suggestions.append(f"Win rate improved by {delta_win:+.1f}pp — keep recent filter changes and validate for another week.")
            elif delta_win < -0.5:
                suggestions.append(f"Win rate dropped {delta_win:+.1f}pp — consider rolling back the most recent rule changes.")
            if delta_pnl > 0:
                learning.append(f"P&L improved {delta_pnl:+.2f} USDC vs prior period.")
            elif delta_pnl < 0:
                learning.append(f"P&L declined {delta_pnl:+.2f} USDC vs prior period.")

        if win_rate >= 60 and closed_pnl > 0:
            suggestions.append("Edge looks solid — hold current strategy mix and only increase size after another consistent week.")
        elif win_rate < 45 and losses > wins:
            suggestions.append("Win rate below 45% — tighten entry filters before increasing activity or leverage.")

        if action_counts.get("COOLDOWN", 0) > 0 or action_counts.get("HALT", 0) > 0:
            learning.append("Risk guardrails (cooldowns/halts) fired — circuit breakers are active.")

        if not suggestions:
            suggestions.append("Sample size is still small — collect another week of data before making parameter changes.")

        return learning, suggestions

    def _coin_action_items(self, current: dict[str, Any]) -> list[str]:
        items: list[str] = []
        for coin, stats in sorted(
            current["outcome_by_coin"].items(),
            key=lambda pair: pair[1]["net_pnl"],
            reverse=True,
        ):
            wins = int(stats["wins"])
            losses = int(stats["losses"])
            net_pnl = float(stats["net_pnl"])
            if net_pnl > 5 and wins >= max(1, losses):
                items.append(f"{coin}: keep active and review whether the current filter set should become the default for similar coins.")
            elif net_pnl > 0:
                items.append(f"{coin}: positive week, but keep size unchanged until the sample is larger.")
            elif net_pnl < 0 or losses > wins:
                items.append(f"{coin}: tighten filters or reduce priority next week; current results are weak.")
            else:
                items.append(f"{coin}: mixed signal so far; keep collecting data before changing anything.")
        if not items and current["coin_activity"]:
            for coin, count in current["coin_activity"].most_common(5):
                items.append(f"{coin}: activity recorded, but there is not enough closed-fill evidence to change the settings yet.")
        return items

    def _render_simplex_text(
        self,
        current: dict[str, Any],
        previous: dict[str, Any] | None,
        workspace_name: str,
    ) -> str:
        lines = [
            f"{workspace_name} weekly report",
            f"Window: {current['window_label']} UTC",
            f"Closed fills: {current['closed_fill_count']} ({current['wins']}W/{current['losses']}L)",
            f"Win rate: {current['win_rate_pct']}%",
            f"Closed PnL: {current['closed_pnl']:+.4f} USDC",
            f"Fees: {current['fees']:.4f} USDC",
        ]
        if previous:
            lines.append(
                f"WoW: win rate {current['win_rate_pct'] - previous['win_rate_pct']:+.1f} pts, PnL {current['closed_pnl'] - previous['closed_pnl']:+.4f} USDC"
            )
        if current["outcome_by_coin"]:
            top_coin = max(current["outcome_by_coin"].items(), key=lambda pair: pair[1]["net_pnl"])
            weak_coin = min(current["outcome_by_coin"].items(), key=lambda pair: pair[1]["net_pnl"])
            lines.append(f"Best coin: {top_coin[0]} {top_coin[1]['net_pnl']:+.4f} USDC")
            lines.append(f"Weakest coin: {weak_coin[0]} {weak_coin[1]['net_pnl']:+.4f} USDC")
        if current["trade_log_count"] == 0:
            lines.append("Note: this report is attributed from Hyperliquid fills; local strategy event logs were not present this week.")
        return "\n".join(lines) + "\n"

    def _render_markdown(
        self,
        current: dict[str, Any],
        previous: dict[str, Any] | None,
        workspace_name: str,
    ) -> str:
        learning, suggestions = self._learning_notes(current, previous)
        current_actions = current["action_counts"]
        current_buckets = current["bucket_counts"]

        lines = [
            f"# {workspace_name} Weekly Trading Report",
            "",
            f"Window: {current['window_label']} UTC",
            f"Generated: {_iso_z(_utc_now())}",
            "",
            "## Summary",
            f"- Closed fills: {current['closed_fill_count']} ({current['wins']}W / {current['losses']}L / {current['flat']} flat)",
            f"- Win rate: {current['win_rate_pct']}%",
            f"- Closed PnL: {current['closed_pnl']:+.4f} USDC",
            f"- Fees: {current['fees']:.4f} USDC",
            f"- Notional traded: {current['gross_volume']:.4f} USDC",
            f"- Trade log events: {current['trade_log_count']}",
            "",
            "## Triage",
        ]

        for bucket, count in current_buckets.most_common():
            lines.append(f"- {bucket.title()}: {count}")
        if not current_buckets:
            lines.append("- No activity recorded in this window.")

        lines.extend([
            "",
            "## What Hyperbot Learned",
        ])
        for item in learning:
            lines.append(f"- {item}")

        lines.extend([
            "",
            "## What Improved",
        ])
        if previous:
            delta_win = current["win_rate_pct"] - previous["win_rate_pct"]
            delta_pnl = current["closed_pnl"] - previous["closed_pnl"]
            lines.append(f"- Win rate changed by {delta_win:+.1f} points versus the prior week.")
            lines.append(f"- Closed PnL changed by {delta_pnl:+.4f} USDC versus the prior week.")
            lines.append(f"- Trade-log volume changed from {previous['trade_log_count']} to {current['trade_log_count']} events.")
        else:
            lines.append("- No prior weekly sample was available for comparison.")

        lines.extend([
            "",
            "## What Changed This Week",
        ])
        if previous:
            lines.append(f"- Closed fills changed from {previous['closed_fill_count']} to {current['closed_fill_count']}.")
            lines.append(f"- Winning fills changed from {previous['wins']} to {current['wins']}; losing fills changed from {previous['losses']} to {current['losses']}.")
            lines.append(f"- Fees changed by {current['fees'] - previous['fees']:+.4f} USDC.")
        else:
            lines.append("- This is the first weekly sample, so there is no prior baseline yet.")

        lines.extend([
            "",
            "## Suggestions",
        ])
        for item in suggestions:
            lines.append(f"- {item}")

        lines.extend([
            "",
            "## Strategy Review",
        ])
        if current["strategy_review"]:
            for strategy, stats in current["strategy_review"].items():
                lines.append(
                    f"- {strategy}: {stats['events']} events, {stats['entries']} entries, {stats['fills']} fills, {stats['skips']} skips, {stats['rejects']} rejects, {stats['risk_flags']} risk flags"
                )
        else:
            lines.append("- No local strategy event log was captured this week, so only fill-level attribution is available.")

        if current["fill_strategy_review"]:
            lines.extend([
                "",
                "## Fill Attribution",
            ])
            for strategy, stats in current["fill_strategy_review"].items():
                lines.append(
                    f"- {strategy}: {stats['fills']} attributed fills, {stats['wins']}W / {stats['losses']}L / {stats['flat']} flat, net {stats['net_pnl']:+.4f} USDC, fees {stats['fees']:.4f} USDC"
                )
        elif current["attributed_fill_count"] == 0 and current["closed_fill_count"] > 0:
            lines.extend([
                "",
                "## Fill Attribution",
                "- No fills could be mapped back to a strategy from the recorded order logs in this window.",
            ])

        if current_actions:
            lines.extend([
                "",
                "## Top Actions",
            ])
            for action, count in current_actions.most_common(10):
                lines.append(f"- {action}: {count}")

        if current["coin_activity"]:
            lines.extend([
                "",
                "## Coin Activity",
            ])
            for coin, count in current["coin_activity"].most_common(10):
                outcome = current["outcome_by_coin"].get(coin, {})
                if outcome:
                    lines.append(
                        f"- {coin}: {count} events, {outcome['wins']} wins / {outcome['losses']} losses, net {outcome['net_pnl']:+.4f} USDC"
                    )
                else:
                    lines.append(f"- {coin}: {count} events")

        lines.extend([
            "",
            "## Per-Coin Actions",
        ])
        for item in self._coin_action_items(current):
            lines.append(f"- {item}")

        if current["trade_log_count"] == 0:
            lines.extend([
                "",
                "## Data Gap",
                "- This report used Hyperliquid fills successfully, but there were no local bot trade-log events for the same window. Going forward, keep the dashboard runtime active so fills and bot decisions can be attributed together.",
            ])

        lines.append("")
        return "\n".join(lines)

    def build_weekly_report(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or _utc_now()
        now_ms = _to_ms(now)
        current_start, current_end, previous_start, previous_end = self._report_window(now_ms)

        records = self._load_records()
        current = self._summary_for_window(records, current_start, current_end)
        if current["fill_count"] == 0 and current["trade_log_count"] == 0:
            return None
        previous = self._summary_for_window(records, previous_start, previous_end)
        if previous["fill_count"] == 0 and previous["trade_log_count"] == 0:
            previous = None

        workspace_name = "Hyperbot"
        if self.manifest_path.exists():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                workspace_name = str(manifest.get("workspace_name") or workspace_name)
            except Exception:
                pass

        markdown = self._render_markdown(current, previous, workspace_name)
        simplex_text = self._render_simplex_text(current, previous, workspace_name)
        report_date = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
        report_path = self.report_dir / f"weekly-report_{report_date}.md"
        self._ensure_dirs()
        report_path.write_text(markdown, encoding="utf-8")
        self.latest_report_path.write_text(markdown, encoding="utf-8")
        self.latest_simplex_path.write_text(simplex_text, encoding="utf-8")

        self._state["last_report_generated_at_ms"] = now_ms
        self._state["last_report_path"] = str(report_path)
        self._save_state()

        return {
            "report_path": report_path,
            "latest_report_path": self.latest_report_path,
            "latest_simplex_path": self.latest_simplex_path,
            "current": current,
            "previous": previous,
            "markdown": markdown,
            "simplex_text": simplex_text,
        }

    def build_daily_report(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Build a 24-hour report suitable for daily email delivery."""
        now = now or _utc_now()
        now_ms = _to_ms(now)
        start_ms = now_ms - DAY_MS
        records = self._load_records()
        current = self._summary_for_window(records, start_ms, now_ms)
        if current["fill_count"] == 0 and current["trade_log_count"] == 0:
            return None

        # Also build 7-day context for comparison
        week_start_ms = now_ms - 7 * DAY_MS
        weekly = self._summary_for_window(records, week_start_ms, now_ms)

        workspace_name = "Hyperbot"
        if self.manifest_path.exists():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                workspace_name = str(manifest.get("workspace_name") or workspace_name)
            except Exception:
                pass

        # Build a concise daily report
        learning, suggestions = self._learning_notes(current, None)
        coin_actions = self._coin_action_items(current)

        lines = [
            f"# {workspace_name} Daily Report",
            f"",
            f"**Date**: {now.strftime('%Y-%m-%d')} UTC",
            f"",
            f"## Today's Summary",
            f"- Closed fills: {current['closed_fill_count']} ({current['wins']}W / {current['losses']}L / {current['flat']} flat)",
            f"- Win rate: {current['win_rate_pct']}%",
            f"- Closed P&L: {current['closed_pnl']:+.4f} USDC",
            f"- Fees: {current['fees']:.4f} USDC",
            f"- Notional traded: {current['gross_volume']:.4f} USDC",
            f"",
        ]

        if current["fill_strategy_review"]:
            lines.append("## By Strategy")
            for strategy, stats in current["fill_strategy_review"].items():
                lines.append(
                    f"- **{strategy}**: {stats['fills']} fills, {stats['wins']}W/{stats['losses']}L, net {stats['net_pnl']:+.4f} USDC"
                )
            lines.append("")

        if current["outcome_by_coin"]:
            lines.append("## By Coin")
            for coin, stats in sorted(current["outcome_by_coin"].items(), key=lambda p: p[1]["net_pnl"], reverse=True):
                lines.append(f"- **{coin}**: {stats['wins']}W/{stats['losses']}L, net {stats['net_pnl']:+.4f} USDC")
            lines.append("")

        lines.append("## 7-Day Context")
        lines.append(f"- Weekly fills: {weekly['closed_fill_count']} ({weekly['wins']}W / {weekly['losses']}L)")
        lines.append(f"- Weekly win rate: {weekly['win_rate_pct']}%")
        lines.append(f"- Weekly P&L: {weekly['closed_pnl']:+.4f} USDC")
        lines.append("")

        if learning:
            lines.append("## Insights")
            for item in learning[:3]:
                lines.append(f"- {item}")
            lines.append("")

        if suggestions:
            lines.append("## Suggestions")
            for item in suggestions[:3]:
                lines.append(f"- {item}")
            lines.append("")

        if coin_actions:
            lines.append("## Coin Actions")
            for item in coin_actions[:5]:
                lines.append(f"- {item}")
            lines.append("")

        markdown = "\n".join(lines)

        # Save daily report
        self._ensure_dirs()
        daily_dir = self.root / "docs" / "reports" / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        report_path = daily_dir / f"daily-report_{now.strftime('%Y-%m-%d')}.md"
        report_path.write_text(markdown, encoding="utf-8")

        return {
            "report_path": report_path,
            "markdown": markdown,
            "current": current,
            "weekly_context": weekly,
        }

    def maybe_generate_weekly_report(self, *, now: datetime | None = None) -> Path | None:
        now = now or _utc_now()
        now_ms = _to_ms(now)
        last_report_ms = _safe_int(self._state.get("last_report_generated_at_ms"))
        if last_report_ms and now_ms - last_report_ms < WEEK_WINDOW_DAYS * DAY_MS:
            return None

        report = self.build_weekly_report(now=now)
        if not report:
            return None
        return report["report_path"]
