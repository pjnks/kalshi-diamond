#!/usr/bin/env python3
"""
DIAMOND Signal Audit — Spearman Rank IC & Alpha Decomposition

Computes the Information Coefficient (Spearman rank correlation) between
the anomaly detection score and binary market outcomes. Answers the
fundamental question: does the core HMM/feature model have statistical
edge, independent of execution thresholds and plumbing?

Two tiers:
  Tier 1 (immediate): Uses settled paper_trades (N=684+). We have outcomes.
  Tier 2 (future):    Expand to all anomalies table via Kalshi API settlement
                      lookup — vastly larger N but requires API calls.

Usage:
  PYTHONPATH=. python evaluate_model.py                    # Full audit
  PYTHONPATH=. python evaluate_model.py --since 2026-04-02 # Post-Sprint 11 only
  PYTHONPATH=. python evaluate_model.py --category "NBA"   # Filter by category

Sprint 13b — April 2026
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "diamond_trades.db"


def load_settled_trades(
    db_path: Path,
    min_opened_at: float | None = None,
    category: str | None = None,
) -> list[dict]:
    """Load all settled paper trades with outcomes."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT id, ticker, side, entry_price, fill_price, pnl_cents,
               anomaly_score, anomaly_level, features_json,
               opened_at, settled_at, category
        FROM paper_trades
        WHERE status = 'settled' AND pnl_cents IS NOT NULL
    """
    params: list = []

    if min_opened_at is not None:
        query += " AND opened_at >= ?"
        params.append(min_opened_at)

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY opened_at"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    trades = []
    for row in rows:
        price = row["fill_price"] or row["entry_price"] or 50
        pnl = float(row["pnl_cents"] or 0)
        # Binary outcome: did the trade win?
        win = 1.0 if pnl > 0 else 0.0

        # Parse features JSON for individual feature scores
        features = {}
        try:
            features = json.loads(row["features_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        trades.append({
            "id": row["id"],
            "ticker": row["ticker"],
            "side": row["side"],
            "entry_price": int(price),
            "implied_prob": float(price) / 100.0,
            "anomaly_score": float(row["anomaly_score"] or 0),
            "anomaly_level": row["anomaly_level"],
            "pnl_cents": pnl,
            "win": win,
            "category": row["category"] or "unknown",
            "opened_at": row["opened_at"],
            "features": features,
        })

    return trades


def spearman_ic(scores: np.ndarray, outcomes: np.ndarray) -> dict:
    """Compute Spearman rank IC with confidence interval."""
    if len(scores) < 10:
        return {"ic": np.nan, "p_value": np.nan, "n": len(scores),
                "ci_lower": np.nan, "ci_upper": np.nan, "significant": False}

    rho, p_val = stats.spearmanr(scores, outcomes)

    # Fisher z-transform for CI
    n = len(scores)
    z = np.arctanh(rho)
    se = 1.0 / np.sqrt(n - 3) if n > 3 else np.inf
    z_lo, z_hi = z - 1.96 * se, z + 1.96 * se
    ci_lo, ci_hi = np.tanh(z_lo), np.tanh(z_hi)

    return {
        "ic": float(rho),
        "p_value": float(p_val),
        "n": n,
        "ci_lower": float(ci_lo),
        "ci_upper": float(ci_hi),
        "significant": p_val < 0.05,
    }


def decile_analysis(
    scores: np.ndarray,
    outcomes: np.ndarray,
    pnl: np.ndarray,
    n_bins: int = 10,
) -> list[dict]:
    """Bin by score deciles and compute WR + P&L per bin."""
    n_bins = min(n_bins, max(3, len(scores) // 15))
    edges = np.percentile(scores, np.linspace(0, 100, n_bins + 1))
    edges = np.unique(edges)

    bins = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if i == len(edges) - 2:
            mask = (scores >= lo) & (scores <= hi)
        else:
            mask = (scores >= lo) & (scores < hi)

        n = mask.sum()
        if n == 0:
            continue
        wr = outcomes[mask].mean()
        total_pnl = pnl[mask].sum()
        avg_pnl = pnl[mask].mean()
        bins.append({
            "range": f"[{lo:.3f}, {hi:.3f})",
            "n": int(n),
            "win_rate": float(wr),
            "total_pnl": float(total_pnl),
            "avg_pnl": float(avg_pnl),
        })
    return bins


def price_bucket_analysis(trades: list[dict]) -> list[dict]:
    """Analyze by the 4 price buckets used in convexity penalty."""
    buckets = [
        ("<25¢", lambda p: p < 25),
        ("25-49¢", lambda p: 25 <= p < 50),
        ("50-74¢", lambda p: 50 <= p < 75),
        ("75¢+", lambda p: p >= 75),
    ]
    results = []
    for name, pred in buckets:
        subset = [t for t in trades if pred(t["entry_price"])]
        if not subset:
            results.append({"bucket": name, "n": 0})
            continue

        scores = np.array([t["anomaly_score"] for t in subset])
        wins = np.array([t["win"] for t in subset])
        pnl = np.array([t["pnl_cents"] for t in subset])
        ic = spearman_ic(scores, wins)

        results.append({
            "bucket": name,
            "n": len(subset),
            "win_rate": float(wins.mean()),
            "total_pnl": float(pnl.sum()),
            "avg_score": float(scores.mean()),
            "ic": ic["ic"],
            "ic_pval": ic["p_value"],
            "ic_sig": ic["significant"],
        })
    return results


def category_analysis(trades: list[dict]) -> list[dict]:
    """Compute IC per market category."""
    categories = sorted(set(t["category"] for t in trades))
    results = []
    for cat in categories:
        subset = [t for t in trades if t["category"] == cat]
        if len(subset) < 10:
            continue

        scores = np.array([t["anomaly_score"] for t in subset])
        wins = np.array([t["win"] for t in subset])
        pnl = np.array([t["pnl_cents"] for t in subset])
        ic = spearman_ic(scores, wins)

        results.append({
            "category": cat,
            "n": len(subset),
            "win_rate": float(wins.mean()),
            "total_pnl": float(pnl.sum()),
            "ic": ic["ic"],
            "ic_pval": ic["p_value"],
            "ic_sig": ic["significant"],
        })

    return sorted(results, key=lambda r: r["n"], reverse=True)


def feature_ic_analysis(trades: list[dict]) -> list[dict]:
    """Compute IC for each raw feature individually."""
    # Collect all feature names that appear
    all_features = set()
    for t in trades:
        all_features.update(t["features"].keys())

    # Exclude non-score fields
    skip = {"composite", "alert_level", "ml_edge"}
    score_features = sorted(all_features - skip)

    wins = np.array([t["win"] for t in trades])
    results = []

    for fname in score_features:
        vals = []
        valid_wins = []
        for t in trades:
            v = t["features"].get(fname)
            if v is not None and isinstance(v, (int, float)):
                vals.append(float(v))
                valid_wins.append(t["win"])

        if len(vals) < 20:
            continue

        ic = spearman_ic(np.array(vals), np.array(valid_wins))
        results.append({
            "feature": fname,
            "n": ic["n"],
            "ic": ic["ic"],
            "p_value": ic["p_value"],
            "significant": ic["significant"],
            "ci": f"[{ic['ci_lower']:.3f}, {ic['ci_upper']:.3f}]",
        })

    return sorted(results, key=lambda r: abs(r["ic"]), reverse=True)


def interaction_analysis(trades: list[dict]) -> dict:
    """Test score × implied_prob interaction IC vs raw score IC."""
    scores = np.array([t["anomaly_score"] for t in trades])
    prices = np.array([t["implied_prob"] for t in trades])
    wins = np.array([t["win"] for t in trades])
    pnl = np.array([t["pnl_cents"] for t in trades])

    interaction = scores * prices

    ic_raw = spearman_ic(scores, wins)
    ic_price = spearman_ic(prices, wins)
    ic_interaction = spearman_ic(interaction, wins)

    return {
        "raw_score_ic": ic_raw,
        "price_ic": ic_price,
        "interaction_ic": ic_interaction,
    }


def print_report(
    trades: list[dict],
    since_label: str | None = None,
    category_filter: str | None = None,
):
    """Print the full signal audit report."""
    n = len(trades)
    if n < 20:
        log.error(f"Only {n} settled trades — need at least 20 for IC calculation")
        return

    scores = np.array([t["anomaly_score"] for t in trades])
    wins = np.array([t["win"] for t in trades])
    pnl = np.array([t["pnl_cents"] for t in trades])
    prices = np.array([t["entry_price"] for t in trades])

    # ── Header ──
    print()
    print("=" * 70)
    print("  DIAMOND SIGNAL AUDIT — Spearman Rank IC")
    print("=" * 70)
    if since_label:
        print(f"\n  Filtering to trades opened after {since_label}")
    if category_filter:
        print(f"  Category filter: {category_filter}")
    print(f"\n  Settled trades: {n}")
    print(f"  Win rate:       {wins.mean():.1%}")
    print(f"  Total P&L:      {pnl.sum():.0f}¢ (${pnl.sum()/100:.2f})")
    print(f"  Score range:    [{scores.min():.3f}, {scores.max():.3f}]")
    print(f"  Price range:    [{prices.min()}¢, {prices.max()}¢]")

    # ── Global IC ──
    print(f"\n  ── Global Information Coefficient ──")
    global_ic = spearman_ic(scores, wins)
    sig = "✓ SIG" if global_ic["significant"] else "✗ not sig"
    print(f"  Anomaly Score IC:  {global_ic['ic']:+.4f}  "
          f"(p={global_ic['p_value']:.4f}, {sig})")
    print(f"  95% CI:            [{global_ic['ci_lower']:+.4f}, "
          f"{global_ic['ci_upper']:+.4f}]")

    price_ic = spearman_ic(prices, wins)
    sig = "✓ SIG" if price_ic["significant"] else "✗ not sig"
    print(f"  Entry Price IC:    {price_ic['ic']:+.4f}  "
          f"(p={price_ic['p_value']:.4f}, {sig})")

    # ── Interaction IC ──
    print(f"\n  ── Interaction Term IC ──")
    inter = interaction_analysis(trades)
    ic_i = inter["interaction_ic"]
    sig = "✓ SIG" if ic_i["significant"] else "✗ not sig"
    print(f"  Score×Price IC:    {ic_i['ic']:+.4f}  "
          f"(p={ic_i['p_value']:.4f}, {sig})")
    print(f"  95% CI:            [{ic_i['ci_lower']:+.4f}, "
          f"{ic_i['ci_upper']:+.4f}]")

    # Compare
    delta_ic = abs(ic_i["ic"]) - abs(global_ic["ic"])
    if delta_ic > 0:
        print(f"  → Interaction adds +{delta_ic:.4f} |IC| over raw score alone")
    else:
        print(f"  → Interaction does NOT improve over raw score ({delta_ic:+.4f})")

    # ── Score Decile Analysis ──
    print(f"\n  ── Score Decile Analysis ──")
    print(f"  {'Decile':>20s}  {'N':>5s}  {'WR':>7s}  {'ΣP&L':>8s}  {'AvgP&L':>8s}")
    deciles = decile_analysis(scores, wins, pnl)
    for d in deciles:
        print(f"  {d['range']:>20s}  {d['n']:>5d}  {d['win_rate']:>6.1%}  "
              f"{d['total_pnl']:>+7.0f}¢  {d['avg_pnl']:>+7.1f}¢")

    # Monotonicity check on deciles
    wrs = [d["win_rate"] for d in deciles]
    inversions = sum(1 for i in range(1, len(wrs)) if wrs[i] < wrs[i - 1])
    if inversions == 0:
        print(f"  ✓ Score-WR is monotonic (0 inversions)")
    else:
        print(f"  ✗ {inversions} inversions — score is NOT monotonically "
              f"predictive of outcome")

    # ── Price Bucket Analysis ──
    print(f"\n  ── Price Bucket Analysis (Convexity Penalty Buckets) ──")
    print(f"  {'Bucket':>10s}  {'N':>5s}  {'WR':>7s}  {'ΣP&L':>8s}  "
          f"{'AvgScore':>9s}  {'IC':>7s}  {'Sig?':>5s}")
    for b in price_bucket_analysis(trades):
        if b["n"] == 0:
            print(f"  {b['bucket']:>10s}  {0:>5d}  {'---':>7s}")
            continue
        sig_str = "✓" if b["ic_sig"] else "✗"
        print(f"  {b['bucket']:>10s}  {b['n']:>5d}  {b['win_rate']:>6.1%}  "
              f"{b['total_pnl']:>+7.0f}¢  {b['avg_score']:>9.3f}  "
              f"{b['ic']:>+6.3f}  {sig_str:>5s}")

    # ── Category IC ──
    print(f"\n  ── Category IC (N≥10 only) ──")
    print(f"  {'Category':>20s}  {'N':>5s}  {'WR':>7s}  {'ΣP&L':>8s}  "
          f"{'IC':>7s}  {'Sig?':>5s}")
    for c in category_analysis(trades):
        sig_str = "✓" if c["ic_sig"] else "✗"
        print(f"  {c['category']:>20s}  {c['n']:>5d}  {c['win_rate']:>6.1%}  "
              f"{c['total_pnl']:>+7.0f}¢  {c['ic']:>+6.3f}  {sig_str:>5s}")

    # ── Per-Feature IC ──
    print(f"\n  ── Per-Feature IC (individual feature predictive power) ──")
    print(f"  {'Feature':>30s}  {'N':>5s}  {'IC':>7s}  {'p-val':>8s}  "
          f"{'Sig?':>5s}  {'95% CI':>20s}")
    for f in feature_ic_analysis(trades):
        sig_str = "✓" if f["significant"] else "✗"
        print(f"  {f['feature']:>30s}  {f['n']:>5d}  {f['ic']:>+6.3f}  "
              f"{f['p_value']:>8.4f}  {sig_str:>5s}  {f['ci']:>20s}")

    # ── Verdict ──
    print(f"\n  ── VERDICT ──")
    if global_ic["significant"] and abs(global_ic["ic"]) > 0.05:
        print(f"  ✓ Anomaly score has statistically significant IC "
              f"({global_ic['ic']:+.4f}, p={global_ic['p_value']:.4f})")
        print(f"    → Core model has predictive signal")
    elif global_ic["significant"]:
        print(f"  ~ Anomaly score IC is significant but weak "
              f"({global_ic['ic']:+.4f})")
        print(f"    → Signal exists but may not overcome transaction costs")
    else:
        print(f"  ✗ Anomaly score IC is NOT significant "
              f"({global_ic['ic']:+.4f}, p={global_ic['p_value']:.4f})")
        print(f"    → Core model may lack predictive power — "
              f"execution won't fix this")

    if inversions > 2:
        print(f"  ✗ Score-WR has {inversions} inversions — non-monotonic")
        print(f"    → Feature space is mis-specified")
    elif inversions > 0:
        print(f"  ~ Score-WR has {inversions} inversion(s) — "
              f"partial monotonicity")

    if price_ic["significant"] and abs(price_ic["ic"]) > abs(global_ic["ic"]):
        print(f"  ⚠ Entry price IC ({price_ic['ic']:+.4f}) > anomaly score IC "
              f"({global_ic['ic']:+.4f})")
        print(f"    → Base-rate anchoring: price predicts outcomes better "
              f"than your anomaly detector")

    print()
    print("=" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="DIAMOND Signal Audit — Spearman Rank IC"
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only analyze trades after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--category", type=str, default=None,
        help="Filter by market category (e.g., NBA, MLB, Crypto)",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to diamond_trades.db (default: auto-detect)",
    )
    args = parser.parse_args()

    db = Path(args.db) if args.db else DB_PATH

    min_opened_at = None
    since_label = None
    if args.since:
        dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        min_opened_at = dt.timestamp()
        since_label = f"{args.since} 00:00 UTC"

    log.info(f"Loading settled trades from {db}...")
    trades = load_settled_trades(db, min_opened_at, args.category)
    log.info(f"Loaded {len(trades)} settled trades")

    print_report(trades, since_label, args.category)


if __name__ == "__main__":
    main()
