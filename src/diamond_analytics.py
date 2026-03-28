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
        active = {k: float(v) for k, v in feats.items()
                  if k not in ("composite", "alert_level", "score_source", "ml_edge") and isinstance(v, (int, float)) and v > 0}

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


def compute_optimal_weights(store, current_weights: dict, min_trades: int = 500) -> dict | None:
    """Compute optimal feature weights from settlement outcomes.

    Uses feature reliability (win_rate × sqrt(trade_count)) as a proxy
    for feature quality. Blends 50/50 with current weights to prevent
    wild swings.

    min_trades=500: PhD review (March 2025) — N=197 over 5 days is
    insufficient for stable weight estimation across 10 continuous
    parameters. 500 trades (~2 weeks) reduces sampling error.

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


def compute_brier_decomposition(store) -> dict | None:
    """Decompose Brier score into Reliability, Resolution, Uncertainty (Murphy 1973).

    BS = Reliability - Resolution + Uncertainty

    - Reliability: measures calibration — are predicted probabilities accurate?
      Low = well-calibrated. Fixable via Platt scaling if high.
    - Resolution: measures discrimination — can the model separate winners from losers?
      High = good discrimination. Requires new features if low.
    - Uncertainty: base rate variance, not controllable.

    Uses the anomaly composite score as the predicted probability proxy,
    binned into 10 bins. Requires 50+ settled trades.
    """
    conn = store._conn
    rows = conn.execute(
        "SELECT anomaly_score, pnl_cents, entry_price "
        "FROM paper_trades WHERE status = 'settled' "
        "AND anomaly_score IS NOT NULL AND pnl_cents IS NOT NULL"
    ).fetchall()

    if len(rows) < 50:
        return {"error": f"Need 50+ settlements, have {len(rows)}"}

    # Use market-implied probability (entry_price/100) as baseline
    # and anomaly_score as the forecast probability
    outcomes = []
    forecasts = []
    for row in rows:
        won = 1.0 if (row["pnl_cents"] or 0) > 0 else 0.0
        # Use anomaly score as predicted P(win) proxy
        forecast = float(row["anomaly_score"] or 0.5)
        outcomes.append(won)
        forecasts.append(forecast)

    n = len(outcomes)
    base_rate = sum(outcomes) / n

    # Uncertainty = base_rate * (1 - base_rate)
    uncertainty = base_rate * (1.0 - base_rate)

    # Bin forecasts into 10 bins for reliability/resolution computation
    n_bins = 10
    bins = [[] for _ in range(n_bins)]
    for i in range(n):
        bin_idx = min(int(forecasts[i] * n_bins), n_bins - 1)
        bins[bin_idx].append(outcomes[i])

    reliability = 0.0
    resolution = 0.0
    for k in range(n_bins):
        nk = len(bins[k])
        if nk == 0:
            continue
        ok = sum(bins[k]) / nk  # observed frequency in bin k
        fk = (k + 0.5) / n_bins  # bin center forecast
        reliability += nk * (fk - ok) ** 2
        resolution += nk * (ok - base_rate) ** 2

    reliability /= n
    resolution /= n

    brier_score = reliability - resolution + uncertainty

    result = {
        "n_trades": n,
        "base_rate": base_rate,
        "brier_score": brier_score,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "diagnosis": "",
    }

    # Diagnostic interpretation
    if reliability > 0.05:
        result["diagnosis"] = "MISCALIBRATED: reliability component is high. " \
            "Predicted probabilities do not match observed frequencies. " \
            "Fix via Platt scaling or isotonic regression."
    elif resolution < uncertainty * 0.3:
        result["diagnosis"] = "LOW DISCRIMINATION: resolution is weak relative to uncertainty. " \
            "The model cannot separate winners from losers. Requires new features or " \
            "more data — recalibration alone will not help."
    else:
        result["diagnosis"] = "Calibration and discrimination are adequate at current sample size."

    return result


def print_brier_report(store):
    """Print Brier decomposition report to stdout."""
    result = compute_brier_decomposition(store)
    if result is None or "error" in result:
        print(f"Brier decomposition: {result.get('error', 'no data') if result else 'no data'}")
        return

    print(f"\n{'='*60}")
    print(f"BRIER SCORE DECOMPOSITION ({result['n_trades']} settled trades)")
    print(f"{'='*60}")
    print(f"  Brier Score:   {result['brier_score']:.4f}")
    print(f"  Reliability:   {result['reliability']:.4f}  (lower = better calibration)")
    print(f"  Resolution:    {result['resolution']:.4f}  (higher = better discrimination)")
    print(f"  Uncertainty:   {result['uncertainty']:.4f}  (base rate variance, not controllable)")
    print(f"  Base win rate: {result['base_rate']:.1%}")
    print(f"\n  Diagnosis: {result['diagnosis']}")


def compute_slippage_report(store) -> dict | None:
    """Measure execution slippage: fill_price - entry_price (signal price).

    Positive slippage = paid more than signal price (crossing cost).
    This tells us empirically whether execution costs are eating the edge.

    Breaks down by alert level and outcome to diagnose if slippage
    is correlated with adverse selection (higher slippage on losers = bad).
    """
    conn = store._conn
    rows = conn.execute(
        "SELECT entry_price, fill_price, anomaly_level, pnl_cents, side "
        "FROM paper_trades "
        "WHERE status IN ('filled', 'settled') "
        "AND entry_price IS NOT NULL AND fill_price IS NOT NULL AND fill_price > 0"
    ).fetchall()

    if len(rows) < 10:
        return {"error": f"Need 10+ filled trades, have {len(rows)}"}

    total_slippage = 0
    by_level = {}
    by_outcome = {"win": [], "loss": [], "open": []}

    for row in rows:
        slip = (row["fill_price"] or 0) - (row["entry_price"] or 0)
        total_slippage += slip

        level = row["anomaly_level"] or "UNKNOWN"
        if level not in by_level:
            by_level[level] = {"count": 0, "total_slip": 0}
        by_level[level]["count"] += 1
        by_level[level]["total_slip"] += slip

        pnl = row["pnl_cents"]
        if pnl is None:
            by_outcome["open"].append(slip)
        elif pnl > 0:
            by_outcome["win"].append(slip)
        else:
            by_outcome["loss"].append(slip)

    n = len(rows)
    avg_slip = total_slippage / n

    result = {
        "n_trades": n,
        "total_slippage_cents": total_slippage,
        "avg_slippage_cents": avg_slip,
        "by_level": {k: {"count": v["count"], "avg_slip": v["total_slip"] / v["count"]}
                     for k, v in by_level.items()},
        "avg_slip_winners": sum(by_outcome["win"]) / len(by_outcome["win"]) if by_outcome["win"] else 0,
        "avg_slip_losers": sum(by_outcome["loss"]) / len(by_outcome["loss"]) if by_outcome["loss"] else 0,
        "n_winners": len(by_outcome["win"]),
        "n_losers": len(by_outcome["loss"]),
        "adverse_selection_flag": False,
        "diagnosis": "",
    }

    # Adverse selection check: if losers have systematically higher slippage,
    # we're getting filled only when the market moves against us
    if result["n_losers"] >= 5 and result["n_winners"] >= 5:
        if result["avg_slip_losers"] > result["avg_slip_winners"] + 1.0:
            result["adverse_selection_flag"] = True
            result["diagnosis"] = (
                f"ADVERSE SELECTION DETECTED: avg slippage on losers "
                f"({result['avg_slip_losers']:.1f}c) > winners "
                f"({result['avg_slip_winners']:.1f}c) by > 1c. "
                f"Resting orders are being picked off by faster participants."
            )
        else:
            result["diagnosis"] = "No significant adverse selection detected in fill slippage."

    return result


def print_slippage_report(store):
    """Print execution slippage report to stdout."""
    result = compute_slippage_report(store)
    if result is None or "error" in result:
        print(f"Slippage report: {result.get('error', 'no data') if result else 'no data'}")
        return

    print(f"\n{'='*60}")
    print(f"EXECUTION SLIPPAGE REPORT ({result['n_trades']} filled trades)")
    print(f"{'='*60}")
    print(f"  Avg slippage:     {result['avg_slippage_cents']:+.1f}c per trade")
    print(f"  Total slippage:   {result['total_slippage_cents']:+d}c")
    print(f"  Slip on winners:  {result['avg_slip_winners']:+.1f}c  (n={result['n_winners']})")
    print(f"  Slip on losers:   {result['avg_slip_losers']:+.1f}c  (n={result['n_losers']})")
    if result["adverse_selection_flag"]:
        print(f"\n  *** {result['diagnosis']}")
    else:
        print(f"\n  {result['diagnosis']}")

    print(f"\n  By alert level:")
    for level, stats in sorted(result["by_level"].items()):
        print(f"    {level:<10s} n={stats['count']:>4d}  avg_slip={stats['avg_slip']:+.1f}c")


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


# ── Deflated Sharpe Ratio ────────────────────────────────────────────

def calculate_deflated_sharpe(
    sharpe_test: float,
    sharpe_trials: list[float],
    num_trials: int,
    train_length: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> tuple[float, float]:
    """Penalize reported Sharpe ratio for multiple testing / grid search.

    Based on Bailey & López de Prado (2014) — "The Deflated Sharpe Ratio."
    When you run N parameter configurations and report the best Sharpe, the
    expected maximum Sharpe of pure noise increases with N. The DSR is the
    probability that the reported Sharpe exceeds this noise ceiling.

    Args:
        sharpe_test: Sharpe of the chosen ("best") configuration.
        sharpe_trials: Sharpes from ALL N configurations (including best).
        num_trials: Number of configurations tested.
        train_length: Number of observations in the training window.
        skew: Skewness of the return distribution (0 = Gaussian).
        kurtosis: Kurtosis (3 = Gaussian; >3 = fat-tailed).

    Returns:
        (dsr, expected_max_sharpe): DSR is in [0, 1]. Values > 0.95 suggest
        the Sharpe is likely real. Values < 0.50 suggest pure noise.
    """
    import numpy as np
    from scipy import stats as sp_stats

    sharpe_arr = np.array(sharpe_trials)
    var_trials = np.var(sharpe_arr)
    mean_trials = np.mean(sharpe_arr)

    if var_trials <= 0 or num_trials <= 1:
        return 0.0, 0.0

    # Expected maximum Sharpe under null (Euler-Mascheroni approximation)
    euler_gamma = 0.57721566
    z1 = sp_stats.norm.ppf(1 - 1.0 / num_trials)
    z2 = sp_stats.norm.ppf(1 - 1.0 / (num_trials * math.e))
    expected_max_sharpe = mean_trials + math.sqrt(var_trials) * (
        (1 - euler_gamma) * z1 + euler_gamma * z2
    )

    # Variance of the Sharpe ratio estimator (Lo 2002, adjusted for non-normality)
    sharpe_var = (
        1 - skew * sharpe_test + ((kurtosis - 1) / 4) * sharpe_test ** 2
    ) / train_length

    if sharpe_var <= 0:
        return 0.0, expected_max_sharpe

    # Deflated Sharpe = P(true Sharpe > expected max of noise)
    dsr = float(sp_stats.norm.cdf(
        (sharpe_test - expected_max_sharpe) / math.sqrt(sharpe_var)
    ))

    return dsr, expected_max_sharpe


def print_dsr_report(sharpe_test: float, sharpe_trials: list[float],
                     num_trials: int, train_length: int):
    """Print Deflated Sharpe Ratio report."""
    dsr, e_max = calculate_deflated_sharpe(
        sharpe_test, sharpe_trials, num_trials, train_length
    )
    print(f"\n{'='*60}")
    print(f"DEFLATED SHARPE RATIO REPORT")
    print(f"{'='*60}")
    print(f"  Reported Sharpe:      {sharpe_test:.3f}")
    print(f"  Trials tested:        {num_trials}")
    print(f"  Expected max(noise):  {e_max:.3f}")
    print(f"  Deflated Sharpe:      {dsr:.4f}")
    if dsr > 0.95:
        print(f"  Verdict: LIKELY REAL (DSR > 0.95)")
    elif dsr > 0.50:
        print(f"  Verdict: INCONCLUSIVE (0.50 < DSR < 0.95) — more data needed")
    else:
        print(f"  Verdict: LIKELY NOISE (DSR < 0.50) — kill this configuration")


# ── Feature Co-Firing Audit (Multicollinearity) ─────────────────────

def compute_cofiring_audit(store, min_trades: int = 50) -> dict | None:
    """Detect multicollinear feature pairs from co-firing rates.

    For each pair of features, computes P(both fire | any anomaly).
    Pairs with co-firing rate > 0.70 are flagged as redundant — when both
    fire, the lower-weight feature's contribution should be masked to
    prevent score inflation from dependent signals.

    Peer review integration: addresses the multicollinearity critique of
    treating correlated features as independent binary channels.

    Returns dict with:
        cofiring_matrix: {(feat_a, feat_b): rate}
        redundant_pairs: [(feat_a, feat_b, rate)] where rate > 0.70
        recommended_masks: {feat_to_suppress: [feat_that_dominates]}
    """
    conn = store._conn
    rows = conn.execute(
        "SELECT features_json FROM paper_trades "
        "WHERE status = 'settled' AND features_json IS NOT NULL"
    ).fetchall()

    if len(rows) < min_trades:
        return {"error": f"Need {min_trades}+ settlements, have {len(rows)}"}

    # Count activations per feature and per pair
    feature_fires = {}  # feat -> count
    pair_cofires = {}   # (feat_a, feat_b) sorted -> count
    n_anomalies = 0

    for row in rows:
        try:
            feats = json.loads(row["features_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        active = sorted([k for k, v in feats.items()
                        if k not in ("composite", "alert_level", "score_source", "ml_edge") and isinstance(v, (int, float)) and v > 0])
        if not active:
            continue
        n_anomalies += 1

        for f in active:
            feature_fires[f] = feature_fires.get(f, 0) + 1

        # All pairs
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                pair = (active[i], active[j])
                pair_cofires[pair] = pair_cofires.get(pair, 0) + 1

    if n_anomalies < min_trades:
        return {"error": f"Only {n_anomalies} anomalies with features"}

    # Compute co-firing rates: P(both | any anomaly)
    cofiring_matrix = {}
    redundant_pairs = []
    for pair, count in pair_cofires.items():
        rate = count / n_anomalies
        cofiring_matrix[pair] = {
            "cofiring_rate": rate,
            "count": count,
            "feat_a_rate": feature_fires.get(pair[0], 0) / n_anomalies,
            "feat_b_rate": feature_fires.get(pair[1], 0) / n_anomalies,
        }
        if rate > 0.70:
            redundant_pairs.append((pair[0], pair[1], rate))

    # For redundant pairs, recommend masking the lower-weight feature
    # (import weights from config to determine which to suppress)
    recommended_masks = {}
    try:
        from diamond_config import FEATURE_WEIGHTS
        for fa, fb, rate in redundant_pairs:
            wa = FEATURE_WEIGHTS.get(fa, 0)
            wb = FEATURE_WEIGHTS.get(fb, 0)
            suppress = fb if wa >= wb else fa
            dominator = fa if wa >= wb else fb
            if suppress not in recommended_masks:
                recommended_masks[suppress] = []
            recommended_masks[suppress].append(dominator)
    except ImportError:
        pass

    return {
        "n_anomalies": n_anomalies,
        "cofiring_matrix": cofiring_matrix,
        "redundant_pairs": redundant_pairs,
        "recommended_masks": recommended_masks,
    }


def print_cofiring_report(store):
    """Print feature co-firing audit report."""
    result = compute_cofiring_audit(store)
    if result is None or "error" in result:
        print(f"Co-firing audit: {result.get('error', 'no data') if result else 'no data'}")
        return

    print(f"\n{'='*60}")
    print(f"FEATURE CO-FIRING AUDIT ({result['n_anomalies']} anomalies)")
    print(f"{'='*60}")

    # Sort by co-firing rate descending
    pairs = sorted(result["cofiring_matrix"].items(),
                   key=lambda x: x[1]["cofiring_rate"], reverse=True)

    print(f"\n{'Feature A':<25s} {'Feature B':<25s} {'Co-fire':>8s} {'Flag':>6s}")
    print("-" * 66)
    for (fa, fb), stats in pairs[:15]:
        rate = stats["cofiring_rate"]
        flag = " *** " if rate > 0.70 else ""
        print(f"  {fa:<23s} {fb:<23s} {rate:>7.0%}{flag}")

    if result["redundant_pairs"]:
        print(f"\n  REDUNDANT PAIRS (co-fire > 70%):")
        for fa, fb, rate in result["redundant_pairs"]:
            print(f"    {fa} + {fb}: {rate:.0%}")

    if result["recommended_masks"]:
        print(f"\n  RECOMMENDED MASKS (suppress lower-weight when dominant fires):")
        for suppress, dominators in result["recommended_masks"].items():
            print(f"    Suppress '{suppress}' when {dominators} fires")
