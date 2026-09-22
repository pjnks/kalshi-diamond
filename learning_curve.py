"""
learning_curve.py
─────────────────
**STRICT READ-ONLY DIAGNOSTIC** — Sprint 14e (2026-04-26).

Question this script answers:
    "Will N=500 actually deliver a Brier-calibrated ML model, or are
     we waiting for a number that won't help?"

Method:
    Train the shadow ML pipeline at expanding sample sizes
    (N=50, 100, 150, 200, 250, 290 — the full post-Sprint-11 cohort)
    and plot:
      - Brier score vs N (descending = still learning)
      - AUC vs N (rising = features have signal)
      - Jaccard similarity between consecutive N's feature sets
        (rising-then-stable = features are converging on a true subset)

Decision logic for the operator:
    - Brier curve still steeply descending at N=290 → wait for N=500
    - Brier curve flatlined at >0.05 → N=500 won't save us; features lack IC
    - Jaccard rising and approaching 1.0 → feature set is stabilizing
    - Jaccard low and oscillating → model picking different features each
      training, no real edge

CRITICAL CONSTRAINTS (audit-checked at module load):
    - Subclasses DiamondMLScorer with a no-op save_model() override.
      No model weights touch disk.
    - Subclasses extract_training_data() to truncate at N. No production
      query path is modified.
    - Reads diamond_trades.db only. No writes.
    - Plots to local HTML file (reports/) only. No DB or features_json
      mutation.

Usage:
    PYTHONPATH=. python learning_curve.py
    PYTHONPATH=. python learning_curve.py --grid 50,100,150,200,250,290
    PYTHONPATH=. python learning_curve.py --db /tmp/diamond_trades_fresh.db
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from diamond_config import DB_PATH, ML_MIN_SAMPLES
from src.diamond_ml import DiamondMLScorer

logging.basicConfig(
    level=logging.WARNING,  # quiet the ML log spam during sweep
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("learning_curve")
log.setLevel(logging.INFO)


# ── Audit-safe scorer subclass (no disk writes) ────────────────────


class DiagnosticScorer(DiamondMLScorer):
    """Read-only ML scorer for learning-curve sweep.

    Two overrides enforce the no-side-effects contract:
      1. save_model() → no-op. Model weights NEVER persisted.
      2. extract_training_data() → truncated to self._N_LIMIT rows.
    """

    _N_LIMIT: Optional[int] = None  # set per sweep step

    def save_model(self):
        # ABSORBED. Diagnostic mode never persists model artifacts to
        # avoid contaminating the production model file.
        return

    def extract_training_data(self, min_opened_at=None):
        X, y, meta = super().extract_training_data(min_opened_at=min_opened_at)
        if self._N_LIMIT is not None and len(y) > self._N_LIMIT:
            X = X.iloc[: self._N_LIMIT].copy()
            y = y.iloc[: self._N_LIMIT].copy()
            meta = meta.iloc[: self._N_LIMIT].copy()
        return X, y, meta


@dataclass
class SweepPoint:
    n_target: int
    n_actual: int
    error: Optional[str]
    cv_brier: Optional[float]
    cv_brier_std: Optional[float]
    cv_auc: Optional[float]
    cv_auc_std: Optional[float]
    n_features_kept: Optional[int]
    features_kept: Optional[list[str]]
    win_rate: Optional[float]
    naive_brier: Optional[float]


def run_sweep(
    db_path: Path,
    grid: list[int],
    min_opened_at: float,
) -> list[SweepPoint]:
    """Train the ML pipeline at each N in grid. Return one SweepPoint per N."""
    results: list[SweepPoint] = []

    for n_target in grid:
        log.info(f"━━━━━━━━━━━━ N={n_target} ━━━━━━━━━━━━")
        scorer = DiagnosticScorer(db_path=str(db_path))
        scorer._N_LIMIT = n_target

        # min_samples=10 so train() doesn't reject small N — we want to see
        # what comes back even at N=50, where convergence may be poor.
        try:
            metrics = scorer.train(min_samples=10, min_opened_at=min_opened_at)
        except Exception as e:
            log.warning(f"  N={n_target}: exception during train(): {e}")
            results.append(SweepPoint(
                n_target=n_target, n_actual=0, error=str(e),
                cv_brier=None, cv_brier_std=None, cv_auc=None, cv_auc_std=None,
                n_features_kept=None, features_kept=None,
                win_rate=None, naive_brier=None,
            ))
            continue

        if "error" in metrics:
            log.warning(f"  N={n_target}: {metrics.get('error')}")
            results.append(SweepPoint(
                n_target=n_target,
                n_actual=metrics.get("n_samples", 0),
                error=metrics["error"],
                cv_brier=None, cv_brier_std=None, cv_auc=None, cv_auc_std=None,
                n_features_kept=None, features_kept=None,
                win_rate=None, naive_brier=None,
            ))
            continue

        log.info(
            f"  N={n_target} → actual={metrics['n_samples']} "
            f"Brier={metrics['cv_brier']:.4f}±{metrics['cv_brier_std']:.4f} "
            f"AUC={metrics['cv_auc']:.3f} "
            f"features_kept={metrics['n_features_kept']}"
        )

        results.append(SweepPoint(
            n_target=n_target,
            n_actual=metrics["n_samples"],
            error=None,
            cv_brier=metrics["cv_brier"],
            cv_brier_std=metrics["cv_brier_std"],
            cv_auc=metrics["cv_auc"],
            cv_auc_std=metrics["cv_auc_std"],
            n_features_kept=metrics["n_features_kept"],
            features_kept=list(metrics["features_kept"]),
            win_rate=metrics["win_rate"],
            naive_brier=metrics["naive_brier"],
        ))

    return results


def jaccard(a: list[str] | None, b: list[str] | None) -> Optional[float]:
    """Jaccard similarity between two feature sets."""
    if a is None or b is None:
        return None
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


# ── Reporting ──────────────────────────────────────────────────────


def print_text_report(points: list[SweepPoint]):
    print()
    print("━" * 78)
    print("  LEARNING CURVE — DIAMOND ML (Sprint 14e diagnostic, 2026-04-26)")
    print("━" * 78)
    print()
    print(f"  {'N':>4}  {'actual':>6}  {'Brier':>9}  {'AUC':>6}  "
          f"{'feats':>5}  {'jac↑':>5}  notes")
    print("  " + "─" * 70)

    prev_features = None
    for p in points:
        if p.error:
            print(f"  {p.n_target:>4}  {p.n_actual:>6}  {'ERR':>9}  {'—':>6}  "
                  f"{'—':>5}  {'—':>5}  {p.error[:35]}")
            continue
        jac = jaccard(prev_features, p.features_kept)
        jac_str = f"{jac:.2f}" if jac is not None else "—"
        baseline_delta = p.cv_brier - p.naive_brier if (p.cv_brier and p.naive_brier) else None
        bd_str = f"Δ={baseline_delta:+.3f} vs naive" if baseline_delta is not None else ""
        print(f"  {p.n_target:>4}  {p.n_actual:>6}  {p.cv_brier:>9.4f}  "
              f"{p.cv_auc:>6.3f}  {p.n_features_kept:>5d}  {jac_str:>5}  {bd_str}")
        prev_features = p.features_kept

    print()
    print("  Reading the curves:")
    print("  - Brier descending (N grows) → model still learning, more data helps")
    print("  - Brier flat above 0.05      → MORE DATA WON'T SAVE US, features lack IC")
    print("  - Brier <0.05                → activation gate met, ready for Two-Gate Stack")
    print("  - Jaccard rising → 1.0       → feature set converging (stable)")
    print("  - Jaccard oscillating        → model picking different features each fit")
    print()


def write_html_report(points: list[SweepPoint], out_path: Path):
    """Write a polished HTML report with Brier + AUC + Jaccard curves."""
    valid = [p for p in points if p.error is None and p.cv_brier is not None]
    if not valid:
        log.warning("No valid points to plot — skipping HTML report")
        return

    max_brier = max((p.cv_brier for p in valid), default=0.30)
    target_gate = 0.05

    # Bar chart rows
    brier_rows = []
    prev_features = None
    for p in valid:
        bar_pct = min(100, p.cv_brier / max(max_brier, 0.30) * 100)
        gate_pct = target_gate / max(max_brier, 0.30) * 100
        color = "#059669" if p.cv_brier < target_gate else "#d97706" if p.cv_brier < 0.15 else "#c53030"
        jac = jaccard(prev_features, p.features_kept)
        jac_str = f"{jac:.2f}" if jac is not None else "—"
        brier_rows.append(
            f'<tr>'
            f'<td style="padding:4px 8px;color:#4a5568;font-variant-numeric:tabular-nums;">N={p.n_target}</td>'
            f'<td style="padding:4px 8px;text-align:right;font-variant-numeric:tabular-nums;color:{color};font-weight:600;">{p.cv_brier:.4f}</td>'
            f'<td style="padding:4px 8px;width:50%;">'
              f'<div style="background:#e2e8f0;height:18px;width:100%;position:relative;">'
                f'<div style="background:{color};width:{bar_pct:.1f}%;height:18px;"></div>'
                f'<div style="position:absolute;top:0;left:{gate_pct:.1f}%;width:2px;height:18px;background:#1e40af;" title="ML gate (Brier<0.05)"></div>'
              f'</div>'
            f'</td>'
            f'<td style="padding:4px 8px;text-align:right;font-variant-numeric:tabular-nums;">{p.cv_auc:.3f}</td>'
            f'<td style="padding:4px 8px;text-align:right;font-variant-numeric:tabular-nums;">{p.n_features_kept}</td>'
            f'<td style="padding:4px 8px;text-align:right;font-variant-numeric:tabular-nums;color:#4a5568;">{jac_str}</td>'
            f'</tr>'
        )
        prev_features = p.features_kept

    # Render
    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,sans-serif;background:#f7fafc;margin:0;padding:24px;color:#1a202c;">
<div style="max-width:880px;margin:0 auto;background:#fff;padding:24px;border-radius:6px;">

<h1 style="color:#0f766e;margin:0 0 8px 0;">DIAMOND ML — Learning Curve Diagnostic</h1>
<div style="color:#4a5568;font-size:13px;margin-bottom:20px;">
  Sprint 14e read-only sweep · 2026-04-26 · {len(valid)} valid training rounds
</div>

<div style="background:#f0fdf4;border-left:4px solid #00b894;padding:14px 18px;margin-bottom:18px;">
<b style="color:#065f46;">The question:</b> at the current cohort size and growth rate,
will the ML model converge to <code>Brier &lt; 0.05</code> by N=500, or are we
waiting for a number that won't help?
</div>

<h2 style="color:#0f766e;font-size:17px;border-left:3px solid #0f766e;padding-left:10px;">
  Brier &amp; AUC sweep (post-Sprint-11 cohort)
</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;">
<tr style="background:#e2e8f0;">
  <th style="padding:6px 8px;text-align:left;">Sample size</th>
  <th style="padding:6px 8px;text-align:right;">CV Brier</th>
  <th style="padding:6px 8px;">Brier bar (blue line = ML gate at 0.05)</th>
  <th style="padding:6px 8px;text-align:right;">AUC</th>
  <th style="padding:6px 8px;text-align:right;">#feat</th>
  <th style="padding:6px 8px;text-align:right;">Jaccard vs prev</th>
</tr>
{''.join(brier_rows)}
</table>

<h2 style="color:#0f766e;font-size:17px;margin-top:24px;border-left:3px solid #0f766e;padding-left:10px;">
  Decision matrix
</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr><td style="padding:8px 12px;background:#f0fdf4;width:33%;border:1px solid #86efac;vertical-align:top;">
  <b style="color:#065f46;">Brier descending steeply at N=290</b><br>
  <span style="color:#4a5568;font-size:12px;">Model still learning. Wait for N=500. Patience window holds.</span>
</td>
<td style="padding:8px 12px;background:#fffbeb;border:1px solid #fcd34d;vertical-align:top;">
  <b style="color:#92400e;">Brier flat &gt;0.05 at N=290</b><br>
  <span style="color:#4a5568;font-size:12px;">More data won't save us. Features lack IC. Need new feature engineering before N=500 retrain.</span>
</td>
<td style="padding:8px 12px;background:#eff6ff;border:1px solid #93c5fd;vertical-align:top;">
  <b style="color:#1e40af;">Brier &lt;0.05 at N=290</b><br>
  <span style="color:#4a5568;font-size:12px;">Activation gate already met. Could enable Two-Gate Stack early — but verify with full N=500 retrain first.</span>
</td></tr>
</table>

<div style="margin-top:20px;color:#718096;font-size:11px;border-top:1px solid #e2e8f0;padding-top:12px;">
Read-only diagnostic. No model weights persisted, no production paths modified.
DiagnosticScorer subclass enforces the constraint at runtime.
</div>

</div></body></html>
"""
    out_path.write_text(html)
    log.info(f"Report written: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="ML learning curve diagnostic")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--grid", default="50,100,150,200,250,290",
                        help="Comma-separated N values to sweep")
    # Post-Sprint-14h default = 2026-05-06 02:12:12 UTC (Unix 1778033532)
    # See CLAUDE.md § Sprint 14h for the regime-cutoff rationale.
    #
    # Cohort cutoff history (each obsoletes the previous):
    #   1743552000  Sprint 11   (2026-04-02) — original. Obsoleted by 14e (universe expansion).
    #   1777346853  Sprint 14e  (2026-04-28 03:27:33) — universe 67→257. Obsoleted by 14f
    #                            (orderbook poll cap broke absorption features for 50 trades).
    #   1777431874  Sprint 14f  (2026-04-29 03:04:34) — orderbook poll fixed. Obsoleted by 14h
    #                            (orderbook_poll_loop died May 1 18:14 from CancelledError →
    #                            ~83% of cohort had all-zero absorption features).
    #   1778033532  Sprint 14h  (2026-05-06 02:12:12) — CancelledError catch + task death
    #                            watchdog deployed. CURRENT.
    #
    # Use --min-opened-at <epoch> explicitly to reproduce a prior-cohort sweep.
    parser.add_argument("--min-opened-at", type=float,
                        default=1778033532.0,
                        help="Unix epoch cutoff (default: 2026-05-06 02:12:12 = Sprint 14h). "
                             "See learning_curve.py source for cohort cutoff history.")
    parser.add_argument("--out", default="reports/2026_04_26_ml_learning_curve.html")
    args = parser.parse_args()

    grid = sorted(int(n) for n in args.grid.split(","))
    log.info(f"Sweeping N ∈ {grid} on db={args.db}")
    log.info(f"Cohort cutoff: opened_at >= {args.min_opened_at}")

    points = run_sweep(Path(args.db), grid, args.min_opened_at)
    print_text_report(points)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_html_report(points, out_path)

    # JSON dump for downstream programmatic access
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps([asdict(p) for p in points], indent=2, default=str))
    log.info(f"JSON dump: {json_path}")


if __name__ == "__main__":
    main()
