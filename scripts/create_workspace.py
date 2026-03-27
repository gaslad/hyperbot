#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates" / "workspace"
PACKS_ROOT = ROOT / "strategy-packs"
KNOWN_ASSETS: dict[str, int] = {}


def infer_coin(symbol: str) -> str:
    for suffix in ("USDT", "USD", "PERP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def load_pack_manifest(pack_id: str) -> dict:
    pack_file = PACKS_ROOT / pack_id / "pack.json"
    if not pack_file.exists():
        raise SystemExit(f"unknown strategy pack: {pack_id}")
    return json.loads(pack_file.read_text(encoding="utf-8"))


def replace_tokens(text: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        text = text.replace(key, value)
    return text


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def install_pack(target: Path, pack_id: str, symbol: str) -> dict:
    pack = load_pack_manifest(pack_id)
    coin = infer_coin(symbol)
    strategy_id = f"{coin.lower()}_{pack_id}"
    display_name = f"{coin} {pack['display_name']}"
    mapping = {
        "__SYMBOL__": symbol,
        "__COIN__": coin,
        "__STRATEGY_ID__": strategy_id,
        "__DISPLAY_NAME__": display_name,
    }

    pack_root = PACKS_ROOT / pack_id
    config_template = pack_root / "templates" / "config.json"
    strategy_template_dir = pack_root / "templates" / "strategy"

    config_target = target / "config" / "strategies" / f"{strategy_id}.json"
    config_target.write_text(replace_tokens(config_template.read_text(encoding="utf-8"), mapping), encoding="utf-8")

    strategy_target_dir = target / "strategies" / strategy_id
    strategy_target_dir.mkdir(parents=True, exist_ok=True)
    for src in strategy_template_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(strategy_template_dir)
        dest = strategy_target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(replace_tokens(src.read_text(encoding="utf-8"), mapping), encoding="utf-8")

    (strategy_target_dir / "pack.json").write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    return {
        "pack_id": pack_id,
        "strategy_id": strategy_id,
        "display_name": display_name,
        "family": pack["family"],
        "confidence_tier": pack["confidence_tier"],
    }


def run_initial_profiles(target: Path, installed: list[dict], profile_days: int) -> list[dict]:
    script = target / "scripts" / "profile_symbol_strategy.py"
    results: list[dict] = []
    for item in installed:
        cmd = [
            sys.executable,
            "-B",
            str(script),
            "--days",
            str(profile_days),
            "--strategy-id",
            item["strategy_id"],
            "--json",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=target,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
            results.append({
                "strategy_id": item["strategy_id"],
                "pack_id": item["pack_id"],
                "status": "completed",
                "artifacts": payload.get("artifacts", {}),
                "selected_pack_id": payload.get("profile", {}).get("selected_pack_id"),
                "selected_strategy_id": payload.get("profile", {}).get("selected_strategy_id"),
            })
        except subprocess.CalledProcessError as exc:
            results.append({
                "strategy_id": item["strategy_id"],
                "pack_id": item["pack_id"],
                "status": "failed",
                "error": exc.stderr.strip() or exc.stdout.strip() or str(exc),
            })
        except Exception as exc:  # pragma: no cover - defensive bootstrap path
            results.append({
                "strategy_id": item["strategy_id"],
                "pack_id": item["pack_id"],
                "status": "failed",
                "error": str(exc),
            })
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Hyperliquid trading workspace from Hyperbot templates.")
    parser.add_argument("workspace_name", nargs="?")
    parser.add_argument("--output-dir", help="Parent directory where the workspace folder will be created")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--strategy-pack", action="append", default=[], help="Strategy pack ids to install")
    parser.add_argument("--account-mode", choices=("test", "production"), default="test")
    parser.add_argument("--max-leverage", type=float, default=4.0)
    parser.add_argument("--notification-email", default="")
    parser.add_argument("--enable-unattended", action="store_true")
    parser.add_argument("--profile-days", type=int, default=90, help="Lookback window for automatic token-specific revision")
    parser.add_argument("--skip-profile", action="store_true", help="Skip the automatic token-specific revision step")
    parser.add_argument("--list-packs", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_packs:
        manifest = json.loads((PACKS_ROOT / "manifest.json").read_text(encoding="utf-8"))
        print(json.dumps(manifest, indent=2))
        return 0

    if not args.workspace_name or not args.output_dir:
        raise SystemExit("workspace_name and --output-dir are required unless --list-packs is used")

    target = Path(args.output_dir).expanduser() / args.workspace_name
    if target.exists():
        raise SystemExit(f"target already exists: {target}")

    selected_packs = args.strategy_pack or ["trend_pullback"]
    shutil.copytree(TEMPLATE_ROOT, target)

    coin = infer_coin(args.symbol)
    market_payload = {
        "markets": [
            {
                "symbol": args.symbol,
                "coin": coin,
                "asset": KNOWN_ASSETS.get(args.symbol),
                "market_type": "perpetual"
            }
        ]
    }
    write_json(target / "config" / "markets" / "hyperliquid_perps.json", market_payload)

    installed = [install_pack(target, pack_id, args.symbol) for pack_id in selected_packs]

    workspace_manifest = {
        "workspace_name": args.workspace_name,
        "symbol": args.symbol,
        "coin": coin,
        "account_mode": args.account_mode,
        "max_leverage": args.max_leverage,
        "notification_email": args.notification_email,
        "enable_unattended": args.enable_unattended,
        "strategy_packs": installed,
        "profile_mode": "baseline_pack_defaults",
        "token_specific_revision": {
            "available": True,
            "auto_run_on_create": not args.skip_profile,
            "days": args.profile_days,
            "status": "pending" if not args.skip_profile else "skipped",
            "results": [],
        },
        "generated_by": "hyperbot"
    }
    write_json(target / "hyperbot.workspace.json", workspace_manifest)

    if not args.skip_profile:
        results = run_initial_profiles(target, installed, args.profile_days)
        workspace_manifest["token_specific_revision"]["results"] = results
        workspace_manifest["token_specific_revision"]["status"] = (
            "completed" if all(item["status"] == "completed" for item in results) else "partial_failure"
        )
        write_json(target / "hyperbot.workspace.json", workspace_manifest)

    print(f"Created workspace: {target}")
    if not args.skip_profile:
        print("Initial token-specific revision:")
        for item in workspace_manifest["token_specific_revision"]["results"]:
            print(f"- {item['strategy_id']}: {item['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
