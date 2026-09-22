# DIAMOND Session Handoff — 2026-05-01

**Status:** Sprint 14g deployed. Kill switch unjammed via two-tier architecture
(daily soft cap + cumulative hard cap). Trading should resume on first post-
deploy ALERT.

---

## What happened

### The narrative trap
Yesterday (Apr 30 UTC) showed 0 new entries despite an active sports schedule
(NBA playoffs + 3 games + MLB). Initial diagnosis was "thin sports schedule
+ low-opportunity regime." Operator pushed back: NBA playoffs are not
a thin schedule. The narrative was wrong.

### The actual cause (Sprint 14g)
`src/diamond_store.py:get_paper_stats()` had this SQL:
```sql
SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades WHERE status = 'settled'
```
No date filter. Result assigned to `total_pnl`, then used as the
`total_daily_pnl_cents` value the daily kill switch checks. The variable
name said "daily" but the SQL was all-time. Bug present since the kill
switch was written; only manifested when cumulative P&L crossed -$20
on 2026-04-29.

**Skip-reasons query (last 24h before fix):**
- `kill_switch`: 1,765
- `min_price`: 359
- `conviction_block`: 344
- `dedup`: 2

The kill switch was responsible for ~71% of all rejections. Today's actual
daily P&L was +$0.00, but the bugged "daily" value reported -$21.95
(cumulative).

---

## The fix (two-tier architecture)

### diamond_config.py
```python
PAPER_MAX_UNREALIZED_CENTS = 2000   # Daily soft cap, $20. Resets at UTC midnight.
PAPER_MAX_CUMULATIVE_CENTS = 5000   # All-time hard cap, $50. Manual review required.
```

### src/diamond_store.py — get_paper_stats()
Added separate `today_realized_pnl` query with explicit date filter:
```sql
SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades
WHERE status = 'settled'
  AND settled_at >= strftime('%s', 'now', 'start of day')
```

Return dict now has both:
- `total_pnl_cents` — cumulative all-time (for dashboard + cumulative kill switch)
- `today_realized_pnl_cents` — today only (Sprint 14g, used by daily kill switch)
- `total_daily_pnl_cents` — today_realized + unrealized (the new daily check)

### src/diamond_paper.py — _execute_trade()
Two-tier kill switch, evaluated in order:

1. **Cumulative hard halt** (Tier 1):
   - `cumulative_pnl < -PAPER_MAX_CUMULATIVE_CENTS` ($50)
   - NEVER auto-resets
   - Skip reason: `kill_switch_cumulative`
   - Log level: `error` (not warning)
   - CRITICAL Pushover alert: "Manual review required"

2. **Daily soft halt** (Tier 2):
   - `today_realized + unrealized + estimated_cost < -PAPER_MAX_UNREALIZED_CENTS` ($20)
   - Resets at UTC midnight
   - Skip reason: `kill_switch_daily`
   - CRITICAL Pushover alert (daily blowout warning)

---

## Deploy timeline

| Time | Event |
|---|---|
| 2026-05-01 03:08:35 UTC | DEPLOY_TS captured |
| 2026-05-01 03:09:02 UTC | systemd restart (new code live) |
| Post-deploy verification | (in progress at handoff write time) |

---

## State going into today

- **Pre-deploy state:** 904 settled, -$20.96 cumulative, 1 open position (Trump tail @8¢ from Apr 24)
- **Cumulative kill switch evaluation:** -$20.96 vs -$50.00 cap → PASSES (well above)
- **Daily kill switch evaluation:** today_realized = +$0.00, unrealized ≈ +$0.00 → PASSES

Both checks should pass. Entries should resume on first post-deploy ALERT.

---

## The third sandbox artifact

This is the **third** sandbox-era artifact discovered in 5 days, all of the
same archetype:

| Sprint | Date | Artifact | Failure mode |
|---|---|---|---|
| 14e | Apr 26 | `BATCH_SIZE = 100; tickers[:BATCH_SIZE]` | Subscription truncated; 60% deaf |
| 14f | Apr 29 | `active_tickers[:30]` | Orderbook polled wrong 30 tickers; sports markets had 0 snapshots |
| 14g | May 1 | `SUM(pnl_cents)` no date filter | Daily kill switch silently checked cumulative |

**Common pattern:** code that was correct at the original small scale,
never revisited as N grew, silently failing in production under
conditions the original author didn't anticipate.

**Backlog (formal Sandbox Artifact Audit):**
After Sprint 14g stabilizes (24-48h), grep the codebase for:
- `SELECT SUM/COUNT/MAX/MIN ... FROM ... WHERE` (look for missing date filters)
- `[:N]` and `LIMIT N` with hardcoded constants
- Loops over `range(N)` with hardcoded N
- `min_size=` / `max_size=` with hardcoded constants

---

## Pending operator actions

**None during the patience window.** Standing checks remain:
1. **+1 hour post-deploy:** confirm at least 1 ALERT converted to entry
   (proves daily kill switch unlatched)
2. **+24 hour post-deploy:** verify daily kill switch correctly resets
   at UTC midnight (skip_reason = `kill_switch_daily`, not `kill_switch_cumulative`)
3. **N=100 post-14f:** when post-14f settled cohort hits 100, run learning curve
4. **N=500 post-14f:** activation gate, ETA mid-May 2026

---

## Standing rules

1. Do NOT touch execution thresholds.
2. Do NOT lower the Brier < 0.05 ML promotion gate.
3. Do NOT flip `KELLY_SIZING_ENABLED` until both gates pass.
4. **Do NOT bypass the cumulative cap.** If we hit -$50 cumulative, the
   strategy is structurally broken and must be re-validated before trading
   resumes. Manually overriding the cap is forbidden.

---

## Lesson crystallized

Yesterday's status report claimed "thin sports schedule" as the cause of
zero entries. That was a narrative fallacy — the data didn't support
it (NBA playoffs, MLB, NHL all firing ALERT-level anomalies). The
operator's pushback ("something doesn't seem right here") forced a real
diagnostic. The lesson:

> **When the comfortable explanation doesn't fit the observable facts,
> the explanation is wrong, not the facts.**

This is the same lesson as the original 67-vs-153 discovery. Mature
quantitative practice requires being willing to discard a satisfying
story when it conflicts with measured reality. Three Sprint-14-era
discoveries in a row have followed exactly this pattern.
