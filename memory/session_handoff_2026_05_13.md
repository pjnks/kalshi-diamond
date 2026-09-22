# DIAMOND Session Handoff — 2026-05-13 (FINAL)

**Status: 🔴 STRATEGY RETIRED**

This is the terminal handoff for DIAMOND. The strategy has been shut down after
the post-Sprint-14h clean cohort produced five-way diagnostic agreement that the
underlying hypothesis is falsified.

Full record: `reports/2026_05_13_diamond_strategy_autopsy.html`

---

## What happened today

1. **Cumulative kill switch latched May 8 at −$51.52** (Tier-1 $50 cap from Sprint 14g)
2. Post-14h clean cohort completed at N=305 with the daemon still running but
   no new entries possible
3. Ran `learning_curve.py --grid 50,100,150,200,250,300` on the frozen cohort.
   Final result:
   ```
   N=300: Brier=0.2000 ± 0.0295  AUC=0.750  features_kept=3  Jac=0.60
   ```
4. Five-way diagnostic agreement triggered the pivot decision:
   - Brier flat across N=150-300 (no descending slope)
   - AUC peaked at N=200 (0.787) and degrading (0.750 at N=300)
   - Feature count collapsed 5 → 3 (Sprint 14e absorption variants failed null-permutation)
   - U-shape monotonicity inversion persisted (orthogonal features did not break base-rate)
   - OOS WR CI [18.6%, 49.9%] includes 50% — cannot reject null hypothesis
5. Authorized to shut down. Executed three actions:
   - Drafted milestone autopsy report (16KB HTML)
   - Stopped + disabled `diamond-monitor` and `diamond-dashboard` on OCI
   - Archived `diamond_trades.db` to `.shutdown-2026-05-13` (VM) + `.gz` (Mac)

---

## Final state

| Metric | Value |
|---|---|
| All-time settled | 1,575 |
| All-time WR | 53.78% |
| Cumulative P&L | **−$51.52** |
| Post-14h clean cohort | 305 settled, 60.33% WR, −$19.65 |
| Open positions at shutdown | 3 (no manual exit per training-distribution rule) |
| Service `diamond-monitor` | stopped + disabled |
| Service `diamond-dashboard` | stopped + disabled |
| DB integrity | ok (0 orphans, all 3 data layers held throughout) |

### Open positions (preserved, no manual exit)
- `KXALIENS-27` no @ 78¢ (opened May 6) — long-dated political
- `KXITFMATCH-26MAY02NIMGUO-GUO` no @ 49¢ (opened May 4) — ITF tennis
- `KXTRUMPOUT27-27-26AUG01` yes @ 8¢ (opened Apr 24) — Trump Aug 2027 tail

These will not be polled for settlement going forward. Archive will show
`status='filled'` indefinitely. Acceptable for a frozen research dataset.

---

## What the autopsy proves

> *"Kalshi orderbook microstructure contains tradeable alpha at retail scale
> via flat-sized longshot-biased anomaly detection."*

This hypothesis is empirically falsified. Both the execution layer
(−$51.52 → kill switch) and the evaluation layer (Brier 0.20 + degrading
AUC + failed null-permutation) independently agreed.

This is **success at the experiment level even though it is failure at the
trading level.** The infrastructure built to test the hypothesis is the
lasting asset.

---

## Institutional assets carried forward to BERYL/CITRINE

| Asset | Type | Transferability |
|---|---|---|
| PiT-purged CV (`pit_mask.sum() >= 50`) | Methodology | Direct port |
| Null-importance permutation test | Methodology | Direct port |
| Cohort-burn discipline | Cultural | Conceptual |
| Two-Gate Stack (Brier + Jaccard) | Policy | Direct port |
| Pre-registered falsifiable thresholds | Cultural | Conceptual |
| Bayesian shrinkage (`src/diamond_shrinkage.py`) | Module | Reusable as-is |
| Three-layer data integrity | Pattern | Conceptual |
| Two-tier kill switch (daily + cumulative) | Pattern | Direct port |
| Task-death watchdog | Pattern | Conceptual (asyncio only) |
| CancelledError-aware exception handling | Pattern | Conceptual (asyncio only) |
| Telemetry against absence | Pattern | Conceptual |

---

## DB archive (for future research)

**VM**:
```
/home/ubuntu/kalshi-diamond/diamond_trades.db.shutdown-2026-05-13     (398MB)
/home/ubuntu/kalshi-diamond/diamond_trades.db.shutdown-2026-05-13.gz  (107MB)
```

**Mac**:
```
~/Documents/quant/diamond/archive/diamond_trades.db.shutdown-2026-05-13.gz  (107MB)
```

**Contents**: 1,787 paper_trades, 89,376 anomalies, 1,275,176 trades,
126,265 book_snapshots, 9,383 skipped_trades, 20,980 market_profiles.
SQLite `integrity_check`: ok.

---

## Operational notes (next session)

There is no "next checkpoint" for DIAMOND. The project is closed.

If a future session asks about DIAMOND:
1. Read this handoff
2. Read `reports/2026_05_13_diamond_strategy_autopsy.html`
3. Read the updated `CLAUDE.md` § "🔴 RETIRED" header
4. Direct attention to BERYL/CITRINE — both are active alpha-hunting candidates

If a future session is tempted to revive DIAMOND:
- **Do not revive for live trading.** The hypothesis is falsified.
- Research-only work on the archived dataset is acceptable.
- Any new live strategy should be a new repo/name, not a DIAMOND revival.

---

## Lesson crystallized

> **Separate the expected value of the strategy from the expected value of the engineer.**
>
> DIAMOND was a textbook quantitative failure handled with textbook quantitative
> rigor. The strategy lost ~$51 over two months. The engineer gained PiT CV,
> asyncio-safe async patterns, three-layer integrity defense, two-tier kill
> switches, and the discipline to burn 733 contaminated trades rather than
> p-hack the result.
>
> The infrastructure outlives the strategy. That is how this works.

---

## Final shutdown checklist

- [x] Autopsy report drafted (`reports/2026_05_13_diamond_strategy_autopsy.html`)
- [x] `diamond-monitor.service` stopped + disabled
- [x] `diamond-dashboard.service` stopped + disabled
- [x] DB archived on VM (`.shutdown-2026-05-13` + `.gz`)
- [x] DB archived on Mac (`~/Documents/quant/diamond/archive/`)
- [x] DIAMOND `CLAUDE.md` updated with 🔴 RETIRED header + final state + institutional assets section
- [x] Parent `~/Documents/quant/CLAUDE.md` updated (quick-nav table, systemd services, DIAMOND status section)
- [x] This handoff file written

The chapter is closed.
