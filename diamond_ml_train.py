#!/usr/bin/env python3
"""
diamond_ml_train.py
───────────────────
CLI for training, evaluating, and comparing the DIAMOND ML anomaly scorer.

Usage:
    python diamond_ml_train.py                # Train + null importance + evaluate
    python diamond_ml_train.py --compare      # Side-by-side ML vs hand-tuned
    python diamond_ml_train.py --backtest     # What would ML have traded differently?
    python diamond_ml_train.py --null-test    # Null importance test only
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from diamond_config import DB_PATH, ML_MODEL_PATH, ML_MIN_SAMPLES
from src.diamond_ml import DiamondMLScorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def cmd_train(scorer: DiamondMLScorer, min_opened_at: float | None = None):
    """Train model and show results."""
    print("\n" + "=" * 70)
    print("  DIAMOND ML SCORER — TRAINING")
    print("=" * 70)

    if min_opened_at is not None:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(min_opened_at, tz=timezone.utc)
        print(f"\n  Filtering to trades opened after {dt.strftime('%Y-%m-%d %H:%M UTC')}")

    metrics = scorer.train(min_samples=ML_MIN_SAMPLES, min_opened_at=min_opened_at)

    if "error" in metrics:
        print(f"\n  ERROR: {metrics['error']}")
        return

    print(f"\n  Model type:       {metrics['model_type']}")
    print(f"  Samples:          {metrics['n_samples']} "
          f"(win rate: {metrics['win_rate']:.1%})")
    print(f"  Features kept:    {metrics['n_features_kept']}/{len(metrics['features_kept']) + len(metrics['features_dropped'])}")
    print(f"  CV Folds:         {metrics['n_folds']}")

    print(f"\n  ── Brier Score (lower = better) ──")
    print(f"  Naive baseline:   {metrics['naive_brier']:.4f}")
    print(f"  Lasso:            {metrics['lasso_brier']:.4f}")
    print(f"  GBM:              {metrics['gbm_brier']:.4f}")
    print(f"  Winner:           {metrics['cv_brier']:.4f} ± {metrics['cv_brier_std']:.4f}")
    print(f"  Improvement:      {metrics['brier_improvement']:+.4f}")

    print(f"\n  ── AUC (higher = better) ──")
    print(f"  Winner:           {metrics['cv_auc']:.3f} ± {metrics['cv_auc_std']:.3f}")

    if metrics["features_dropped"]:
        print(f"\n  ── Dropped by Null Importance ──")
        for f in metrics["features_dropped"]:
            print(f"    ✗ {f}")

    print(f"\n  ── Feature Importance (kept) ──")
    for fname, imp in metrics["feature_importance"].items():
        bar = "█" * int(imp * 50 / max(metrics["feature_importance"].values()))
        print(f"    {fname:30s} {imp:.4f}  {bar}")

    # OOS edge analysis (held-out predictions only)
    print(f"\n  ── OUT-OF-SAMPLE Edge Analysis (held-out CV folds only) ──")
    print(f"  Held-out samples: {metrics.get('oos_total_held_out', 0)}")
    oos_n = metrics.get("oos_edge_trades", 0)
    oos_wr = metrics.get("oos_edge_win_rate", 0)
    oos_ci = metrics.get("oos_edge_win_rate_ci_95", [0, 1])
    oos_pnl = metrics.get("oos_edge_pnl_cents", 0)
    print(f"  Edge > 0 trades:  {oos_n}")
    print(f"  Win rate:         {oos_wr:.1%}  95% CI: [{oos_ci[0]:.1%}, {oos_ci[1]:.1%}]")
    print(f"  P&L:              {oos_pnl:.0f}¢ (${oos_pnl/100:.2f})")

    cal_status = "YES (Platt)" if metrics.get("calibrated") else "NO (raw probabilities — ordinal only)"
    print(f"\n  Calibrated:       {cal_status}")

    # Quality verdict
    print(f"\n  ── VERDICT ──")
    if metrics["brier_improvement"] > 0.01:
        print(f"  ✓ Model beats naive baseline by {metrics['brier_improvement']:.4f} Brier")
    elif metrics["brier_improvement"] > 0:
        print(f"  ~ Marginal improvement ({metrics['brier_improvement']:+.4f} Brier)")
    else:
        print(f"  ✗ Model WORSE than naive baseline — do NOT activate")

    if metrics["cv_auc"] > 0.55:
        print(f"  ✓ AUC {metrics['cv_auc']:.3f} > 0.55 — model has discriminative power")
    else:
        print(f"  ✗ AUC {metrics['cv_auc']:.3f} ≤ 0.55 — no better than random")

    if oos_ci[0] > 0.50:
        print(f"  ✓ OOS win rate CI excludes 50% — statistically significant edge")
    else:
        print(f"  ✗ OOS win rate CI [{oos_ci[0]:.1%}, {oos_ci[1]:.1%}] includes 50% "
              f"— CANNOT reject null hypothesis of no edge")

    print()


def cmd_compare(scorer: DiamondMLScorer):
    """Compare ML vs hand-tuned."""
    print("\n" + "=" * 70)
    print("  DIAMOND ML SCORER — COMPARISON: ML vs HAND-TUNED")
    print("=" * 70)

    if not scorer.load_model():
        print("\n  No trained model found. Run `python diamond_ml_train.py` first.")
        return

    results = scorer.compare_with_handtuned()

    if "error" in results:
        print(f"\n  ERROR: {results['error']}")
        return

    print(f"\n  Total trades:      {results['n_trades']}")
    print(f"  Actual win rate:   {results['actual_win_rate']:.1%}")

    print(f"\n  ── Hand-Tuned Composite ──")
    print(f"  Brier Score:       {results.get('handtuned_brier', 'N/A')}")
    print(f"  AUC:               {results.get('handtuned_auc', 'N/A')}")
    print(f"  P&L (all trades):  {results['handtuned_pnl_cents']:.0f}¢ "
          f"(${results['handtuned_pnl_cents']/100:.2f})")

    if "ml_brier_oos" in results:
        print(f"\n  ── ML Edge Scorer (OUT-OF-SAMPLE ONLY) ──")
        print(f"  Held-out samples:  {results['n_held_out']}")
        print(f"  Brier Score (OOS): {results['ml_brier_oos']:.4f}")
        print(f"  AUC (OOS):         {results.get('ml_auc_oos', 'N/A')}")

        print(f"\n  ── OOS Edge Threshold Sensitivity ──")
        print(f"  {'Threshold':>12s}  {'Trades':>6s}  {'Wins':>5s}  "
              f"{'Win Rate':>8s}  {'95% CI':>16s}  {'P&L':>10s}  {'Sig?':>5s}")
        for threshold in [0.0, 0.05, 0.10, 0.15]:
            key = f"oos_edge>{threshold:.2f}"
            if key in results:
                d = results[key]
                ci = d.get("win_rate_ci_95", [0, 1])
                sig = "YES" if d.get("ci_excludes_50", False) else "no"
                print(f"  {'>' + f'{threshold:.2f}':>12s}  {d['trades']:>6d}  "
                      f"{d['wins']:>5d}  {d['win_rate']:>7.1%}  "
                      f"[{ci[0]:.1%},{ci[1]:.1%}]  "
                      f"{d['pnl_cents']:>9.0f}¢  {sig:>5s}")

        # Verdict
        print(f"\n  ── VERDICT ──")
        brier_diff = results.get("handtuned_brier", 1) - results.get("ml_brier_oos", 1)
        oos_e0 = results.get("oos_edge>0.00", {})
        oos_pnl = oos_e0.get("pnl_cents", 0)
        oos_sig = oos_e0.get("ci_excludes_50", False)
        pnl_diff = oos_pnl - results["handtuned_pnl_cents"]

        if oos_sig and pnl_diff > 0:
            print(f"  ✓ ML wins: better P&L ({pnl_diff:+.0f}¢) AND statistically significant edge")
            print(f"    → Safe to enable ML_SCORER_ACTIVE=true")
        elif pnl_diff > 0 and not oos_sig:
            print(f"  ~ ML has better P&L ({pnl_diff:+.0f}¢) but worse calibration")
            print(f"    → Collect more A/B data before activating")
        else:
            print(f"  ✗ Hand-tuned still better on P&L ({pnl_diff:+.0f}¢)")
            print(f"    → Keep ML in logging mode")

    print()


def cmd_residual(scorer: DiamondMLScorer, min_opened_at: float | None = None):
    """Train residual (alpha-only) model — Phase 2 architecture."""
    print("\n" + "=" * 70)
    print("  DIAMOND ML SCORER — TARGET RESIDUALIZATION (Phase 2)")
    print("=" * 70)

    if min_opened_at is not None:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(min_opened_at, tz=timezone.utc)
        print(f"\n  Filtering to trades opened after {dt.strftime('%Y-%m-%d %H:%M UTC')}")

    metrics = scorer.train_residual(min_samples=100, min_opened_at=min_opened_at)

    if "error" in metrics:
        print(f"\n  ERROR: {metrics['error']}")
        return

    print(f"\n  Model type:       {metrics['model_type']}")
    print(f"  Samples:          {metrics['n_samples']}")
    print(f"  Features kept:    {metrics['n_features']}")

    print(f"\n  ── Ridge Regression (RMSE, lower = better) ──")
    print(f"  RMSE:             {metrics['rmse_mean']:.4f} ± {metrics['rmse_std']:.4f}")

    print(f"\n  ── Rank Correlation (Spearman ρ) ──")
    sig = "✓ SIG" if metrics["spearman_significant"] else "✗ not sig"
    print(f"  ρ = {metrics['spearman_rho']:+.4f}  (p={metrics['spearman_pval']:.4f}, {sig})")

    print(f"\n  ── OOS Residual Decile P&L ──")
    print(f"  {'Decile':>20s}  {'N':>5s}  {'AvgResid':>10s}  {'ΣP&L':>8s}  {'AvgP&L':>8s}")
    for d in metrics.get("decile_results", []):
        print(f"  {d['range']:>20s}  {d['n']:>5d}  "
              f"{d['avg_residual']:>+9.4f}  "
              f"{d['total_pnl']:>+7.0f}¢  {d['avg_pnl']:>+7.1f}¢")

    print(f"\n  ── VERDICT ──")
    if metrics["spearman_significant"] and metrics["top_bucket_pnl"] > 0:
        print(f"  ✓ Residual model has significant rank correlation AND "
              f"top bucket profits (+{metrics['top_bucket_pnl']:.0f}¢)")
        print(f"    → Alpha exists independent of base rate")
    elif metrics["spearman_significant"]:
        print(f"  ~ Significant rank correlation but top bucket P&L unclear")
        print(f"    → Needs more data or feature engineering")
    else:
        print(f"  ✗ No significant rank correlation (ρ={metrics['spearman_rho']:+.4f})")
        print(f"    → Anomaly scores do not predict edge after removing base rate")

    print()


def cmd_null_test(scorer: DiamondMLScorer):
    """Run null importance test only."""
    print("\n" + "=" * 70)
    print("  DIAMOND ML SCORER — NULL IMPORTANCE TEST")
    print("=" * 70)

    X, y, meta = scorer.extract_training_data()
    if len(y) < 50:
        print(f"\n  Not enough data ({len(y)} < 50)")
        return

    print(f"\n  Samples: {len(y)} (win rate: {y.mean():.1%})")
    print(f"  Running 100 permutations...\n")

    keep = scorer.null_importance_test(X, y)

    print(f"\n  ── SURVIVORS ({len(keep)}/{X.shape[1]}) ──")
    for f in keep:
        print(f"    ✓ {f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="DIAMOND ML Scorer Training CLI")
    parser.add_argument("--compare", action="store_true",
                        help="Compare ML vs hand-tuned scoring")
    parser.add_argument("--backtest", action="store_true",
                        help="Replay trades with ML edge filter")
    parser.add_argument("--null-test", action="store_true",
                        help="Run null importance test only")
    parser.add_argument("--residual", action="store_true",
                        help="Train target residualization model (Phase 2). "
                             "Predicts alpha residual instead of binary outcome.")
    parser.add_argument("--since", type=str, default=None,
                        help="Only train on trades after this date (YYYY-MM-DD). "
                             "Use to isolate post-penalty data regimes. "
                             "E.g., --since 2026-04-02 for post-Sprint 11 data.")
    args = parser.parse_args()

    # Parse --since to epoch timestamp
    min_opened_at = None
    if args.since:
        from datetime import datetime, timezone
        try:
            dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            min_opened_at = dt.timestamp()
        except ValueError:
            print(f"ERROR: Invalid date format '{args.since}'. Use YYYY-MM-DD.")
            sys.exit(1)

    scorer = DiamondMLScorer(db_path=DB_PATH, model_path=ML_MODEL_PATH)

    if args.null_test:
        cmd_null_test(scorer)
    elif args.residual:
        cmd_residual(scorer, min_opened_at=min_opened_at)
    elif args.compare or args.backtest:
        cmd_compare(scorer)
    else:
        cmd_train(scorer, min_opened_at=min_opened_at)
        # Auto-run comparison after training
        cmd_compare(scorer)


if __name__ == "__main__":
    main()
