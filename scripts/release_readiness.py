#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "GEMINI.md",
    ROOT / "requirements.txt",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "local-first-roadmap.md",
    ROOT / "docs" / "release-readiness.md",
    ROOT / "install.sh",
    ROOT / "scripts" / "hyperbot.py",
    ROOT / "scripts" / "create_workspace.py",
    ROOT / "scripts" / "validate_apply_revision.py",
    ROOT / "scripts" / "release_readiness.py",
    ROOT / "templates" / "workspace" / "scripts" / "apply_revision.py",
]
WORKSPACE_TEXT_EXTENSIONS = {".md", ".json", ".py", ".yaml", ".yml", ".example"}
WORKSPACE_FORBIDDEN_TERMS = ("Codex", "codex", "LLM", "OpenAI")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
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


def add_issue(issues: list[str], message: str) -> None:
    issues.append(message)


def add_warning(warnings: list[str], message: str) -> None:
    warnings.append(message)


def validate_workspace_agnostic(issues: list[str]) -> None:
    workspace_root = ROOT / "templates" / "workspace"
    for path in workspace_root.rglob("*"):
        if not path.is_file() or path.suffix not in WORKSPACE_TEXT_EXTENSIONS:
            continue
        text = path.read_text(encoding="utf-8")
        for term in WORKSPACE_FORBIDDEN_TERMS:
            if term in text:
                add_issue(
                    issues,
                    f"workspace template should remain LLM-agnostic: found '{term}' in {path.relative_to(ROOT)}",
                )


def validate_repo_hygiene(warnings: list[str]) -> None:
    for path in ROOT.rglob("__pycache__"):
        if path.is_dir():
            add_warning(warnings, f"remove committed cache directory: {path.relative_to(ROOT)}")


def main() -> int:
    issues: list[str] = []
    warnings: list[str] = []

    for path in REQUIRED_FILES:
        if not path.exists():
            add_issue(issues, f"missing required release file: {path.relative_to(ROOT)}")

    validate_workspace_agnostic(issues)
    validate_repo_hygiene(warnings)

    run(
        [
            sys.executable,
            "-m",
            "py_compile",
            "scripts/hyperbot.py",
            "scripts/release_readiness.py",
            "scripts/validate_apply_revision.py",
            "scripts/create_workspace.py",
            "templates/workspace/scripts/apply_revision.py",
            "templates/workspace/scripts/profile_symbol_strategy.py",
            "templates/workspace/tests/test_hl_client.py",
            "templates/workspace/tests/test_signals.py",
        ],
        cwd=ROOT,
    )
    run([sys.executable, "scripts/validate_apply_revision.py"], cwd=ROOT)

    if issues:
        print("release_readiness=blocked")
        print("issues:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("release_readiness=ready")

    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
