#!/usr/bin/env python3
"""Hyperliquid API client for generated workspaces.

Handles:
- Reading credentials from macOS Keychain
- Market data (prices, candles, orderbook) via /info endpoint
- Account state (positions, equity) via /info endpoint
- Order placement/cancellation via /exchange endpoint (requires hyperliquid SDK)

The /info endpoints need no signing. Order execution requires the SDK
for EIP-712 phantom agent signing.
"""
from __future__ import annotations

import json
import platform
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HL_MAINNET = "https://api.hyperliquid.xyz"
HL_TESTNET = "https://api.hyperliquid-testnet.xyz"
SERVICE_NAME = "hyperbot"

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFEST = ROOT / "hyperbot.workspace.json"


# ---------------------------------------------------------------------------
# Credential access
# ---------------------------------------------------------------------------

def read_credential(key: str) -> str | None:
    if platform.system() == "Darwin":
        account = f"{SERVICE_NAME}.{key}"
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", account, "-w"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    # Fallback: file-based
    cred_file = Path.home() / ".hyperbot" / "credentials" / f"{key}.secret"
    return cred_file.read_text(encoding="utf-8").strip() if cred_file.exists() else None


def get_credentials() -> dict[str, str | None]:
    return {
        "master_address": read_credential("master_address"),
        "agent_private_key": read_credential("agent_private_key"),
    }


# ---------------------------------------------------------------------------
# Workspace config
# ---------------------------------------------------------------------------

def load_workspace() -> dict:
    return json.loads(WORKSPACE_MANIFEST.read_text(encoding="utf-8"))


def get_symbol() -> str:
    return load_workspace().get("symbol", "BTCUSDT")


def infer_coin(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


# ---------------------------------------------------------------------------
# Info API (no signing required)
# ---------------------------------------------------------------------------

def _info_post(payload: dict, base_url: str = HL_MAINNET) -> Any:
    req = urllib.request.Request(
        f"{base_url}/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_all_mids(base_url: str = HL_MAINNET) -> dict[str, str]:
    """Get mid prices for all coins. Returns {coin: price_str}."""
    return _info_post({"type": "allMids"}, base_url)


def get_mid_price(coin: str, base_url: str = HL_MAINNET) -> float | None:
    mids = get_all_mids(base_url)
    price_str = mids.get(coin)
    return float(price_str) if price_str else None


def get_l2_book(coin: str, base_url: str = HL_MAINNET) -> dict:
    return _info_post({"type": "l2Book", "coin": coin}, base_url)


def get_candles(coin: str, interval: str, lookback_days: int, base_url: str = HL_MAINNET) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (lookback_days * 86400 * 1000)
    return _info_post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": now_ms},
    }, base_url)


def get_clearinghouse_state(address: str, base_url: str = HL_MAINNET) -> dict:
    """Get positions and margin summary for an address."""
    return _info_post({"type": "clearinghouseState", "user": address}, base_url)


def get_open_orders(address: str, base_url: str = HL_MAINNET) -> list[dict]:
    return _info_post({"type": "openOrders", "user": address}, base_url)


def get_user_fills(address: str, base_url: str = HL_MAINNET) -> list[dict]:
    return _info_post({"type": "userFills", "user": address}, base_url)


def get_meta(base_url: str = HL_MAINNET) -> dict:
    """Get universe metadata (asset IDs, tick sizes, etc.)."""
    return _info_post({"type": "meta"}, base_url)


def get_asset_id(coin: str, base_url: str = HL_MAINNET) -> int | None:
    """Look up the integer asset ID for a coin."""
    meta = get_meta(base_url)
    for i, asset in enumerate(meta.get("universe", [])):
        if asset.get("name") == coin:
            return i
    return None


# ---------------------------------------------------------------------------
# Exchange API (requires hyperliquid SDK for signing)
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    ok: bool
    order_id: int | None = None
    error: str | None = None
    raw: dict | None = None


def _get_exchange_client(base_url: str = HL_MAINNET):
    """Lazy-load the Hyperliquid SDK exchange client."""
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
    except ImportError:
        raise RuntimeError(
            "hyperliquid SDK not installed. Run: pip install hyperliquid-python-sdk"
        )

    creds = get_credentials()
    if not creds["master_address"] or not creds["agent_private_key"]:
        raise RuntimeError("Wallet not connected. Run: hyperbot connect")

    api_url = base_url
    if base_url == HL_MAINNET:
        api_url = constants.MAINNET_API_URL
    elif base_url == HL_TESTNET:
        api_url = constants.TESTNET_API_URL

    return Exchange(
        wallet=None,
        base_url=api_url,
        account_address=creds["master_address"],
        vault_address=None,
    ), creds["agent_private_key"]


def place_order(
    coin: str,
    is_buy: bool,
    size: float,
    price: float | None = None,
    reduce_only: bool = False,
    order_type: str = "market",
    base_url: str = HL_MAINNET,
) -> OrderResult:
    """Place an order. order_type: 'market', 'limit', 'post_only'."""
    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.signing import get_timestamp_ms
        from eth_account import Account

        creds = get_credentials()
        if not creds["master_address"] or not creds["agent_private_key"]:
            return OrderResult(ok=False, error="Wallet not connected")

        wallet = Account.from_key(creds["agent_private_key"])
        exchange = Exchange(wallet, base_url, account_address=creds["master_address"])

        if order_type == "market":
            # Market order: use aggressive limit with IOC
            mid = get_mid_price(coin, base_url)
            if mid is None:
                return OrderResult(ok=False, error=f"Cannot get price for {coin}")
            # 0.5% slippage for market orders
            slippage = 0.005
            limit_price = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
            result = exchange.order(coin, is_buy, size, limit_price, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only)
        elif order_type == "limit":
            if price is None:
                return OrderResult(ok=False, error="Limit orders require a price")
            result = exchange.order(coin, is_buy, size, price, {"limit": {"tif": "Gtc"}}, reduce_only=reduce_only)
        elif order_type == "post_only":
            if price is None:
                return OrderResult(ok=False, error="Post-only orders require a price")
            result = exchange.order(coin, is_buy, size, price, {"limit": {"tif": "Alo"}}, reduce_only=reduce_only)
        else:
            return OrderResult(ok=False, error=f"Unknown order type: {order_type}")

        # Parse response
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            oid = None
            for s in statuses:
                if isinstance(s, dict) and "resting" in s:
                    oid = s["resting"].get("oid")
                elif isinstance(s, dict) and "filled" in s:
                    oid = s["filled"].get("oid")
            return OrderResult(ok=True, order_id=oid, raw=result)
        else:
            return OrderResult(ok=False, error=str(result), raw=result)

    except ImportError:
        return OrderResult(ok=False, error="hyperliquid SDK not installed. Run: pip install hyperliquid-python-sdk")
    except Exception as e:
        return OrderResult(ok=False, error=str(e))


def cancel_order(coin: str, order_id: int, base_url: str = HL_MAINNET) -> OrderResult:
    try:
        from hyperliquid.exchange import Exchange
        from eth_account import Account

        creds = get_credentials()
        wallet = Account.from_key(creds["agent_private_key"])
        exchange = Exchange(wallet, base_url, account_address=creds["master_address"])
        result = exchange.cancel(coin, order_id)
        return OrderResult(ok=result.get("status") == "ok", raw=result)
    except Exception as e:
        return OrderResult(ok=False, error=str(e))


def cancel_all_orders(coin: str, base_url: str = HL_MAINNET) -> list[OrderResult]:
    creds = get_credentials()
    orders = get_open_orders(creds["master_address"], base_url)
    results = []
    for order in orders:
        if order.get("coin") == coin:
            results.append(cancel_order(coin, order["oid"], base_url))
    return results
