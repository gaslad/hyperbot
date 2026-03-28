#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFEST = ROOT / "hyperbot.workspace.json"
CONFIG_DIR = ROOT / "config" / "strategies"
REVISION_DIR = ROOT / "research" / "revisions"
BACKUP_DIR = CONFIG_DIR / "backups"
DEFAULT_POLICY_PATH = ROOT / "config" / "policy" / "operator-policy.json"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def discover_latest_revision(strategy_id: str) -> Path:
    matches = sorted(REVISION_DIR.glob(f"*_{strategy_id}_*_revision_*.json"))
    if not matches:
        raise SystemExit(f"no revision files found for strategy_id: {strategy_id}")
    return matches[-1]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_prefix = f"{prefix}.{key}" if prefix else key
            out.update(flatten(value, next_prefix))
    else:
        out[prefix] = obj
    return out


def diff_paths(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    flat_before = flatten(before)
    flat_after = flatten(after)
    paths = sorted(set(flat_before) | set(flat_after))
    changes: list[dict[str, Any]] = []
    for path in paths:
        old = flat_before.get(path)
        new = flat_after.get(path)
        if old != new:
            changes.append({"path": path, "before": old, "after": new})
    return changes


def load_policy(policy_path: Path | None) -> dict[str, Any]:
    path = policy_path or DEFAULT_POLICY_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def revision_within_safe_bands(config: dict[str, Any], revision: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str]]:
    """Check whether a revision stays within policy safe bands.

    Returns (safe, reasons) where safe is True when auto-apply is allowed.
    """
    auto_apply = policy.get("auto_apply", {})
    if not auto_apply.get("enabled", False):
        return False, ["auto_apply is disabled in policy"]

    bands = auto_apply.get("safe_bands", {})
    overrides = revision.get("recommended_overrides", {})
    violations: list[str] = []

    # Check leverage unchanged or lower
    new_leverage = _deep_get(overrides, "risk.max_leverage")
    old_leverage = _deep_get(config, "risk.max_leverage")
    ceiling = bands.get("leverage_max", 4.0)
    if new_leverage is not None:
        if old_leverage is not None and new_leverage > old_leverage:
            violations.append(f"leverage increased: {old_leverage} -> {new_leverage}")
        if new_leverage > ceiling:
            violations.append(f"leverage {new_leverage} exceeds policy ceiling {ceiling}")

    # Check risk_per_trade unchanged or lower
    new_risk = _deep_get(overrides, "risk.position_sizing.risk_per_trade_pct")
    old_risk = _deep_get(config, "risk.position_sizing.risk_per_trade_pct")
    risk_ceiling = bands.get("risk_per_trade_pct_max", 1.0)
    if new_risk is not None:
        if old_risk is not None and new_risk > old_risk:
            violations.append(f"risk_per_trade increased: {old_risk} -> {new_risk}")
        if new_risk > risk_ceiling:
            violations.append(f"risk_per_trade {new_risk} exceeds policy ceiling {risk_ceiling}")

    # Check stop-loss not widened
    may_widen = bands.get("stop_loss_may_widen", False)
    new_inv = _deep_get(overrides, "risk.invalidation_below_sma_pct")
    old_inv = _deep_get(config, "risk.invalidation_below_sma_pct")
    inv_ceiling = bands.get("invalidation_below_sma_pct_max", 5.0)
    if new_inv is not None:
        if not may_widen and old_inv is not None and new_inv > old_inv:
            violations.append(f"stop-loss widened: invalidation {old_inv} -> {new_inv}")
        if new_inv > inv_ceiling:
            violations.append(f"invalidation {new_inv} exceeds policy ceiling {inv_ceiling}")

    # Check overextension filter within bounds
    new_ext = _deep_get(overrides, "filters.overextension_max_pct")
    ext_ceiling = bands.get("overextension_max_pct_max", 25.0)
    if new_ext is not None and new_ext > ext_ceiling:
        violations.append(f"overextension_max_pct {new_ext} exceeds policy ceiling {ext_ceiling}")

    if violations:
        return False, violations
    return True, []


def _deep_get(obj: dict[str, Any], dotted_path: str) -> Any:
    parts = dotted_path.split(".")
    current: Any = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def validate_revision(workspace: dict[str, Any], config: dict[str, Any], revision: dict[str, Any], revision_path: Path) -> None:
    strategy_id = revision.get("strategy_id")
    installed = {item["strategy_id"]: item for item in workspace.get("strategy_packs", [])}
    if strategy_id not in installed:
        raise SystemExit(f"revision strategy is not installed in this workspace: {strategy_id}")
    if config.get("strategy_id") != strategy_id:
        raise SystemExit("config strategy_id does not match revision strategy_id")

    config_symbol = config.get("market", {}).get("symbol")
    profile_symbol = revision.get("profile_summary", {}).get("symbol")
    workspace_symbol = workspace.get("symbol")
    for label, symbol in (("config", config_symbol), ("workspace", workspace_symbol), ("revision", profile_symbol)):
        if not symbol:
            raise SystemExit(f"missing symbol in {label} for validation: {revision_path}")
    if not (config_symbol == workspace_symbol == profile_symbol):
        raise SystemExit("symbol mismatch between config, workspace, and revision")

    overrides = revision.get("recommended_overrides")
    if not isinstance(overrides, dict) or not overrides:
        raise SystemExit("revision has no recommended_overrides to apply")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply a token-specific revision to an installed strategy config.")
    parser.add_argument("--strategy-id", help="Installed strategy id in this workspace")
    parser.add_argument("--revision", help="Path to a specific revision JSON file")
    parser.add_argument("--config", help="Optional explicit config path")
    parser.add_argument("--apply", action="store_true", help="Write the merged config and create a backup")
    parser.add_argument("--auto-apply-safe", action="store_true", help="Auto-apply if revision stays within policy safe bands")
    parser.add_argument("--policy", help="Path to operator policy JSON (default: config/policy/operator-policy.json)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = load_json(WORKSPACE_MANIFEST)

    if not args.revision and not args.strategy_id:
        raise SystemExit("either --strategy-id or --revision is required")

    revision_path = Path(args.revision).expanduser() if args.revision else discover_latest_revision(args.strategy_id)
    revision = load_json(revision_path)
    strategy_id = args.strategy_id or revision.get("strategy_id")
    if not strategy_id:
        raise SystemExit("could not infer strategy_id from arguments or revision file")

    config_path = Path(args.config).expanduser() if args.config else (CONFIG_DIR / f"{strategy_id}.json")
    if not config_path.exists():
        raise SystemExit(f"config not found: {config_path}")

    config = load_json(config_path)
    validate_revision(workspace, config, revision, revision_path)

    overrides = revision["recommended_overrides"]
    merged = deep_merge(config, overrides)
    changes = diff_paths(config, merged)

    # Determine effective mode
    do_apply = args.apply
    policy_check: dict[str, Any] = {}
    if args.auto_apply_safe and not args.apply:
        policy_path = Path(args.policy).expanduser() if args.policy else None
        policy = load_policy(policy_path)
        safe, reasons = revision_within_safe_bands(config, revision, policy)
        policy_check = {"safe": safe, "reasons": reasons}
        if safe:
            do_apply = True

    result = {
        "mode": "auto-apply" if (do_apply and args.auto_apply_safe) else ("apply" if do_apply else "preview"),
        "strategy_id": strategy_id,
        "config_path": str(config_path),
        "revision_path": str(revision_path),
        "changes": changes,
        "changed_paths": [item["path"] for item in changes],
        "backup_path": None,
        "policy_check": policy_check if policy_check else None,
    }

    if do_apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"{strategy_id}_{utc_stamp()}.json"
        write_json(backup_path, config)
        write_json(config_path, merged)
        result["backup_path"] = str(backup_path)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Mode:          {result['mode']}")
    print(f"Strategy:      {strategy_id}")
    print(f"Config:        {config_path}")
    print(f"Revision:      {revision_path}")
    print(f"Changes:       {len(changes)}")
    if result["backup_path"]:
        print(f"Backup:        {result['backup_path']}")
    for item in changes:
        print(f"- {item['path']}: {item['before']} -> {item['after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
