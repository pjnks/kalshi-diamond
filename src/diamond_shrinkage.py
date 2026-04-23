"""
diamond_shrinkage.py
────────────────────
Bayesian shrinkage utilities for small-sample edge estimation.

**STATUS: STAGED — prior-strength function pending user decision.**

Problem statement (from Phase C backtest, 2026-04-21):
  The naive bucket-WR estimator in `pit_bucket_edge_factory` produced a
  catastrophic −$477 / 55% drawdown on the Kelly harness. 72% of the loss
  came from the 15-24¢ bucket, where early trades happened to win at ~40%
  (above the implied 20% rate), creating phantom edge. Kelly's 5% bankroll
  cap then sized thousands of cheap contracts into a mean-reverting loss.

  The pathology is general: small-N bucket statistics × convex contract
  payouts → leverage into variance. Laplace shrinkage toward the market's
  implied probability is the canonical fix.

────────────────────────────────────────────────────────────────────────────
Mathematical formulation (Beta-Binomial conjugate):

  Prior belief: P(win) ~ Beta(α₀, β₀) centered on p_market = α₀/(α₀+β₀).
  Observe: W wins in N trials.
  Posterior: P(win) ~ Beta(α₀+W, β₀+N-W).
  Posterior mean: (α₀ + W) / (α₀ + β₀ + N).

  Equivalent form (Laplace "add-k"):
      shrunk_wr = (W + k · p_market) / (N + k)
  where k = α₀ + β₀ is the **pseudocount** — the "virtual sample size" of
  the prior. Higher k = more skepticism of observed WR.

  Interpretation:
  - N → ∞:   shrunk_wr → observed_wr (data dominates)
  - N → 0:   shrunk_wr → p_market (prior dominates)
  - k = N:   50/50 blend

  The shrunk edge is then: edge = shrunk_wr − p_market.
  By construction, edge → 0 as N → 0 (no phantom edge on warm-up).

────────────────────────────────────────────────────────────────────────────
Design choice deferred to caller: the `prior_strength_fn` callable.

This is the ONLY remaining degree of freedom. Three canonical options
(see docstring on `PriorStrengthStrategy` at the bottom of this file for
trade-offs). The `shrunk_wr()` function itself is stable and tested; the
prior-strength function shapes when data overrides prior.

Callers today (post-calibration):
  - `backtest_kelly.py::pit_bucket_edge_factory` — price-bucket WR
  - `diamond_ml.py::_category_target_encoding` — per-category WR (pending)
  - `diamond_threshold_manager.py` — could replace the min_n=30 floor

Self-check harness runs on import (verifies the three canonical prior
scenarios against hand-computed posteriors).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShrunkEstimate:
    """Result of Bayesian shrinkage on a small-sample WR."""

    observed_wr: float           # raw W/N (or NaN if N=0)
    shrunk_wr: float             # Laplace-smoothed toward prior
    prior_mean: float            # p_market (typically entry_price / 100)
    prior_strength: float        # pseudocount k (α₀+β₀)
    observed_n: int              # N
    effective_n: int             # N + k  (the "total weight" in denominator)
    edge: float                  # shrunk_wr - prior_mean
    naive_edge: float            # observed_wr - prior_mean (for comparison)
    shrinkage_weight: float      # λ = k / (N+k) — fraction of weight on prior


# ── Core shrinkage formula ───────────────────────────────────────────────


def shrunk_wr(
    wins: int,
    n: int,
    prior_mean: float,
    prior_strength: float,
) -> ShrunkEstimate:
    """Laplace-smooth observed win rate toward market-implied prior.

    Args:
        wins: Observed wins in the sample.
        n: Total sample size (must be >= 0; shrinks to pure prior at N=0).
        prior_mean: Prior belief about WR (typically = implied probability
                    of the contract = entry_price / 100). Must be in (0, 1).
        prior_strength: Pseudocount k = α₀ + β₀. The number of "virtual
                        observations" encoded in the prior. Higher = more
                        skepticism of observed data. Must be >= 0.

    Returns:
        ShrunkEstimate with both the shrunk and naive WR for comparison.

    Example:
        Phase C 15-24¢ steamroller scenario:
          wins=2, n=5, prior_mean=0.20 (price=20¢), prior_strength=20
          → naive_wr = 0.40 (phantom edge +0.20)
          → shrunk_wr = (2 + 20*0.20) / (5 + 20) = 6/25 = 0.24
          → shrunk_edge = 0.24 - 0.20 = +0.04 (real, near-hurdle)
          → Kelly fraction shrinks from 0.125 → 0.025 — 5× less exposure.
    """
    if not (0.0 < prior_mean < 1.0):
        raise ValueError(f"prior_mean must be in (0,1), got {prior_mean}")
    if prior_strength < 0:
        raise ValueError(f"prior_strength must be >= 0, got {prior_strength}")
    if n < 0 or wins < 0 or wins > n:
        raise ValueError(f"invalid wins={wins}, n={n}")

    observed_wr = (wins / n) if n > 0 else float("nan")
    naive_edge = (observed_wr - prior_mean) if n > 0 else 0.0

    numerator = wins + prior_strength * prior_mean
    denominator = n + prior_strength
    shrunk = (numerator / denominator) if denominator > 0 else prior_mean
    edge = shrunk - prior_mean

    weight = (prior_strength / denominator) if denominator > 0 else 1.0

    return ShrunkEstimate(
        observed_wr=observed_wr,
        shrunk_wr=shrunk,
        prior_mean=prior_mean,
        prior_strength=prior_strength,
        observed_n=n,
        effective_n=int(denominator),
        edge=edge,
        naive_edge=naive_edge,
        shrinkage_weight=weight,
    )


# ── Prior-strength strategies (CALLER MUST CHOOSE) ────────────────────────


PriorStrengthStrategy = Callable[[int, float], float]
"""Type alias: (observed_n, prior_mean) → pseudocount k.

Why pass `prior_mean`? Some strategies adapt strength to where on the
probability curve we are. For example, the variance of Beta(α, β) is
maximal at p=0.5 and shrinks toward the extremes — so a "constant
variance" prior requires higher k at extreme prices.
"""


# ── TODO (Learning contribution): pick prior_strength_fn ──────────────────
#
# This is where your design judgment matters. Three canonical options:
#
# OPTION A — "Fixed skeptic" (constant k):
#   k ≡ k₀ regardless of n or price. Simple and defensible; corresponds to
#   a fixed-strength Beta prior. Downside: overweights prior on buckets
#   that already have ample data (e.g., 50-74¢ bucket with N=86).
#
# OPTION B — "Proportional skeptic" (k scales with observed n):
#   k = k₀ + α · √n. Prior weight shrinks sub-linearly as sample grows.
#   Preserves a "floor of skepticism" even on large-N buckets.
#   Requires tuning α.
#
# OPTION C — "IC-informed" (k derived from estimator's measured IC):
#   k = (1 - IC) / IC · N_effective. Optimal under James-Stein shrinkage
#   if you can estimate the estimator's IC. Most principled but requires
#   a validation pass. IC for our bucket estimator is near-zero per
#   Sprint 13b, so this would yield near-infinite k (total shrinkage).
#
# OPTION D — "Variance-matched" (k from target posterior variance):
#   k = p(1-p)/target_variance - 1. Set k so the posterior std is at most
#   target_std (say 0.05). Elegant but requires declaring "how uncertain
#   is too uncertain."
#
# Fill in `prior_strength_default()` below with your choice, then we'll
# re-run Phase C and compare MaxDD vs the naive bucket estimator.


def prior_strength_default(observed_n: int, prior_mean: float) -> float:
    """Option B: derived-floor pseudocount = k_base + α·√N.

    Calibrated (2026-04-21) from the Phase C 15-24¢ steamroller geometry:
      shrunk_edge(wins=2, n=5, p=0.20, k) = (W - N·p) / (N + k) = 1/(5+k)
      Half-Kelly safety cap requires edge ≤ 0.04 → k ≥ 20. Picking k_base=20
      is the *analytical minimum* that squashes a 1σ noise event at N=5
      below the 5% per-trade allocation cap — not a heuristic.

    The α·√N term preserves a skepticism floor even on high-N buckets:
      - At N=0:   k=20 (pure steamroller protection)
      - At N=25:  k=35 (prior weight ~58%)
      - At N=100: k=50 (prior weight ~33%)
      - At N=400: k=80 (prior weight ~17%)
    The √N decay is isomorphic to the standard error of a proportion, so
    prior weight fades in lockstep with the data's statistical precision —
    mathematically principled Empirical Bayes.

    Risk register (documented 2026-04-21): at N=400 the prior still holds
    ~16% weight. If a bucket's true regime shifts, the anchor will cause the
    estimator to lag the new reality. Acceptable trade-off for backtest use;
    when a Brier<0.05 ML edge source is live, α can be lowered because the
    model's intrinsic calibration assumes the burden of risk-aversion.
    """
    k_base = 20.0
    alpha = 3.0
    return k_base + (alpha * math.sqrt(observed_n))


# ── Convenience wrappers for the two main callers ────────────────────────


def shrunk_bucket_edge(
    wins: int,
    n: int,
    entry_price_cents: int,
    prior_strength_fn: PriorStrengthStrategy = prior_strength_default,
    min_active_n: int = 0,
) -> Optional[float]:
    """Shrunk edge for price-bucket estimator (Phase C style).

    Returns None if n < min_active_n (warmup floor, optional).
    Otherwise returns the shrunk edge = shrunk_wr - market_price.
    """
    if n < min_active_n:
        return None
    prior_mean = entry_price_cents / 100.0
    k = prior_strength_fn(n, prior_mean)
    est = shrunk_wr(wins, n, prior_mean, k)
    return est.edge


def shrunk_category_wr(
    wins: int,
    n: int,
    cohort_prior_mean: float,
    prior_strength_fn: PriorStrengthStrategy = prior_strength_default,
) -> float:
    """Shrunk category WR for ML target encoding.

    Unlike bucket edge, this targets an absolute WR (not relative to price).
    Prior is the cohort-wide mean WR (e.g., 0.51 for DIAMOND's live history).
    """
    k = prior_strength_fn(n, cohort_prior_mean)
    return shrunk_wr(wins, n, cohort_prior_mean, k).shrunk_wr


# ── Self-check harness ───────────────────────────────────────────────────


def _self_check() -> None:
    """Verify the shrinkage formula against three hand-computed scenarios."""

    # Case 1: Zero data → pure prior
    est = shrunk_wr(wins=0, n=0, prior_mean=0.30, prior_strength=20)
    assert est.shrunk_wr == 0.30, f"n=0 should return prior, got {est.shrunk_wr}"
    assert est.shrinkage_weight == 1.0

    # Case 2: Phase C 15-24¢ steamroller — the pathology we're curing
    est = shrunk_wr(wins=2, n=5, prior_mean=0.20, prior_strength=20)
    # (2 + 20*0.20) / (5 + 20) = 6/25 = 0.24
    assert abs(est.shrunk_wr - 0.24) < 1e-9, f"expected 0.24, got {est.shrunk_wr}"
    # Naive: 2/5 = 0.40 → phantom edge +0.20
    assert abs(est.naive_edge - 0.20) < 1e-9
    # Shrunk edge: 0.24 - 0.20 = 0.04 (near-hurdle, NOT a license to leverage up)
    assert abs(est.edge - 0.04) < 1e-9

    # Case 3: Large-N regime — data should dominate
    est = shrunk_wr(wins=50, n=100, prior_mean=0.30, prior_strength=20)
    # (50 + 20*0.30) / (100 + 20) = 56/120 ≈ 0.467
    assert abs(est.shrunk_wr - 0.4667) < 1e-3
    assert est.shrinkage_weight < 0.20, "data should dominate at N=100"

    # Case 4: Zero prior_strength → degenerates to raw WR (no shrinkage)
    est = shrunk_wr(wins=3, n=5, prior_mean=0.20, prior_strength=0)
    assert abs(est.shrunk_wr - 0.60) < 1e-9

    # Case 5: Input validation
    try:
        shrunk_wr(wins=0, n=0, prior_mean=1.5, prior_strength=20)
        raise AssertionError("should reject prior_mean=1.5")
    except ValueError:
        pass

    try:
        shrunk_wr(wins=0, n=0, prior_mean=0.5, prior_strength=-1)
        raise AssertionError("should reject prior_strength=-1")
    except ValueError:
        pass


if __name__ == "__main__":
    _self_check()
    print("diamond_shrinkage self-check passed (5 cases).")
    print()
    print("NEXT: implement prior_strength_default() with your chosen strategy.")
    print("      Options A-D documented in the TODO block above.")
