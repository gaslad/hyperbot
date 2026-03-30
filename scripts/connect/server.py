#!/usr/bin/env python3
"""Local callback server for Hyperbot wallet connect.

Starts a minimal HTTP server on localhost that:
1. Serves the wallet_connect.html page
2. Provides nonce for EIP-712 signing
3. Submits the approveAgent action to Hyperliquid
4. Stores credentials in macOS Keychain (or fallback encrypted file)

All crypto (key generation, address derivation) happens in the browser
via ethers.js — no Python crypto dependencies needed.
"""
from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HTML_PATH = Path(__file__).parent / "wallet_connect.html"
HL_API_URL = "https://api.hyperliquid.xyz"
SERVICE_NAME = "hyperbot"
SHUTDOWN_EVENT = threading.Event()
LAUNCH_DASHBOARD = threading.Event()  # Signal to launch dashboard after connect


# ---------------------------------------------------------------------------
# Hyperliquid API helpers
# ---------------------------------------------------------------------------

def get_nonce(address: str) -> int:
    """Fresh nonce for Hyperliquid (timestamp in ms)."""
    return int(time.time() * 1000)


def submit_approve_agent(master_address: str, agent_address: str, signature: str, nonce: int) -> dict:
    """Submit the signed approveAgent action to Hyperliquid exchange API."""
    sig = signature.removeprefix("0x")
    r = "0x" + sig[:64]
    s = "0x" + sig[64:128]
    v = int(sig[128:130], 16)
    if v < 27:
        v += 27

    payload = {
        "action": {
            "type": "approveAgent",
            "hyperliquidChain": "Mainnet",
            "signatureChainId": "0xa4b1",
            "agentAddress": agent_address.lower(),
            "agentName": "hyperbot",
            "nonce": nonce,
        },
        "nonce": nonce,
        "signature": {"r": r, "s": s, "v": v},
        "vaultAddress": None,
    }
    print(f"  Payload: {json.dumps(payload)}", flush=True)

    req = urllib.request.Request(
        f"{HL_API_URL}/exchange",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.request.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  HL HTTP {e.code}: {body}", flush=True)
        try:
            return json.loads(body)
        except Exception:
            return {"status": "err", "response": f"HTTP {e.code}: {body[:200]}"}


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def store_credential(key: str, value: str) -> None:
    if platform.system() == "Darwin":
        _store_keychain(key, value)
    else:
        _store_file(key, value)


def _store_keychain(key: str, value: str) -> None:
    account = f"{SERVICE_NAME}.{key}"
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE_NAME, "-a", account],
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", SERVICE_NAME, "-a", account, "-w", value, "-U"],
        check=True,
        capture_output=True,
    )


def _store_file(key: str, value: str) -> None:
    cred_dir = Path.home() / ".hyperbot" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_file = cred_dir / f"{key}.secret"
    cred_file.write_text(value, encoding="utf-8")
    cred_file.chmod(0o600)


def read_credential(key: str) -> str | None:
    if platform.system() == "Darwin":
        return _read_keychain(key)
    return _read_file(key)


def _read_keychain(key: str) -> str | None:
    account = f"{SERVICE_NAME}.{key}"
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _read_file(key: str) -> str | None:
    cred_file = Path.home() / ".hyperbot" / "credentials" / f"{key}.secret"
    return cred_file.read_text(encoding="utf-8").strip() if cred_file.exists() else None


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class ConnectHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress access logs

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            html = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif self.path.startswith("/nonce"):
            qs = parse_qs(urlparse(self.path).query)
            address = qs.get("address", [""])[0]
            self._json_response({"nonce": get_nonce(address)})
        elif self.path == "/status":
            master = read_credential("master_address")
            self._json_response({"connected": master is not None, "master_address": master})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        if self.path == "/save-credentials":
            try:
                master = body["master_address"]
                agent_pk = body["agent_private_key"]

                print(f"  Wallet:  {master}", flush=True)

                # Verify the key looks valid
                pk = agent_pk.removeprefix("0x")
                if len(pk) != 64 or not all(c in "0123456789abcdefABCDEF" for c in pk):
                    raise ValueError("Invalid private key format")

                # Store credentials securely
                store_credential("master_address", master)
                store_credential("agent_private_key", agent_pk)

                print(f"  Credentials saved to macOS Keychain.", flush=True)
                self._json_response({"ok": True, "master_address": master})

                threading.Timer(1.0, lambda: (LAUNCH_DASHBOARD.set(), SHUTDOWN_EVENT.set())).start()

            except Exception as e:
                print(f"  ERROR: {e}", flush=True)
                self._json_response({"ok": False, "error": str(e)}, 500)

        elif self.path == "/complete":
            try:
                master = body["master_address"]
                agent_addr = body["agent_address"]
                agent_pk = body["agent_private_key"]
                signature = body["signature"]
                nonce = body["nonce"]

                print(f"  Submitting approveAgent to Hyperliquid...", flush=True)
                result = submit_approve_agent(master, agent_addr, signature, nonce)
                print(f"  Response: {json.dumps(result)}", flush=True)

                if isinstance(result, dict) and result.get("status") == "err":
                    raise RuntimeError(result.get("response", json.dumps(result)))

                store_credential("master_address", master)
                store_credential("agent_address", agent_addr)
                store_credential("agent_private_key", agent_pk)

                print(f"  Credentials saved to macOS Keychain.", flush=True)
                self._json_response({"ok": True, "master_address": master, "agent_address": agent_addr})

                # Signal to launch dashboard, then shut down connect server
                threading.Timer(1.0, lambda: (LAUNCH_DASHBOARD.set(), SHUTDOWN_EVENT.set())).start()

            except Exception as e:
                print(f"  ERROR: {e}", flush=True)
                self._json_response({"ok": False, "error": str(e)}, 500)
        else:
            self.send_error(404)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(port: int | None = None) -> int:
    if port is None:
        port = find_free_port()

    server = HTTPServer(("127.0.0.1", port), ConnectHandler)
    server.timeout = 1.0

    url = f"http://127.0.0.1:{port}/?port={port}"
    print(f"[hyperbot connect] Opening wallet connect at {url}", flush=True)
    print("  Waiting for wallet connection...", flush=True)

    webbrowser.open(url)

    while not SHUTDOWN_EVENT.is_set():
        server.handle_request()

    server.server_close()
    print("[hyperbot connect] Done.", flush=True)

    if LAUNCH_DASHBOARD.is_set():
        return _launch_dashboard()

    return 0


def _launch_dashboard() -> int:
    """Launch 'hyperbot dashboard' after successful wallet connect."""
    hyperbot_script = Path(__file__).resolve().parent.parent / "hyperbot.py"
    if not hyperbot_script.exists():
        print("[hyperbot connect] Could not find hyperbot.py to launch dashboard.", flush=True)
        return 0

    print("[hyperbot connect] Launching dashboard...", flush=True)
    import os
    os.execv(
        sys.executable,
        [sys.executable, str(hyperbot_script), "dashboard"],
    )


if __name__ == "__main__":
    raise SystemExit(run_server())
