#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyperbot",
        description="Local CLI for generating and validating Hyperbot trading workspaces.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-packs", help="List available strategy packs")
    subparsers.add_parser("validate", help="Run local apply_revision validation")
    subparsers.add_parser("release-readiness", help="Run repo release-readiness checks")

    create = subparsers.add_parser("create-workspace", help="Generate a new trading workspace")
    create.add_argument("workspace_name")
    create.add_argument("--output-dir", required=True)
    create.add_argument("--symbol", default="BTCUSDT")
    create.add_argument("--strategy-pack", action="append", default=[])
    create.add_argument("--account-mode", choices=("test", "production"), default="test")
    create.add_argument("--max-leverage", type=float, default=4.0)
    create.add_argument("--notification-email", default="")
    create.add_argument("--enable-unattended", action="store_true")
    create.add_argument("--profile-days", type=int, default=90)
    create.add_argument("--skip-profile", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list-packs":
        return run([sys.executable, str(ROOT / "scripts" / "create_workspace.py"), "--list-packs"])

    if args.command == "validate":
        return run([sys.executable, str(ROOT / "scripts" / "validate_apply_revision.py")])

    if args.command == "release-readiness":
        return run([sys.executable, str(ROOT / "scripts" / "release_readiness.py")])

    if args.command == "create-workspace":
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "create_workspace.py"),
            args.workspace_name,
            "--output-dir",
            args.output_dir,
            "--symbol",
            args.symbol,
            "--account-mode",
            args.account_mode,
            "--max-leverage",
            str(args.max_leverage),
            "--notification-email",
            args.notification_email,
            "--profile-days",
            str(args.profile_days),
        ]
        for pack in args.strategy_pack:
            cmd.extend(["--strategy-pack", pack])
        if args.enable_unattended:
            cmd.append("--enable-unattended")
        if args.skip_profile:
            cmd.append("--skip-profile")
        return run(cmd)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
