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

from copy import deepcopy
import json
import math
import platform
import shutil
import subprocess
import time
import urllib.request
from http.client import IncompleteRead, RemoteDisconnected
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

HL_MAINNET = "https://api.hyperliquid.xyz"
HL_TESTNET = "https://api.hyperliquid-testnet.xyz"
SERVICE_NAME = "hyperbot"

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFEST = ROOT / "hyperbot.workspace.json"
DEFAULT_STALE_IF_ERROR_SECONDS = 300.0
_INFO_CACHE: dict[tuple[str, str], tuple[float, float, Any]] = {}
_INFO_BACKOFF: dict[tuple[str, str], tuple[float, float]] = {}


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

def _cache_key(base_url: str, payload: dict) -> tuple[str, str]:
    return base_url, json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _is_retryable_info_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (URLError, RemoteDisconnected, IncompleteRead, TimeoutError, ConnectionError, OSError))


def _info_post_urllib(payload: dict, base_url: str = HL_MAINNET) -> Any:
    req = urllib.request.Request(
        f"{base_url}/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _info_post_curl(payload: dict, base_url: str = HL_MAINNET) -> Any:
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is not available for fallback transport")
    proc = subprocess.run(
        [
            curl,
            "-fsS",
            "--max-time",
            "10",
            "-X",
            "POST",
            f"{base_url}/info",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "curl request failed").strip())
    return json.loads(proc.stdout)


def _info_post(
    payload: dict,
    base_url: str = HL_MAINNET,
    *,
    cache_ttl: float = 0.0,
    stale_if_error_ttl: float = DEFAULT_STALE_IF_ERROR_SECONDS,
) -> Any:
    cache_key = _cache_key(base_url, payload)
    now = time.monotonic()
    cached = _INFO_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return deepcopy(cached[2])

    backoff_until, backoff_delay = _INFO_BACKOFF.get(cache_key, (0.0, 0.0))
    if cached and now < backoff_until and now - cached[1] <= stale_if_error_ttl:
        return deepcopy(cached[2])

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            result = _info_post_urllib(payload, base_url)
            stamp = time.monotonic()
            _INFO_CACHE[cache_key] = (stamp + cache_ttl, stamp, result)
            _INFO_BACKOFF.pop(cache_key, None)
            return deepcopy(result)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_info_error(exc) or attempt == 2:
                break
            time.sleep(0.35 * (2 ** attempt))

    try:
        result = _info_post_curl(payload, base_url)
        stamp = time.monotonic()
        _INFO_CACHE[cache_key] = (stamp + cache_ttl, stamp, result)
        _INFO_BACKOFF.pop(cache_key, None)
        return deepcopy(result)
    except Exception as exc:
        failure = last_exc or exc
        if _is_retryable_info_error(failure):
            prev_delay = backoff_delay or 2.0
            if isinstance(failure, HTTPError) and failure.code == 429:
                next_delay = min(max(prev_delay * 2.0, 5.0), 60.0)
            else:
                next_delay = min(max(prev_delay * 1.5, 2.0), 30.0)
            _INFO_BACKOFF[cache_key] = (time.monotonic() + next_delay, next_delay)
        if cached and now - cached[1] <= stale_if_error_ttl:
            return deepcopy(cached[2])
        if last_exc is not None:
            raise last_exc
        raise exc


def get_all_mids(base_url: str = HL_MAINNET) -> dict[str, str]:
    """Get mid prices for all coins. Returns {coin: price_str}."""
    return _info_post({"type": "allMids"}, base_url, cache_ttl=2.0, stale_if_error_ttl=120.0)


def get_meta(base_url: str = HL_MAINNET) -> dict:
    """Get perps metadata: universe of assets with szDecimals, maxLeverage, etc."""
    return _info_post({"type": "meta"}, base_url, cache_ttl=3600.0, stale_if_error_ttl=3600.0)


def get_spot_meta(base_url: str = HL_MAINNET) -> dict:
    """Get spot metadata: universe of spot tokens."""
    return _info_post({"type": "spotMeta"}, base_url, cache_ttl=3600.0, stale_if_error_ttl=3600.0)


def get_meta_and_asset_ctxs(base_url: str = HL_MAINNET) -> list:
    """Get perps metadata + asset contexts (prices, funding, OI) in one call."""
    return _info_post({"type": "metaAndAssetCtxs"}, base_url, cache_ttl=15.0, stale_if_error_ttl=300.0)


def get_spot_meta_and_asset_ctxs(base_url: str = HL_MAINNET) -> list:
    """Get spot metadata + asset contexts in one call."""
    return _info_post({"type": "spotMetaAndAssetCtxs"}, base_url, cache_ttl=15.0, stale_if_error_ttl=300.0)


def get_all_markets(base_url: str = HL_MAINNET) -> dict:
    """Get the full categorized market universe.

    Returns: {
        "perps": [...], "spot": [...],
        Each item has: coin, price, dayNtlVlm, maxLeverage, category
        Categories: "crypto", "tradfi", "hip3", "prelaunch"
    }
    """
    # Known TradFi tickers on Hyperliquid (stocks, indices, commodities, forex)
    TRADFI = {
        # Equities / indices
        "AAPL", "AMZN", "GOOG", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
        "AMD", "NFLX", "COIN", "MSTR", "GME", "AMC", "PLTR", "BABA",
        "TSM", "INTC", "UBER", "ABNB", "SNAP", "SQ", "SHOP", "RBLX",
        "SPY", "QQQ", "DIA", "IWM", "GLD", "SLV", "USO", "TLT",
        # Commodities / forex
        "XAU", "XAG", "WTI", "BRENT", "NG", "EUR", "GBP", "JPY",
    }

    result: dict[str, list] = {"perps": [], "spot": []}

    # Perps: meta + asset contexts come as [meta_dict, [asset_ctx, ...]]
    try:
        perp_data = get_meta_and_asset_ctxs(base_url)
        if isinstance(perp_data, list) and len(perp_data) == 2:
            meta = perp_data[0]
            ctxs = perp_data[1]
            universe = meta.get("universe", [])
            for i, asset in enumerate(universe):
                ctx = ctxs[i] if i < len(ctxs) else {}
                coin = asset.get("name", "")
                max_lev = asset.get("maxLeverage", 1)
                vol = ctx.get("dayNtlVlm", "0")
                # Categorize
                if coin.upper() in TRADFI:
                    cat = "tradfi"
                elif asset.get("isPreLaunch"):
                    cat = "prelaunch"
                elif max_lev <= 3:
                    # Low leverage + small name often = pre-launch or HIP-3
                    cat = "crypto"
                else:
                    cat = "crypto"
                result["perps"].append({
                    "coin": coin,
                    "szDecimals": asset.get("szDecimals", 0),
                    "maxLeverage": max_lev,
                    "price": ctx.get("markPx", "0"),
                    "funding": ctx.get("funding", "0"),
                    "openInterest": ctx.get("openInterest", "0"),
                    "dayNtlVlm": vol,
                    "category": cat,
                })
    except Exception as e:
        print(f"  [hl_client] Perps meta error: {e}", flush=True)

    # Spot: spotMeta + asset contexts come as [meta_dict, [asset_ctx, ...]]
    try:
        spot_data = get_spot_meta_and_asset_ctxs(base_url)
        if isinstance(spot_data, list) and len(spot_data) == 2:
            meta = spot_data[0]
            ctxs = spot_data[1]
            tokens = meta.get("tokens", [])
            universe = meta.get("universe", [])

            # Build token index lookup for HIP-3 detection
            token_index = {}
            for ti, tok in enumerate(tokens):
                token_index[ti] = tok

            for i, market in enumerate(universe):
                ctx = ctxs[i] if i < len(ctxs) else {}
                raw_name = market.get("name", "")
                is_canonical = market.get("isCanonical", False)

                # Resolve human-readable name from token index
                # Canonical markets have names like "PURR/USDC"
                # Non-canonical (HIP-3) markets have names like "@1", "@2"
                base_idx = market.get("tokens", [None, None])[0]
                base_token = token_index.get(base_idx, {}) if base_idx is not None else {}
                token_name = base_token.get("name", "")

                if is_canonical and "/" in raw_name:
                    # Use the base part of "PURR/USDC"
                    coin_name = raw_name.split("/")[0]
                elif token_name and not raw_name.startswith("@"):
                    coin_name = raw_name
                elif token_name:
                    coin_name = token_name
                else:
                    coin_name = raw_name

                # Skip tokens with no usable name
                if not coin_name or coin_name.startswith("@"):
                    continue

                is_hip3 = not is_canonical
                result["spot"].append({
                    "coin": coin_name,
                    "price": ctx.get("markPx", "0"),
                    "dayNtlVlm": ctx.get("dayNtlVlm", "0"),
                    "category": "hip3" if is_hip3 else "spot",
                })
    except Exception as e:
        print(f"  [hl_client] Spot meta error: {e}", flush=True)

    return result


def get_mid_price(coin: str, base_url: str = HL_MAINNET, mids: dict[str, str] | None = None) -> float | None:
    if mids is None:
        mids = get_all_mids(base_url)
    price_str = mids.get(coin)
    return float(price_str) if price_str else None


def get_l2_book(coin: str, base_url: str = HL_MAINNET) -> dict:
    return _info_post({"type": "l2Book", "coin": coin}, base_url, cache_ttl=1.0, stale_if_error_ttl=30.0)


def get_candles(coin: str, interval: str, lookback_days: int, base_url: str = HL_MAINNET) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (lookback_days * 86400 * 1000)
    interval_key = str(interval).lower()
    ttl_map = {
        "1m": 15.0,
        "5m": 30.0,
        "15m": 60.0,
        "1h": 120.0,
        "4h": 300.0,
        "1d": 900.0,
    }
    stale_map = {
        "1m": 120.0,
        "5m": 180.0,
        "15m": 300.0,
        "1h": 600.0,
        "4h": 1800.0,
        "1d": 7200.0,
    }
    return _info_post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": now_ms},
    }, base_url, cache_ttl=ttl_map.get(interval_key, 60.0), stale_if_error_ttl=stale_map.get(interval_key, 600.0))


def get_clearinghouse_state(address: str, base_url: str = HL_MAINNET) -> dict:
    """Get perps positions and margin summary for an address."""
    return _info_post({"type": "clearinghouseState", "user": address}, base_url, cache_ttl=10.0, stale_if_error_ttl=120.0)


def get_spot_clearinghouse_state(address: str, base_url: str = HL_MAINNET) -> dict:
    """Get spot balances for an address."""
    return _info_post({"type": "spotClearinghouseState", "user": address}, base_url, cache_ttl=10.0, stale_if_error_ttl=120.0)


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
    def _sfloat(v) -> float:
        """Safe float: handles None, missing keys, and explicit JSON nulls."""
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    result = {
        "perps_equity": 0.0,
        "spot_usdc": 0.0,
        "spot_total_usd": 0.0,
        "total_equity": 0.0,
        "unrealized_pnl": 0.0,
        "positions": [],
        "error": None,
    }

    # Perps clearinghouse — let network errors bubble up so the caller
    # (trading loop) can surface them in the UI instead of showing $0.
    try:
        ch = get_clearinghouse_state(address, base_url)
        margin = ch.get("marginSummary") or {}
        result["perps_equity"] = _sfloat(margin.get("accountValue"))
        result["unrealized_pnl"] = _sfloat(margin.get("totalUnrealizedPnl"))
        result["positions"] = [
            {
                "coin": p["position"]["coin"],
                "size": p["position"]["szi"],
                "entry_price": p["position"]["entryPx"],
                "unrealized_pnl": p["position"]["unrealizedPnl"],
                "leverage": (p["position"].get("leverage") or {}).get("value", "?"),
            }
            for p in ch.get("assetPositions", [])
            if _sfloat(p.get("position", {}).get("szi")) != 0
        ]
    except (ConnectionError, TimeoutError, OSError) as e:
        # Network errors bubble up — caller should surface in UI
        raise
    except Exception as e:
        print(f"  [portfolio] Perps parse error: {e}", flush=True)
        result["error"] = f"Perps: {e}"

    # Spot clearinghouse
    try:
        spot = get_spot_clearinghouse_state(address, base_url)
        mids = {}
        try:
            mids = get_all_mids(base_url)
        except Exception as e:
            print(f"  [portfolio] Mid price snapshot failed: {e}", flush=True)
        for bal in (spot.get("balances") or []):
            token = bal.get("coin", "")
            amount = _sfloat(bal.get("total"))
            if token == "USDC":
                result["spot_usdc"] = amount
                result["spot_total_usd"] += amount
            elif amount > 0:
                mid = get_mid_price(token, base_url, mids=mids)
                if mid:
                    result["spot_total_usd"] += amount * mid
    except (ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        print(f"  [portfolio] Spot parse error: {e}", flush=True)
        result["error"] = f"Spot: {e}"

    result["total_equity"] = result["perps_equity"] + result["spot_total_usd"]
    return result


def get_open_orders(address: str, base_url: str = HL_MAINNET) -> list[dict]:
    return _info_post({"type": "openOrders", "user": address}, base_url, cache_ttl=5.0, stale_if_error_ttl=60.0)


def get_user_fills(address: str, base_url: str = HL_MAINNET) -> list[dict]:
    return _info_post({"type": "userFills", "user": address}, base_url, cache_ttl=5.0, stale_if_error_ttl=60.0)


def get_user_fills_by_time(
    address: str,
    start_ms: int,
    end_ms: int | None = None,
    base_url: str = HL_MAINNET,
    aggregate_by_time: bool = True,
) -> list[dict]:
    payload: dict[str, Any] = {
        "type": "userFillsByTime",
        "user": address,
        "startTime": start_ms,
        "aggregateByTime": aggregate_by_time,
    }
    if end_ms is not None:
        payload["endTime"] = end_ms
    return _info_post(payload, base_url, cache_ttl=30.0, stale_if_error_ttl=180.0)


def get_meta(base_url: str = HL_MAINNET) -> dict:
    """Get universe metadata (asset IDs, tick sizes, etc.)."""
    return _info_post({"type": "meta"}, base_url, cache_ttl=3600.0, stale_if_error_ttl=3600.0)


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


def _parse_order_result(result: dict[str, Any]) -> OrderResult:
    """Normalize Hyperliquid order responses into OrderResult."""
    if result.get("status") != "ok":
        err = result.get("response", str(result))
        print(f"  [order] REJECTED: {err}", flush=True)
        return OrderResult(ok=False, error=str(err), raw=result)

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    oid = None
    errors = []
    for status in statuses:
        if isinstance(status, dict):
            if "resting" in status:
                oid = status["resting"].get("oid")
            elif "filled" in status:
                oid = status["filled"].get("oid")
            elif "error" in status:
                errors.append(status["error"])
        elif isinstance(status, str):
            errors.append(status)
    if errors:
        err_msg = "; ".join(errors)
        print(f"  [order] STATUS ERRORS: {err_msg}", flush=True)
        return OrderResult(ok=False, error=err_msg, raw=result)
    if oid is None:
        print(f"  [order] WARNING: status=ok but no oid and no errors. Statuses: {statuses}", flush=True)
    return OrderResult(ok=oid is not None, order_id=oid, raw=result)


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

        return _parse_order_result(result)

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


# ---------------------------------------------------------------------------
# Best bid/ask from L2 book
# ---------------------------------------------------------------------------

def get_best_bid_ask(coin: str, base_url: str = HL_MAINNET) -> dict:
    """Get best bid and ask prices from L2 order book.

    Returns: {"best_bid": float|None, "best_ask": float|None, "spread_pct": float|None}
    """
    try:
        book = get_l2_book(coin, base_url)
        levels = book.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []
        best_bid = float(bids[0]["px"]) if bids else None
        best_ask = float(asks[0]["px"]) if asks else None
        spread_pct = None
        if best_bid and best_ask:
            spread_pct = (best_ask - best_bid) / best_ask
        return {"best_bid": best_bid, "best_ask": best_ask, "spread_pct": spread_pct}
    except Exception as e:
        print(f"  [hl_client] get_best_bid_ask({coin}) error: {e}", flush=True)
        return {"best_bid": None, "best_ask": None, "spread_pct": None}


def check_depth(
    coin: str,
    order_size_usd: float,
    depth_pct: float = 0.001,
    min_depth_multiple: float = 2.0,
    base_url: str = HL_MAINNET,
) -> dict:
    """Check if the order book has enough depth to absorb an order.

    Sums cumulative USD depth within `depth_pct` (default 0.1%) of the
    mid price on both bid and ask sides.

    Args:
        coin: Asset symbol (e.g. "BTC")
        order_size_usd: Notional value of the intended order
        depth_pct: How far from mid to measure depth (0.001 = 0.1%)
        min_depth_multiple: Required depth as a multiple of order size
        base_url: API endpoint

    Returns: {
        "sufficient": bool,
        "bid_depth_usd": float,
        "ask_depth_usd": float,
        "order_size_usd": float,
        "required_usd": float,    # order_size * min_depth_multiple
        "depth_ratio": float,     # min(bid,ask) / order_size
    }
    """
    result = {
        "sufficient": False,
        "bid_depth_usd": 0.0,
        "ask_depth_usd": 0.0,
        "order_size_usd": order_size_usd,
        "required_usd": order_size_usd * min_depth_multiple,
        "depth_ratio": 0.0,
    }

    try:
        book = get_l2_book(coin, base_url)
        levels = book.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        if not bids or not asks:
            return result

        mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
        threshold = mid * depth_pct

        # Sum bid depth within threshold
        bid_depth = 0.0
        for level in bids:
            px = float(level["px"])
            sz = float(level["sz"])
            if mid - px <= threshold:
                bid_depth += px * sz
            else:
                break

        # Sum ask depth within threshold
        ask_depth = 0.0
        for level in asks:
            px = float(level["px"])
            sz = float(level["sz"])
            if px - mid <= threshold:
                ask_depth += px * sz
            else:
                break

        result["bid_depth_usd"] = bid_depth
        result["ask_depth_usd"] = ask_depth
        min_depth = min(bid_depth, ask_depth)
        result["depth_ratio"] = min_depth / order_size_usd if order_size_usd > 0 else 0.0
        result["sufficient"] = min_depth >= order_size_usd * min_depth_multiple

    except Exception as e:
        print(f"  [hl_client] check_depth({coin}) error: {e}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Leverage management
# ---------------------------------------------------------------------------

def update_leverage(
    coin: str,
    leverage: int,
    margin_mode: str = "isolated",
    base_url: str = HL_MAINNET,
) -> OrderResult:
    """Set leverage and explicit margin mode for a coin."""
    try:
        exchange = _get_exchange_client(base_url)
        mode = str(margin_mode or "isolated").strip().lower()
        is_cross = mode == "cross"
        result = exchange.update_leverage(int(leverage), coin, is_cross=is_cross)
        print(f"  [order] Set leverage {coin} → {leverage}x mode={mode}: {result}", flush=True)
        return OrderResult(ok=result.get("status") == "ok", raw=result)
    except Exception as e:
        print(f"  [order] update_leverage({coin}, {leverage}, margin_mode={margin_mode}) error: {e}", flush=True)
        return OrderResult(ok=False, error=str(e))


# ---------------------------------------------------------------------------
# Trigger orders (TP/SL with explicit limit prices)
# ---------------------------------------------------------------------------

def place_trigger_order(
    coin: str,
    is_buy: bool,
    size: float,
    trigger_price: float,
    limit_price: float,
    tp_or_sl: str = "sl",
    reduce_only: bool = True,
    base_url: str = HL_MAINNET,
) -> OrderResult:
    """Place a trigger order (TP or SL) with explicit limit price.

    Args:
        coin: Asset symbol (e.g. "BTC")
        is_buy: True for buy (closing a short), False for sell (closing a long)
        size: Position size to close
        trigger_price: Mark price level that activates the order
        limit_price: Limit price for the fill (must be worse than trigger for safety)
        tp_or_sl: "tp" or "sl"
        reduce_only: Should always be True for exits
    """
    try:
        exchange = _get_exchange_client(base_url)

        # Look up asset metadata for proper rounding
        asset_info = get_asset_info(coin, base_url)
        sz_decimals = asset_info.get("szDecimals", 3) if asset_info else 3
        rounded_size = round_size(size, sz_decimals)
        if rounded_size <= 0:
            return OrderResult(ok=False, error=f"Trigger order size rounds to 0")

        rp_trigger = round_price(trigger_price, coin, base_url)
        rp_limit = round_price(limit_price, coin, base_url)

        print(f"  [order] TRIGGER {tp_or_sl.upper()} {coin} "
              f"{'BUY' if is_buy else 'SELL'} size={rounded_size} "
              f"trigger={rp_trigger} limit={rp_limit} reduce_only={reduce_only}",
              flush=True)

        result = exchange.order(
            coin,
            is_buy,
            rounded_size,
            rp_limit,
            {
                "trigger": {
                    "triggerPx": rp_trigger,
                    "isMarket": False,
                    "tpsl": tp_or_sl,
                }
            },
            reduce_only=reduce_only,
        )
        print(f"  [order] TRIGGER RAW RESPONSE: {json.dumps(result)}", flush=True)
        return _parse_order_result(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return OrderResult(ok=False, error=str(e))
