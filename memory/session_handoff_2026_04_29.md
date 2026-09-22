# DIAMOND Session Handoff — 2026-04-29

**Status:** Sprint 14f deployed. Second cohort reset in 24 hours. Patience window
restarted with corrected book-snapshot pipeline. All three data-collection
fixes (Phase 1 subscriptions, Phase 3 absorption keys, Sprint 14f orderbook
poll) now live and integrated.

---

## What happened since 2026-04-28 handoff

### Sprint 14f — Orderbook poll universe expansion (deployed 2026-04-29 03:04:34 UTC)

**Caught by the Phase 3 telemetry within 23 hours of Sprint 14e deploy.**

**Bug:** `orderbook_poll_loop` in `diamond_monitor.py` had a hardcoded
`tickers_to_poll = active_tickers[:30]` cap from the early sandbox era.
Combined with `get_all_active_tickers()` returning historical (alphabetically-
ordered) tickers, the poll was capturing book snapshots only for ~30
long-dated political/entertainment markets (KXAMERICANIDOL, KXARREST,
KXARTISTSTREAMS) that almost never fire anomalies. Sports markets that
actually fired anomalies had **zero book snapshots ever recorded**.

**Symptom:** `_book_absorption_stale: 1.0` flag firing on **100% of 34,960
post-14e anomalies**. Phase 3 features were logging zeros, not real values.

**The flag worked.** Without the `_book_absorption_stale` canary built into
Sprint 14e, this would have been a 9-day silent failure ending in a
mysterious null-importance result on all 4 absorption variants. We caught
it in 23 hours because the flag was designed to be loud about the
exact failure mode that occurred.

**Fix:**
- `orderbook_poll_loop`: poll target changed from
  `store.get_all_active_tickers()[:30]` to `list(market_cache.keys())`
  (the actual current active universe per metadata refresh loop).
- Per-request sleep tightened from `0.5s` → `0.1s` (10 req/sec, 50%
  safety buffer under Kalshi's 20 req/sec limit).
- Math: ~250 tickers × 0.1s = 25s per full sweep. With
  `REST_POLL_INTERVAL_SEC` between sweeps, every ticker gets polled
  every 30-60s — comfortably inside the 60s absorption window lookback.

**Verification (~10 min post-deploy):**
- 241 snapshots across 99 distinct tickers in 5 min (was: 33 tickers)
- Stale rate: 100% → **11.5%** (above 5% target but improving rapidly)
- 61.5% of post-14f anomalies now have **nonzero absorption values**

The remaining 11.5% stale rate is dominated by legitimate cold-start
cases (brand-new tickers without 60s-prior snapshots). Should converge
below 5% within 1-2 hours of accumulated snapshot history.

### Cohort reset (SECOND in 24h)

| Cohort | Window | Status |
|---|---|---|
| Pre-14e | 2026-04-02 → 2026-04-28 03:27:33 UTC | Purged. ~290 trades, no absorption code. |
| Post-14e / Pre-14f | 2026-04-28 03:27:33 → 2026-04-29 03:04:34 UTC | **Purged.** ~50 trades, broken absorption (all zeros). |
| **Post-14f** | 2026-04-29 03:04:34 UTC → present | **Counting starts at 0.** Real absorption data. |

**N=500 gate epoch:** `1777431874` (was `1777346853` for ~24 hours).

**Cost of reset:** 50 trades + ~22 hours of patience window.
**Cost of NOT resetting:** false-negative null importance on absorption
features → 9-day patience window wasted → another retrain cycle needed.
The reset was the cheap option.

---

## Active state (2026-04-29 ~03:15 UTC)

- **Service:** `diamond-monitor` running since 03:04:34 UTC.
- **Cumulative:** 829+ settled, 50.4% WR, **−$16.34** P&L.
- **Open positions** (6 last check): 4 MLB games settling tonight, 1 NBA,
  1 long-dated Trump tail (the Apr 24 yes @8¢).
- **Data integrity:** orphans=0, L2/L3 firings=0.
- **Today's trading (Apr 28 UTC):** 55 opened, 50 settled, −$2.95 P&L,
  50% WR. Worst single trade: KXMLBGAME TBCLE no @50¢ → −$1.10.

---

## Monitoring protocol (active)

### Stale-rate convergence checks
- **+1 hour** (~04:05 UTC): re-query stale rate on post-14f anomalies.
  Expect: single digits.
- **+24 hours** (~2026-04-30 03:04 UTC): canonical health check.
  Expect: < 5% sustained.
- **Persistent > 20%** → P0 investigation. Would indicate Kalshi rate
  limiting or a poll-loop degradation.

```bash
ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51 \
  "/home/ubuntu/miniconda3/bin/python3 << 'PYEOF'
import sqlite3, json, time
c = sqlite3.connect('/home/ubuntu/kalshi-diamond/diamond_trades.db').cursor()
c.execute('SELECT features FROM anomalies WHERE created_at > 1777431874 AND alert_level != \"NONE\"')
total = stale = nonzero = 0
for (fj,) in c.fetchall():
    if not fj: continue
    try: f = json.loads(fj)
    except: continue
    if 'book_absorption_static' not in f: continue
    total += 1
    if f.get('_book_absorption_stale', 0) >= 0.5: stale += 1
    elif any(abs(f.get(k,0)) > 0.001 for k in
             ['book_absorption_static','book_absorption_depletion',
              'book_absorption_sided','book_absorption_replenish']):
        nonzero += 1
print(f'Post-14f anomalies: {total}')
print(f'  Stale: {stale} = {stale/total*100:.1f}% (target <5%)')
print(f'  Nonzero values: {nonzero} = {nonzero/total*100:.1f}%')
PYEOF"
```

### Learning curve checks (post-14f only)
- **N=100 post-14f** (~2 days, ~2026-05-01): first valid PiT-purged CV
  result on the new cohort. Run:
  ```bash
  scp ubuntu@129.158.40.51:/home/ubuntu/kalshi-diamond/diamond_trades.db /tmp/diamond_check.db
  PYTHONPATH=. python learning_curve.py --db /tmp/diamond_check.db --grid 50,100
  ```
  (`--min-opened-at 1777431874` is now the default.)
- **N=500 post-14f** (~2026-05-08 ETA at 50/day): activation gate. Begin ML
  retrain + Two-Gate Stack validation sequence.

### Standing rules (no action required)
1. Do NOT touch execution thresholds.
2. Do NOT lower the Brier < 0.05 ML promotion gate.
3. Do NOT flip `KELLY_SIZING_ENABLED` until both gates pass.
4. Continue weekly learning-curve sweeps.
5. **No more cohort resets unless another regime change is detected.** Two
   resets in 24h is the limit; if a third is needed, the underlying issue
   is feature-set inadequacy, not a regime artifact.

---

## Pending operator actions

None. Patience window resumes from epoch `1777431874`. Daemon runs.

---

## Lessons crystallized this sprint

1. **Hardcoded sandbox-era caps are the most expensive bugs in production.**
   Both the Phase 1 subscription cap (`BATCH_SIZE=100`) and the orderbook
   poll cap (`active_tickers[:30]`) shared the same archetype: a number
   that was sensible when the universe was small, never revisited as it
   grew. Audit the codebase for similar `[:N]` slices and make them
   universe-aware.
2. **Telemetry must be designed for the silent failure mode.** The
   `_book_absorption_stale` flag was the single most valuable line of
   code we wrote in Sprint 14e. Without it, Sprint 14f wouldn't have
   happened until the N=500 retrain failed mysteriously.
3. **Verification gates must check breadth, not just presence.** Naive
   "is the system doing something?" tests pass on broken systems
   (the old orderbook poll WAS producing snapshots — for the wrong
   tickers). The verification needs to assert against both volume
   and breadth at the right scale.
4. **Cohort resets are not goalpost shifting.** If the data-generating
   distribution changes, training on mixed-regime data is statistical
   malpractice. Documenting the reset publicly (in CLAUDE.md, with
   reasoning) is the discipline that makes the reset honest.
