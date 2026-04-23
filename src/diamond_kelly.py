"""
diamond_kelly.py
────────────────
Kelly-criterion position sizing for binary YES/NO prediction-market contracts.

**STATUS: DORMANT / SHADOW.**
This module is NOT wired into the live execution path. It will remain dormant
until the ML shadow model passes validation gates (**Brier < 0.05** AND
Jaccard ≥ 0.70 across CV folds — gate tightened 2026-04-21 after Phase B
backtest; see diamond_config.py::KELLY_BRIER_PROMOTION_THRESHOLD). At that
point, live sizing can migrate from tiered-contract (CRITICAL=3 / ALERT=2
/ ALERT=1) to edge-proportional Kelly sizing.

Activation path:
  1. N ≥ 500 post-Sprint-11 settled trades accumulated
  2. ML retrain produces validated model (Brier < 0.05 + Jaccard ≥ 0.70)
  3. Wire ML edge into ENTRY gate (not just sizing) — composite score has
     near-zero IC per Sprint 13b; Kelly-sizing composite-admitted trades
     reproduces Phase C's −$477 / 55% drawdown disaster.
  4. Backtest Half-Kelly vs flat sizing on settled cohort
     (see backtest_kelly.py Phase C — previous bucket-edge estimator
      failed this gate catastrophically; ML-edge estimator must pass)
  5. Flip `KELLY_SIZING_ENABLED = True` in diamond_config.py
  6. Wire `kelly_contracts()` into `diamond_paper._execute_trade()`

────────────────────────────────────────────────────────────────────────────
Core formula (verified from first principles):

  For YES contract at price p (0 < p < 1) with true win prob q, edge E = q - p,
  optimal Kelly fraction is:

      f* = E / (1 - p)

  Derivation:
    Win:  bankroll × (1 + f · (1-p)/p)
    Loss: bankroll × (1 - f)
    Maximize E[log growth] → (q - p) / (1 - p)

Hurdle rate calibration (as of 2026-04-20):
  Phase 1 audit measured +0.77¢ mean adverse slippage on 77.6% of fills.
  Typical Kalshi spread cost: ~1-2¢ on thin markets.
  MIN_EDGE_HURDLE = 0.02 (2% edge) is the floor to clear execution friction.
  A 1% hurdle would trade on phantom edge eaten by slippage.

Safety constraints:
  - Half-Kelly (0.5 multiplier) is standard for noisy ML estimators.
  - Hard cap at 5% of bankroll per trade — survives tail events on thin markets.
  - Reject implied_prob ≥ 0.99 (division-by-zero + longshot degenerate case).
  - Reject edge < hurdle (trade cannot overcome execution friction).

Risk register item: Over-Kelly ruin. Raw Kelly at E=0.15, p=0.80 → f*=0.75.
A single loss wipes out 75% of the bankroll. Both Half-Kelly and the 5% cap
address this. Validate with backtest maxDD comparison before flipping on.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ── Calibrated Constants ────────────────────────────────────────────────

MIN_EDGE_HURDLE: float = 0.02
"""Minimum predicted edge to even consider a trade.

Calibration (Phase 1 audit, 2026-04-20):
  - Measured adverse slippage: +0.77¢/trade mean, 77.6% of fills adverse
  - Assumed spread cost: ~0.01
  - Total execution friction floor: ~0.02
Anything below this is structurally unable to clear costs.
"""

DEFAULT_KELLY_FRACTION: float = 0.5
"""Half-Kelly default — survives parameter estimation noise at N<1000."""

MAX_PER_TRADE_FRACTION: float = 0.05
"""Hard cap on any single trade — 5% of bankroll ceiling.

Prevents Over-Kelly ruin: raw Kelly at E=0.15, p=0.80 yields f*=0.75, which
a single loss would destroy. This cap guarantees survival under tail events.
"""

MAX_SANE_IMPLIED_PROB: float = 0.99
"""Reject trades on near-certainties (division-by-zero + degenerate edge)."""


# ── Result Type ──────────────────────────────────────────────────────────


@dataclass
class KellyAllocation:
    """Result of a Kelly sizing calculation."""

    fraction: float              # Fraction of bankroll allocated (0.0 to MAX_PER_TRADE_FRACTION)
    raw_kelly: float             # Unclamped f* before safety fraction & cap
    contracts: int               # Integer contract count (at call time)
    rejected: bool               # True if trade was rejected (sub-hurdle, invalid inputs, etc.)
    reject_reason: Optional[str] # Human-readable rejection reason (None if accepted)


# ── Core Calculation ─────────────────────────────────────────────────────


def kelly_fraction(
    implied_prob: float,
    predicted_edge: float,
    safety_fraction: float = DEFAULT_KELLY_FRACTION,
    min_edge_hurdle: float = MIN_EDGE_HURDLE,
    max_fraction: float = MAX_PER_TRADE_FRACTION,
) -> KellyAllocation:
    """Compute Kelly-optimal bankroll fraction for a YES-side prediction contract.

    Args:
        implied_prob: Market's implied probability [0.0, 1.0] (Kalshi price / 100).
        predicted_edge: ML-predicted residual (P_true - P_implied). Must be > 0
                        to justify a YES-side trade. Negative edges should be
                        routed to NO-side by the caller.
        safety_fraction: Multiplier on raw Kelly (default 0.5 = Half-Kelly).
        min_edge_hurdle: Minimum edge required to overcome execution friction
                         (default MIN_EDGE_HURDLE = 0.02).
        max_fraction: Hard ceiling on bankroll fraction per trade (default
                      MAX_PER_TRADE_FRACTION = 0.05).

    Returns:
        KellyAllocation with the recommended fraction (0 contracts=0 if rejected).
    """
    # Input validation — reject degenerate and unprofitable cases
    if not (0.0 < implied_prob < MAX_SANE_IMPLIED_PROB):
        return KellyAllocation(
            fraction=0.0, raw_kelly=0.0, contracts=0, rejected=True,
            reject_reason=f"implied_prob {implied_prob:.4f} outside (0, {MAX_SANE_IMPLIED_PROB})",
        )
    if not math.isfinite(predicted_edge):
        return KellyAllocation(
            fraction=0.0, raw_kelly=0.0, contracts=0, rejected=True,
            reject_reason=f"predicted_edge not finite: {predicted_edge!r}",
        )
    if predicted_edge < min_edge_hurdle:
        return KellyAllocation(
            fraction=0.0, raw_kelly=0.0, contracts=0, rejected=True,
            reject_reason=f"edge {predicted_edge:.4f} < hurdle {min_edge_hurdle:.4f}",
        )

    # Raw Kelly: f* = E / (1 - p)
    raw = predicted_edge / (1.0 - implied_prob)

    # Apply safety fraction (Half-Kelly by default)
    scaled = raw * safety_fraction

    # Hard cap to survive tail events
    final = min(scaled, max_fraction)

    return KellyAllocation(
        fraction=final, raw_kelly=raw, contracts=0,  # contracts computed separately
        rejected=False, reject_reason=None,
    )


def kelly_contracts(
    implied_prob: float,
    predicted_edge: float,
    bankroll_dollars: float,
    contract_price_dollars: float,
    **kwargs,
) -> KellyAllocation:
    """Compute integer contract count using Kelly sizing.

    Args:
        implied_prob: See kelly_fraction().
        predicted_edge: See kelly_fraction().
        bankroll_dollars: Current total bankroll in dollars.
        contract_price_dollars: Price per YES contract in dollars (0 < p < 1).
        **kwargs: Forwarded to kelly_fraction() (safety_fraction, etc.).

    Returns:
        KellyAllocation with integer `contracts` field populated.
        Returns 0 contracts if Kelly fraction is too small to buy 1 contract.
    """
    alloc = kelly_fraction(implied_prob, predicted_edge, **kwargs)
    if alloc.rejected:
        return alloc

    dollars_allocated = alloc.fraction * bankroll_dollars
    if contract_price_dollars <= 0:
        return KellyAllocation(
            fraction=0.0, raw_kelly=alloc.raw_kelly, contracts=0, rejected=True,
            reject_reason=f"invalid contract price {contract_price_dollars}",
        )
    n = int(dollars_allocated / contract_price_dollars)
    return KellyAllocation(
        fraction=alloc.fraction,
        raw_kelly=alloc.raw_kelly,
        contracts=n,
        rejected=(n == 0),
        reject_reason=None if n > 0 else "allocation too small for 1 contract",
    )


# ── Self-check harness (runs on import in debug; noop in production) ─────


def _self_check() -> None:
    """Sanity-check the formula against known cases."""
    # Case 1: E=0 → f*=0
    r = kelly_fraction(implied_prob=0.5, predicted_edge=0.0)
    assert r.rejected, "E=0 should be rejected (below hurdle)"

    # Case 2: E=0.10 at p=0.50 → raw f* = 0.10/0.50 = 0.20, half=0.10, capped=0.05
    r = kelly_fraction(implied_prob=0.5, predicted_edge=0.10)
    assert abs(r.raw_kelly - 0.20) < 1e-9, f"raw kelly wrong: {r.raw_kelly}"
    assert r.fraction == MAX_PER_TRADE_FRACTION, f"should hit cap: {r.fraction}"

    # Case 3: Over-Kelly ruin scenario (E=0.15, p=0.80) → raw f*=0.75; capped=0.05
    r = kelly_fraction(implied_prob=0.80, predicted_edge=0.15)
    assert abs(r.raw_kelly - 0.75) < 1e-9
    assert r.fraction == MAX_PER_TRADE_FRACTION, "tail-event guard MUST clamp to cap"

    # Case 4: Sub-hurdle (E=0.01) rejected
    r = kelly_fraction(implied_prob=0.5, predicted_edge=0.01)
    assert r.rejected
    assert "hurdle" in (r.reject_reason or "")

    # Case 5: Degenerate high price
    r = kelly_fraction(implied_prob=0.995, predicted_edge=0.05)
    assert r.rejected

    # Case 6: Real contract math — $1000 bankroll, 40¢ contract, E=0.05
    # Half-Kelly: 0.05/0.60 * 0.5 = 0.0417 (under cap). $1000 * 0.0417 = $41.67. / $0.40 = 104 contracts.
    r = kelly_contracts(0.40, 0.05, bankroll_dollars=1000.0, contract_price_dollars=0.40)
    assert r.contracts == 104, f"expected 104 contracts, got {r.contracts}"


if __name__ == "__main__":
    _self_check()
    print("diamond_kelly self-check passed.")
