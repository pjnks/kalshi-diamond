#!/usr/bin/env python3
"""
Sprint 14 Layer 3 Migration: Add CHECK constraint to paper_trades.

Rebuilds paper_trades with:
    CONSTRAINT check_valid_fill CHECK (
        status NOT IN ('filled', 'settled', 'voided')
        OR (fill_price IS NOT NULL AND fill_price > 0
            AND fill_count IS NOT NULL AND fill_count > 0)
    )

SQLite has no ALTER TABLE ADD CONSTRAINT, so this is a 5-step rebuild:
    1. CREATE TABLE paper_trades_new (full schema + CHECK)
    2. INSERT INTO paper_trades_new SELECT * FROM paper_trades
    3. DROP TABLE paper_trades
    4. RENAME paper_trades_new TO paper_trades
    5. Recreate indexes

Runs as a single transaction. If any step fails, the original table survives.

USAGE:
    # Dry run (prints plan, no writes):
    python apply_check_constraint.py

    # Apply (destructive):
    python apply_check_constraint.py --apply

PRECONDITIONS:
    - diamond-monitor STOPPED (no concurrent writers)
    - DB backed up to diamond_trades.db.pre-check-constraint.bak
    - Preflight check: zero rows violate the new constraint
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


DB_PATH = Path("/home/ubuntu/kalshi-diamond/diamond_trades.db")

# Match live schema exactly (introspected 2026-04-20 via PRAGMA table_info).
# Column order matters: INSERT INTO new SELECT * FROM old relies on identical order.
CREATE_NEW = """
CREATE TABLE paper_trades_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,
    client_order_id TEXT,
    ticker          TEXT NOT NULL,
    title           TEXT,
    side            TEXT NOT NULL,
    action          TEXT NOT NULL DEFAULT 'buy',
    count           INTEGER NOT NULL DEFAULT 1,
    entry_price     INTEGER,
    fill_price      INTEGER,
    fill_count      INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    settlement      TEXT,
    pnl_cents       REAL,
    anomaly_score   REAL,
    anomaly_level   TEXT,
    features_json   TEXT,
    opened_at       REAL NOT NULL,
    filled_at       REAL,
    settled_at      REAL,
    event_id        TEXT,
    category        TEXT,
    ml_edge         REAL,
    realized_edge   REAL,
    CONSTRAINT check_valid_fill CHECK (
        status NOT IN ('filled', 'settled', 'voided')
        OR (fill_price IS NOT NULL AND fill_price > 0
            AND fill_count IS NOT NULL AND fill_count > 0)
    )
)
"""

# Indexes to recreate post-rebuild (introspected from sqlite_master).
REBUILD_INDEXES = [
    "CREATE INDEX idx_paper_ticker ON paper_trades(ticker)",
    "CREATE INDEX idx_paper_status ON paper_trades(status)",
]


def preflight(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (row_count, violation_count). Aborts if violations > 0."""
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    violations = cur.execute(
        """
        SELECT COUNT(*) FROM paper_trades
        WHERE status IN ('filled','settled','voided')
          AND (fill_price IS NULL OR fill_price <= 0
               OR fill_count IS NULL OR fill_count <= 0)
        """
    ).fetchone()[0]

    return total, violations


def run_migration(conn: sqlite3.Connection) -> None:
    """Execute the 5-step rebuild inside a single transaction."""
    cur = conn.cursor()

    t0 = time.monotonic()
    print("Step 1/5: CREATE TABLE paper_trades_new (with CHECK)...")
    cur.execute(CREATE_NEW)

    print("Step 2/5: INSERT INTO paper_trades_new SELECT * FROM paper_trades...")
    cur.execute("INSERT INTO paper_trades_new SELECT * FROM paper_trades")
    copied = cur.rowcount
    print(f"    {copied} rows copied in {time.monotonic()-t0:.1f}s")

    print("Step 3/5: DROP TABLE paper_trades...")
    cur.execute("DROP TABLE paper_trades")

    print("Step 4/5: RENAME paper_trades_new -> paper_trades...")
    cur.execute("ALTER TABLE paper_trades_new RENAME TO paper_trades")

    print("Step 5/5: Recreate indexes...")
    for ddl in REBUILD_INDEXES:
        print(f"    {ddl}")
        cur.execute(ddl)

    total_elapsed = time.monotonic() - t0
    print(f"Migration complete in {total_elapsed:.1f}s")


def verify(conn: sqlite3.Connection, expected_rows: int) -> None:
    """Post-migration sanity checks."""
    cur = conn.cursor()

    # Row count preserved
    actual = cur.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    print(f"\nPost-migration row count: {actual} (expected {expected_rows})")
    assert actual == expected_rows, "row count MISMATCH — rollback"

    # Status breakdown unchanged
    print("Status counts:")
    for s, n in cur.execute("SELECT status, COUNT(*) FROM paper_trades GROUP BY status").fetchall():
        print(f"  {s}: {n}")

    # Indexes present
    idx = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='paper_trades'"
    ).fetchall()]
    print(f"Indexes: {idx}")
    assert "idx_paper_ticker" in idx, "idx_paper_ticker missing"
    assert "idx_paper_status" in idx, "idx_paper_status missing"

    # CHECK constraint present
    schema = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_trades'"
    ).fetchone()[0]
    assert "check_valid_fill" in schema, "CHECK constraint missing from rebuilt schema"
    print("CHECK constraint present: ✓")

    # Smoke test: does the constraint actually fire?
    try:
        cur.execute(
            "INSERT INTO paper_trades (ticker, side, status, opened_at, fill_price, fill_count) "
            "VALUES ('__TEST__', 'yes', 'filled', strftime('%s','now'), NULL, NULL)"
        )
        cur.execute("DELETE FROM paper_trades WHERE ticker='__TEST__'")
        raise AssertionError("CHECK constraint did NOT reject NULL fill_price on filled row")
    except sqlite3.IntegrityError as e:
        print(f"CHECK smoke test: constraint correctly rejected bad row ({e})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually perform the migration (default: dry-run)")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # Safety for RENAME

    # ── Preflight ──────────────────────────────────────────
    print("=== PREFLIGHT ===")
    total, viol = preflight(conn)
    print(f"Total rows: {total}")
    print(f"Constraint violations in current data: {viol}")
    if viol > 0:
        print("\nABORT: existing rows would violate the new constraint.")
        return 1

    # ── Dry-run check ───────────────────────────────────────
    if not args.apply:
        print("\n(dry-run — pass --apply to execute)")
        print("\nWould execute:")
        print(CREATE_NEW)
        for ddl in REBUILD_INDEXES:
            print(ddl + ";")
        return 0

    # ── Apply ───────────────────────────────────────────────
    print("\n=== APPLY ===")
    try:
        conn.execute("BEGIN TRANSACTION")
        run_migration(conn)
        conn.commit()
        print("\nTransaction committed.")
    except Exception as e:
        conn.rollback()
        print(f"\nMIGRATION FAILED, rolled back: {e}")
        return 1

    # ── Verify ──────────────────────────────────────────────
    print("\n=== VERIFY ===")
    verify(conn, expected_rows=total)

    conn.close()
    print("\nDone. Monitor restart is safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
