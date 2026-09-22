# DIAMOND Session Handoff — 2026-04-28

**Status:** Sprint 14e fully deployed. Regime cutoff established. Patience window
restarted with new gate criteria.

---

## What changed since 2026-04-26 handoff

### Phase 1 (subscription fix) — confirmed working at scale
- 24h after deploy, log showed `Status: ... (+713/min)` trade flow (vs the
  ~0/min "Kalshi is quiet" diagnosis pre-fix).
- WS subscriptions reached 257 tickers vs 67 pre-fix (4× expansion).
- `[RESUB] WS subscriptions synced: +N new, -M expired` log entries firing
  every 5 min as designed — the resubscribe loop is keeping up with
  Kalshi's market-universe churn.

### Phase 3 (book absorption metrics) — DEPLOYED 2026-04-28 03:27:33 UTC
- Code shipped via `./deploy.sh --restart`. Service active, no startup errors.
- All 5 expected keys appearing in `features_json` on live anomalies:
  `book_absorption_static`, `book_absorption_depletion`, `book_absorption_sided`,
  `book_absorption_replenish`, `_book_absorption_stale`.
- First post-restart anomaly showed `_book_absorption_stale: 1.0` (no
  60s-prior snapshot for that brand-new MLB ticker) — designed behavior,
  flag working as kill-switch telemetry. Will drop to 0 as snapshot
  history accumulates per-ticker over 5-10 min.

### Learning curve at N=340 — Brier monotonically rising
| N (post-Sprint-11) | Brier | AUC | Δ vs naive |
|---|---|---|---|
| 250 | 0.1923 | 0.761 | −0.058 |
| 290 | 0.2087 | 0.744 | −0.041 |
| **340** | **0.2291** | **0.714** | **−0.020** |

Diagnosed as **regime-mixing contamination**, not feature-IC failure.
Pre-14e cohort (67-ticker universe) and post-14e cohort (257+-ticker universe)
have different distributions; training a single Ridge regression across both
violates stationarity.

---

## NEW GATE: N=500 post-Sprint-14e (DECISIVE)

**Old gate (OBSOLETE):** N=500 post-Sprint-11 (Apr 2 onwards)
**New gate:** N=500 post-Sprint-14e (Apr 28, 2026, 03:27:33 UTC onwards)

Unix epoch: `1777346853`

### Cohort definitions
- **Pre-14e Cohort** (290 trades, opened Apr 2 → Apr 28 03:27:32 UTC):
  Constrained 67-ticker universe. Phase 3 absorption features structurally
  zero (didn't exist yet). **Purged from final ML training set.**
  Retained for historical analysis only.
- **Post-14e Cohort** (counting starts at 0 on Apr 28 03:27:33 UTC):
  Full 257+-ticker universe. All Phase 3 absorption variants populating.
  This is the canonical training cohort for the N=500 retrain.

### Timeline
- ETA at sustained ~50/day tempo: **~10 days**, gate hits ~May 8, 2026.
- Pre-fix would have been mid-July (10 weeks). Subscription fix saved 9 weeks.
- Velocity caveat: 50/day may be partly backlog clearing from the universe
  expansion. If post-14e tempo stabilizes lower (25-30/day), gate slides
  to mid-May.

---

## Monitoring protocol (active)

### 48-hour check (~2026-04-30 03:27 UTC)
Verify the absorption pipeline is producing real values:

```bash
scp -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51:/home/ubuntu/kalshi-diamond/diamond_trades.db /tmp/diamond_trades_check.db

/Users/perryjenkins/opt/anaconda3/bin/python -c "
import sqlite3, json
conn = sqlite3.connect('/tmp/diamond_trades_check.db')
c = conn.cursor()
c.execute('SELECT features FROM anomalies WHERE created_at > 1777346853 AND alert_level != \"NONE\" ORDER BY created_at DESC LIMIT 200')
stale_count = total = 0
for (feat_json,) in c.fetchall():
    f = json.loads(feat_json) if feat_json else {}
    if 'book_absorption_static' in f:
        total += 1
        if f.get('_book_absorption_stale', 0) >= 0.5:
            stale_count += 1
print(f'Stale rate: {stale_count}/{total} = {stale_count/total*100:.1f}%' if total else 'No data')
"
```

**PASS:** stale rate < 5% — pipeline producing real values.
**FAIL:** stale rate > 20% — book-snapshot pipeline degraded; investigate.

### N=100 check (~2026-04-30 to 2026-05-02)
Re-run learning curve on the **isolated post-14e cohort**:

```bash
PYTHONPATH=. /Users/perryjenkins/opt/anaconda3/bin/python learning_curve.py \
  --db /tmp/diamond_trades_check.db \
  --min-opened-at 1777346853 \
  --grid 50,100
```

**PASS:** Brier descends from N=50 to N=100, OR stays stable in 0.15-0.22 range.
**WARN:** Brier still rising. If it's worse than the pre-14e curve, the
regime-mixing diagnosis is wrong and we have a deeper feature problem.
**FAIL:** Brier > 0.30 → call a feature-engineering reset before continuing.

### Standing rules (no action required)
1. Do NOT touch execution thresholds (composite, ALERT, CRITICAL).
2. Do NOT lower the Brier < 0.05 ML promotion gate.
3. Do NOT flip `KELLY_SIZING_ENABLED` until both gates pass.
4. Continue weekly learning curve sweeps.

---

## Active state

- **Service:** `diamond-monitor` running since 2026-04-28 03:27:33 UTC.
  RAM ~120MB / 600MB. Restart count: 2 since pre-fix baseline.
- **Open positions** (5 as of last check):
  - KXNBAGAME-26APR27DETORL-ORL no @ 46¢
  - KXMLBGAME-26APR271940SEAMIN-MIN yes @ 55¢
  - KXNBAGAME-26APR27OKCPHX-PHX yes @ 19¢
  - KXSPOTIFYGLOBALD-26APR27-MAN no @ 43¢
  - KXTRUMPOUT27-27-26AUG01 yes @ 8¢ (still riding from Apr 24)
- **Cumulative P&L:** −$12.11 across 782 settled.
- **Data integrity:** orphans=0, L2 firings=0, L3 CHECK violations=0.

---

## Pending operator actions

None.

The patience window has begun (again). Daemon runs. Post-14e cohort accumulates.
Next session checkpoint: 2026-04-30 (48h check + N=100 learning curve).
