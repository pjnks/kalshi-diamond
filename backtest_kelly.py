"""
backtest_kelly.py
─────────────────
Half-Kelly sizing vs flat tiered sizing — historical backtest harness.

Three phases (run in order, each answers a different question):
  Phase A: Computational correctness  — oracle edge estimator
           Q: "Does kelly_contracts() produce the right allocation given perfect info?"
  Phase B: Sensitivity analysis       — noisy oracle, σ sweep
           Q: "How bad can our ML model's calibration be before Kelly breaks?"
  Phase C: Empirical lower bound      — PiT price-bucket empirical edge
           Q: "Does Kelly shrink max-DD vs flat sizing, even on a crude proxy?"

Statistical framework:
  - Event-cluster block bootstrap (not IID)  — preserves sibling correlation
  - PiT bucket edges computed ONCE on original cohort (not per-resample)
  - Ledger invariant asserted at every simulator tick

Usage:
  python backtest_kelly.py --phase A
  python backtest_kelly.py --phase B --sigmas 0.01,0.02,0.05,0.10
  python backtest_kelly.py --phase C --min-active-n 10 --bootstrap 10000

Outputs: JSON report + CSV per-trade ledger.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# ── DIAMOND imports ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from src.diamond_kelly import kelly_contracts, KellyAllocation

DB_PATH = Path(__file__).parent / "diamond_trades.db"


# ── Data model ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SimTrade:
    """A single settled trade as input to the simulator.

    All cents-denominated amounts are integers.
    """
    trade_id: int
    ticker: str
    event_id: str           # for block bootstrap
    side: str               # 'yes' or 'no'
    entry_price: int        # cents (0-100)
    original_count: int     # the live system's actual size
    pnl_per_contract: float # realized cents per single contract (win=+100-p, loss=-p)
    opened_at: float        # unix ts
    settled_at: float       # unix ts (must be non-None — we only backtest settled)
    alert_level: str        # 'CRITICAL' | 'ALERT' | 'NOTABLE'
    anomaly_score: float


@dataclass
class OpenPosition:
    trade_id: int
    opened_at: float
    settled_at: float
    contracts: int
    entry_price: int
    cost_cents: int             # = contracts * entry_price
    realized_pnl_cents: float   # = contracts * pnl_per_contract (known at sim time; materialized at settle)


@dataclass
class SimState:
    cash_cents: float
    realized_pnl_cents: float = 0.0
    open_positions: list[OpenPosition] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    initial_bankroll_cents: float = 0.0
    equity_curve: list[tuple[float, float]] = field(default_factory=list)  # (ts, equity_cents)

    def locked_cents(self) -> int:
        return sum(p.cost_cents for p in self.open_positions)

    def assert_invariant(self) -> None:
        """cash + locked - realized == initial.

        Mechanics: on settlement, cash receives (cost + pnl) and realized receives pnl.
        Locked decreases by cost. Net delta: Δcash + Δlocked - Δrealized = 0.
        The '+ realized' formulation would double-count gains (bug fixed 2026-04-21).
        """
        lhs = self.cash_cents + self.locked_cents() - self.realized_pnl_cents
        rhs = self.initial_bankroll_cents
        assert abs(lhs - rhs) < 0.01, (
            f"Ledger invariant violated: "
            f"cash={self.cash_cents:.2f} + locked={self.locked_cents()} - "
            f"realized={self.realized_pnl_cents:.2f} = {lhs:.2f}, "
            f"expected initial={rhs}"
        )


# ── Data loading ─────────────────────────────────────────────────


def load_cohort(
    db_path: Path = DB_PATH,
    opened_since: str = "2026-04-02",  # post-Sprint-11 default
) -> list[SimTrade]:
    """Pull settled trades from the DB, computing per-contract P&L from pnl_cents."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, ticker, event_id, side, entry_price, count,
               pnl_cents, opened_at, settled_at, anomaly_level, anomaly_score
        FROM paper_trades
        WHERE status = 'settled'
          AND opened_at >= strftime('%s', ?)
          AND entry_price IS NOT NULL AND entry_price > 0
          AND settled_at IS NOT NULL
          AND count IS NOT NULL AND count > 0
        ORDER BY opened_at
        """,
        (opened_since,),
    )

    trades: list[SimTrade] = []
    for row in cur.fetchall():
        tid, ticker, event_id, side, entry, count, pnl, opened, settled, lvl, score = row
        # pnl_cents is TOTAL P&L on the position — divide by count for per-contract
        pnl_per = float(pnl) / count if count else 0.0
        # event_id may be NULL on older rows — derive from ticker
        if not event_id:
            event_id = ticker.rsplit("-", 1)[0] if "-" in ticker else ticker
        trades.append(SimTrade(
            trade_id=tid, ticker=ticker, event_id=event_id, side=side,
            entry_price=int(entry), original_count=int(count),
            pnl_per_contract=pnl_per,
            opened_at=float(opened), settled_at=float(settled),
            alert_level=lvl or "ALERT", anomaly_score=float(score or 0),
        ))
    conn.close()
    return trades


# ── Bucket mapping ───────────────────────────────────────────────


PRICE_BUCKETS = [(0, 14), (15, 24), (25, 49), (50, 74), (75, 89), (90, 99)]


def price_bucket_cents(price: int) -> tuple[int, int]:
    for lo, hi in PRICE_BUCKETS:
        if lo <= price <= hi:
            return (lo, hi)
    return (0, 99)  # degenerate fallback — shouldn't happen on Kalshi


# ── Edge estimators (three phases) ───────────────────────────────


def oracle_edge(trade: SimTrade, *, cohort=None) -> float:
    """Phase A: use the ACTUAL outcome as q_true (lookahead — by design)."""
    won = trade.pnl_per_contract > 0
    q_true = 1.0 if won else 0.0
    p = trade.entry_price / 100.0
    return q_true - p


def noisy_oracle_edge(sigma: float, rng: np.random.Generator) -> Callable[..., float]:
    """Phase B: oracle + Gaussian noise, clipped to valid [0.01, 0.99] probability range.

    Biased clip is intentional (collaborator ruling 2026-04-20) — an uncalibrated
    ML model overshooting bounds would NOT self-center to zero mean.
    """
    def _fn(trade: SimTrade, *, cohort=None) -> float:
        won = trade.pnl_per_contract > 0
        q_true = 1.0 if won else 0.0
        p = trade.entry_price / 100.0
        q_noisy = float(np.clip(q_true + rng.normal(0, sigma), 0.01, 0.99))
        return q_noisy - p
    return _fn


def pit_bucket_edge_factory(
    cohort: list[SimTrade],
    min_active_n: int = 10,
) -> tuple[Callable[..., Optional[float]], set[int]]:
    """Phase C: expanding-window price-bucket empirical edge.

    Precomputes edges ONCE on the original cohort (see bootstrap design note).
    Returns (edge_fn, warmup_skip_ids). The warmup_skip_ids set is the N-matching
    filter: callers MUST exclude these trade_ids from the comparison cohort so
    Kelly and Flat operate on the same universe (fix 2026-04-21).

    PiT gate: for arriving trade at time t_open, observable priors are trades
    with same bucket whose settled_at < t_open. Using opened_at < t_open would
    leak contemporaneous-open outcomes.
    """
    # Build settled-at index per bucket (pool of observable priors for future trades).
    bucket_prior: dict[tuple[int, int], list[tuple[float, bool]]] = {}
    for t in sorted(cohort, key=lambda t: t.settled_at):
        bucket = price_bucket_cents(t.entry_price)
        won = t.pnl_per_contract > 0
        bucket_prior.setdefault(bucket, []).append((t.settled_at, won))

    edges: dict[int, Optional[float]] = {}
    warmup_skip_ids: set[int] = set()

    for arriving in sorted(cohort, key=lambda t: t.opened_at):
        bucket = price_bucket_cents(arriving.entry_price)
        observable = [
            (ts, won) for (ts, won) in bucket_prior.get(bucket, [])
            if ts < arriving.opened_at  # PiT gate: settled strictly before open
        ]
        if len(observable) < min_active_n:
            edges[arriving.trade_id] = None
            warmup_skip_ids.add(arriving.trade_id)
        else:
            wins = sum(1 for _, w in observable if w)
            bucket_wr = wins / len(observable)
            edges[arriving.trade_id] = bucket_wr - (arriving.entry_price / 100.0)

    print(f"PiT bucket edges: {len(edges)} trades, "
          f"{len(warmup_skip_ids)} warmup skips ({100*len(warmup_skip_ids)/len(edges):.1f}%)")

    def _lookup(trade: SimTrade, *, cohort=None) -> Optional[float]:
        return edges.get(trade.trade_id)

    return _lookup, warmup_skip_ids


# ── Sizing strategies ────────────────────────────────────────────


def flat_tiered_size(
    trade: SimTrade, *, cash_cents: float, state: SimState, **kw,
) -> int:
    """Mirror of live tiered sizing: CRITICAL=3, High ALERT=2, Low ALERT=1."""
    if trade.alert_level == "CRITICAL" and trade.anomaly_score >= 0.78:
        contracts = 3
    elif trade.alert_level == "ALERT" and trade.anomaly_score >= 0.65:
        contracts = 2
    else:
        contracts = 1
    # Cash guard — can't spend what you don't have
    max_affordable = int(cash_cents // trade.entry_price) if trade.entry_price > 0 else 0
    return min(contracts, max_affordable)


def half_kelly_size(
    trade: SimTrade, *,
    cash_cents: float, state: SimState,
    edge_fn: Callable[[SimTrade], Optional[float]],
    safety_fraction: float = 0.5,
    min_edge_hurdle: float = 0.02,
    max_fraction: float = 0.05,
) -> int:
    """Kelly-sized against AVAILABLE CASH (not total wealth)."""
    edge = edge_fn(trade)
    if edge is None or edge <= 0:  # warmup skip or negative-edge trade
        return 0

    implied = trade.entry_price / 100.0
    alloc = kelly_contracts(
        implied_prob=implied,
        predicted_edge=edge,
        bankroll_dollars=cash_cents / 100.0,
        contract_price_dollars=trade.entry_price / 100.0,
        safety_fraction=safety_fraction,
        min_edge_hurdle=min_edge_hurdle,
        max_fraction=max_fraction,
    )
    return alloc.contracts


# ── Simulator ────────────────────────────────────────────────────


def advance_clock(state: SimState, now: float) -> None:
    """Release any positions that have settled by `now`, logging equity curve."""
    still_open: list[OpenPosition] = []
    # Settle in chronological order so equity curve is monotone-in-time
    to_settle = sorted(
        [p for p in state.open_positions if p.settled_at <= now],
        key=lambda p: p.settled_at,
    )
    for p in to_settle:
        # Return stake + pnl to cash; tally realized.
        state.cash_cents += p.cost_cents + p.realized_pnl_cents
        state.realized_pnl_cents += p.realized_pnl_cents
        # Equity at this instant (cost-basis, no mark-to-market for still-open):
        equity = state.initial_bankroll_cents + state.realized_pnl_cents
        state.equity_curve.append((p.settled_at, equity))
    for p in state.open_positions:
        if p.settled_at > now:
            still_open.append(p)
    state.open_positions = still_open


def simulate(
    cohort: list[SimTrade],
    sizing_fn: Callable,
    initial_bankroll_cents: float = 100_000.0,  # $1000
    **sizing_kwargs,
) -> SimState:
    state = SimState(
        cash_cents=initial_bankroll_cents,
        initial_bankroll_cents=initial_bankroll_cents,
    )

    sorted_cohort = sorted(cohort, key=lambda t: t.opened_at)

    for trade in sorted_cohort:
        advance_clock(state, trade.opened_at)
        state.assert_invariant()

        contracts = sizing_fn(
            trade, cash_cents=state.cash_cents, state=state, **sizing_kwargs,
        )

        if contracts <= 0:
            state.ledger.append({
                "trade_id": trade.trade_id, "action": "skip",
                "cash": state.cash_cents, "reason": "zero_size_or_no_edge",
            })
            continue

        cost = contracts * trade.entry_price
        if cost > state.cash_cents:
            state.ledger.append({
                "trade_id": trade.trade_id, "action": "skip",
                "cash": state.cash_cents, "reason": "insufficient_cash",
            })
            continue

        state.cash_cents -= cost
        state.open_positions.append(OpenPosition(
            trade_id=trade.trade_id,
            opened_at=trade.opened_at, settled_at=trade.settled_at,
            contracts=contracts, entry_price=trade.entry_price,
            cost_cents=cost,
            realized_pnl_cents=contracts * trade.pnl_per_contract,
        ))
        state.ledger.append({
            "trade_id": trade.trade_id, "action": "enter",
            "contracts": contracts, "cost": cost, "cash_after": state.cash_cents,
        })
        state.assert_invariant()

    # Settle any still-open at end of simulation
    advance_clock(state, float("inf"))
    state.assert_invariant()
    return state


# ── Metrics ──────────────────────────────────────────────────────


def max_drawdown_cents(equity_curve: list[tuple[float, float]], initial: float) -> dict:
    """Peak-to-trough drawdown in absolute cents + percent of peak equity."""
    if not equity_curve:
        return {"cents": 0.0, "pct_of_peak": 0.0, "peak_equity": initial, "trough_equity": initial}
    peak = initial
    max_dd_cents = 0.0
    peak_at_max_dd = initial
    trough_at_max_dd = initial
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd_cents:
            max_dd_cents = dd
            peak_at_max_dd = peak
            trough_at_max_dd = eq
    pct = (max_dd_cents / peak_at_max_dd * 100.0) if peak_at_max_dd > 0 else 0.0
    return {
        "cents": float(max_dd_cents),
        "pct_of_peak": float(pct),
        "peak_equity": float(peak_at_max_dd),
        "trough_equity": float(trough_at_max_dd),
    }


def per_trade_pnl_sharpe(state: SimState) -> float:
    """Sharpe on per-trade realized P&L (ignores trade duration — directional only)."""
    pnls = [p.realized_pnl_cents for p in []] + [
        # Reconstruct per-trade from ledger "enter" actions — cost-basis independent
        r.get("pnl", 0.0) for r in state.ledger if r.get("action") == "settle"
    ]
    # If simulator doesn't emit settle records, fall back to equity curve diffs
    if not pnls:
        prev = state.initial_bankroll_cents
        pnls = []
        for _, eq in state.equity_curve:
            pnls.append(eq - prev)
            prev = eq
    if len(pnls) < 2:
        return 0.0
    arr = np.array(pnls)
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1))
    return mu / sd if sd > 0 else 0.0


def compute_metrics(state: SimState) -> dict:
    """Extract Kelly-relevant statistics from a completed simulation."""
    n_enters = sum(1 for r in state.ledger if r["action"] == "enter")
    n_skips = sum(1 for r in state.ledger if r["action"] == "skip")
    total_pnl = state.realized_pnl_cents

    # Terminal wealth = cash only (all positions settled; invariant: cash = initial + realized).
    # Adding `+ realized_pnl_cents` would double-count gains (bug fixed 2026-04-21).
    terminal = state.cash_cents
    dd = max_drawdown_cents(state.equity_curve, state.initial_bankroll_cents)

    return {
        "n_enters": n_enters,
        "n_skips": n_skips,
        "skip_rate": n_skips / (n_enters + n_skips) if (n_enters + n_skips) else 0,
        "terminal_bankroll_cents": float(terminal),
        "total_pnl_cents": float(total_pnl),
        "return_pct": float(total_pnl / state.initial_bankroll_cents * 100.0)
                      if state.initial_bankroll_cents > 0 else 0.0,
        "max_drawdown_cents": dd["cents"],
        "max_drawdown_pct": dd["pct_of_peak"],
        "peak_equity_cents": dd["peak_equity"],
        "trough_equity_cents": dd["trough_equity"],
        "per_trade_sharpe": per_trade_pnl_sharpe(state),
    }


# ── Event-cluster block bootstrap ────────────────────────────────


def event_block_bootstrap(
    cohort: list[SimTrade],
    sizing_fn: Callable,
    n_replicates: int = 10_000,
    seed: int = 42,
    **sizing_kwargs,
) -> list[dict]:
    """Resample event clusters WITH REPLACEMENT, simulate each, collect metrics."""
    rng = random.Random(seed)

    # Group trades by event_id
    by_event: dict[str, list[SimTrade]] = {}
    for t in cohort:
        by_event.setdefault(t.event_id, []).append(t)

    event_ids = list(by_event.keys())
    results = []
    for i in range(n_replicates):
        sampled_events = [rng.choice(event_ids) for _ in event_ids]
        replicate = [t for eid in sampled_events for t in by_event[eid]]
        state = simulate(replicate, sizing_fn, **sizing_kwargs)
        results.append(compute_metrics(state))
    return results


# ── Phase orchestration ──────────────────────────────────────────


def run_phase_a(cohort: list[SimTrade]) -> dict:
    """Phase A: oracle edge, no noise — proves the code path."""
    flat = simulate(cohort, flat_tiered_size)
    kelly = simulate(
        cohort, half_kelly_size,
        edge_fn=lambda t, **kw: oracle_edge(t),
    )
    return {
        "phase": "A",
        "flat": compute_metrics(flat),
        "kelly_oracle": compute_metrics(kelly),
    }


def run_phase_b(cohort: list[SimTrade], sigmas: list[float]) -> dict:
    """Phase B: noisy oracle across σ sweep — maps calibration-noise tolerance.

    Includes Flat baseline computed on the same cohort for direct σ_max derivation.
    """
    flat_state = simulate(cohort, flat_tiered_size)
    flat_metrics = compute_metrics(flat_state)

    rng = np.random.default_rng(42)
    sweep = []
    for sigma in sigmas:
        edge_fn = noisy_oracle_edge(sigma, rng)
        state = simulate(cohort, half_kelly_size, edge_fn=edge_fn)
        m = compute_metrics(state)
        m["sigma"] = sigma
        m["pnl_vs_flat_cents"] = m["total_pnl_cents"] - flat_metrics["total_pnl_cents"]
        m["dd_vs_flat_cents"] = flat_metrics["max_drawdown_cents"] - m["max_drawdown_cents"]
        sweep.append(m)
    return {"phase": "B", "flat": flat_metrics, "sweep": sweep}


def run_phase_c(
    cohort: list[SimTrade],
    min_active_n: int = 10,
    n_bootstrap: int = 1000,
) -> dict:
    """Phase C: PiT empirical bucket edge — lower bound on real Kelly advantage.

    N-matching invariant (fix 2026-04-21): warmup trades (bucket N < min_active_n)
    return None from edge_fn. These MUST be excluded from BOTH Kelly and Flat
    before metric comparison, else Kelly runs on a different universe than Flat
    and the comparison is meaningless (survival-bias trap).
    """
    edge_fn, warmup_skip_ids = pit_bucket_edge_factory(cohort, min_active_n=min_active_n)

    # Matched cohort: exclude ONLY the trades where Kelly can't decide (warmup).
    # Kelly still legitimately skips trades where edge < hurdle on the matched set;
    # that's a strategy choice, not an estimator deficiency.
    matched_cohort = [t for t in cohort if t.trade_id not in warmup_skip_ids]
    print(f"Phase C matched cohort: {len(matched_cohort)}/{len(cohort)} trades "
          f"({len(warmup_skip_ids)} warmup-dropped from BOTH strategies)")

    # Full-cohort Flat for the "what you'd actually observe live" reference.
    flat_full = simulate(cohort, flat_tiered_size)
    flat_matched = simulate(matched_cohort, flat_tiered_size)
    kelly_matched = simulate(matched_cohort, half_kelly_size, edge_fn=edge_fn)

    # Event-cluster bootstrap on MATCHED cohort — preserves sibling ±1 correlation.
    flat_boot = event_block_bootstrap(
        matched_cohort, flat_tiered_size, n_replicates=n_bootstrap,
    )
    kelly_boot = event_block_bootstrap(
        matched_cohort, half_kelly_size, n_replicates=n_bootstrap, edge_fn=edge_fn,
    )
    pnl_diffs = [k["total_pnl_cents"] - f["total_pnl_cents"]
                 for k, f in zip(kelly_boot, flat_boot)]
    dd_diffs = [f["max_drawdown_cents"] - k["max_drawdown_cents"]  # positive = Kelly shrinks DD
                for k, f in zip(kelly_boot, flat_boot)]

    return {
        "phase": "C",
        "cohort_size_total": len(cohort),
        "cohort_size_matched": len(matched_cohort),
        "warmup_dropped": len(warmup_skip_ids),
        "flat_full_cohort": compute_metrics(flat_full),
        "flat_matched": compute_metrics(flat_matched),
        "kelly_matched": compute_metrics(kelly_matched),
        "bootstrap_pnl_diff": {
            "mean": float(np.mean(pnl_diffs)),
            "p05": float(np.percentile(pnl_diffs, 5)),
            "p50": float(np.percentile(pnl_diffs, 50)),
            "p95": float(np.percentile(pnl_diffs, 95)),
            "n_replicates": n_bootstrap,
        },
        "bootstrap_dd_diff_cents": {
            "mean": float(np.mean(dd_diffs)),
            "p05": float(np.percentile(dd_diffs, 5)),
            "p50": float(np.percentile(dd_diffs, 50)),
            "p95": float(np.percentile(dd_diffs, 95)),
            "description": "Positive = Kelly shrinks MaxDD vs Flat (what we want)",
        },
    }


# ── Entry point ──────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["A", "B", "C", "all"], default="all")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--since", default="2026-04-02")
    parser.add_argument("--sigmas", default="0.01,0.02,0.05,0.10")
    parser.add_argument("--min-active-n", type=int, default=10)
    parser.add_argument("--bootstrap", type=int, default=1000)
    args = parser.parse_args()

    cohort = load_cohort(Path(args.db), opened_since=args.since)
    print(f"Cohort: {len(cohort)} settled trades (opened since {args.since})")

    reports = {}
    if args.phase in ("A", "all"):
        reports["A"] = run_phase_a(cohort)
    if args.phase in ("B", "all"):
        sigmas = [float(s) for s in args.sigmas.split(",")]
        reports["B"] = run_phase_b(cohort, sigmas)
    if args.phase in ("C", "all"):
        reports["C"] = run_phase_c(
            cohort,
            min_active_n=args.min_active_n,
            n_bootstrap=args.bootstrap,
        )

    print(json.dumps(reports, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
