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
        """Open database connection and create tables."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
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
            "SELECT * FROM trades WHERE ticker = ? AND ts >= ? ORDER BY ts",
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
               ORDER BY hour_ts""",
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
            "SELECT * FROM anomalies WHERE ticker = ? AND ts >= ? ORDER BY ts DESC",
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

    # ── Paper Trades ─────────────────────────────────────────────────

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
    ) -> int:
        """Insert a new paper trade record. Returns the row ID."""
        cur = self._conn.execute(
            """INSERT INTO paper_trades
               (order_id, client_order_id, ticker, title, side, action, count,
                entry_price, anomaly_score, anomaly_level, features_json, opened_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (order_id, client_order_id, ticker, title, side, action, count,
             entry_price, anomaly_score, anomaly_level, json.dumps(features), time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_paper_fill(self, row_id: int, order_id: str, fill_price: int, fill_count: int, status: str = "filled"):
        """Update a paper trade after order response."""
        self._conn.execute(
            """UPDATE paper_trades
               SET order_id = ?, fill_price = ?, fill_count = ?, status = ?, filled_at = ?
               WHERE id = ?""",
            (order_id, fill_price, fill_count, status, time.time(), row_id),
        )
        self._conn.commit()

    def update_paper_settlement(self, row_id: int, settlement: str, pnl_cents: float):
        """Update a paper trade when its market settles."""
        self._conn.execute(
            """UPDATE paper_trades
               SET settlement = ?, pnl_cents = ?, status = 'settled', settled_at = ?
               WHERE id = ?""",
            (settlement, pnl_cents, time.time(), row_id),
        )
        self._conn.commit()

    def get_open_paper_trades(self) -> list[dict]:
        """Get all paper trades with status 'filled' (waiting for settlement)."""
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'filled' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_paper_trades(self) -> list[dict]:
        """Get all paper trades with status 'pending' (order placed, fill unknown)."""
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'pending' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def has_open_position(self, ticker: str) -> bool:
        """Check if there's already an open/pending/recent paper trade for this ticker.

        Also blocks if there was ANY order attempt in the last 30 minutes,
        to prevent re-ordering when the API response is lost but the order
        actually went through on Kalshi's side.
        """
        # Check for active positions
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE ticker = ? AND status IN ('pending', 'filled')",
            (ticker,),
        ).fetchone()
        if row["n"] > 0:
            return True
        # Block re-orders for 30 min after ANY attempt (including unfilled)
        cutoff = time.time() - 1800
        row2 = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE ticker = ? AND opened_at >= ?",
            (ticker, cutoff),
        ).fetchone()
        return row2["n"] > 0

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
        total_pnl = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_cents), 0) as pnl FROM paper_trades WHERE status = 'settled'"
        ).fetchone()["pnl"]
        open_count = self._conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE status IN ('pending', 'filled')"
        ).fetchone()["n"]

        # Unrealized P&L: estimate using last trade price per ticker for open positions
        # For each open position, compare entry price to current estimated price (last trade)
        unrealized_pnl = 0
        open_trades = self._conn.execute(
            "SELECT pt.id, pt.ticker, pt.side, pt.entry_price, pt.fill_count "
            "FROM paper_trades pt WHERE pt.status = 'filled' AND pt.fill_count > 0"
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
                # P&L = (current - entry) * count (if yes: profit if price goes up)
                pnl_per_contract = (current_price - entry_price)
                unrealized_pnl += pnl_per_contract * fill_count

        # Total daily P&L = realized + unrealized
        total_daily_pnl = total_pnl + unrealized_pnl

        return {
            "total": total,
            "filled": filled,
            "settled": settled,
            "wins": wins,
            "win_rate": wins / settled if settled > 0 else 0.0,
            "total_pnl_cents": total_pnl,
            "unrealized_pnl_cents": int(unrealized_pnl),
            "total_daily_pnl_cents": int(total_daily_pnl),
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

    # ── Maintenance ───────────────────────────────────────────────────

    def prune_old_data(self):
        """Delete records older than PRUNE_DAYS (trades/anomalies) and 2 days (book snapshots)."""
        cutoff = time.time() - PRUNE_DAYS * 86400
        book_cutoff = time.time() - 2 * 86400  # Book snapshots only need 2 days
        c1 = self._conn.execute("DELETE FROM trades WHERE ts < ?", (cutoff,)).rowcount
        c2 = self._conn.execute("DELETE FROM anomalies WHERE ts < ?", (cutoff,)).rowcount
        c3 = self._conn.execute("DELETE FROM book_snapshots WHERE ts < ?", (book_cutoff,)).rowcount
        self._conn.commit()
        if c1 or c2 or c3:
            log.info(f"Pruned old data: {c1} trades, {c2} anomalies, {c3} book snapshots")

    def get_db_stats(self) -> dict:
        """Get database statistics."""
        trades = self._conn.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
        anomalies = self._conn.execute("SELECT COUNT(*) as n FROM anomalies").fetchone()["n"]
        profiles = self._conn.execute("SELECT COUNT(*) as n FROM market_profiles").fetchone()["n"]
        return {"trades": trades, "anomalies": anomalies, "market_profiles": profiles}
