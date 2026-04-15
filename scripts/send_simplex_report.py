#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DB_PREFIX = str(Path.home() / ".simplex" / "hyperbot-reporter" / "simplex_v1")
DEFAULT_FALLBACK_DB_PREFIX = str(Path(tempfile.gettempdir()) / "hyperbot-simplex" / "hyperbot-reporter" / "simplex_v1")
DEFAULT_PORTABLE_BINARY_ROOT = Path(tempfile.gettempdir()) / "hyperbot-simplex"
DEFAULT_CONTACT = "File dismiss"
PROFILE_SUFFIXES = ("_chat.db", "_chat.db-shm", "_chat.db-wal", "_agent.db", "_agent.db-shm", "_agent.db-wal")
OPENSSL_LIBRARY_FILENAMES = ("libcrypto.3.dylib", "libssl.3.dylib")
OPENSSL_SEARCH_ROOTS = (Path("/Applications"), Path("/Library"), Path("/usr/local"), Path("/opt/homebrew"))


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    returncode: int
    message: str
    attempts: int = 0


def build_message(contact: str, body: str) -> str:
    return f"@'{contact}' {body}"


def _resolve_binary(binary: str) -> str | None:
    if not binary:
        return None
    resolved = shutil.which(binary)
    if resolved:
        return resolved
    candidate = Path(binary).expanduser()
    return str(candidate) if candidate.exists() else None


def _run_capture(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def _get_binary_dependencies(binary_path: Path) -> list[str]:
    completed = _run_capture(["otool", "-L", str(binary_path)])
    if not completed or completed.returncode != 0:
        return []
    dependencies: list[str] = []
    for line in completed.stdout.splitlines()[1:]:
        dep = line.strip().split(" ", 1)[0]
        if dep:
            dependencies.append(dep)
    return dependencies


def _is_x86_64_compatible_dylib(path: Path) -> bool:
    completed = _run_capture(["lipo", "-info", str(path)])
    if not completed or completed.returncode != 0:
        return False
    info = f"{completed.stdout}\n{completed.stderr}".lower()
    return "x86_64" in info or "universal" in info


def _discover_openssl_pair() -> tuple[Path, Path] | None:
    env_dir = os.environ.get("HYPERBOT_SIMPLEX_OPENSSL_LIB_DIR")
    if env_dir:
        base_dir = Path(env_dir).expanduser()
        crypto = base_dir / "libcrypto.3.dylib"
        ssl = base_dir / "libssl.3.dylib"
        if crypto.exists() and ssl.exists() and _is_x86_64_compatible_dylib(crypto) and _is_x86_64_compatible_dylib(ssl):
            return crypto, ssl

    env_crypto = os.environ.get("HYPERBOT_SIMPLEX_OPENSSL_LIBCRYPTO")
    env_ssl = os.environ.get("HYPERBOT_SIMPLEX_OPENSSL_LIBSSL")
    if env_crypto and env_ssl:
        crypto = Path(env_crypto).expanduser()
        ssl = Path(env_ssl).expanduser()
        if crypto.exists() and ssl.exists() and _is_x86_64_compatible_dylib(crypto) and _is_x86_64_compatible_dylib(ssl):
            return crypto, ssl

    for root in OPENSSL_SEARCH_ROOTS:
        if not root.exists():
            continue
        for crypto in root.rglob("libcrypto.3.dylib"):
            ssl = crypto.with_name("libssl.3.dylib")
            if not ssl.exists():
                continue
            if _is_x86_64_compatible_dylib(crypto) and _is_x86_64_compatible_dylib(ssl):
                return crypto, ssl
    return None


def _get_openssl_dependency_paths(binary_path: Path) -> list[str]:
    dependencies = _get_binary_dependencies(binary_path)
    return [dep for dep in dependencies if dep.endswith(OPENSSL_LIBRARY_FILENAMES)]


def _ensure_portable_binary(source_binary_path: Path) -> tuple[bool, str, str]:
    portable_root = Path(os.environ.get("HYPERBOT_SIMPLEX_PORTABLE_ROOT", DEFAULT_PORTABLE_BINARY_ROOT)).expanduser()
    portable_binary_path = portable_root / source_binary_path.name
    portable_lib_dir = portable_root / "lib"
    openssl_pair = _discover_openssl_pair()
    if not openssl_pair:
        return False, "", "no x86_64-compatible OpenSSL libraries were found for a portable SimpleX binary"

    crypto_src, ssl_src = openssl_pair
    try:
        portable_lib_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, "", f"cannot prepare portable SimpleX directory {portable_lib_dir}: {exc}"

    source_deps = _get_openssl_dependency_paths(source_binary_path)
    if not source_deps:
        return False, "", f"no OpenSSL dependencies found in {source_binary_path}"

    crypto_target = portable_lib_dir / "libcrypto.3.dylib"
    ssl_target = portable_lib_dir / "libssl.3.dylib"
    try:
        if not crypto_target.exists() or crypto_src.stat().st_mtime > crypto_target.stat().st_mtime:
            shutil.copy2(crypto_src, crypto_target)
        if not ssl_target.exists() or ssl_src.stat().st_mtime > ssl_target.stat().st_mtime:
            shutil.copy2(ssl_src, ssl_target)
        if (not portable_binary_path.exists()) or source_binary_path.stat().st_mtime > portable_binary_path.stat().st_mtime:
            shutil.copy2(source_binary_path, portable_binary_path)
    except Exception as exc:
        return False, "", f"failed to stage portable SimpleX binary: {exc}"

    current_deps = _get_openssl_dependency_paths(portable_binary_path)
    changes: list[tuple[str, str]] = []
    for dependency in current_deps:
        dependency_name = Path(dependency).name
        if dependency_name == "libcrypto.3.dylib" and dependency != str(crypto_target):
            changes.append((dependency, str(crypto_target)))
        elif dependency_name == "libssl.3.dylib" and dependency != str(ssl_target):
            changes.append((dependency, str(ssl_target)))

    if changes:
        install_name_tool = shutil.which("install_name_tool")
        if not install_name_tool:
            return False, "", "install_name_tool is not available to rewrite the SimpleX binary"
        command = [install_name_tool]
        for old_path, new_path in changes:
            command.extend(["-change", old_path, new_path])
        command.append(str(portable_binary_path))
        completed = _run_capture(command)
        if not completed or completed.returncode != 0:
            combined_output = "\n".join(part for part in ((completed.stdout if completed else ""), (completed.stderr if completed else "")) if part).strip()
            if not combined_output:
                combined_output = "install_name_tool failed while rewriting the portable SimpleX binary"
            return False, "", combined_output

    verified_deps = _get_openssl_dependency_paths(portable_binary_path)
    expected_targets: list[str] = []
    for dependency in source_deps:
        dependency_name = Path(dependency).name
        if dependency_name == "libcrypto.3.dylib":
            expected_targets.append(str(crypto_target))
        elif dependency_name == "libssl.3.dylib":
            expected_targets.append(str(ssl_target))
    if not expected_targets:
        return False, "", f"portable SimpleX binary does not expose OpenSSL dependencies in {source_binary_path}"
    for target in expected_targets:
        if target not in verified_deps:
            return False, "", f"portable SimpleX binary does not reference the staged OpenSSL libraries at {portable_lib_dir}"

    return True, str(portable_binary_path), f"using portable SimpleX binary at {portable_binary_path}"


def _prepare_db_prefix(db_prefix_path: Path) -> tuple[bool, str]:
    try:
        db_prefix_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"cannot prepare SimpleX db directory {db_prefix_path.parent}: {exc}"

    probe_path = db_prefix_path.parent / f".{db_prefix_path.name}.write-test"
    try:
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink()
    except Exception as exc:
        return False, f"SimpleX db directory is not writable: {db_prefix_path.parent}: {exc}"

    return True, ""


def _seed_db_prefix(source_prefix: Path, target_prefix: Path) -> tuple[bool, str]:
    source_dir = source_prefix.parent
    target_dir = target_prefix.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"cannot prepare fallback SimpleX directory {target_dir}: {exc}"

    copied = 0
    for suffix in PROFILE_SUFFIXES:
        source_file = source_dir / f"{source_prefix.name}{suffix}"
        if not source_file.exists():
            continue
        target_file = target_dir / f"{target_prefix.name}{suffix}"
        if target_file.exists():
            continue
        try:
            shutil.copy2(source_file, target_file)
            copied += 1
        except Exception as exc:
            return False, f"failed to seed fallback SimpleX profile from {source_file} to {target_file}: {exc}"

    target_main = target_dir / f"{target_prefix.name}_chat.db"
    if copied == 0 and not target_main.exists():
        return False, f"no readable SimpleX profile files found at {source_dir}"

    if copied > 0:
        return True, f"seeded fallback SimpleX profile from {source_dir}"
    return True, "fallback SimpleX profile already exists"


def _is_transient_error(text: str) -> bool:
    lowered = text.lower()
    transient_patterns = (
        "database is locked",
        "busy",
        "temporarily unavailable",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "resource temporarily unavailable",
    )
    permanent_patterns = (
        "readonly database",
        "read only database",
        "permission denied",
        "no such file or directory",
        "not found",
        "unknown contact",
        "contact not found",
        "invalid command",
    )
    if any(pattern in lowered for pattern in permanent_patterns):
        return False
    return any(pattern in lowered for pattern in transient_patterns)


def _is_profile_path_failure(text: str) -> bool:
    lowered = text.lower()
    failure_patterns = (
        "readonly database",
        "read only database",
        "attempt to write a readonly database",
        "attempt to write a read only database",
        "errorreadonly",
        "errorcantopen",
        "cannot open database",
        "could not open database",
        "unable to open database file",
        "unable to open database",
        "operation not permitted",
        "permission denied",
    )
    return any(pattern in lowered for pattern in failure_patterns)


def _is_binary_dependency_failure(text: str) -> bool:
    lowered = text.lower()
    failure_patterns = (
        "image not found",
        "library not loaded",
        "dyld: library not loaded",
        "dlopen",
        "symbol not found",
        "no such file or directory",
        "can't open",
    )
    return any(pattern in lowered for pattern in failure_patterns)


def _load_report(report_path: Path) -> tuple[bool, str]:
    if not report_path.exists():
        return False, f"report file does not exist: {report_path}"
    if not report_path.is_file():
        return False, f"report path is not a file: {report_path}"
    try:
        body = report_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        return False, f"failed to read report file {report_path}: {exc}"
    if not body:
        return False, f"report file is empty: {report_path}"
    return True, body


def send_report(
    report_path: Path,
    *,
    db_prefix: str | Path = DEFAULT_DB_PREFIX,
    contact: str = DEFAULT_CONTACT,
    binary: str = "simplex-chat",
    timeout_seconds: int = 8,
    retry_count: int = 3,
    retry_delay_seconds: float = 2.0,
) -> SendOutcome:
    report_path = Path(report_path).expanduser()
    ok, body_or_error = _load_report(report_path)
    if not ok:
        return SendOutcome(False, 2, body_or_error, attempts=0)

    binary_path = _resolve_binary(binary)
    if not binary_path:
        return SendOutcome(False, 2, f"SimpleX binary not found: {binary}", attempts=0)

    binary_candidates = [binary_path]
    portable_ready_message = ""
    if Path(binary_path).exists():
        dependency_paths = _get_openssl_dependency_paths(Path(binary_path))
        if dependency_paths:
            dependency_problem = False
            for dependency_path in dependency_paths:
                dep_path = Path(dependency_path)
                if not dep_path.exists() or not _is_x86_64_compatible_dylib(dep_path):
                    dependency_problem = True
                    break
            if dependency_problem:
                portable_ok, portable_binary_path, portable_message = _ensure_portable_binary(Path(binary_path))
                if portable_ok and portable_binary_path not in binary_candidates:
                    binary_candidates.insert(0, portable_binary_path)
                    portable_ready_message = portable_message

    primary_db_prefix = Path(db_prefix).expanduser()
    fallback_db_prefix = Path(
        os.environ.get("HYPERBOT_SIMPLEX_FALLBACK_DB_PREFIX", DEFAULT_FALLBACK_DB_PREFIX)
    ).expanduser()
    candidate_prefixes = [primary_db_prefix]
    if fallback_db_prefix != primary_db_prefix:
        candidate_prefixes.append(fallback_db_prefix)

    attempts = max(1, int(retry_count))
    messages: list[str] = []
    if portable_ready_message:
        messages.append(portable_ready_message)

    binary_index = 0
    while binary_index < len(binary_candidates):
        current_binary = binary_candidates[binary_index]
        for prefix_index, db_prefix_path in enumerate(candidate_prefixes):
            if prefix_index > 0:
                seeded, seed_message = _seed_db_prefix(primary_db_prefix, db_prefix_path)
                if not seeded:
                    messages.append(f"{db_prefix_path}: {seed_message}")
                    continue
            ready, prep_message = _prepare_db_prefix(db_prefix_path)
            if not ready:
                messages.append(f"{db_prefix_path}: {prep_message}")
                continue

            command = [
                current_binary,
                "-d",
                str(db_prefix_path),
                "-e",
                build_message(contact, body_or_error),
                "-t",
                str(timeout_seconds),
                "--execute-log",
                "all",
            ]

            last_message = ""
            last_code = 1
            for attempt in range(1, attempts + 1):
                completed = subprocess.run(command, capture_output=True, text=True)
                combined_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
                if completed.returncode == 0:
                    body = combined_output or "sent"
                    if current_binary != binary_path:
                        body = f"{portable_ready_message or f'using portable SimpleX binary at {current_binary}'}; {body}"
                    if prefix_index > 0:
                        body = f"using fallback SimpleX profile at {db_prefix_path}; {body}"
                    return SendOutcome(True, 0, body, attempts=attempt)

                last_code = completed.returncode or 1
                last_message = combined_output or f"simplex-chat exited with {last_code}"
                if attempt < attempts and _is_transient_error(last_message):
                    time.sleep(max(0.0, retry_delay_seconds) * (2 ** (attempt - 1)))
                    continue
                break

            messages.append(f"{current_binary}: {db_prefix_path}: {last_message}")
            if prefix_index == 0 and len(candidate_prefixes) > 1 and _is_profile_path_failure(last_message):
                continue
            if current_binary == binary_path and _is_binary_dependency_failure(last_message):
                portable_ok, portable_binary_path, portable_message = _ensure_portable_binary(Path(binary_path))
                if portable_ok and portable_binary_path not in binary_candidates:
                    binary_candidates.append(portable_binary_path)
                    if portable_message:
                        messages.append(portable_message)
                    break
            if current_binary != binary_path and binary_index + 1 < len(binary_candidates):
                break
            return SendOutcome(False, last_code, last_message, attempts=attempt)

        binary_index += 1

    if len(messages) == 1:
        return SendOutcome(False, 2, messages[0], attempts=0)
    return SendOutcome(False, 2, "SimpleX delivery failed for all profile paths and binary candidates: " + " | ".join(messages), attempts=0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a validated Hyperbot report via SimpleX.")
    parser.add_argument(
        "--db-prefix",
        default=os.environ.get("HYPERBOT_SIMPLEX_DB_PREFIX", DEFAULT_DB_PREFIX),
        help="SimpleX database prefix",
    )
    parser.add_argument(
        "--contact",
        default=os.environ.get("HYPERBOT_SIMPLEX_CONTACT", DEFAULT_CONTACT),
        help="Local SimpleX contact name",
    )
    parser.add_argument(
        "--report",
        default=os.environ.get("HYPERBOT_REPORT_PATH", ""),
        help="Plain-text report file to send",
    )
    parser.add_argument(
        "--binary",
        default=os.environ.get("HYPERBOT_SIMPLEX_BINARY", "simplex-chat"),
        help="SimpleX CLI binary",
    )
    parser.add_argument("--timeout-seconds", type=int, default=8, help="Seconds to wait after sending")
    parser.add_argument("--retry-count", type=int, default=3, help="How many times to retry transient send failures")
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0, help="Base delay between retries")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.report:
        print("ERROR: --report or HYPERBOT_REPORT_PATH is required", file=sys.stderr)
        return 2

    outcome = send_report(
        Path(args.report),
        db_prefix=args.db_prefix,
        contact=args.contact,
        binary=args.binary,
        timeout_seconds=args.timeout_seconds,
        retry_count=args.retry_count,
        retry_delay_seconds=args.retry_delay_seconds,
    )
    if outcome.ok:
        if outcome.message:
            print(outcome.message, flush=True)
        return 0

    print(outcome.message, file=sys.stderr, flush=True)
    return outcome.returncode


if __name__ == "__main__":
    raise SystemExit(main())
