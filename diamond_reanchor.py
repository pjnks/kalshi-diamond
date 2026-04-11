"""
diamond_reanchor.py
───────────────────
Empirical threshold re-anchoring based on post-penalty score CDF.

After the Sprint 11 convexity penalty shifted the score distribution left,
static thresholds (especially CRITICAL=0.78) no longer align with their
intended quantile targets. This script computes the actual percentiles
and recommends new thresholds.

Usage:
    PYTHONPATH=. python diamond_reanchor.py [--hours 72] [--apply]

    --hours N   Window of recent data to analyze (default: 72)
    --apply     Write recommended thresholds directly to diamond_config.py
                (without this flag, only prints recommendations)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from diamond_config import (
    ALERT_THRESHOLD_ALERT,
    ALERT_THRESHOLD_CRITICAL,
    ALERT_THRESHOLD_LOG,
    ALERT_THRESHOLD_NOTABLE,
    DB_PATH,
)

# ── Target distribution ──────────────────────────────────────────────────
# These define what fraction of ALL raw trades should reach each level.
# The anomaly table only stores score > 0, so we map through the empirical
# anomaly rate to get the right percentile within the anomaly distribution.
#
# Targets (fraction of all trades):
#   LOG:      ~5%    (most anomalies — recorded, not acted on)
#   NOTABLE:  ~1.5%  (macOS notification, no trade)
#   ALERT:    ~0.4%  (Pushover + trade placement)
#   CRITICAL: ~0.05% (emergency — aggressive spread crossing, 3 contracts)
#
# For CRITICAL, the absolute target is 1-3 trades/day at current volume
# (~35k anomalies/day), which is ~0.005-0.01% of anomalies.

TARGET_QUANTILES = {
    "LOG":      0.00,    # baseline — keep at fixed 0.25
    "NOTABLE":  70.0,    # top 30% of anomalies
    "ALERT":    98.5,    # top 1.5% of anomalies
    "CRITICAL": 99.7,    # top 0.3% of anomalies (~50-100/day → 1-3 trades at fill rate)
}


def compute_empirical_thresholds(db_path: Path, hours_back: int = 72) -> dict:
    """Compute recommended thresholds from empirical score CDF."""
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    cutoff = now - hours_back * 3600

    # Raw trade count for anomaly rate
    total_trades = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]

    # All anomaly scores > 0
    rows = conn.execute(
        "SELECT score FROM anomalies WHERE ts >= ? AND score > 0", (cutoff,)
    ).fetchall()
    conn.close()

    scores = np.array([r[0] for r in rows])

    if len(scores) < 500:
        print(f"WARNING: Only {len(scores)} anomalies in {hours_back}h. "
              f"Need ≥500 for stable quantile estimates.")
        if len(scores) < 100:
            print("ABORT: Insufficient data. Wait for more trades.")
            sys.exit(1)

    anomaly_rate = len(scores) / total_trades if total_trades > 0 else 0

    print(f"{'═' * 60}")
    print(f"  DIAMOND Empirical Threshold Re-anchoring")
    print(f"{'═' * 60}")
    print(f"  Window:        last {hours_back}h")
    print(f"  Raw trades:    {total_trades:,}")
    print(f"  Anomalies:     {len(scores):,} ({anomaly_rate:.2%} of trades)")
    print(f"  Score range:   [{scores.min():.3f}, {scores.max():.3f}]")
    print(f"  Score median:  {np.median(scores):.3f}")
    print(f"  Score mean:    {scores.mean():.3f}")
    print()

    # Current thresholds and their empirical percentile
    current = {
        "LOG":      ALERT_THRESHOLD_LOG,
        "NOTABLE":  ALERT_THRESHOLD_NOTABLE,
        "ALERT":    ALERT_THRESHOLD_ALERT,
        "CRITICAL": ALERT_THRESHOLD_CRITICAL,
    }

    print(f"  {'Level':<12} {'Current':>8} {'Pctl':>8} {'Target Pctl':>12} {'Recommended':>12} {'Δ':>8}")
    print(f"  {'─' * 62}")

    recommended = {}
    for level, target_pctl in TARGET_QUANTILES.items():
        cur = current[level]
        # Where does the current threshold sit in the empirical CDF?
        cur_pctl = (scores < cur).sum() / len(scores) * 100

        if level == "LOG":
            # LOG stays fixed — it's the recording threshold, not a trading gate
            rec = cur
        else:
            rec = float(np.percentile(scores, target_pctl))
            # Safety floor: never go below the level below
            if level == "NOTABLE" and rec < ALERT_THRESHOLD_LOG + 0.05:
                rec = ALERT_THRESHOLD_LOG + 0.05
            if level == "ALERT" and rec <= recommended.get("NOTABLE", 0.45) + 0.02:
                rec = recommended.get("NOTABLE", 0.45) + 0.02

        delta = rec - cur
        recommended[level] = rec
        print(f"  {level:<12} {cur:>8.3f} {cur_pctl:>7.1f}% {target_pctl:>11.1f}% {rec:>12.3f} {delta:>+8.3f}")

    print()

    # Impact analysis: how many trades/day at each level
    hours_in_window = hours_back
    for level in ["ALERT", "CRITICAL"]:
        n_above = (scores >= recommended[level]).sum()
        rate_per_hour = n_above / hours_in_window
        rate_per_day = rate_per_hour * 24
        print(f"  {level}: {n_above} signals in {hours_back}h "
              f"→ ~{rate_per_day:.0f}/day at recommended threshold")

    # Win rate by bucket (if paper_trades data available)
    try:
        conn = sqlite3.connect(str(db_path))
        for level, thresh in [("ALERT", recommended["ALERT"]),
                              ("CRITICAL", recommended["CRITICAL"])]:
            # Approximate: settled trades whose anomaly_score falls in range
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) "
                "FROM paper_trades WHERE status='settled' AND anomaly_score >= ?",
                (thresh,)
            ).fetchone()
            if row[0] and row[0] > 0:
                wr = row[1] / row[0] * 100
                print(f"  Historical WR at {level} threshold ({thresh:.3f}): "
                      f"{row[1]}/{row[0]} = {wr:.1f}%")
        conn.close()
    except Exception:
        pass

    print(f"\n{'═' * 60}")

    return recommended


def apply_thresholds(recommended: dict, config_path: Path):
    """Update diamond_config.py with recommended thresholds."""
    content = config_path.read_text()
    replacements = {
        "ALERT_THRESHOLD_NOTABLE": recommended["NOTABLE"],
        "ALERT_THRESHOLD_ALERT": recommended["ALERT"],
        "ALERT_THRESHOLD_CRITICAL": recommended["CRITICAL"],
    }
    for var, val in replacements.items():
        import re
        pattern = rf"^({var}\s*=\s*)[\d.]+(.*)$"
        replacement = rf"\g<1>{val:.3f}\2"
        content, n = re.subn(pattern, replacement, content, flags=re.MULTILINE)
        if n == 0:
            print(f"WARNING: Could not find {var} in {config_path}")
    config_path.write_text(content)
    print(f"\n  Updated {config_path} with new thresholds.")
    print("  IMPORTANT: Re-deploy to OCI with ./deploy.sh --restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Empirical threshold re-anchoring")
    parser.add_argument("--hours", type=int, default=72, help="Hours of data to analyze")
    parser.add_argument("--apply", action="store_true", help="Write to diamond_config.py")
    args = parser.parse_args()

    rec = compute_empirical_thresholds(DB_PATH, args.hours)

    if args.apply:
        config_path = Path(__file__).parent / "diamond_config.py"
        apply_thresholds(rec, config_path)
    else:
        print("\n  Dry run. Use --apply to write to diamond_config.py")
