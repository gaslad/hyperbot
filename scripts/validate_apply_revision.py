#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREATE_WORKSPACE = ROOT / "scripts" / "create_workspace.py"


def run(cmd: list[str], cwd: Path | None = None, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if expect_ok and proc.returncode != 0:
        raise SystemExit(
            "command failed:\n"
            f"cmd: {' '.join(cmd)}\n"
            f"exit: {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hyperbot-validate-") as temp_dir:
        workspace_parent = Path(temp_dir)
        workspace_name = "validate-apply-revision"
        symbol = "BTCUSDT"

        run(
            [
                sys.executable,
                str(CREATE_WORKSPACE),
                workspace_name,
                "--output-dir",
                str(workspace_parent),
                "--symbol",
                symbol,
                "--strategy-pack",
                "trend_pullback",
                "--skip-profile",
            ],
            cwd=ROOT,
        )

        workspace_dir = workspace_parent / workspace_name
        strategy_id = "btc_trend_pullback"
        config_path = workspace_dir / "config" / "strategies" / f"{strategy_id}.json"
        revision_path = workspace_dir / "research" / "revisions" / (
            f"{symbol.lower()}_{strategy_id}_90d_revision_20260327T000000Z.json"
        )

        revision_payload = {
            "generated_at": "2026-03-27T00:00:00Z",
            "strategy_id": strategy_id,
            "pack_id": "trend_pullback",
            "profile_summary": {
                "symbol": symbol,
            },
            "recommended_overrides": {
                "entry": {
                    "sma_period": 20,
                    "pullback_zone_pct": 4.0,
                },
                "filters": {
                    "min_pullback_pct": 2.5,
                },
                "risk": {
                    "invalidation_below_sma_pct": 2.0,
                },
            },
            "notes": [
                "Synthetic revision used by Hyperbot validation.",
            ],
        }
        write_json(revision_path, revision_payload)

        preview = run(
            [
                sys.executable,
                "scripts/apply_revision.py",
                "--strategy-id",
                strategy_id,
                "--json",
            ],
            cwd=workspace_dir,
        )
        preview_payload = json.loads(preview.stdout)
        changed_paths = set(preview_payload["changed_paths"])
        assert_true(preview_payload["mode"] == "preview", "preview mode did not report correctly")
        assert_true(preview_payload["backup_path"] is None, "preview should not create a backup")
        assert_true("entry.sma_period" in changed_paths, "expected entry.sma_period change in preview output")
        assert_true(
            "risk.invalidation_below_sma_pct" in changed_paths,
            "expected risk.invalidation_below_sma_pct change in preview output",
        )

        before_config = load_json(config_path)

        apply = run(
            [
                sys.executable,
                "scripts/apply_revision.py",
                "--strategy-id",
                strategy_id,
                "--apply",
                "--json",
            ],
            cwd=workspace_dir,
        )
        apply_payload = json.loads(apply.stdout)
        after_config = load_json(config_path)
        backup_path = Path(apply_payload["backup_path"])

        assert_true(apply_payload["mode"] == "apply", "apply mode did not report correctly")
        assert_true(backup_path.exists(), "backup file was not created")
        assert_true(load_json(backup_path) == before_config, "backup did not preserve the original config")
        assert_true(after_config["entry"]["sma_period"] == 20, "applied config did not update entry.sma_period")
        assert_true(
            after_config["risk"]["invalidation_below_sma_pct"] == 2.0,
            "applied config did not update risk invalidation",
        )

        bad_revision_path = workspace_dir / "research" / "revisions" / "bad_symbol_revision.json"
        bad_revision_payload = json.loads(json.dumps(revision_payload))
        bad_revision_payload["profile_summary"]["symbol"] = "ETHUSDT"
        write_json(bad_revision_path, bad_revision_payload)

        rejected = run(
            [
                sys.executable,
                "scripts/apply_revision.py",
                "--revision",
                str(bad_revision_path),
                "--json",
            ],
            cwd=workspace_dir,
            expect_ok=False,
        )
        assert_true(rejected.returncode != 0, "invalid revision unexpectedly succeeded")
        assert_true(
            "symbol mismatch between config, workspace pairs, and revision" in rejected.stderr,
            "invalid revision did not fail for the expected reason",
        )

        shutil.rmtree(workspace_dir)

    print("apply_revision validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
