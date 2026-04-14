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
    create.add_argument("--symbol", action="append", default=[], help="Trading pair(s) — can specify multiple times")
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
    run_cmd.add_argument("--symbol", action="append", default=[], help="Trading pair(s) — can specify multiple times")
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

    debrief_cmd = subparsers.add_parser("debrief", help="Show morning debrief — trade review with what-if analysis")
    debrief_cmd.add_argument("workspace_path", nargs="?", default=None, help="Path to workspace directory")
    debrief_cmd.add_argument("--days", type=int, default=1, help="How many days back to review (default: 1)")

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
    pip_cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        # System Python on macOS is often PEP 668 managed; user installs avoid the blocked
        # global site-packages path without relying on deprecated pip flags.
        pip_cmd.append("--user")
    pip_cmd.append("hyperliquid-python-sdk")
    rc = subprocess.run(
        pip_cmd,
    ).returncode
    if rc != 0:
        log("[dashboard] WARNING: Could not install hyperliquid-python-sdk.")
        log("[dashboard]   Live order execution won't work. View-only mode is fine.")
        return False
    return True


def launch_dashboard(args: argparse.Namespace) -> int:
    """One-command flow: ensure deps → check credentials → create workspace → launch."""
    # Step 1: Ensure SDK is available
    ensure_sdk()

    # Step 2: Check for wallet credentials (optional — dashboard can connect via browser wallet)
    try:
        from connect.server import read_credential
        master = read_credential("master_address")
        if master:
            log(f"[hyperbot] Wallet from Keychain: {master}")
        else:
            log("[hyperbot] No saved wallet — dashboard will prompt for browser wallet connect.")
    except Exception:
        log("[hyperbot] No saved wallet — dashboard will prompt for browser wallet connect.")

    # Step 3: Find or create workspace
    if args.workspace_path:
        workspace = Path(args.workspace_path).expanduser().resolve()
    else:
        # Check for any existing hyperbot-* workspace
        workspace = ROOT.parent / "hyperbot-workspace"
        for candidate in sorted(ROOT.parent.glob("hyperbot-*")):
            if candidate.is_dir() and (candidate / "hyperbot.workspace.json").exists():
                workspace = candidate
                break

    if not workspace.exists():
        log(f"[hyperbot] Creating workspace at {workspace}...")
        output_dir = str(workspace.parent)
        workspace_name = workspace.name
        create_cmd = [
            sys.executable,
            str(ROOT / "scripts" / "create_workspace.py"),
            workspace_name,
            "--output-dir", output_dir,
            "--empty",
            "--account-mode", "test",
        ]
        rc = subprocess.run(create_cmd).returncode
        if rc != 0:
            log("[hyperbot] Workspace creation failed.")
            return rc
        log(f"[hyperbot] Workspace ready at {workspace}")
    else:
        log(f"[hyperbot] Using workspace: {workspace}")

    # Sync template scripts into workspace (keeps strategy code up to date)
    template_scripts = ROOT / "templates" / "hyperbot-multi" / "scripts"
    workspace_scripts = workspace / "scripts"
    if template_scripts.is_dir() and workspace_scripts.is_dir():
        import shutil
        synced = []
        for src in template_scripts.glob("*.py"):
            dst = workspace_scripts / src.name
            # Always overwrite — template is source of truth for code
            if not dst.exists() or src.read_bytes() != dst.read_bytes():
                shutil.copy2(src, dst)
                synced.append(src.name)
        if synced:
            log(f"[hyperbot] Synced {len(synced)} script(s): {', '.join(synced)}")

    # Step 4: Launch dashboard
    dash_script = workspace / "scripts" / "dashboard.py"
    if not dash_script.exists():
        log(f"[hyperbot] ERROR: {dash_script} not found.")
        return 1

    dash_cmd = [sys.executable, str(dash_script)]
    if args.live:
        if not args.confirm_risk:
            log("[hyperbot] ERROR: --live requires --confirm-risk. Real money at stake.")
            return 1
        dash_cmd.extend(["--live", "--confirm-risk"])
    if args.port:
        dash_cmd.extend(["--port", str(args.port)])

    if args.live:
        log("[hyperbot] Launching LIVE dashboard — real orders can be sent.")
    else:
        log("[hyperbot] Launching VIEW-ONLY dashboard — no orders will be sent.")
        log("[hyperbot]   To enable live trading, relaunch with: hyperbot dashboard --live --confirm-risk")
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
        symbols = args.symbol if args.symbol else ["BTCUSDT"]
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "create_workspace.py"),
            args.workspace_name,
            "--output-dir",
            args.output_dir,
            "--account-mode",
            args.account_mode,
            "--max-leverage",
            str(args.max_leverage),
            "--notification-email",
            args.notification_email,
            "--profile-days",
            str(args.profile_days),
        ]
        for sym in symbols:
            cmd.extend(["--symbol", sym])
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

    if args.command == "debrief":
        return run_debrief(args)

    parser.error(f"unknown command: {args.command}")
    return 2


def run_debrief(args: argparse.Namespace) -> int:
    """Show morning debrief — trade review with what-if analysis."""
    workspace = _resolve_workspace(args)
    if not workspace:
        log("[hyperbot] No workspace found. Run: hyperbot dashboard")
        return 1

    # Add workspace scripts to path for debrief module
    scripts_dir = workspace / "scripts"
    if scripts_dir.is_dir():
        sys.path.insert(0, str(scripts_dir))

    try:
        import debrief
        report = debrief.generate_debrief(workspace, lookback_days=args.days)
        md = debrief.format_markdown(report)
        print(md)
        return 0
    except Exception as e:
        log(f"[hyperbot] Debrief error: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _resolve_workspace(args: argparse.Namespace) -> Path | None:
    """Find the workspace directory from args or common locations."""
    if getattr(args, "workspace_path", None):
        p = Path(args.workspace_path).expanduser().resolve()
        if p.exists():
            return p

    # Try common workspace locations
    candidates = [
        Path.cwd(),
        Path.home() / "Desktop" / "Hyperbot",
    ]
    # Also try hyperbot-* dirs in cwd parent
    for child in Path.cwd().parent.iterdir():
        if child.is_dir() and child.name.startswith("hyperbot-"):
            candidates.append(child)

    for candidate in candidates:
        if (candidate / "hyperbot.workspace.json").exists():
            return candidate

    return None


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
    symbols = args.symbol if args.symbol else ["BTCUSDT"]
    log(f"[hyperbot run] Step 1/4: Creating workspace {args.workspace_name}...")
    create_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "create_workspace.py"),
        args.workspace_name,
        "--output-dir", args.output_dir,
        "--account-mode", args.account_mode,
        "--max-leverage", str(args.max_leverage),
        "--notification-email", args.notification_email,
        "--profile-days", str(args.profile_days),
        "--skip-profile",
    ]
    for sym in symbols:
        create_cmd.extend(["--symbol", sym])
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
