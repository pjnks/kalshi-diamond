# DIAMOND Session Handoff — 2026-05-04

**Source:** Reconstructed from Apr 29-30 UTC session transcript (docx).
Sprint 14g already deployed (May 1) and documented in prior handoff. This
handoff captures the Apr 29-30 performance data and Sprint 14f verification
results that the May 1 handoff didn't fully cover.

---

## Sprint 14f verification — PASSED (24h results)

The +24h health check ran implicitly as the post-14f cohort accumulated
on Apr 29-30. Results across 47,101 post-14f anomalies:

| Metric                    | Pre-14f | +10 min | +24h (Apr 30) | Target |
|---------------------------|---------|---------|---------------|--------|
| `_book_absorption_stale`  | 100%    | 11.5%   | **0.0%** (9/47,101) | <5% |
| Nonzero absorption values | 0%      | 61.5%   | **98.8%** (46,548/47,101) | majority |
| Snapshot pipeline breadth  | 33 tkrs | 99 tkrs | 125 tkrs      | broad  |

The 9 remaining stale firings are brand-new tickers without 60s-prior
history — legitimate cold-start case the flag was designed to catch. The
absorption pipeline is fully operational.

---

## Performance snapshot (Apr 30 ~02:00 UTC)

| Metric              | Value           | Δ vs prior |
|---------------------|-----------------|------------|
| Total settled       | 903             | +74        |
| All-time WR         | 51.1%           | +0.7pp     |
| Cumulative P&L      | −$20.32         | −$3.98     |
| Apr 29 UTC day      | 83 settled, 56.6% WR, −$5.19 |        |
| Post-14f cohort     | 67/500 (13.4%)  | new        |

**Character of the day:** High WR, negative P&L. This is the convexity-trap
signature — winning small on favorites (75-89c), losing big on rare
50-74c losers. Exactly what the absorption features are designed to
eventually address via ML-backed entry rejection.

**Open positions (Apr 30):**
- `KXNBAGAME-26APR29HOULAL-LAL` yes @ 63c (Apr 29 — NBA game)
- `KXTRUMPOUT27-27-26AUG01` yes @ 8c (Apr 24 — long-dated tail, holding per standing rule)

---

## Velocity revision (material)

| Metric         | Prior estimate | Actual (Apr 29-30) |
|----------------|----------------|---------------------|
| Trades/day     | ~50/day        | **~70/day** (67 settled in 22.8h) |
| N=500 ETA      | ~mid-May       | **~May 6** (6 days from Apr 30) |

The Sprint 14e subscription fix expanded the universe from ~67 to ~250+
tickers, increasing trade throughput ~40% above projection. This is a
material acceleration of the patience window.

---

## Activation timeline (collaborator discussion, Apr 30)

Full Kelly activation path, realistic timeline ~4 weeks from Apr 30
(late May / early June):

1. **N=500 gate hit** — ~May 6 at 70/day
2. **ML retrain** — +1-2 days. First retrain with absorption features.
3. **Two-Gate Stack validation** — Brier < 0.05 AND Jaccard >= 0.70.
   First retrain probably won't pass (current Brier ~0.21, gate is 0.05).
4. **Wire ML edge into ENTRY gate** — +1-2 days if model passes.
5. **Backtest re-validates** — +1 day.
6. **KELLY_SIZING_ENABLED=true** — the flag flip.

**Best case:** ~2 weeks (mid-May). Requires 4x Brier improvement on
first try — ambitious.
**Realistic:** ~4 weeks (late May). First retrain near-miss, accumulate
to N=750-1000, second retrain passes.
**Pessimistic:** 8+ weeks if absorption features fail null-importance.

---

## Pending action items (as of May 4)

1. **N=100 learning curve check** — was expected ~May 1 at 70/day.
   Should have been run by now. Run:
   ```
   PYTHONPATH=. python learning_curve.py --grid 50,100
   ```
   Pre-registered thresholds:
   - Brier < 0.18 → absorption hypothesis supported
   - Brier 0.20-0.22 → not yet differentiating, needs more N
   - Brier > 0.23 → regime contamination concern

2. **Sprint 14g +24h verification** — confirm daily kill switch correctly
   resets at UTC midnight. Check skip_reason in skipped_trades table:
   should see `kill_switch_daily` (not `kill_switch_cumulative`).

3. **Weekly stale-rate monitoring** — canary should stay near 0%. Alert
   if sustained above 5%.

4. **Sandbox artifact audit** (backlog) — grep codebase for unbounded
   SQL aggregations, hardcoded `[:N]` slices, `LIMIT N` with hardcoded
   constants. Three found in 5 days (14e/14f/14g); likely more exist.

---

## Data integrity — perfect (continuous since Apr 16)

| Layer | Status |
|-------|--------|
| Layer 1 (code) | 0 corrupt fills |
| Layer 2 (app guard) | 0 firings |
| Layer 3 (CHECK constraint) | 0 violations |
| Orphan rows | 0 |

---

## Standing rules (unchanged)

1. Do NOT touch execution thresholds.
2. Do NOT lower the Brier < 0.05 ML promotion gate.
3. Do NOT flip `KELLY_SIZING_ENABLED` until both gates pass.
4. Do NOT bypass the cumulative -$50 cap.
5. Do NOT intervene on long-dated tail trades (Trump @ 8c etc).

---

## Key insight from session

> "Infrastructure buys you the ability to interpret your losses honestly."
> Until Sprint 14f, a negative P&L day was epistemological fog — couldn't
> separate flawed strategy from broken data conduit. Post-14f, every loss
> is a real loss on real data, attributable to real model behavior, not
> artifact of broken plumbing.
