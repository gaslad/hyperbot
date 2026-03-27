#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
REQUIRED_FILES = [
    ROOT / "README.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "scripts" / "create_workspace.py",
    ROOT / "scripts" / "validate_apply_revision.py",
    ROOT / "templates" / "workspace" / "scripts" / "apply_revision.py",
]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
URL_RE = re.compile(r"^https://")


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


def validate_manifest(manifest: dict, issues: list[str], warnings: list[str]) -> None:
    for key in ("name", "version", "description", "homepage", "repository", "license", "skills", "interface"):
        if not manifest.get(key):
            add_issue(issues, f"plugin.json missing required top-level field: {key}")

    version = manifest.get("version", "")
    if version and not SEMVER_RE.match(version):
        add_issue(issues, f"plugin.json version is not semver: {version}")

    homepage = manifest.get("homepage", "")
    repository = manifest.get("repository", "")
    for label, value in (("homepage", homepage), ("repository", repository)):
        if value and not URL_RE.match(value):
            add_issue(issues, f"plugin.json {label} must be an https URL: {value}")

    interface = manifest.get("interface", {})
    for key in (
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "websiteURL",
        "privacyPolicyURL",
        "termsOfServiceURL",
        "defaultPrompt",
        "brandColor",
    ):
        if not interface.get(key):
            add_issue(issues, f"plugin.json interface missing required field: {key}")

    for key in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
        value = interface.get(key, "")
        if value and not URL_RE.match(value):
            add_issue(issues, f"plugin.json interface {key} must be an https URL: {value}")

    if homepage and repository and homepage == repository:
        add_issue(issues, "plugin.json homepage and repository should not be the same URL for release")

    website_url = interface.get("websiteURL", "")
    privacy_url = interface.get("privacyPolicyURL", "")
    terms_url = interface.get("termsOfServiceURL", "")
    if website_url and privacy_url and website_url == privacy_url:
        add_issue(issues, "plugin.json interface privacyPolicyURL should not reuse websiteURL for release")
    if website_url and terms_url and website_url == terms_url:
        add_issue(issues, "plugin.json interface termsOfServiceURL should not reuse websiteURL for release")

    skills_path = ROOT / str(manifest.get("skills", "")).strip()
    if not skills_path.exists():
        add_issue(issues, f"plugin.json skills path does not exist: {skills_path}")
    elif not any(skills_path.rglob("SKILL.md")):
        add_issue(issues, f"plugin.json skills path has no SKILL.md files: {skills_path}")

    prompts = interface.get("defaultPrompt", [])
    if isinstance(prompts, list) and len(prompts) < 3:
        add_warning(warnings, "plugin.json defaultPrompt has fewer than 3 examples")


def main() -> int:
    issues: list[str] = []
    warnings: list[str] = []

    for path in REQUIRED_FILES:
        if not path.exists():
            add_issue(issues, f"missing required release file: {path.relative_to(ROOT)}")

    manifest = load_json(PLUGIN_MANIFEST)
    validate_manifest(manifest, issues, warnings)

    run(
        [
            sys.executable,
            "-m",
            "py_compile",
            "scripts/release_readiness.py",
            "scripts/validate_apply_revision.py",
            "scripts/create_workspace.py",
            "templates/workspace/scripts/apply_revision.py",
            "templates/workspace/scripts/profile_symbol_strategy.py",
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
