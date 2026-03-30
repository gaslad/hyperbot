#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MANIFEST = ROOT / "hyperbot.workspace.json"
CONFIG_DIR = ROOT / "config" / "strategies"
RESEARCH_DIR = ROOT / "research"
PROFILES_DIR = RESEARCH_DIR / "profiles"
REVISIONS_DIR = RESEARCH_DIR / "revisions"


@dataclass(slots=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_hl(cls, raw: dict[str, Any]) -> "Candle":
        return cls(
            timestamp=int(raw["t"]),
            open=float(raw["o"]),
            high=float(raw["h"]),
            low=float(raw["l"]),
            close=float(raw["c"]),
            volume=float(raw["v"]),
        )


CACHE_DIR = RESEARCH_DIR / "cache"


class CandleFetcher:
    def __init__(self, base_url: str = "https://api.hyperliquid.xyz", cache_max_age_hours: int = 24) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_max_age_seconds = cache_max_age_hours * 3600

    def _cache_path(self, coin: str, interval: str, lookback_days: int) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR / f"{coin.lower()}_{interval}_{lookback_days}d.json"

    def _cache_valid(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        return age < self.cache_max_age_seconds

    def interval(self, coin: str, interval: str, lookback_days: int) -> list[Candle]:
        cache_path = self._cache_path(coin, interval, lookback_days)

        if self._cache_valid(cache_path):
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            return [Candle.from_hl(item) for item in raw]

        lookback_ms = lookback_days * 24 * 60 * 60 * 1000
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback_ms
        body = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": now_ms,
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/info",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))

        cache_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return [Candle.from_hl(item) for item in raw]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def infer_coin(symbol: str) -> str:
    for suffix in ("USDT", "USD", "PERP"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def pct_change(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return (b - a) / a * 100.0


def compute_returns(candles: list[Candle]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(candles, candles[1:]):
        if prev.close:
            out.append((cur.close - prev.close) / prev.close)
    return out


def compute_sma(closes: list[float], period: int, end_index: int) -> float | None:
    if end_index + 1 < period:
        return None
    window = closes[end_index - period + 1 : end_index + 1]
    return mean(window)


def compute_market_profile(symbol: str, daily: list[Candle], h4: list[Candle], h1: list[Candle]) -> dict[str, Any]:
    closes = [c.close for c in daily]
    daily_returns = compute_returns(daily)
    recent_window = daily[-20:] if len(daily) >= 20 else daily
    prior_window = daily[-40:-20] if len(daily) >= 40 else daily[:-20]

    sma20 = mean(closes[-20:]) if len(closes) >= 20 else mean(closes)
    sma50 = mean(closes[-50:]) if len(closes) >= 50 else mean(closes)
    close_now = closes[-1] if closes else 0.0
    trend_90d_pct = pct_change(closes[0], closes[-1]) if len(closes) >= 2 else 0.0
    volatility_pct = statistics.pstdev(daily_returns) * 100 if len(daily_returns) >= 2 else 0.0
    avg_daily_range_pct = mean([
        ((c.high - c.low) / c.close * 100.0) if c.close else 0.0
        for c in recent_window
    ])
    above_sma20_ratio = mean([1.0 if c.close > sma20 else 0.0 for c in recent_window]) if recent_window else 0.0
    compression_ratio = 1.0
    if recent_window and prior_window:
        recent_range = mean([((c.high - c.low) / c.close * 100.0) if c.close else 0.0 for c in recent_window])
        prior_range = mean([((c.high - c.low) / c.close * 100.0) if c.close else 0.0 for c in prior_window])
        if prior_range:
            compression_ratio = recent_range / prior_range
    wickiness = mean([
        (((c.high - max(c.open, c.close)) + (min(c.open, c.close) - c.low)) / max(abs(c.close - c.open), 0.0001))
        for c in recent_window
    ]) if recent_window else 0.0

    sweep_events = 0
    for idx in range(5, len(daily)):
        prev_high = max(c.high for c in daily[idx - 5:idx])
        prev_low = min(c.low for c in daily[idx - 5:idx])
        bar = daily[idx]
        if bar.high > prev_high and bar.close < prev_high:
            sweep_events += 1
        if bar.low < prev_low and bar.close > prev_low:
            sweep_events += 1
    sweep_rate_pct = (sweep_events / max(len(daily), 1)) * 100.0

    return {
        "symbol": symbol,
        "daily_bars": len(daily),
        "h4_bars": len(h4),
        "h1_bars": len(h1),
        "close_now": round(close_now, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "trend_90d_pct": round(trend_90d_pct, 3),
        "volatility_pct": round(volatility_pct, 3),
        "avg_daily_range_pct": round(avg_daily_range_pct, 3),
        "above_sma20_ratio": round(above_sma20_ratio, 3),
        "compression_ratio": round(compression_ratio, 3),
        "wickiness": round(wickiness, 3),
        "sweep_rate_pct": round(sweep_rate_pct, 3),
    }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_pack(pack_id: str, profile: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    trend = profile["trend_90d_pct"]
    volatility = profile["volatility_pct"]
    above_sma = profile["above_sma20_ratio"]
    compression = profile["compression_ratio"]
    wickiness = profile["wickiness"]
    sweep_rate = profile["sweep_rate_pct"]

    if pack_id == "trend_pullback":
        score = 50.0
        score += clamp(trend / 2.5, -15, 20)
        score += clamp((above_sma - 0.5) * 40.0, -10, 15)
        score -= clamp((volatility - 4.0) * 3.0, 0, 15)
        if trend > 10:
            reasons.append("90d trend is positive enough to support continuation logic.")
        if above_sma > 0.6:
            reasons.append("Price has spent most recent daily bars above the 20-day mean.")
        if volatility > 6:
            reasons.append("Elevated volatility slightly penalizes trend pullback quality.")
        return round(clamp(score, 0, 100), 2), reasons

    if pack_id == "compression_breakout":
        score = 45.0
        score += clamp((1.0 - compression) * 35.0, -10, 25)
        score += clamp(volatility * 2.0, 0, 15)
        score -= clamp(abs(trend) / 5.0, 0, 10)
        if compression < 0.85:
            reasons.append("Recent daily range is compressed relative to the prior regime.")
        if volatility > 4:
            reasons.append("There is enough realized movement to reward expansion trades.")
        return round(clamp(score, 0, 100), 2), reasons

    if pack_id == "liquidity_sweep_reversal":
        score = 40.0
        score += clamp(wickiness * 8.0, 0, 25)
        score += clamp(sweep_rate * 6.0, 0, 20)
        score += clamp(volatility * 1.5, 0, 10)
        score -= clamp(abs(trend) / 4.0, 0, 10)
        if wickiness > 1.5:
            reasons.append("Recent candles show enough wickiness to justify reversal attention.")
        if sweep_rate > 5:
            reasons.append("Recent bars contain multiple sweep-and-reclaim style events.")
        return round(clamp(score, 0, 100), 2), reasons

    return 0.0, ["Unknown pack id."]


@dataclass(slots=True)
class TrendSweepConfig:
    sma_period: int
    pullback_zone_pct: float
    min_pullback_pct: float
    max_distance_pct: float
    invalidation_below_sma_pct: float


@dataclass(slots=True)
class TrendSweepResult:
    trades: int
    win_rate: float
    expectancy: float
    total_r: float
    max_drawdown_r: float
    params: dict[str, Any]


def simulate_trend_trade(candles: list[Candle], start_idx: int, entry: float, stop: float, after_tp1_mode: str) -> tuple[float, int]:
    risk = entry - stop
    if risk <= 0:
        return 0.0, start_idx
    tp1 = entry + risk
    tp2 = entry + 2.0 * risk
    realized = 0.0
    remaining = 1.0
    tp1_hit = False
    active_stop = stop

    for idx in range(start_idx, len(candles)):
        bar = candles[idx]
        if not tp1_hit:
            if bar.low <= active_stop:
                return -1.0 * remaining, idx
            if bar.high >= tp1:
                realized += 0.5
                remaining = 0.5
                tp1_hit = True
                if after_tp1_mode == "breakeven":
                    active_stop = entry
                if bar.high >= tp2:
                    realized += 1.0
                    return realized, idx
        else:
            if bar.low <= active_stop:
                if active_stop == entry:
                    return realized, idx
                realized -= 0.5
                return realized, idx
            if bar.high >= tp2:
                realized += 1.0
                return realized, idx

    last_close = candles[-1].close
    if tp1_hit:
        realized += 0.5 * ((last_close - entry) / risk)
    else:
        realized += (last_close - entry) / risk
    return realized, len(candles) - 1


def run_trend_pullback_sweep(candles: list[Candle], after_tp1_mode: str) -> list[TrendSweepResult]:
    closes = [c.close for c in candles]
    configs: list[TrendSweepConfig] = []
    for sma_period in (10, 16, 20, 30):
        for pullback_zone_pct in (3.0, 5.0, 7.5):
            for min_pullback_pct in (2.0, 3.0, 5.0):
                for max_distance_pct in (10.0, 15.0, 20.0):
                    for invalidation_below_sma_pct in (2.0, 3.0, 5.0):
                        configs.append(TrendSweepConfig(
                            sma_period=sma_period,
                            pullback_zone_pct=pullback_zone_pct,
                            min_pullback_pct=min_pullback_pct,
                            max_distance_pct=max_distance_pct,
                            invalidation_below_sma_pct=invalidation_below_sma_pct,
                        ))

    results: list[TrendSweepResult] = []
    for cfg in configs:
        trades: list[float] = []
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        idx = cfg.sma_period + 12
        while idx < len(candles) - 1:
            sma = compute_sma(closes, cfg.sma_period, idx)
            prev_sma = compute_sma(closes, cfg.sma_period, idx - 1)
            if sma is None or prev_sma is None:
                idx += 1
                continue
            regime_ok = candles[idx].close > sma and sma > prev_sma
            recent_high = max(c.high for c in candles[idx - 10:idx])
            pullback_pct = ((recent_high - candles[idx].low) / recent_high * 100.0) if recent_high else 0.0
            distance_pct = ((candles[idx].close - sma) / sma * 100.0) if sma else 0.0
            zone_ok = candles[idx].low <= sma * (1.0 + cfg.pullback_zone_pct / 100.0)
            confirmation_ok = candles[idx].close > candles[idx - 1].high
            if regime_ok and zone_ok and confirmation_ok and pullback_pct >= cfg.min_pullback_pct and distance_pct <= cfg.max_distance_pct:
                entry = candles[idx].close
                stop = sma * (1.0 - cfg.invalidation_below_sma_pct / 100.0)
                if stop < entry:
                    trade_r, exit_idx = simulate_trend_trade(candles, idx + 1, entry, stop, after_tp1_mode)
                    trades.append(trade_r)
                    cumulative += trade_r
                    peak = max(peak, cumulative)
                    max_dd = max(max_dd, peak - cumulative)
                    idx = max(exit_idx + 1, idx + 1)
                    continue
            idx += 1

        if len(trades) >= 2:
            win_rate = sum(1 for r in trades if r > 0) / len(trades) * 100.0
            total_r = sum(trades)
            expectancy = total_r / len(trades)
            results.append(TrendSweepResult(
                trades=len(trades),
                win_rate=round(win_rate, 3),
                expectancy=round(expectancy, 4),
                total_r=round(total_r, 4),
                max_drawdown_r=round(max_dd, 4),
                params=asdict(cfg),
            ))
    results.sort(key=lambda row: (row.expectancy, row.total_r, -row.max_drawdown_r, row.win_rate), reverse=True)
    return results


def load_workspace_manifest() -> dict[str, Any]:
    if not WORKSPACE_MANIFEST.exists():
        raise SystemExit(f"workspace manifest missing: {WORKSPACE_MANIFEST}")
    return json.loads(WORKSPACE_MANIFEST.read_text(encoding="utf-8"))


def load_strategy_configs() -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    if not CONFIG_DIR.exists():
        return configs
    for path in sorted(CONFIG_DIR.glob("*.json")):
        if path.name == "README.md":
            continue
        configs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return configs


def pick_target_strategy(args: argparse.Namespace, workspace: dict[str, Any], configs: dict[str, dict[str, Any]], ranked: list[dict[str, Any]]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    installed = {item["strategy_id"]: item for item in workspace.get("strategy_packs", [])}
    if args.strategy_id:
        item = installed.get(args.strategy_id)
        if item is None:
            raise SystemExit(f"strategy_id not installed in workspace: {args.strategy_id}")
        return args.strategy_id, configs[args.strategy_id], item
    if args.pack_id:
        for strategy_id, item in installed.items():
            if item.get("pack_id") == args.pack_id:
                return strategy_id, configs[strategy_id], item
        raise SystemExit(f"pack not installed in workspace: {args.pack_id}")
    top = ranked[0]
    strategy_id = top["strategy_id"]
    return strategy_id, configs[strategy_id], installed[strategy_id]


def build_revision(strategy_id: str, config: dict[str, Any], pack_id: str, profile: dict[str, Any], ranked: list[dict[str, Any]], daily: list[Candle]) -> dict[str, Any]:
    revision: dict[str, Any] = {
        "strategy_id": strategy_id,
        "pack_id": pack_id,
        "generated_at": utc_now_iso(),
        "profile_summary": profile,
        "pack_rankings": ranked,
        "recommended_overrides": {},
        "notes": [],
    }

    if pack_id == "trend_pullback":
        after_tp1 = config.get("risk", {}).get("stop_management", {}).get("after_tp1", {}).get("mode", "breakeven")
        sweep = run_trend_pullback_sweep(daily, after_tp1)
        best = asdict(sweep[0]) if sweep else None
        revision["trend_pullback_sweep_top_10"] = [asdict(item) for item in sweep[:10]]
        revision["best_sweep_result"] = best
        if best:
            revision["recommended_overrides"] = {
                "entry": {
                    "sma_period": best["params"]["sma_period"],
                    "pullback_zone_pct": best["params"]["pullback_zone_pct"],
                    "confirmation_type": "close_above_prev_high",
                },
                "filters": {
                    "min_pullback_pct": best["params"]["min_pullback_pct"],
                    "overextension_max_pct": best["params"]["max_distance_pct"],
                },
                "risk": {
                    "invalidation_below_sma_pct": best["params"]["invalidation_below_sma_pct"],
                },
            }
            revision["notes"].append("Trend pullback revision is based on a 90-day sweep over recent daily candles for this symbol.")
    elif pack_id == "compression_breakout":
        atr_gate = 20 if profile["compression_ratio"] < 0.85 else 30
        trigger_tf = "1H" if profile["avg_daily_range_pct"] >= 4 else "4H"
        revision["recommended_overrides"] = {
            "runner": {"trigger_timeframe": trigger_tf},
            "filters": {
                "compression_lookback_bars": 20,
                "atr_percentile_max": atr_gate,
                "volume_expansion_required": True,
            },
        }
        revision["notes"].append("Compression breakout revision is heuristic and uses symbol compression diagnostics rather than a full strategy-specific sweep.")
    elif pack_id == "liquidity_sweep_reversal":
        lookback = 10 if profile["sweep_rate_pct"] >= 5 else 14
        risk_pct = 0.5 if profile["volatility_pct"] >= 6 else 0.75
        revision["recommended_overrides"] = {
            "filters": {
                "swing_lookback_bars": lookback,
                "reclaim_required": True,
                "regime_alignment_required": profile["trend_90d_pct"] > 10,
            },
            "risk": {
                "position_sizing": {
                    "risk_per_trade_pct": risk_pct,
                }
            },
        }
        revision["notes"].append("Liquidity sweep reversal revision is heuristic and emphasizes recent wickiness and sweep frequency.")
    return revision


def write_outputs(symbol: str, days: int, profile_payload: dict[str, Any], revision_payload: dict[str, Any]) -> tuple[Path, Path, Path]:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    REVISIONS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    profile_path = PROFILES_DIR / f"{symbol.lower()}_{days}d_profile_{stamp}.json"
    revision_path = REVISIONS_DIR / f"{symbol.lower()}_{revision_payload['strategy_id']}_{days}d_revision_{stamp}.json"
    summary_path = REVISIONS_DIR / f"{symbol.lower()}_{revision_payload['strategy_id']}_{days}d_revision_{stamp}.md"

    profile_path.write_text(json.dumps(profile_payload, indent=2), encoding="utf-8")
    revision_path.write_text(json.dumps(revision_payload, indent=2), encoding="utf-8")

    lines = [
        f"# Token-Specific Revision: {revision_payload['strategy_id']}",
        "",
        f"- Symbol: {symbol}",
        f"- Lookback: {days} days",
        f"- Generated: {revision_payload['generated_at']}",
        "",
        "## Pack Ranking",
    ]
    for row in revision_payload["pack_rankings"]:
        lines.append(f"- {row['pack_id']}: score {row['score']} ({'; '.join(row['reasons']) if row['reasons'] else 'no extra notes'})")
    lines.extend([
        "",
        "## Recommended Overrides",
        "```json",
        json.dumps(revision_payload["recommended_overrides"], indent=2),
        "```",
    ])
    if revision_payload.get("notes"):
        lines.extend(["", "## Notes"])
        for note in revision_payload["notes"]:
            lines.append(f"- {note}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile_path, revision_path, summary_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Profile a symbol and generate a token-specific strategy revision.")
    parser.add_argument("--strategy-id")
    parser.add_argument("--pack-id")
    parser.add_argument("--symbol")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--api-url", default="https://api.hyperliquid.xyz")
    parser.add_argument("--cache-max-age", type=int, default=24, help="Max cache age in hours (default 24, 0 to disable)")
    parser.add_argument("--offline", action="store_true", help="Use cached data only, fail if cache is missing or stale")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = load_workspace_manifest()
    configs = load_strategy_configs()
    symbol = args.symbol
    if not symbol and args.strategy_id and args.strategy_id in configs:
        symbol = configs[args.strategy_id].get("market", {}).get("symbol")
    if not symbol:
        symbol = workspace.get("symbol", "BTCUSDT")
    coin = workspace.get("coin") or infer_coin(symbol)

    cache_age = args.cache_max_age
    if args.offline:
        cache_age = 999999  # accept any cached data in offline mode

    fetcher = CandleFetcher(base_url=args.api_url, cache_max_age_hours=cache_age)

    if args.offline:
        # In offline mode, verify cache exists before attempting fetch
        for iv, lb in [("1d", max(args.days, 120)), ("4h", max(args.days, 30)), ("1h", max(args.days, 14))]:
            cp = fetcher._cache_path(coin, iv, lb)
            if not cp.exists():
                raise SystemExit(f"offline mode: no cached data at {cp}. Run once online first.")

    daily = fetcher.interval(coin, "1d", max(args.days, 120))
    h4 = fetcher.interval(coin, "4h", max(args.days, 30))
    h1 = fetcher.interval(coin, "1h", max(args.days, 14))

    profile = compute_market_profile(symbol, daily[-args.days:], h4, h1)
    rankings = []
    for installed in workspace.get("strategy_packs", []):
        score, reasons = score_pack(installed["pack_id"], profile)
        rankings.append({
            "strategy_id": installed["strategy_id"],
            "pack_id": installed["pack_id"],
            "display_name": installed.get("display_name"),
            "score": score,
            "reasons": reasons,
        })
    rankings.sort(key=lambda row: row["score"], reverse=True)

    strategy_id, config, installed = pick_target_strategy(args, workspace, configs, rankings)
    revision = build_revision(strategy_id, config, installed["pack_id"], profile, rankings, daily[-max(args.days, 120):])
    profile_payload = {
        "generated_at": utc_now_iso(),
        "symbol": symbol,
        "days": args.days,
        "market_profile": profile,
        "ranked_packs": rankings,
        "selected_strategy_id": strategy_id,
        "selected_pack_id": installed["pack_id"],
    }
    profile_path, revision_path, summary_path = write_outputs(symbol, args.days, profile_payload, revision)

    if args.json:
        print(json.dumps({
            "profile": profile_payload,
            "revision": revision,
            "artifacts": {
                "profile_path": str(profile_path),
                "revision_path": str(revision_path),
                "summary_path": str(summary_path),
            },
        }, indent=2))
        return 0

    print(f"Profile written:  {profile_path}")
    print(f"Revision written: {revision_path}")
    print(f"Summary written:  {summary_path}")
    print(f"Selected pack:    {installed['pack_id']}")
    print(f"Selected strat:   {strategy_id}")
    if rankings:
        print(f"Top score:        {rankings[0]['score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
