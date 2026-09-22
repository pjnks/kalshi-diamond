# DIAMOND Session Handoff — 2026-05-06 (Sprint 14h)

**Status:** Sprint 14h deployed at **2026-05-06 02:12:12 UTC** (epoch
`1778033532`). Three-patch fix for silent task death. N=500 cohort reset.
Activation timeline shifts ~7 days right.

---

## What was wrong

`orderbook_poll_loop` had been silently dead for **~5 days**.

- **2026-05-01 03:09 UTC**: Sprint 14g restart. orderbook_poll_loop starts.
- **2026-05-01 18:14 UTC**: aiohttp session recycled (every 30 min via
  `SESSION_RECYCLE_SEC = 1800`). Recycle calls `await self._session.close()`
  which cancels in-flight requests. orderbook_poll_loop was mid-`await
  rest.get_orderbook()`. The cancellation raised `asyncio.CancelledError`
  inside the await.
- **CancelledError is `BaseException`, not `Exception`** (intentional in
  Python 3.8+ so cancellation can't be swallowed by generic handlers). The
  `except Exception` blocks did NOT catch it. Task died.
- `asyncio.gather(return_exceptions=True)` in `monitor()` suppressed the
  death notification.
- **No logs, no alert, no Pushover.** Other loops (metadata_refresh,
  status_report, paper_poll, resubscribe) survived because none had
  in-flight requests at the cancel moment.
- **2026-05-01 18:17 UTC**: last `book_snapshots` row written.
- **2026-05-03 18:27 UTC**: 2-day pruning catches up; table empty.
- **2026-05-04+**: `_book_absorption_stale = 1.0` on 100% of anomalies.
  The Sprint 14e canary fired correctly — but no human was watching.
- **2026-05-05 02:00 UTC**: User asked for daily status. Audit caught it.

---

## Diagnostic chain (evidence trail)

1. SQL query showed `_book_absorption_stale = 100%` over last 6h on 9,151
   anomalies; 0% nonzero. Same numbers as pre-Sprint-14f.
2. `book_snapshots` table empty (0 rows). PRUNE_DAYS=2 had wiped it.
3. `ss -tnp` on the daemon showed only **1 ESTABLISHED TCP connection**
   (the WebSocket) — no Kalshi REST connections.
4. `strace -c -f` for 5s across all threads showed ZERO `sendto`,
   `recvfrom`, `connect`, `getaddrinfo` syscalls. Only SQLite I/O
   (`pwrite64`, `pread64`, `fcntl`).
5. Direct Kalshi API test from a fresh script with same credentials:
   `get_orderbook` returned full book in <1s. **API was fine.**
6. py-spy dump showed only the WebSocket thread active. Other tasks were
   either awaiting (invisible) or dead.
7. Searched rotated `diamond_monitor.log.1` for orderbook activity —
   found `[ERROR] asyncio: Unclosed client session` at 18:14:00 on May 1.
8. Pruning history: snapshots dropped from 466 (May 3 18:17) to 0 (May 3
   18:27), bracketing the kill at May 1 18:17 ± 2 days.
9. Code review confirmed `except Exception:` pattern in
   `orderbook_poll_loop` and `metadata_refresh_loop` — both vulnerable.
10. No `CancelledError` or `BaseException` references anywhere in
    `kalshi_client.py` — no protection against this failure class.

---

## The fix (three patches, deployed atomically)

### Patch 1 — `orderbook_poll_loop` (`diamond_monitor.py:399`)

- Inner `try/except` catches `asyncio.CancelledError` separately, logs
  warning, continues
- Outer `try/except` distinguishes "real shutdown cancel" (`running=False`,
  propagate) from "stray cancel while running=True" (log, continue)
- Final `except BaseException` catches everything else
  (KeyboardInterrupt, SystemExit) — better to log + sleep than die
- Startup banner: `[ORDERBOOK_POLL] Loop started`
- Heartbeat every 10 iterations (~5 min): `[ORDERBOOK_POLL] iter=N
  written=W errors=E cancels=C tickers=T`

### Patch 2 — Top-level task death watchdog (`diamond_monitor.py:642`)

- `_task_death_logger(name)` returns a `done_callback`
- On task completion: distinguishes clean cancel (info log), unexpected
  death (CRITICAL log + Pushover priority=1), unexpected clean exit
  (error log)
- `_spawn(coro, name)` helper: `asyncio.create_task()` + attach callback
  in one step
- All 7 background tasks (metadata_refresh, orderbook_poll, status_report,
  paper_poll, ml_retrain, ws_with_subscribe, resubscribe) wrapped with
  `_spawn`
- **Structurally guarantees silent task death is impossible going forward**

### Patch 3 — `metadata_refresh_loop` (`diamond_monitor.py:370`)

Same vulnerability (HTTP via `refresh_markets` → `rest._request`), same fix.

---

## Cohort reset (Sprint 14h)

| Cohort | Epoch range | Trades | Reason for purge |
|---|---|---|---|
| Pre-14e | (anything) → 2026-04-28 03:27 | 290 | Universe contamination |
| Post-14e/Pre-14f | Apr 28 03:27 → Apr 29 03:04 | 50 | Orderbook poll cap broken |
| **Post-14f/Pre-14h** | **Apr 29 03:04 → May 6 02:12** | **393** | **~83% had zeroed absorption (this fix)** |
| Post-14h | May 6 02:12 → onward | 0 (counting from now) | CURRENT |

**Total cohort cost:** 733 trades purged across Sprints 14e/14f/14h.

**N=500 ETA:** ~May 13-16, 2026 at sustained 70-100/day tempo.

**Activation timeline:** ~4 weeks from May 6 = early June 2026 for full
Kelly activation (assuming first retrain near-miss + second retrain at
N=750-1000 passes Brier < 0.05 + Jaccard ≥ 0.70).

---

## Verification protocol (post-deploy)

1. **+1 min**: `[ORDERBOOK_POLL] Loop started` log line appears
2. **+2 min**: `[METADATA_REFRESH] Loop started` log line appears
3. **+90s**: First heartbeat `[ORDERBOOK_POLL] iter=1 written=N` appears
4. **+5 min**: `book_snapshots` table has >100 rows; >50 distinct tickers
5. **+30 min**: First aiohttp session recycle. Look for `[ORDERBOOK_POLL]
   Request cancelled for X (likely aiohttp session recycle); continuing`
   — first session recycle should be survived gracefully
6. **+24h**: stale rate on post-14h anomalies < 5%
7. **+48h**: nonzero absorption on >95% of anomalies

---

## Files touched

| File | Change |
|---|---|
| `diamond_monitor.py` | Patches 1+2+3 (~150 lines added) |
| `learning_curve.py` | Default `--min-opened-at` = `1778033532.0` |
| `CLAUDE.md` | Sprint 14h section, Patience Discipline, Sandbox Artifact table |

VM backup of pre-deploy state retained: `diamond_monitor.py.pre-sprint14h.bak`

---

## Standing rules (unchanged)

1. Do NOT touch execution thresholds.
2. Do NOT lower the Brier < 0.05 ML promotion gate.
3. Do NOT flip `KELLY_SIZING_ENABLED` until both gates pass.
4. Do NOT bypass the cumulative -$50 cap.
5. Do NOT intervene on long-dated tail trades (Trump @ 8c etc).

---

## Backlog: formal Sandbox Artifact Audit

Four bugs of the same archetype in 9 days (14e/14f/14g/14h). Schedule
systematic grep for:

- Hardcoded `[:N]` slices and `LIMIT N` constants
- Unbounded `SELECT SUM/COUNT/MAX/MIN` without date filters
- `except Exception` around `await` calls (CancelledError can escape)
- Loops over `range(N)` with hardcoded N
- `min_size=` / `max_size=` with hardcoded constants

Run after Sprint 14h stabilizes (give it 48h to confirm the absorption
pipeline holds steady).

---

## Lesson crystallized

> **Monitoring presence is not enough. You must monitor absence.**
> The service was "running" for 5 days while one of its most important
> loops was dead. systemd, journal, watchdog, and dashboards all reported
> green. The only signal was the absorption-stale telemetry — which
> required a human to look at it. Sprint 14h's task death watchdog
> closes that loop: future task deaths fire a Pushover alert within
> seconds, regardless of whether anyone is paying attention.

The previous lessons (14e/14f/14g) crystallized: "code correct at
small N silently fails at production scale." Sprint 14h adds a corollary:
"silent failures at production scale are inevitable; observability against
absence is mandatory."
