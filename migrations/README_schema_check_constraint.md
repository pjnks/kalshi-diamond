# Migration: CHECK constraint on `paper_trades`

**Status:** ✅ **DEPLOYED 2026-04-20 04:42 UTC.** 805 rows rebuilt in <0.1s. CHECK constraint live on production DB. Smoke test confirmed constraint rejects inserts with `status='filled'` + `NULL fill_price`: `IntegrityError: CHECK constraint failed: check_valid_fill`.

**Historical note:** Initially deferred from hot-path deploy (2026-04-16) to a scheduled maintenance window. Executed 2026-04-20 after collaborator sign-off during low-frequency regime.

## Why this isn't deployed yet

The collaborator audit (2026-04-16) proposed locking the schema with:

```sql
ALTER TABLE paper_trades
ADD CONSTRAINT check_valid_fill
CHECK (
    status NOT IN ('filled', 'settled') 
    OR (fill_price IS NOT NULL AND fill_count > 0)
);
```

This is the correct end state, but **SQLite does not support `ALTER TABLE ADD CONSTRAINT`**. SQLite's `ALTER TABLE` is limited to `ADD COLUMN`, `RENAME TABLE`, and `RENAME COLUMN`. To add a CHECK constraint, the full table-rebuild dance is required:

1. `BEGIN TRANSACTION;`
2. Create `paper_trades_new` with the desired CHECK constraint
3. `INSERT INTO paper_trades_new SELECT * FROM paper_trades;`  (~790 rows today, scales with trade history)
4. `DROP TABLE paper_trades;`
5. `ALTER TABLE paper_trades_new RENAME TO paper_trades;`
6. Recreate indexes that existed on the original table
7. `COMMIT;`

On a 125 MB DB on the 1-OCPU Oracle Cloud VM, the `INSERT INTO ... SELECT *` for the full history is a **synchronous 30–60s operation**. Per Sprint 12's event-loop-starvation lessons (async-safe refactors in the monitor), we cannot run this inline in the hot path. It needs:

- Monitor stopped (brief maintenance window)
- Backup snapshot (`cp diamond_trades.db diamond_trades.db.pre-check-constraint.bak`)
- Migration script runs standalone
- Verification (row count pre/post, sample queries)
- Monitor restarted

## Runbook (for when we deploy)

### Prerequisites
- Confirm zero `stuck` rows exist (checked via `SELECT COUNT(*) FROM paper_trades WHERE status='stuck'`)
- Confirm zero rows violate the invariant: `SELECT COUNT(*) FROM paper_trades WHERE status IN ('filled','settled','voided') AND (fill_price IS NULL OR fill_count IS NULL OR fill_count <= 0)` should return 0
- Application-layer guard (`update_paper_fill` in `src/diamond_store.py`) has been in production for at least 48h without firing a CRITICAL log

### Execution

```bash
ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51

# 1. Stop monitor
sudo systemctl stop diamond-monitor

# 2. Backup
cd /home/ubuntu/kalshi-diamond
cp diamond_trades.db diamond_trades.db.pre-check-constraint.bak
ls -la diamond_trades.db*

# 3. Verify no violations in current data
/home/ubuntu/miniconda3/bin/python -c "
import sqlite3
c = sqlite3.connect('diamond_trades.db')
n = c.execute('''
    SELECT COUNT(*) FROM paper_trades
    WHERE status IN (\"filled\",\"settled\",\"voided\")
      AND (fill_price IS NULL OR fill_count IS NULL OR fill_count <= 0)
''').fetchone()[0]
print(f'violations: {n}')
assert n == 0, 'pre-migration violations must be zero'
"

# 4. Run migration (script below)
/home/ubuntu/miniconda3/bin/python /home/ubuntu/kalshi-diamond/migrations/apply_check_constraint.py --apply

# 5. Verify row count preserved
/home/ubuntu/miniconda3/bin/python -c "
import sqlite3
c = sqlite3.connect('diamond_trades.db')
print(c.execute('SELECT status, COUNT(*) FROM paper_trades GROUP BY status').fetchall())
"

# 6. Restart monitor
sudo systemctl start diamond-monitor
sudo systemctl status diamond-monitor

# 7. Tail logs for 5 min to confirm no regression
tail -f diamond_monitor.log
```

### Rollback

If the migration misbehaves:

```bash
sudo systemctl stop diamond-monitor
mv diamond_trades.db diamond_trades.db.failed-migration
mv diamond_trades.db.pre-check-constraint.bak diamond_trades.db
sudo systemctl start diamond-monitor
```

## Proposed migration SQL (draft)

```sql
BEGIN TRANSACTION;

-- 1. Create new table with CHECK constraint.
-- Note: the invariant is "any status that implies a fill must have valid fill data".
-- We do NOT include 'voided' here even though voided trades DID fill — the store
-- method mark_paper_voided() writes fill_price unchanged (preserves the original
-- fill), and we want the DB to enforce that voided rows came from a filled state.
-- 'stuck' is intentionally allowed to have NULL fill data — it represents
-- unrecoverable rows where we gave up.

CREATE TABLE paper_trades_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT,
    side TEXT,
    action TEXT,
    count INTEGER,
    entry_price INTEGER,
    fill_count INTEGER,
    fill_price INTEGER,
    anomaly_score REAL,
    anomaly_level TEXT,
    features TEXT,
    client_order_id TEXT,
    order_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    pnl_cents REAL,
    opened_at REAL NOT NULL,
    filled_at REAL,
    settled_at REAL,
    settlement TEXT,
    realized_edge REAL,
    event_id TEXT,
    category TEXT,
    ml_edge REAL,
    CONSTRAINT check_valid_fill CHECK (
        status NOT IN ('filled', 'settled', 'voided')
        OR (fill_price IS NOT NULL AND fill_price > 0
            AND fill_count IS NOT NULL AND fill_count > 0)
    )
);

-- 2. Copy all rows — the invariant will reject any violations (zero expected
--    post-backfill; see prerequisite check above).
INSERT INTO paper_trades_new SELECT * FROM paper_trades;

-- 3. Atomic swap
DROP TABLE paper_trades;
ALTER TABLE paper_trades_new RENAME TO paper_trades;

-- 4. Recreate indexes (check schema for full list first)
--    The live schema in DiamondStore._create_tables/_migrate establishes these
--    via `CREATE INDEX IF NOT EXISTS`, so they'll be auto-created on monitor
--    startup. But explicit recreation here avoids a warmup hole.
-- (add index statements based on current DB introspection)

COMMIT;

VACUUM;  -- reclaim space from dropped table. 125MB DB → expect ~125MB after.
```

## Why the defense-in-depth matters

Even with:
1. Line-544 bug fixed (the original source)
2. Application-layer guard in `update_paper_fill` (catches regressions)
3. DB-level CHECK constraint (planned here)

...we still want all three layers because:

- **Layer 1 (code)** can regress if someone copies the pattern again
- **Layer 2 (application)** only fires if code flows through `update_paper_fill`. Future code that bypasses this method (e.g., direct SQL in a migration, another store method added later) skips the guard
- **Layer 3 (schema)** is the last line. If both code and application layer fail, SQLite itself refuses the write. The transaction rolls back, and the caller gets a clear `IntegrityError: CHECK constraint failed` instead of silent data rot

This is Kent Beck's "defense in depth" pattern applied to data integrity. Each layer has different blind spots; all three layers have no shared blind spot.

## Open question (for a follow-up)

Should `voided` rows require valid fill data? Arguments both ways:

- **Yes** (current draft): voided trades DID fill (we bought, then the market refunded). So the fill data should still be present. `mark_paper_voided` in the store preserves it.
- **No**: If Kalshi refunded without ever having filled (cancelled-before-fill), the state is better represented as `cancelled` with NULL fill data. Our current code writes `unfilled` in that case, not `voided`, so the distinction is already drawn elsewhere. But a future caller might violate this.

**Recommendation:** keep the constraint strict (require fill data on `voided`), since `cancelled` and `unfilled` are the correct status values for "refunded without filling". If we ever see a legitimate voided-without-fill case, we'll revisit.
