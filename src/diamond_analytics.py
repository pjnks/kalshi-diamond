"""
diamond_analytics.py
────────────────────
Self-learning analytics for DIAMOND — feature attribution, win rate analysis,
and adaptive weight computation from settlement data.

Activated once sufficient settled trades exist (50+).
"""

from __future__ import annotations

import json
import logging
import math
import time

log = logging.getLogger(__name__)


def compute_feature_attribution(store) -> dict:
    """Compute per-feature win rates and performance metrics from settled trades.

    Returns dict keyed by feature name with:
      {win_rate, avg_pnl, trade_count, avg_score_wins, avg_score_losses}
    """
    conn = store._conn
    settled = conn.execute(
        "SELECT features_json, pnl_cents, anomaly_score, anomaly_level, category, side "
        "FROM paper_trades WHERE status = 'settled' AND features_json IS NOT NULL"
    ).fetchall()

    if len(settled) < 10:
        return {"error": f"Need 10+ settlements, have {len(settled)}"}

    # Per-feature analysis
    feature_stats = {}
    # Per co-occurrence pattern analysis
    pattern_stats = {}

    for row in settled:
        try:
            feats = json.loads(row["features_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        pnl = row["pnl_cents"] or 0
        won = pnl > 0

        # Identify active features (score > 0) and primary driver
        active = {k: v for k, v in feats.items()
                  if k not in ("composite", "alert_level") and v > 0}

        if not active:
            continue

        primary = max(active, key=active.get)

        # Per-feature stats
        for feat_name, feat_score in active.items():
            if feat_name not in feature_stats:
                feature_stats[feat_name] = {
                    "total": 0, "wins": 0, "total_pnl": 0,
                    "score_sum_wins": 0, "score_sum_losses": 0,
                    "as_primary": 0, "primary_wins": 0,
                }
            fs = feature_stats[feat_name]
            fs["total"] += 1
            fs["total_pnl"] += pnl
            if won:
                fs["wins"] += 1
                fs["score_sum_wins"] += feat_score
            else:
                fs["score_sum_losses"] += feat_score
            if feat_name == primary:
                fs["as_primary"] += 1
                if won:
                    fs["primary_wins"] += 1

        # Co-occurrence pattern: sorted tuple of active feature names
        pattern_key = "+".join(sorted(active.keys()))
        if pattern_key not in pattern_stats:
            pattern_stats[pattern_key] = {"total": 0, "wins": 0, "total_pnl": 0}
        pattern_stats[pattern_key]["total"] += 1
        pattern_stats[pattern_key]["total_pnl"] += pnl
        if won:
            pattern_stats[pattern_key]["wins"] += 1

    # Compute derived metrics
    result = {
        "total_settled": len(settled),
        "features": {},
        "patterns": {},
    }

    for feat, fs in feature_stats.items():
        result["features"][feat] = {
            "trade_count": fs["total"],
            "win_rate": fs["wins"] / fs["total"] if fs["total"] > 0 else 0,
            "avg_pnl": fs["total_pnl"] / fs["total"] if fs["total"] > 0 else 0,
            "primary_count": fs["as_primary"],
            "primary_win_rate": fs["primary_wins"] / fs["as_primary"] if fs["as_primary"] > 0 else 0,
            "avg_score_wins": fs["score_sum_wins"] / fs["wins"] if fs["wins"] > 0 else 0,
            "avg_score_losses": fs["score_sum_losses"] / (fs["total"] - fs["wins"]) if fs["total"] > fs["wins"] else 0,
        }

    for pattern, ps in sorted(pattern_stats.items(), key=lambda x: x[1]["total"], reverse=True):
        if ps["total"] >= 3:  # Only show patterns with enough data
            result["patterns"][pattern] = {
                "trade_count": ps["total"],
                "win_rate": ps["wins"] / ps["total"],
                "avg_pnl": ps["total_pnl"] / ps["total"],
            }

    return result


def compute_optimal_weights(store, current_weights: dict, min_trades: int = 50) -> dict | None:
    """Compute optimal feature weights from settlement outcomes.

    Uses feature reliability (win_rate × sqrt(trade_count)) as a proxy
    for feature quality. Blends 50/50 with current weights to prevent
    wild swings.

    Returns None if insufficient data.
    """
    attribution = compute_feature_attribution(store)
    if "error" in attribution:
        return None

    if attribution["total_settled"] < min_trades:
        return None

    features = attribution["features"]
    if not features:
        return None

    # Compute reliability score per feature
    reliability = {}
    for feat, stats in features.items():
        if feat not in current_weights:
            continue
        # Reliability = win_rate * sqrt(trade_count) / normalizer
        # Penalize features with < 50% win rate (they're hurting us)
        edge = max(0, stats["win_rate"] - 0.40)  # Only credit above 40% baseline
        reliability[feat] = edge * math.sqrt(stats["trade_count"])

    if not reliability or sum(reliability.values()) <= 0:
        return None

    # Normalize to sum to 1.0
    total_rel = sum(reliability.values())
    computed = {k: v / total_rel for k, v in reliability.items()}

    # Blend 50/50 with current weights
    blended = {}
    for feat in current_weights:
        cur = current_weights[feat]
        comp = computed.get(feat, 0)
        blended[feat] = 0.5 * cur + 0.5 * comp

    # Normalize to sum to 1.0
    total = sum(blended.values())
    if total > 0:
        blended = {k: v / total for k, v in blended.items()}

    return blended


def print_attribution_report(store):
    """Print a formatted attribution report to stdout."""
    result = compute_feature_attribution(store)

    if "error" in result:
        print(f"Attribution: {result['error']}")
        return

    print(f"\n{'='*60}")
    print(f"FEATURE ATTRIBUTION REPORT ({result['total_settled']} settled trades)")
    print(f"{'='*60}")

    print(f"\n{'Feature':<30s} {'Trades':>6s} {'WinRate':>8s} {'AvgP&L':>8s} {'Primary':>8s} {'PriWR':>6s}")
    print("-" * 68)
    for feat in sorted(result["features"].keys(),
                       key=lambda f: result["features"][f]["win_rate"], reverse=True):
        fs = result["features"][feat]
        print(f"  {feat:<28s} {fs['trade_count']:>6d} {fs['win_rate']:>7.0%} "
              f"{fs['avg_pnl']:>+7.1f}¢ {fs['primary_count']:>7d} {fs['primary_win_rate']:>5.0%}")

    if result["patterns"]:
        print(f"\n{'Pattern':<45s} {'Trades':>6s} {'WinRate':>8s} {'AvgP&L':>8s}")
        print("-" * 68)
        for pattern, ps in sorted(result["patterns"].items(),
                                  key=lambda x: x[1]["trade_count"], reverse=True)[:10]:
            # Shorten feature names for display
            short = pattern.replace("trade_size_zscore", "zscore") \
                          .replace("volume_spike_ratio", "vol") \
                          .replace("order_book_imbalance", "book") \
                          .replace("taker_side_skew", "skew") \
                          .replace("cross_market_correlation", "xmkt") \
                          .replace("size_concentration", "conc") \
                          .replace("book_pressure_delta", "bkdelta")
            print(f"  {short:<43s} {ps['trade_count']:>6d} {ps['win_rate']:>7.0%} {ps['avg_pnl']:>+7.1f}¢")
