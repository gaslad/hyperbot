#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path | None = None) -> int:
    proc = subprocess.run(cmd, cwd=cwd or ROOT)
    return proc.returncode


def run_capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyperbot",
        description="Local CLI for generating and validating Hyperbot trading workspaces.",
    )
    parser.add_argument("--local-only", action="store_true", help="Disable any future model-dependent features; use cached data only")
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

    run_cmd = subparsers.add_parser("run", help="Full pipeline: create workspace -> validate -> profile -> safe-apply revisions")
    run_cmd.add_argument("workspace_name")
    run_cmd.add_argument("--output-dir", required=True)
    run_cmd.add_argument("--symbol", default="BTCUSDT")
    run_cmd.add_argument("--strategy-pack", action="append", default=[])
    run_cmd.add_argument("--account-mode", choices=("test", "production"), default="test")
    run_cmd.add_argument("--max-leverage", type=float, default=4.0)
    run_cmd.add_argument("--notification-email", default="")
    run_cmd.add_argument("--profile-days", type=int, default=90)
    run_cmd.add_argument("--force", action="store_true", help="Replace existing workspace if it exists")

    connect_cmd = subparsers.add_parser("connect", help="Connect your Hyperliquid wallet via browser (one-click)")
    connect_cmd.add_argument("--status", action="store_true", help="Check if a wallet is already connected")

    dash_cmd = subparsers.add_parser("dashboard", help="Launch the web dashboard for a workspace (one command does everything)")
    dash_cmd.add_argument("workspace_path", nargs="?", default=None, help="Path to workspace directory (default: ./hyperbot-workspace)")
    dash_cmd.add_argument("--live", action="store_true", help="Enable live trading controls in the dashboard")
    dash_cmd.add_argument("--confirm-risk", action="store_true", help="Confirm you understand live trading risks")
    dash_cmd.add_argument("--port", type=int, default=0, help="Port to run on (0 = auto)")

    return parser


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_sdk() -> bool:
    """Install hyperliquid-python-sdk if missing. Returns True if available."""
    try:
        import importlib
        importlib.import_module("hyperliquid")
        return True
    except ImportError:
        pass
    log("[dashboard] Installing hyperliquid-python-sdk...")
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", "hyperliquid-python-sdk"],
    ).returncode
    if rc != 0:
        log("[dashboard] WARNING: Could not install hyperliquid-python-sdk.")
        log("[dashboard]   Live order execution won't work. View-only mode is fine.")
        return False
    return True


def launch_dashboard(args: argparse.Namespace) -> int:
    """One-command flow: ensure deps → generate workspace if needed → launch dashboard."""
    # Default workspace path: next to the repo
    if args.workspace_path:
        workspace = Path(args.workspace_path).expanduser().resolve()
    else:
        workspace = ROOT.parent / "hyperbot-workspace"

    # Step 1: Ensure SDK is available
    ensure_sdk()

    # Step 2: Generate workspace if it doesn't exist
    # The wizard handles pair selection, strategy, risk, and credentials
    # so we create a generic workspace with all packs and a placeholder symbol
    if not workspace.exists():
        log(f"[dashboard] Creating workspace at {workspace}...")
        output_dir = str(workspace.parent)
        workspace_name = workspace.name
        packs = ["trend_pullback", "compression_breakout", "liquidity_sweep_reversal"]
        create_cmd = [
            sys.executable,
            str(ROOT / "scripts" / "create_workspace.py"),
            workspace_name,
            "--output-dir", output_dir,
            "--symbol", "BTCUSDT",  # placeholder — wizard will override
            "--account-mode", "test",
            "--skip-profile",
        ]
        for pack in packs:
            create_cmd.extend(["--strategy-pack", pack])
        rc = subprocess.run(create_cmd).returncode
        if rc != 0:
            log("[dashboard] Workspace creation failed.")
            return rc
        log(f"[dashboard] Workspace created at {workspace}")
    else:
        log(f"[dashboard] Using existing workspace: {workspace}")

    # Step 4: Launch dashboard
    dash_script = workspace / "scripts" / "dashboard.py"
    if not dash_script.exists():
        log(f"[dashboard] ERROR: {dash_script} not found. Re-generate the workspace with --force.")
        return 1

    dash_cmd = [sys.executable, str(dash_script)]
    if args.live:
        if not args.confirm_risk:
            log("[dashboard] ERROR: --live requires --confirm-risk. Real money at stake.")
            return 1
        dash_cmd.extend(["--live", "--confirm-risk"])
    if args.port:
        dash_cmd.extend(["--port", str(args.port)])

    log(f"[dashboard] Launching {'LIVE' if args.live else 'view-only'} dashboard...")
    return subprocess.run(dash_cmd, cwd=workspace).returncode


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    local_only = getattr(args, "local_only", False)

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

    if args.command == "run":
        return run_pipeline(args, local_only)

    if args.command == "connect":
        from connect.server import run_server, read_credential
        if args.status:
            master = read_credential("master_address")
            agent = read_credential("agent_address")
            if master:
                log(f"Connected: {master}")
                log(f"Agent:     {agent}")
            else:
                log("Not connected. Run: hyperbot connect")
            return 0
        return run_server()

    if args.command == "dashboard":
        return launch_dashboard(args)

    parser.error(f"unknown command: {args.command}")
    return 2


def run_pipeline(args: argparse.Namespace, local_only: bool) -> int:
    """Full pipeline: create -> validate -> profile -> auto-apply-safe."""
    target = Path(args.output_dir).expanduser() / args.workspace_name
    packs = args.strategy_pack or ["trend_pullback"]
    log(f"[hyperbot run] target: {target}")

    # Handle --force: remove existing workspace
    if target.exists():
        if getattr(args, "force", False):
            log(f"[hyperbot run] --force: removing existing {target}")
            shutil.rmtree(target)
        else:
            log(f"[hyperbot run] ERROR: {target} already exists. Use --force to replace it.")
            return 1

    # Step 1: Create workspace (skip-profile, we run it ourselves with cache flags)
    log(f"[hyperbot run] Step 1/4: Creating workspace {args.workspace_name}...")
    create_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "create_workspace.py"),
        args.workspace_name,
        "--output-dir", args.output_dir,
        "--symbol", args.symbol,
        "--account-mode", args.account_mode,
        "--max-leverage", str(args.max_leverage),
        "--notification-email", args.notification_email,
        "--profile-days", str(args.profile_days),
        "--skip-profile",
    ]
    for pack in packs:
        create_cmd.extend(["--strategy-pack", pack])
    rc = run(create_cmd)
    if rc != 0:
        log("[hyperbot run] FAILED at workspace creation.")
        return rc

    # Step 2: Validate
    log("[hyperbot run] Step 2/4: Validating workspace...")
    rc = run([sys.executable, str(ROOT / "scripts" / "validate_apply_revision.py")])
    if rc != 0:
        log("[hyperbot run] WARNING: validation returned non-zero (may be expected for fresh workspace).")

    # Step 3: Profile each installed strategy
    log("[hyperbot run] Step 3/4: Profiling symbol and generating revisions...")
    manifest_path = target / "hyperbot.workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile_script = target / "scripts" / "profile_symbol_strategy.py"

    for installed in manifest.get("strategy_packs", []):
        strategy_id = installed["strategy_id"]
        profile_cmd = [
            sys.executable, "-B", str(profile_script),
            "--days", str(args.profile_days),
            "--strategy-id", strategy_id,
            "--json",
        ]
        if local_only:
            profile_cmd.append("--offline")
        rc_p, stdout, stderr = run_capture(profile_cmd, cwd=target)
        if rc_p != 0:
            err_lines = [l for l in stderr.strip().splitlines() if l.strip()]
            err_msg = err_lines[-1] if err_lines else "unknown error"
            log(f"  {strategy_id}: profile FAILED — {err_msg}")
        else:
            log(f"  {strategy_id}: profile OK")

    # Step 4: Auto-apply revisions within policy safe bands
    log("[hyperbot run] Step 4/4: Auto-applying safe revisions...")
    apply_script = target / "scripts" / "apply_revision.py"
    policy_path = target / "config" / "policy" / "operator-policy.json"

    for installed in manifest.get("strategy_packs", []):
        strategy_id = installed["strategy_id"]
        apply_cmd = [
            sys.executable, "-B", str(apply_script),
            "--strategy-id", strategy_id,
            "--auto-apply-safe",
            "--json",
        ]
        if policy_path.exists():
            apply_cmd.extend(["--policy", str(policy_path)])
        rc_a, stdout, stderr = run_capture(apply_cmd, cwd=target)
        if rc_a != 0:
            err_lines = [l for l in stderr.strip().splitlines() if l.strip()]
            err_msg = err_lines[-1] if err_lines else "unknown error"
            log(f"  {strategy_id}: auto-apply FAILED — {err_msg}")
        else:
            try:
                result = json.loads(stdout)
                mode = result.get("mode", "unknown")
                n_changes = len(result.get("changes", []))
                safe_check = result.get("policy_check", {})
                if mode == "auto-apply":
                    log(f"  {strategy_id}: auto-applied {n_changes} changes (within policy safe bands)")
                elif mode == "preview":
                    reasons = safe_check.get("reasons", [])
                    log(f"  {strategy_id}: skipped (outside safe bands: {'; '.join(reasons)})")
                else:
                    log(f"  {strategy_id}: {mode}, {n_changes} changes")
            except json.JSONDecodeError:
                log(f"  {strategy_id}: completed (non-JSON output)")

    log(f"\n[hyperbot run] Done. Workspace ready at: {target}")
    log("  Live trading remains disabled. To enable, explicitly edit the workspace config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
