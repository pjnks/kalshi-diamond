"""
diamond_backtest.py
───────────────────
Step 8: Historical replay & grid search for DIAMOND.

Modes:
  1. replay   — Fetch historical trades, replay through feature engine, report stats
  2. gridsearch — Sweep threshold/weight combos, find optimal config
  3. evaluate — Check if alerts preceded price moves (precision analysis)

Usage:
  PYTHONPATH=. python diamond_backtest.py replay --tickers 10 --trades 500
  PYTHONPATH=. python diamond_backtest.py gridsearch --tickers 5 --trades 200
  PYTHONPATH=. python diamond_backtest.py evaluate
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import itertools
import logging
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")

from diamond_config import FEATURE_WEIGHTS
from diamond_monitor import _normalize_trade
from src.diamond_features import (
    FeatureEngine,
    classify_alert_level,
    compute_composite_score,
    trade_size_zscore,
    volume_spike_ratio,
    order_book_imbalance,
    taker_side_skew,
    price_impact,
    cross_market_correlation,
)
from src.diamond_store import DiamondStore
from src.kalshi_client import KalshiRESTClient


# ── Historical Data Fetcher ──────────────────────────────────────────────


async def fetch_historical_trades(
    rest: KalshiRESTClient,
    num_tickers: int = 10,
    trades_per_ticker: int = 500,
) -> dict[str, list[dict]]:
    """
    Fetch historical trades for the most active tickers.
    Returns: {ticker: [list of normalized trades sorted by ts]}
    """
    log.info(f"Fetching recent trades to discover active tickers...")
    data = await rest.get_trades(limit=100)
    recent = data.get("trades", [])

    # Find most active tickers
    ticker_counts = Counter(t.get("ticker", t.get("market_ticker", "")) for t in recent)
    active_tickers = [t for t, _ in ticker_counts.most_common(num_tickers)]
    log.info(f"Selected {len(active_tickers)} active tickers")

    all_trades: dict[str, list[dict]] = {}

    for ticker in active_tickers:
        trades = []
        cursor = None
        fetched = 0

        while fetched < trades_per_ticker:
            batch_size = min(100, trades_per_ticker - fetched)
            try:
                data = await rest.get_trades(
                    ticker=ticker, limit=batch_size, cursor=cursor,
                )
                batch = data.get("trades", [])
                if not batch:
                    break
                trades.extend(batch)
                fetched += len(batch)
                cursor = data.get("cursor")
                if not cursor:
                    break
            except Exception as e:
                log.debug(f"Fetch error for {ticker}: {e}")
                break
            await asyncio.sleep(0.1)

        # Normalize and sort chronologically
        normalized = []
        for t in trades:
            nt = _normalize_trade(t)
            if nt.get("ticker"):
                normalized.append(nt)
        normalized.sort(key=lambda x: x.get("ts", 0))

        all_trades[ticker] = normalized
        log.info(f"  {ticker}: {len(normalized)} trades fetched")

    total = sum(len(v) for v in all_trades.values())
    log.info(f"Total: {total} trades across {len(all_trades)} tickers")
    return all_trades


# ── Replay Engine ────────────────────────────────────────────────────────


def replay_trades(
    trades_by_ticker: dict[str, list[dict]],
    weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict:
    """
    Replay historical trades through a fresh feature engine.

    Args:
        trades_by_ticker: {ticker: [trades sorted by ts]}
        weights: Optional override for FEATURE_WEIGHTS
        thresholds: Optional override for alert thresholds

    Returns: stats dict with score distribution, alert counts, feature firing rates, etc.
    """
    if weights is None:
        weights = FEATURE_WEIGHTS.copy()
    if thresholds is None:
        from diamond_config import (
            ALERT_THRESHOLD_LOG,
            ALERT_THRESHOLD_NOTABLE,
            ALERT_THRESHOLD_ALERT,
            ALERT_THRESHOLD_CRITICAL,
        )
        thresholds = {
            "LOG": ALERT_THRESHOLD_LOG,
            "NOTABLE": ALERT_THRESHOLD_NOTABLE,
            "ALERT": ALERT_THRESHOLD_ALERT,
            "CRITICAL": ALERT_THRESHOLD_CRITICAL,
        }

    # Create fresh store and engine (in-memory DB)
    store = DiamondStore(db_path=":memory:")
    store.connect()
    engine = FeatureEngine(store)

    # Merge all trades into chronological order
    all_trades = []
    for ticker, trades in trades_by_ticker.items():
        all_trades.extend(trades)
    all_trades.sort(key=lambda x: x.get("ts", 0))

    if not all_trades:
        store.close()
        return {"error": "No trades to replay"}

    # Phase 1: Insert first 30% as baseline (build profiles without scoring)
    baseline_count = max(len(all_trades) // 3, 50)
    baseline_trades = all_trades[:baseline_count]
    scoring_trades = all_trades[baseline_count:]

    for trade in baseline_trades:
        store.insert_trade(trade)

    # Build profiles from baseline
    for ticker in trades_by_ticker:
        store.update_market_profile(ticker)

    # Phase 2: Replay remaining trades through engine
    all_scores = []
    level_counts = Counter()
    feature_scores = defaultdict(list)
    feature_names = list(weights.keys())
    ticker_alerts = defaultdict(list)  # ticker → [(ts, score, level)]
    price_history = defaultdict(list)  # ticker → [(ts, yes_price)]

    for trade in scoring_trades:
        ticker = trade["ticker"]
        store.insert_trade(trade)

        result = engine.compute(trade)

        # Use custom weights for composite
        custom_features = {f: result.get(f, 0) for f in feature_names}
        composite = _compute_custom_composite(custom_features, weights)
        level = _classify_custom(composite, thresholds)

        all_scores.append(composite)
        level_counts[level] += 1
        for feat in feature_names:
            feature_scores[feat].append(result.get(feat, 0))

        if level != "NONE":
            store.insert_anomaly(ticker, composite, result, level)
            ticker_alerts[ticker].append({
                "ts": trade.get("ts", 0),
                "score": composite,
                "level": level,
            })

        # Track price for precision evaluation
        if trade.get("yes_price") is not None:
            price_history[ticker].append({
                "ts": trade.get("ts", 0),
                "price": float(trade["yes_price"]),
            })

    # Periodically update profiles during replay
    profile_interval = max(len(scoring_trades) // 5, 100)
    for i in range(0, len(scoring_trades), profile_interval):
        for ticker in trades_by_ticker:
            store.update_market_profile(ticker)

    store.close()

    # Compile results
    result = {
        "total_trades": len(scoring_trades),
        "baseline_trades": baseline_count,
        "scores": all_scores,
        "level_counts": dict(level_counts),
        "feature_scores": {k: list(v) for k, v in feature_scores.items()},
        "ticker_alerts": dict(ticker_alerts),
        "price_history": {k: list(v) for k, v in price_history.items()},
        "weights": weights,
        "thresholds": thresholds,
    }
    return result


def _compute_custom_composite(features: dict[str, float], weights: dict[str, float]) -> float:
    """Compute composite with custom weights (same capped redistribution logic)."""
    active_weight = 0.0
    active_count = 0
    raw_score = 0.0
    for name, weight in weights.items():
        val = features.get(name, 0.0)
        raw_score += weight * val
        if val > 0:
            active_weight += weight
            active_count += 1

    if active_weight <= 0:
        return 0.0

    total_weight = sum(weights.values())
    redistribution = min(total_weight / active_weight, 1.5)
    if active_count < 3:
        redistribution = min(redistribution, 1.3)

    return max(0.0, min(1.0, raw_score * redistribution))


def _classify_custom(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds.get("CRITICAL", 0.70):
        return "CRITICAL"
    elif score >= thresholds.get("ALERT", 0.50):
        return "ALERT"
    elif score >= thresholds.get("NOTABLE", 0.35):
        return "NOTABLE"
    elif score >= thresholds.get("LOG", 0.20):
        return "LOG"
    return "NONE"


# ── Grid Search ──────────────────────────────────────────────────────────


def grid_search(
    trades_by_ticker: dict[str, list[dict]],
    param_grid: dict | None = None,
) -> list[dict]:
    """
    Sweep over threshold and weight combinations.
    Returns sorted list of configs by a quality metric.
    """
    if param_grid is None:
        param_grid = default_param_grid()

    results = []
    configs = list(_expand_grid(param_grid))
    log.info(f"Grid search: {len(configs)} configurations to test")

    for i, config in enumerate(configs):
        weights = config.get("weights", FEATURE_WEIGHTS.copy())
        thresholds = config["thresholds"]

        replay_result = replay_trades(trades_by_ticker, weights, thresholds)
        if "error" in replay_result:
            continue

        scores = replay_result["scores"]
        levels = replay_result["level_counts"]
        total = replay_result["total_trades"]

        if total == 0:
            continue

        # Quality metrics
        none_pct = levels.get("NONE", 0) / total
        log_pct = levels.get("LOG", 0) / total
        notable_pct = levels.get("NOTABLE", 0) / total
        alert_pct = levels.get("ALERT", 0) / total
        critical_pct = levels.get("CRITICAL", 0) / total

        # Target distribution score: penalize deviation from ideal
        # Ideal: NONE ~85%, LOG ~10%, NOTABLE ~3%, ALERT ~1.5%, CRITICAL ~0.5%
        dist_penalty = (
            abs(none_pct - 0.85) * 2 +
            abs(log_pct - 0.10) * 1.5 +
            abs(notable_pct - 0.03) * 3 +
            abs(alert_pct - 0.015) * 5 +
            abs(critical_pct - 0.005) * 10
        )

        # Feature diversity: reward configs where multiple features contribute
        feature_firing_rates = {}
        for feat, feat_scores in replay_result["feature_scores"].items():
            firing = sum(1 for s in feat_scores if s > 0) / max(len(feat_scores), 1)
            feature_firing_rates[feat] = firing

        # Penalize if any feature fires > 60% or < 2% (except cross_market and price_impact)
        diversity_penalty = 0
        for feat, rate in feature_firing_rates.items():
            if feat in ("cross_market_correlation", "price_impact"):
                continue
            if rate > 0.60:
                diversity_penalty += (rate - 0.60) * 2
            if rate < 0.02:
                diversity_penalty += 0.1

        quality = 1.0 - dist_penalty - diversity_penalty

        results.append({
            "config": config,
            "quality": quality,
            "dist_penalty": dist_penalty,
            "diversity_penalty": diversity_penalty,
            "none_pct": none_pct,
            "log_pct": log_pct,
            "notable_pct": notable_pct,
            "alert_pct": alert_pct,
            "critical_pct": critical_pct,
            "feature_firing": feature_firing_rates,
            "mean_score": statistics.mean(scores) if scores else 0,
            "median_score": statistics.median(scores) if scores else 0,
        })

        if (i + 1) % 10 == 0:
            log.info(f"  Tested {i + 1}/{len(configs)} configs...")

    results.sort(key=lambda x: x["quality"], reverse=True)
    return results


def default_param_grid() -> dict:
    """Default parameter grid for threshold search."""
    return {
        "thresholds": [
            {"LOG": 0.15, "NOTABLE": 0.30, "ALERT": 0.45, "CRITICAL": 0.65},
            {"LOG": 0.20, "NOTABLE": 0.35, "ALERT": 0.50, "CRITICAL": 0.70},
            {"LOG": 0.20, "NOTABLE": 0.40, "ALERT": 0.55, "CRITICAL": 0.75},
            {"LOG": 0.25, "NOTABLE": 0.40, "ALERT": 0.55, "CRITICAL": 0.75},
            {"LOG": 0.25, "NOTABLE": 0.45, "ALERT": 0.60, "CRITICAL": 0.80},
        ],
        "weights": [
            # Current config
            {"trade_size_zscore": 0.30, "volume_spike_ratio": 0.20,
             "order_book_imbalance": 0.20, "taker_side_skew": 0.15,
             "price_impact": 0.10, "cross_market_correlation": 0.05},
            # More weight on size + spike
            {"trade_size_zscore": 0.35, "volume_spike_ratio": 0.25,
             "order_book_imbalance": 0.15, "taker_side_skew": 0.10,
             "price_impact": 0.10, "cross_market_correlation": 0.05},
            # Balanced
            {"trade_size_zscore": 0.25, "volume_spike_ratio": 0.20,
             "order_book_imbalance": 0.20, "taker_side_skew": 0.20,
             "price_impact": 0.10, "cross_market_correlation": 0.05},
            # Heavy on book + skew
            {"trade_size_zscore": 0.25, "volume_spike_ratio": 0.15,
             "order_book_imbalance": 0.25, "taker_side_skew": 0.20,
             "price_impact": 0.10, "cross_market_correlation": 0.05},
        ],
        # ── Conviction params — uncomment when 100+ settlements exist ──
        # Sweep over half_life (decay speed) and flip_threshold (conviction
        # advantage needed to justify exiting + re-entering).
        # "conviction": [
        #     {"half_life": 180, "flip_threshold": 0.3},   # Fast decay, easy flip
        #     {"half_life": 300, "flip_threshold": 0.3},   # 5 min, easy flip
        #     {"half_life": 300, "flip_threshold": 0.5},   # 5 min, harder flip
        #     {"half_life": 420, "flip_threshold": 0.4},   # 7 min (default)
        #     {"half_life": 600, "flip_threshold": 0.4},   # 10 min
        #     {"half_life": 600, "flip_threshold": 0.6},   # 10 min, hard flip
        #     {"half_life": 900, "flip_threshold": 0.5},   # 15 min, moderate
        # ],
    }


def _expand_grid(grid: dict) -> list[dict]:
    """Expand parameter grid into list of config dicts."""
    threshold_options = grid.get("thresholds", [{}])
    weight_options = grid.get("weights", [FEATURE_WEIGHTS])

    configs = []
    for thresh in threshold_options:
        for weights in weight_options:
            configs.append({"thresholds": thresh, "weights": weights})
    return configs


# ── Precision Evaluation ─────────────────────────────────────────────────


def evaluate_precision(replay_result: dict) -> dict:
    """
    Check if alerts preceded significant price moves.

    For each alert, look at price change in the next N trades.
    A "hit" is when price moves >= 3 cents after an alert.

    Returns precision stats per alert level.
    """
    ticker_alerts = replay_result.get("ticker_alerts", {})
    price_history = replay_result.get("price_history", {})

    level_stats = defaultdict(lambda: {"hits": 0, "misses": 0, "total": 0,
                                        "avg_move": [], "max_move": 0})

    for ticker, alerts in ticker_alerts.items():
        prices = price_history.get(ticker, [])
        if not prices:
            continue

        for alert in alerts:
            alert_ts = alert["ts"]
            level = alert["level"]

            # Find price at alert time
            price_at_alert = None
            for p in prices:
                if p["ts"] <= alert_ts:
                    price_at_alert = p["price"]
                else:
                    break

            if price_at_alert is None:
                continue

            # Find max price move in next 5 minutes worth of trades
            future_prices = [p["price"] for p in prices
                             if alert_ts < p["ts"] <= alert_ts + 300]

            if not future_prices:
                level_stats[level]["misses"] += 1
                level_stats[level]["total"] += 1
                continue

            max_move = max(abs(fp - price_at_alert) for fp in future_prices)
            level_stats[level]["avg_move"].append(max_move)
            level_stats[level]["max_move"] = max(level_stats[level]["max_move"], max_move)
            level_stats[level]["total"] += 1

            if max_move >= 3.0:  # 3+ cent move = significant
                level_stats[level]["hits"] += 1
            else:
                level_stats[level]["misses"] += 1

    # Compute averages
    result = {}
    for level in ["LOG", "NOTABLE", "ALERT", "CRITICAL"]:
        stats = level_stats[level]
        total = stats["total"]
        hits = stats["hits"]
        moves = stats["avg_move"]
        result[level] = {
            "total_alerts": total,
            "hits": hits,
            "precision": hits / total if total > 0 else 0,
            "avg_move_cents": statistics.mean(moves) if moves else 0,
            "max_move_cents": stats["max_move"],
        }
    return result


# ── Report Printing ──────────────────────────────────────────────────────


def print_replay_report(result: dict):
    """Print a comprehensive replay report."""
    print("\n" + "=" * 70)
    print(f"  DIAMOND BACKTEST REPORT")
    print(f"  {result['total_trades']} trades replayed "
          f"(+ {result['baseline_trades']} baseline)")
    print("=" * 70)

    scores = result["scores"]
    if not scores:
        print("No trades scored.")
        return

    levels = result["level_counts"]
    total = result["total_trades"]

    print(f"\n--- Score Distribution ---")
    print(f"  Min:    {min(scores):.3f}")
    print(f"  Max:    {max(scores):.3f}")
    print(f"  Mean:   {statistics.mean(scores):.3f}")
    print(f"  Median: {statistics.median(scores):.3f}")
    if len(scores) > 1:
        print(f"  Stdev:  {statistics.stdev(scores):.3f}")

    print(f"\n--- Alert Levels ---")
    for level in ["NONE", "LOG", "NOTABLE", "ALERT", "CRITICAL"]:
        cnt = levels.get(level, 0)
        pct = cnt * 100 / total if total > 0 else 0
        print(f"  {level:10s}: {cnt:5d} ({pct:.1f}%)")

    print(f"\n--- Feature Firing Rates ---")
    for feat, feat_scores in sorted(result["feature_scores"].items()):
        firing = sum(1 for s in feat_scores if s > 0)
        total_f = len(feat_scores)
        avg_f = statistics.mean([s for s in feat_scores if s > 0]) if firing > 0 else 0
        print(f"  {feat:30s}: fires {firing:4d}/{total_f:4d} "
              f"({firing * 100 / max(total_f, 1):.0f}%)  avg={avg_f:.3f}")

    # Alerts per ticker
    alerts_by_ticker = result.get("ticker_alerts", {})
    if alerts_by_ticker:
        print(f"\n--- Top Tickers by Alert Count ---")
        sorted_tickers = sorted(alerts_by_ticker.items(),
                                 key=lambda x: len(x[1]), reverse=True)
        for ticker, alerts in sorted_tickers[:10]:
            notable_plus = sum(1 for a in alerts if a["level"] in ("NOTABLE", "ALERT", "CRITICAL"))
            print(f"  {ticker[:50]:50s}: {len(alerts):3d} alerts ({notable_plus} notable+)")


def print_grid_report(results: list[dict]):
    """Print grid search results."""
    print("\n" + "=" * 70)
    print(f"  DIAMOND GRID SEARCH RESULTS — {len(results)} configs tested")
    print("=" * 70)

    if not results:
        print("No results.")
        return

    # Top 5 configs
    print(f"\n--- Top 5 Configurations ---")
    for i, r in enumerate(results[:5]):
        thresh = r["config"]["thresholds"]
        print(f"\n  #{i+1}  quality={r['quality']:.3f}  "
              f"(dist_penalty={r['dist_penalty']:.3f}, "
              f"diversity_penalty={r['diversity_penalty']:.3f})")
        print(f"    Thresholds: LOG={thresh['LOG']:.2f} NOTABLE={thresh['NOTABLE']:.2f} "
              f"ALERT={thresh['ALERT']:.2f} CRITICAL={thresh['CRITICAL']:.2f}")
        print(f"    Distribution: NONE={r['none_pct']:.1%} LOG={r['log_pct']:.1%} "
              f"NOTABLE={r['notable_pct']:.1%} ALERT={r['alert_pct']:.1%} "
              f"CRITICAL={r['critical_pct']:.1%}")
        w = r["config"]["weights"]
        print(f"    Weights: size={w['trade_size_zscore']:.2f} "
              f"spike={w['volume_spike_ratio']:.2f} "
              f"book={w['order_book_imbalance']:.2f} "
              f"skew={w['taker_side_skew']:.2f} "
              f"impact={w['price_impact']:.2f} "
              f"xmkt={w['cross_market_correlation']:.2f}")
        # Feature firing rates
        ff = r.get("feature_firing", {})
        if ff:
            rates = " | ".join(f"{k[:6]}={v:.0%}" for k, v in sorted(ff.items()))
            print(f"    Firing: {rates}")

    # Worst config for comparison
    if len(results) > 5:
        worst = results[-1]
        print(f"\n  Worst: quality={worst['quality']:.3f}  "
              f"NONE={worst['none_pct']:.1%} LOG={worst['log_pct']:.1%} "
              f"NOTABLE={worst['notable_pct']:.1%} ALERT={worst['alert_pct']:.1%}")

    # Best config details
    best = results[0]
    print(f"\n--- Recommended Config ---")
    print(f"  Copy to diamond_config.py:")
    t = best["config"]["thresholds"]
    print(f"    ALERT_THRESHOLD_LOG = {t['LOG']}")
    print(f"    ALERT_THRESHOLD_NOTABLE = {t['NOTABLE']}")
    print(f"    ALERT_THRESHOLD_ALERT = {t['ALERT']}")
    print(f"    ALERT_THRESHOLD_CRITICAL = {t['CRITICAL']}")
    w = best["config"]["weights"]
    print(f"    FEATURE_WEIGHTS = {{")
    for k, v in w.items():
        print(f"        \"{k}\": {v:.2f},")
    print(f"    }}")


def print_precision_report(precision: dict):
    """Print precision evaluation results."""
    print(f"\n--- Alert Precision (price moves >= 3¢ in 5 min) ---")
    for level in ["LOG", "NOTABLE", "ALERT", "CRITICAL"]:
        stats = precision.get(level, {})
        total = stats.get("total_alerts", 0)
        hits = stats.get("hits", 0)
        prec = stats.get("precision", 0)
        avg_move = stats.get("avg_move_cents", 0)
        max_move = stats.get("max_move_cents", 0)
        if total > 0:
            print(f"  {level:10s}: {hits:3d}/{total:3d} hits "
                  f"({prec:.0%} precision)  "
                  f"avg_move={avg_move:.1f}¢  max_move={max_move:.1f}¢")
        else:
            print(f"  {level:10s}: no alerts")


# ── CLI ──────────────────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(description="DIAMOND Backtester")
    parser.add_argument("mode", choices=["replay", "gridsearch", "evaluate"],
                        help="Backtest mode")
    parser.add_argument("--tickers", type=int, default=10,
                        help="Number of tickers to backtest (default: 10)")
    parser.add_argument("--trades", type=int, default=500,
                        help="Trades per ticker to fetch (default: 500)")
    args = parser.parse_args()

    rest = KalshiRESTClient()

    log.info(f"Mode: {args.mode} | Tickers: {args.tickers} | Trades/ticker: {args.trades}")

    # Fetch historical data
    trades = await fetch_historical_trades(rest, args.tickers, args.trades)
    await rest.close()

    if not trades:
        log.error("No trades fetched. Check API connection.")
        return

    if args.mode == "replay":
        result = replay_trades(trades)
        print_replay_report(result)

        # Also run precision evaluation
        precision = evaluate_precision(result)
        print_precision_report(precision)

    elif args.mode == "gridsearch":
        results = grid_search(trades)
        print_grid_report(results)

        # Also replay with current config for comparison
        print("\n--- Current Config Baseline ---")
        current = replay_trades(trades)
        print_replay_report(current)

    elif args.mode == "evaluate":
        result = replay_trades(trades)
        precision = evaluate_precision(result)
        print_replay_report(result)
        print_precision_report(precision)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
