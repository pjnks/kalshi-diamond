"""
diamond_store.py
────────────────
SQLite storage for DIAMOND — trades, anomalies, market profiles, order book snapshots.

All writes are synchronous (SQLite is fast enough for our throughput).
Rolling aggregates computed via SQL for efficiency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from diamond_config import DB_PATH, PRUNE_DAYS, ROLLING_WINDOW_1H, ROLLING_WINDOW_24H, PAPER_TRADING_ENABLED

log = logging.getLogger(__name__)


class DiamondStore:
    """SQLite-backed storage for trades, anomalies, and market profiles."""

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = str(db_path or DB_PATH)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        """Open database connection and enforce strict memory pragmas."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row

        # WAL mode for concurrent read/write
        self._conn.execute("PRAGMA journal_mode=WAL")

        # Relax sync to reduce disk I/O blocking the async event loop
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # Enable mmap for SQLite — reduces pread64 syscall overhead.
        # Default is 0 (disabled) on this platform. Set to 256MB.
        self._conn.execute("PRAGMA mmap_size=268435456")

        self._create_tables()
        self._migrate()
        log.info(f"DiamondStore connected: {self._db_path}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id    TEXT,
                ticker      TEXT NOT NULL,
                count       INTEGER NOT NULL,
                yes_price   REAL,
                no_price    REAL,
                taker_side  TEXT,
                ts          REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

            CREATE TABLE IF NOT EXISTS anomalies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                title       TEXT,
                score       REAL NOT NULL,
                features    TEXT NOT NULL,
                alert_level TEXT NOT NULL,
                ts          REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_anomalies_ticker_ts ON anomalies(ticker, ts);
            CREATE INDEX IF NOT EXISTS idx_anomalies_score ON anomalies(score);
            CREATE INDEX IF NOT EXISTS idx_anomalies_ts ON anomalies(ts);

            CREATE TABLE IF NOT EXISTS market_profiles (
                ticker      TEXT PRIMARY KEY,
                title       TEXT,
                mean_size   REAL NOT NULL DEFAULT 0,
                std_size    REAL NOT NULL DEFAULT 1,
                volume_24h  INTEGER NOT NULL DEFAULT 0,
                trade_count_24h INTEGER NOT NULL DEFAULT 0,
                avg_hourly_volume REAL NOT NULL DEFAULT 0,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                yes_bids    TEXT,
                yes_asks    TEXT,
                no_bids     TEXT,
                no_asks     TEXT,
                ts          REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_book_ticker_ts ON book_snapshots(ticker, ts);
        """)
        self._conn.commit()

    def _migrate(self):
        """Apply schema migrations for existing databases."""
        # Add title column to anomalies if it doesn't exist
        cols = [row[1] for row in self._conn.execute("PRAGMA table_info(anomalies)").fetchall()]
        if "title" not in cols:
            self._conn.execute("ALTER TABLE anomalies ADD COLUMN title TEXT")
            self._conn.commit()
            log.info("Migration: added 'title' column to anomalies table")

        # Add title column to market_profiles if it doesn't exist
        prof_cols = [row[1] for row in self._conn.execute("PRAGMA table_info(market_profiles)").fetchall()]
        if "title" not in prof_cols:
            self._conn.execute("ALTER TABLE market_profiles ADD COLUMN title TEXT")
            self._conn.commit()
            log.info("Migration: added 'title' column to market_profiles table")

        # Create paper_trades table if it doesn't exist
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
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
                settled_at      REAL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_ticker ON paper_trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
        """)
        self._conn.commit()

        # Add event_id and category columns to paper_trades
        pt_cols = [row[1] for row in self._conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
        if "category" not in pt_cols:
            self._conn.execute("ALTER TABLE paper_trades ADD COLUMN category TEXT")
            self._conn.commit()
            log.info("Migration: added 'category' column to paper_trades table")
        if "event_id" not in pt_cols:
            self._conn.execute("ALTER TABLE paper_trades ADD COLUMN event_id TEXT")
            self._conn.commit()
            log.info("Migration: added 'event_id' column to paper_trades table")
        if "ml_edge" not in pt_cols:
            self._conn.execute("ALTER TABLE paper_trades ADD COLUMN ml_edge REAL")
            self._conn.commit()
            log.info("Migration: added 'ml_edge' column to paper_trades table")
        if "realized_edge" not in pt_cols:
            self._conn.execute("ALTER TABLE paper_trades ADD COLUMN realized_edge REAL")
            self._conn.commit()
            log.info("Migration: added 'realized_edge' column to paper_trades table")

        # Create conviction_signals table for persisting conviction state
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS conviction_signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                score       REAL NOT NULL,
                ts          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conviction_event_ts ON conviction_signals(event_id, ts);
        """)
        self._conn.commit()

        # Create skipped_trades table for dashboard visibility
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS skipped_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                title           TEXT,
                side            TEXT,
                price_cents     INTEGER,
                anomaly_score   REAL,
                anomaly_level   TEXT,
                skip_reason     TEXT NOT NULL,
                detail          TEXT,
                ts              REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_skipped_ts ON skipped_trades(ts);
        """)
        self._conn.commit()

    # ── Trade Operations ──────────────────────────────────────────────

    def insert_trade(self, trade: dict):
        """Insert a single trade record."""
        self._conn.execute(
            """INSERT INTO trades (trade_id, ticker, count, yes_price, no_price, taker_side, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.get("trade_id", ""),
                trade["ticker"],
                trade.get("count", trade.get("contracts", 1)),
                trade.get("yes_price"),
                trade.get("no_price"),
                trade.get("taker_side", ""),
                trade.get("ts", trade.get("created_time", time.time())),
            ),
        )
        self._conn.commit()

    def insert_trades_batch(self, trades: list[dict]):
        """Batch insert trades."""
        self._conn.executemany(
            """INSERT INTO trades (trade_id, ticker, count, yes_price, no_price, taker_side, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    t.get("trade_id", ""),
                    t["ticker"],
                    t.get("count", t.get("contracts", 1)),
                    t.get("yes_price"),
                    t.get("no_price"),
                    t.get("taker_side", ""),
                    t.get("ts", t.get("created_time", time.time())),
                )
                for t in trades
            ],
        )
        self._conn.commit()

    def get_trades_since(self, ticker: str, since_ts: float) -> list[dict]:
        """Get trades for a ticker since a timestamp."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE ticker = ? AND ts >= ? ORDER BY ts LIMIT 10000",
            (ticker, since_ts),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_trades(self, ticker: str, window_sec: int = ROLLING_WINDOW_1H) -> list[dict]:
        """Get trades within a rolling window."""
        cutoff = time.time() - window_sec
        return self.get_trades_since(ticker, cutoff)

    # ── Rolling Aggregates ────────────────────────────────────────────

    def get_trade_stats(self, ticker: str, window_sec: int = ROLLING_WINDOW_24H) -> dict:
        """Compute mean/std of trade size for a ticker over a window."""
        cutoff = time.time() - window_sec
        row = self._conn.execute(
            """SELECT
                 COUNT(*) as n,
                 COALESCE(AVG(count), 0) as mean_size,
                 COALESCE(
                   CASE WHEN COUNT(*) > 1
                     THEN SQRT(SUM((count - sub.avg_c) * (count - sub.avg_c)) / (COUNT(*) - 1))
                     ELSE 1
                   END, 1
                 ) as std_size,
                 COALESCE(SUM(count), 0) as total_volume
               FROM trades,
                    (SELECT AVG(count) as avg_c FROM trades WHERE ticker = ? AND ts >= ?) sub
               WHERE ticker = ? AND ts >= ?""",
            (ticker, cutoff, ticker, cutoff),
        ).fetchone()
        return dict(row) if row else {"n": 0, "mean_size": 0, "std_size": 1, "total_volume": 0}

    def get_hourly_volume(self, ticker: str, hours_back: int = 24) -> list[dict]:
        """Get hourly volume buckets for a ticker."""
        cutoff = time.time() - hours_back * 3600
        rows = self._conn.execute(
            """SELECT
                 CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
                 SUM(count) as volume,
                 COUNT(*) as trade_count
               FROM trades
               WHERE ticker = ? AND ts >= ?
               GROUP BY hour_ts
               ORDER BY hour_ts
               LIMIT 10000""",
            (ticker, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_volume_in_window(self, ticker: str, window_sec: int) -> int:
        """Get total volume (sum of counts) in a time window."""
        cutoff = time.time() - window_sec
        row = self._conn.execute(
            "SELECT COALESCE(SUM(count), 0) as vol FROM trades WHERE ticker = ? AND ts >= ?",
            (ticker, cutoff),
        ).fetchone()
        return row["vol"]

    def get_taker_side_ratio(self, ticker: str, window_sec: int = ROLLING_WINDOW_1H) -> float:
        """Get yes-taker ratio in window. Returns 0.5 if no data."""
        cutoff = time.time() - window_sec
        row = self._conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN taker_side = 'yes' THEN count ELSE 0 END), 0) as yes_vol,
                 COALESCE(SUM(count), 0) as total_vol
               FROM trades
               WHERE ticker = ? AND ts >= ?""",
            (ticker, cutoff),
        ).fetchone()
        total = row["total_vol"]
        if total == 0:
            return 0.5
        return row["yes_vol"] / total

    # ── Market Profiles ───────────────────────────────────────────────

    def update_market_profile(self, ticker: str, title: str = ""):
        """Recompute and store rolling market profile."""
        stats = self.get_trade_stats(ticker)
        hourly = self.get_hourly_volume(ticker)
        avg_hourly = sum(h["volume"] for h in hourly) / max(len(hourly), 1)

        self._conn.execute(
            """INSERT INTO market_profiles (ticker, title, mean_size, std_size, volume_24h, trade_count_24h, avg_hourly_volume, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 title = COALESCE(excluded.title, market_profiles.title),
                 mean_size = excluded.mean_size,
                 std_size = excluded.std_size,
                 volume_24h = excluded.volume_24h,
                 trade_count_24h = excluded.trade_count_24h,
                 avg_hourly_volume = excluded.avg_hourly_volume,
                 updated_at = excluded.updated_at""",
            (ticker, title or None, stats["mean_size"], stats["std_size"], stats["total_volume"],
             stats["n"], avg_hourly, time.time()),
        )
        self._conn.commit()

    def get_market_profile(self, ticker: str) -> dict | None:
        """Get cached market profile."""
        row = self._conn.execute(
            "SELECT * FROM market_profiles WHERE ticker = ?", (ticker,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_active_tickers(self) -> list[str]:
        """Get tickers that have had trades in the last 24h."""
        cutoff = time.time() - ROLLING_WINDOW_24H
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM trades WHERE ts >= ? ORDER BY ticker",
            (cutoff,),
        ).fetchall()
        return [r["ticker"] for r in rows]

    # ── Anomalies ─────────────────────────────────────────────────────

    def insert_anomaly(self, ticker: str, score: float, features: dict,
                       alert_level: str, title: str = ""):
        """Record a detected anomaly."""
        self._conn.execute(
            """INSERT INTO anomalies (ticker, title, score, features, alert_level, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, title or None, score, json.dumps(features), alert_level, time.time()),
        )
        self._conn.commit()

    def get_recent_anomalies(self, limit: int = 50) -> list[dict]:
        """Get most recent anomalies across all markets."""
        rows = self._conn.execute(
            "SELECT * FROM anomalies ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d["features"])
            result.append(d)
        return result

    def get_anomalies_for_ticker(self, ticker: str, hours_back: int = 24) -> list[dict]:
        """Get anomalies for a specific ticker."""
        cutoff = time.time() - hours_back * 3600
        rows = self._conn.execute(
            "SELECT * FROM anomalies WHERE ticker = ? AND ts >= ? ORDER BY ts DESC LIMIT 10000",
            (ticker, cutoff),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d["features"])
            result.append(d)
        return result

    def count_recent_anomalies(self, window_sec: int = 300) -> dict[str, int]:
        """Count anomalies per ticker in a window (for cross-market correlation)."""
        cutoff = time.time() - window_sec
        rows = self._conn.execute(
            """SELECT ticker, COUNT(*) as cnt
               FROM anomalies WHERE ts >= ?
               GROUP BY ticker""",
            (cutoff,),
        ).fetchall()
        return {r["ticker"]: r["cnt"] for r in rows}

    # ── Order Book Snapshots ──────────────────────────────────────────

    def insert_book_snapshot(self, ticker: str, orderbook: dict):
        """
        Store an order book snapshot.

        Kalshi API format: {"orderbook_fp": {"yes_dollars": [[price, qty], ...], "no_dollars": [...]}}
        or the inner dict directly: {"yes_dollars": [...], "no_dollars": [...]}
        """
        # Handle both wrapped and unwrapped formats
        book = orderbook.get("orderbook_fp", orderbook)
        yes_levels = book.get("yes_dollars", book.get("yes", {}).get("bids", []))
        no_levels = book.get("no_dollars", book.get("no", {}).get("bids", []))

        self._conn.execute(
            """INSERT INTO book_snapshots (ticker, yes_bids, yes_asks, no_bids, no_asks, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                json.dumps(yes_levels),
                "[]",  # Kalshi combines bids+asks into one list per side
                json.dumps(no_levels),
                "[]",
                time.time(),
            ),
        )
        self._conn.commit()

    def get_latest_book(self, ticker: str) -> dict | None:
        """Get most recent order book snapshot."""
        row = self._conn.execute(
            "SELECT * FROM book_snapshots WHERE ticker = ? ORDER BY ts DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["yes_bids"] = json.loads(d["yes_bids"])
        d["yes_asks"] = json.loads(d["yes_asks"])
        d["no_bids"] = json.loads(d["no_bids"])
        d["no_asks"] = json.loads(d["no_asks"])
        return d

    # ── Conviction Persistence ────────────────────────────────────────

    def insert_conviction_signal(self, event_id: str, ticker: str, score: float, ts: float):
        """Persist a conviction signal to SQLite."""
        self._conn.execute(
            "INSERT INTO conviction_signals (event_id, ticker, score, ts) VALUES (?, ?, ?, ?)",
            (event_id, ticker, score, ts),
        )
        self._conn.commit()

    def get_recent_conviction_signals(self, max_age_sec: float = 3600) -> list[dict]:
        """Load recent conviction signals for state restoration on startup."""
        cutoff = time.time() - max_age_sec
        rows = self._conn.execute(
            "SELECT event_id, ticker, score, ts FROM conviction_signals WHERE ts >= ? ORDER BY ts LIMIT 10000",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_conviction_signals(self, max_age_sec: float = 7200):
        """Remove old conviction signals."""
        cutoff = time.time() - max_age_sec
        self._conn.execute("DELETE FROM conviction_signals WHERE ts < ?", (cutoff,))
        self._conn.commit()

    # ── Order Book Snapshots ──────────────────────────────────────────

    def get_book_snapshot_at(self, ticker: str, target_ts: float) -> dict | None:
        """Get the book snapshot closest to (but not after) target_ts.

        Used for computing book pressure delta — comparing current book
        to the book state N minutes ago.
        """
        row = self._conn.execute(
            "SELECT * FROM book_snapshots WHERE ticker = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (ticker, target_ts),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["yes_bids"] = json.loads(d["yes_bids"])
        d["yes_asks"] = json.loads(d["yes_asks"])
        d["no_bids"] = json.loads(d["no_bids"])
        d["no_asks"] = json.loads(d["no_asks"])
        return d

    # ── Paper Trades ─────────────────────────────────────────────────

    def insert_skipped_trade(
        self,
        ticker: str,
        title: str,
        side: str | None,
        price_cents: int | None,
        anomaly_score: float,
        anomaly_level: str,
        skip_reason: str,
        detail: str = "",
    ):
        """Record a skipped trade for dashboard visibility."""
        self._conn.execute(
            """INSERT INTO skipped_trades
               (ticker, title, side, price_cents, anomaly_score, anomaly_level, skip_reason, detail, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, title, side, price_cents, anomaly_score, anomaly_level, skip_reason, detail, time.time()),
        )
        self._conn.commit()

    def get_recent_skipped_trades(self, limit: int = 200) -> list[dict]:
        """Get recent skipped trades for dashboard display."""
        rows = self._conn.execute(
            "SELECT * FROM skipped_trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_paper_trade(
        self,
        ticker: str,
        title: str,
        side: str,
        action: str,
        count: int,
        entry_price: int,
        anomaly_score: float,
        anomaly_level: str,
        features: dict,
        order_id: str | None = None,
        client_order_id: str | None = None,
        event_id: str | None = None,
        category: str | None = None,
    ) -> int:
        """Insert a new paper trade record. Returns the row ID."""
        ml_edge = features.get("ml_edge")
        cur = self._conn.execute(
            """INSERT INTO paper_trades
               (order_id, client_order_id, ticker, title, side, action, count,
                entry_price, anomaly_score, anomaly_level, features_json, opened_at, status,
                event_id, category, ml_edge)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (order_id, client_order_id, ticker, title, side, action, count,
             entry_price, anomaly_score, anomaly_level, json.dumps(features), time.time(),
             event_id, category, ml_edge),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_paper_fill(self, row_id: int, order_id: str, fill_price: int, fill_count: int, status: str = "filled"):
        """Update a paper trade after order response.

        Enforces the mathematical invariant: a row with status='filled' MUST have
        a positive fill_price and fill_count. Any caller passing corrupt combos
        (None, 0, negative) with status='filled' is downgraded to 'unfilled'
        with a CRITICAL log so the data contamination surfaces loudly instead
        of silently rotting the DB (as happened Mar 29 – Apr 10, producing 9
        orphan rows that jammed the settlement poll for 16.5 days).

        Note: status='pending' and status='unfilled' legitimately pass
        fill_price=0/fill_count=0 — those are sentinel values for "nothing
        has filled yet". The guard only applies to status='filled'.
        """
        # Integrity guard: reject corrupt 'filled' writes at the boundary.
        if status == "filled":
            is_corrupt = (
                fill_price is None
                or not isinstance(fill_price, (int, float))
                or fill_price <= 0
                or fill_count is None
                or not isinstance(fill_count, (int, float))
                or fill_count <= 0
            )
            if is_corrupt:
                import logging as _logging
                _logging.getLogger(__name__).critical(
                    f"[STORE] REJECTED CORRUPT FILL: row_id={row_id} order_id={order_id!r} "
                    f"attempted status='filled' with fill_price={fill_price!r} "
                    f"fill_count={fill_count!r}. Downgrading to 'unfilled' to preserve "
                    f"data integrity. Check upstream caller — this should not happen."
                )
                status = "unfilled"
                fill_price = 0
                fill_count = 0

        self._conn.execute(
            """UPDATE paper_trades
               SET order_id = ?, fill_price = ?, fill_count = ?, status = ?, filled_at = ?
               WHERE id = ?""",
            (order_id, fill_price, fill_count, status, time.time(), row_id),
        )
        self._conn.commit()

    def update_paper_settlement(self, row_id: int, settlement: str, pnl_cents: float):
        """Update a paper trade when its market settles.

        Also computes realized_edge = settlement_value - fill_price (in cents).
        This tracks adverse selection: if realized_edge is systematically worse
        than predicted edge (ml_edge), we're paying adverse selection.
        """
        # Compute realized edge: settlement value - fill price
        # settlement_value = 100 if we bet YES and outcome is YES (or NO and outcome is NO)
        # settlement_value = 0 otherwise
        row = self._conn.execute(
            "SELECT side, fill_price FROM paper_trades WHERE id = ?", (row_id,)
        ).fetchone()
        realized_edge = None
        if row:
            side = row["side"]
            fill = row["fill_price"] or 0
            won = (side == "yes" and settlement == "yes") or \
                  (side == "no" and settlement == "no")
            settlement_value = 100 if won else 0
            realized_edge = (settlement_value - fill) / 100.0  # Normalize to 0-1 scale

        self._conn.execute(
            """UPDATE paper_trades
               SET settlement = ?, pnl_cents = ?, status = 'settled', settled_at = ?,
                   realized_edge = ?
               WHERE id = ?""",
            (settlement, pnl_cents, time.time(), realized_edge, row_id),
        )
        self._conn.commit()

    def mark_paper_voided(self, row_id: int):
        """Mark a paper trade as voided by the exchange (refund, $0 P&L).

        Use ONLY when Kalshi explicitly reports status in ('voided', 'canceled',
        'refunded'). The trade is a known-$0 outcome — counts as a breakeven in
        analytics. Do NOT use for unknown/unresolved outcomes — use
        mark_paper_stuck() for those (right-censored).
        """
        self._conn.execute(
            """UPDATE paper_trades
               SET settlement = 'void', pnl_cents = 0, status = 'voided',
                   settled_at = ?, realized_edge = NULL
               WHERE id = ?""",
            (time.time(), row_id),
        )
        self._conn.commit()

    def mark_paper_stuck(self, row_id: int):
        """Mark a paper trade as stuck — outcome UNKNOWN (right-censored observation).

        Use when we've given up polling (e.g. past latest_expiration_time + grace)
        but Kalshi has not published a result. pnl_cents is set to NULL so the
        trade is EXCLUDED from win-rate/Sharpe/ML training datasets rather than
        contaminating them as a false breakeven.

        If Kalshi later publishes a result, a reconciliation script can flip
        this back to 'settled' — the original outcome data is preserved.
        """
        self._conn.execute(
            """UPDATE paper_trades
               SET settlement = NULL, pnl_cents = NULL, status = 'stuck',
                   settled_at = ?, realized_edge = NULL
               WHERE id = ?""",
            (time.time(), row_id),
        )
        self._conn.commit()

    def get_open_paper_trades(self) -> list[dict]:
        """Get all paper trades with status 'filled' (waiting for settlement)."""
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'filled' ORDER BY opened_at DESC LIMIT 10000"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_paper_trades(self) -> list[dict]:
        """Get all paper trades with status 'pending' (order placed, fill unknown)."""
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'pending' ORDER BY opened_at DESC LIMIT 10000"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_open_position(self, ticker: str) -> bool:
        """Check if there's already an open/pending/recent paper trade for this ticker.

        Blocks if there was ANY order attempt in the last 24 hours on this ticker.
        This prevents re-entering the same market repeatedly during a game/event
        that can last several hours. One shot per ticker per day.
        """
        # Check for active positions
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE ticker = ? AND status IN ('pending', 'filled')",
            (ticker,),
        ).fetchone()
        if row["n"] > 0:
            return True
        # Block re-orders for 24h after ANY attempt (including unfilled)
        cutoff = time.time() - 86400
        row2 = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE ticker = ? AND opened_at >= ?",
            (ticker, cutoff),
        ).fetchone()
        return row2["n"] > 0

    def get_open_position_for_ticker(self, ticker: str) -> dict | None:
        """Get the open (pending or filled) paper trade for a ticker, if any."""
        row = self._conn.execute(
            "SELECT * FROM paper_trades WHERE ticker = ? AND status IN ('pending', 'filled') "
            "ORDER BY opened_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return dict(row) if row else None

    def get_open_positions_for_event(self, event_id: str) -> list[dict]:
        """Get all positions (any status) for an event in the last 24 hours.

        Uses a 24-hour window to match the per-ticker dedup. This prevents
        entering multiple sides of the same event across the full game lifetime.
        """
        cutoff = time.time() - 86400
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE event_id = ? AND opened_at >= ? "
            "ORDER BY opened_at DESC LIMIT 10000",
            (event_id, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_event_sibling_volumes(self, event_prefix: str) -> list[dict]:
        """Get volume stats for all tickers sharing an event prefix.

        Used for event-relative conviction delta: this ticker's share of
        event-level anomaly activity.

        Returns [{ticker, volume_24h, trade_count_24h, updated_at}].
        The updated_at field lets the caller enforce staleness guards —
        out-of-order WebSocket delivery can leave a sibling's denominator
        unrefreshed, artificially inflating the triggering ticker's share.
        """
        rows = self._conn.execute(
            "SELECT ticker, volume_24h, trade_count_24h, updated_at "
            "FROM market_profiles WHERE ticker LIKE ? || '%'",
            (event_prefix,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_positions_by_category(self, category: str) -> int:
        """Count open positions in a specific market category."""
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE category = ? AND status IN ('pending', 'filled')",
            (category,),
        ).fetchone()
        return row["n"]

    def count_recent_paper_trades(self, window_sec: int = 300) -> int:
        """Count paper trades placed in the last N seconds (burst throttle)."""
        cutoff = time.time() - window_sec
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE opened_at >= ?",
            (cutoff,),
        ).fetchone()
        return row["n"]

    def get_daily_spend_cents(self) -> int:
        """Get total spend (fill_price * fill_count) for today's paper trades."""
        # Use midnight UTC as the day boundary
        today_start = int(time.time() / 86400) * 86400
        row = self._conn.execute(
            """SELECT COALESCE(SUM(fill_price * fill_count), 0) as spend
               FROM paper_trades WHERE filled_at >= ? AND status IN ('filled', 'settled')""",
            (today_start,),
        ).fetchone()
        return int(row["spend"])

    def get_paper_stats(self) -> dict:
        """Get aggregate paper trading statistics."""
        total = self._conn.execute("SELECT COUNT(*) as n FROM paper_trades").fetchone()["n"]
        filled = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('filled', 'settled')"
        ).fetchone()["n"]
        settled = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status = 'settled'"
        ).fetchone()["n"]
        wins = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status = 'settled' AND pnl_cents > 0"
        ).fetchone()["n"]
        # Sprint 14g (2026-05-01): TWO P&L queries — cumulative (for dashboard
        # + cumulative kill switch) and daily-realized (for daily kill switch).
        # Pre-fix: a single SUM with no date filter was assigned to the
        # variable that the daily kill switch checks. The kill switch
        # silently behaved as cumulative, latching permanently after
        # cumulative crossed -$20. See CLAUDE.md § Sprint 14g.
        total_pnl = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_cents), 0) as pnl FROM paper_trades WHERE status = 'settled'"
        ).fetchone()["pnl"]
        today_realized_pnl = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_cents), 0) as pnl FROM paper_trades "
            "WHERE status = 'settled' AND settled_at >= strftime('%s', 'now', 'start of day')"
        ).fetchone()["pnl"]
        open_count = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('pending', 'filled')"
        ).fetchone()["n"]

        # Unrealized P&L: estimate using last trade price per ticker for open positions
        # For each open position, compare entry price to current estimated price (last trade)
        unrealized_pnl = 0
        open_trades = self._conn.execute(
            "SELECT pt.id, pt.ticker, pt.side, pt.entry_price, pt.fill_count "
            "FROM paper_trades pt WHERE pt.status = 'filled' AND pt.fill_count > 0 LIMIT 10000"
        ).fetchall()

        for trade in open_trades:
            ticker = trade["ticker"]
            entry_price = trade["entry_price"]
            fill_count = trade["fill_count"]
            side = trade["side"]

            # Get last trade price for this ticker as current market estimate
            last_trade = self._conn.execute(
                "SELECT yes_price, no_price FROM trades WHERE ticker = ? ORDER BY ts DESC LIMIT 1",
                (ticker,)
            ).fetchone()

            if last_trade:
                # Use the appropriate side price
                current_price = last_trade["yes_price"] if side == "yes" else last_trade["no_price"]
                if current_price is not None and entry_price is not None:
                    # P&L = (current - entry) * count (if yes: profit if price goes up)
                    pnl_per_contract = (float(current_price) - float(entry_price))
                    unrealized_pnl += pnl_per_contract * fill_count

        # Sprint 14g fix: daily P&L = TODAY's realized + current unrealized.
        # Was previously total_pnl (all-time) + unrealized — the bug.
        total_daily_pnl = today_realized_pnl + unrealized_pnl

        return {
            "total": total,
            "filled": filled,
            "settled": settled,
            "wins": wins,
            "win_rate": wins / settled if settled > 0 else 0.0,
            "total_pnl_cents": total_pnl,                       # CUMULATIVE — for dashboard + cumulative kill switch
            "today_realized_pnl_cents": int(today_realized_pnl),  # daily realized only (Sprint 14g)
            "unrealized_pnl_cents": int(unrealized_pnl),
            "total_daily_pnl_cents": int(total_daily_pnl),      # daily realized + unrealized (for daily kill switch)
            "open_positions": open_count,
            "daily_spend_cents": self.get_daily_spend_cents(),
        }

    def get_paper_history(self, limit: int = 100) -> list[dict]:
        """Get paper trade history, most recent first."""
        rows = self._conn.execute(
            "SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("features_json"):
                try:
                    d["features"] = json.loads(d["features_json"])
                except (json.JSONDecodeError, TypeError):
                    d["features"] = {}
            result.append(d)
        return result

    # ── Dynamic Threshold Support ────────────────────────────────────

    def get_settled_trades_for_threshold(self, window_n: int) -> list[dict]:
        """Return recent settled trades for CategoryThresholdManager initialization.

        Pulls window_n × 10 rows total (enough to fill all category + hour
        buckets with recent data while keeping memory bounded). Returns
        oldest-first so deque replay produces correct recency ordering.

        Args:
            window_n: Per-bucket rolling window size (e.g., 100).

        Returns:
            List of dicts with keys: category, opened_at, pnl_cents.
        """
        limit = window_n * 10
        rows = self._conn.execute(
            """SELECT category, opened_at, pnl_cents
               FROM paper_trades
               WHERE status = 'settled'
                 AND pnl_cents IS NOT NULL
               ORDER BY opened_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        # Reverse: replay oldest-to-newest into the deques
        return [dict(r) for r in reversed(rows)]

    # ── Maintenance ───────────────────────────────────────────────────

    def prune_old_data(self, force_vacuum: bool = False):
        """Delete records older than PRUNE_DAYS (trades/anomalies) and 1 day (book snapshots).

        Also prunes stale market_profiles and skipped_trades.
        Runs incremental VACUUM every ~6 hours (or when force_vacuum=True) to
        reclaim disk space and prevent mmap bloat in the systemd cgroup.
        """
        cutoff = time.time() - PRUNE_DAYS * 86400
        book_cutoff = time.time() - 86400          # Book snapshots: 1 day
        profile_cutoff = time.time() - 3 * 86400   # Market profiles: 3 days
        skip_cutoff = time.time() - 7 * 86400      # Skipped trades: 7 days
        c1 = self._conn.execute("DELETE FROM trades WHERE ts < ?", (cutoff,)).rowcount
        c2 = self._conn.execute("DELETE FROM anomalies WHERE ts < ?", (cutoff,)).rowcount
        c3 = self._conn.execute("DELETE FROM book_snapshots WHERE ts < ?", (book_cutoff,)).rowcount
        c4 = self._conn.execute("DELETE FROM market_profiles WHERE updated_at < ?", (profile_cutoff,)).rowcount
        c5 = self._conn.execute("DELETE FROM skipped_trades WHERE ts < ?", (skip_cutoff,)).rowcount
        self._conn.commit()
        total_pruned = c1 + c2 + c3 + c4 + c5
        if total_pruned:
            log.info(f"Pruned old data: {c1} trades, {c2} anomalies, {c3} book snapshots, {c4} profiles, {c5} skipped")

        # VACUUM removed — it rewrites the entire DB file and blocks the
        # asyncio event loop for minutes on a 1-OCPU VM. Run manually when
        # needed via: sqlite3 diamond_trades.db "VACUUM"

    def get_db_stats(self) -> dict:
        """Get database statistics."""
        trades = self._conn.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
        anomalies = self._conn.execute("SELECT COUNT(*) as n FROM anomalies").fetchone()["n"]
        profiles = self._conn.execute("SELECT COUNT(*) as n FROM market_profiles").fetchone()["n"]
        return {"trades": trades, "anomalies": anomalies, "market_profiles": profiles}
