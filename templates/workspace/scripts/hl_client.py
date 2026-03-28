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
import math
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
    """Get perps positions and margin summary for an address."""
    return _info_post({"type": "clearinghouseState", "user": address}, base_url)


def get_spot_clearinghouse_state(address: str, base_url: str = HL_MAINNET) -> dict:
    """Get spot balances for an address."""
    return _info_post({"type": "spotClearinghouseState", "user": address}, base_url)


def get_portfolio_value(address: str, base_url: str = HL_MAINNET) -> dict:
    """Get full portfolio: perps equity + spot balances combined.

    Returns: {
        "perps_equity": float,    # perps margin account value
        "spot_usdc": float,       # USDC in spot
        "spot_total_usd": float,  # all spot balances in USD
        "total_equity": float,    # perps + spot
        "unrealized_pnl": float,
        "positions": list[dict],
    }
    """
    result = {
        "perps_equity": 0.0,
        "spot_usdc": 0.0,
        "spot_total_usd": 0.0,
        "total_equity": 0.0,
        "unrealized_pnl": 0.0,
        "positions": [],
    }

    # Perps clearinghouse
    try:
        ch = get_clearinghouse_state(address, base_url)
        margin = ch.get("marginSummary", {})
        result["perps_equity"] = float(margin.get("accountValue", 0))
        result["unrealized_pnl"] = float(margin.get("totalUnrealizedPnl", 0))
        result["positions"] = [
            {
                "coin": p["position"]["coin"],
                "size": p["position"]["szi"],
                "entry_price": p["position"]["entryPx"],
                "unrealized_pnl": p["position"]["unrealizedPnl"],
                "leverage": p["position"].get("leverage", {}).get("value", "?"),
            }
            for p in ch.get("assetPositions", [])
            if float(p["position"]["szi"]) != 0
        ]
    except Exception as e:
        print(f"  [portfolio] Perps query error: {e}", flush=True)

    # Spot clearinghouse
    try:
        spot = get_spot_clearinghouse_state(address, base_url)
        for bal in spot.get("balances", []):
            token = bal.get("coin", "")
            amount = float(bal.get("total", 0))
            if token == "USDC":
                result["spot_usdc"] = amount
                result["spot_total_usd"] += amount
            elif amount > 0:
                # Estimate USD value from mid price
                try:
                    mid = get_mid_price(token, base_url)
                    if mid:
                        result["spot_total_usd"] += amount * mid
                except Exception:
                    pass
    except Exception as e:
        print(f"  [portfolio] Spot query error: {e}", flush=True)

    result["total_equity"] = result["perps_equity"] + result["spot_total_usd"]
    return result


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


def get_asset_info(coin: str, base_url: str = HL_MAINNET) -> dict | None:
    """Get asset metadata: szDecimals, maxLeverage, name, etc."""
    meta = get_meta(base_url)
    for asset in meta.get("universe", []):
        if asset.get("name") == coin:
            return asset
    return None


def round_size(size: float, sz_decimals: int) -> float:
    """Round order size to valid decimals for this asset."""
    if sz_decimals <= 0:
        return round(size)
    return round(size, sz_decimals)


def round_price(price: float, coin: str, base_url: str = HL_MAINNET) -> float:
    """Round price to a valid tick size. HL uses 5 significant figures."""
    # Hyperliquid uses 5 significant figures for prices
    if price <= 0:
        return price
    sig_figs = 5
    magnitude = math.floor(math.log10(abs(price)))
    factor = 10 ** (sig_figs - 1 - magnitude)
    return round(price * factor) / factor


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
    """Create an Exchange client with the stored API wallet credentials."""
    try:
        from hyperliquid.exchange import Exchange
        from eth_account import Account
    except ImportError:
        raise RuntimeError(
            "hyperliquid SDK not installed. Run: pip install hyperliquid-python-sdk"
        )

    creds = get_credentials()
    if not creds["master_address"] or not creds["agent_private_key"]:
        raise RuntimeError("Wallet not connected. Run: hyperbot connect")

    wallet = Account.from_key(creds["agent_private_key"])
    return Exchange(
        wallet=wallet,
        base_url=base_url,
        account_address=creds["master_address"],
    )


def place_order(
    coin: str,
    is_buy: bool,
    size: float,
    price: float | None = None,
    reduce_only: bool = False,
    order_type: str = "market",
    base_url: str = HL_MAINNET,
) -> OrderResult:
    """Place an order. order_type: 'market', 'limit', 'post_only'.

    Automatically looks up asset metadata for proper size/price rounding.
    """
    try:
        from hyperliquid.exchange import Exchange
        from eth_account import Account

        creds = get_credentials()
        if not creds["master_address"] or not creds["agent_private_key"]:
            return OrderResult(ok=False, error="Wallet not connected")

        # Log credentials being used (addresses only, never keys)
        master = creds["master_address"]
        pk = creds["agent_private_key"]
        wallet = Account.from_key(pk)
        agent_addr = wallet.address
        print(f"  [order] master_address: {master}", flush=True)
        print(f"  [order] agent_address (derived from key): {agent_addr}", flush=True)

        exchange = Exchange(wallet, base_url, account_address=master)

        # Look up asset metadata for proper rounding
        asset_info = get_asset_info(coin, base_url)
        sz_decimals = 3  # safe default
        if asset_info:
            sz_decimals = asset_info.get("szDecimals", 3)
            print(f"  [order] {coin} szDecimals={sz_decimals} maxLeverage={asset_info.get('maxLeverage')}", flush=True)
        else:
            print(f"  [order] WARNING: No asset info for {coin}, using szDecimals={sz_decimals}", flush=True)

        # Round size to valid decimals
        rounded_size = round_size(size, sz_decimals)
        if rounded_size <= 0:
            return OrderResult(ok=False, error=f"Order size {size} rounds to 0 with {sz_decimals} decimals")

        print(f"  [order] {coin} {'BUY' if is_buy else 'SELL'} size={rounded_size} (raw={size}) type={order_type} reduce_only={reduce_only}", flush=True)

        if order_type == "market":
            # Market order: use aggressive limit with IOC
            mid = get_mid_price(coin, base_url)
            if mid is None:
                return OrderResult(ok=False, error=f"Cannot get price for {coin}")
            # 0.5% slippage for market orders
            slippage = 0.005
            limit_price = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
            limit_price = round_price(limit_price, coin, base_url)
            print(f"  [order] market IOC: mid={mid}, limit_price={limit_price} (slippage={slippage})", flush=True)
            result = exchange.order(coin, is_buy, rounded_size, limit_price, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only)
        elif order_type == "limit":
            if price is None:
                return OrderResult(ok=False, error="Limit orders require a price")
            rp = round_price(price, coin, base_url)
            result = exchange.order(coin, is_buy, rounded_size, rp, {"limit": {"tif": "Gtc"}}, reduce_only=reduce_only)
        elif order_type == "post_only":
            if price is None:
                return OrderResult(ok=False, error="Post-only orders require a price")
            rp = round_price(price, coin, base_url)
            result = exchange.order(coin, is_buy, rounded_size, rp, {"limit": {"tif": "Alo"}}, reduce_only=reduce_only)
        else:
            return OrderResult(ok=False, error=f"Unknown order type: {order_type}")

        # Log the full raw response for debugging
        print(f"  [order] RAW RESPONSE: {json.dumps(result)}", flush=True)

        # Parse response
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            oid = None
            errors = []
            for s in statuses:
                if isinstance(s, dict):
                    if "resting" in s:
                        oid = s["resting"].get("oid")
                    elif "filled" in s:
                        oid = s["filled"].get("oid")
                    elif "error" in s:
                        errors.append(s["error"])
                elif isinstance(s, str):
                    # String statuses like "WouldCrossMakerOrder" indicate issues
                    errors.append(s)
            if errors:
                err_msg = "; ".join(errors)
                print(f"  [order] STATUS ERRORS: {err_msg}", flush=True)
                return OrderResult(ok=False, error=err_msg, raw=result)
            if oid is None and not errors:
                print(f"  [order] WARNING: status=ok but no oid and no errors. Statuses: {statuses}", flush=True)
            return OrderResult(ok=oid is not None, order_id=oid, raw=result)
        else:
            err = result.get("response", str(result))
            print(f"  [order] REJECTED: {err}", flush=True)
            return OrderResult(ok=False, error=str(err), raw=result)

    except ImportError:
        return OrderResult(ok=False, error="hyperliquid SDK not installed. Run: pip install hyperliquid-python-sdk")
    except Exception as e:
        import traceback
        traceback.print_exc()
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
