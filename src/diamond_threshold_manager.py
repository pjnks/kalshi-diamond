"""
diamond_threshold_manager.py
─────────────────────────────
Context-aware dynamic threshold adjustment for DIAMOND trade entry.

Adjusts the base ALERT threshold (0.55) up or down based on two rolling
context dimensions: market category and UTC hour-of-day. Categories and
hours with positive edge get lower thresholds (easier entry). Those with
negative edge get higher thresholds (harder entry).

Score stays clean — adjustments are applied to the threshold, not the score.
The anomaly DB always records the raw two-stage composite score.

Formula:
    category_adj = clamp(-(cat_wr - baseline_wr) * sensitivity, -MAX_CAT, +MAX_CAT)
    hour_adj     = clamp(-(hour_wr - baseline_wr) * sensitivity, -MAX_HOUR, +MAX_HOUR)
    effective    = clamp(base + category_adj + hour_adj, FLOOR, CEIL)

Positive edge → negative adj → lower threshold → easier entry.
Negative edge → positive adj → higher threshold → harder entry.

Quant review (March 2026): replaces hardcoded time-of-day score multiplier
(which contaminated raw scores) and adds category-level filtering based on
rolling settlement performance.
"""
from __future__ import annotations

import collections
import datetime
import logging
import time
from typing import Deque

log = logging.getLogger(__name__)

_WIN = 1
_LOSS = 0


class CategoryThresholdManager:
    """Dynamic ALERT threshold adjustment based on rolling category and hour-of-day performance."""

    def __init__(self, store, base_threshold: float):
        from diamond_config import (
            CTM_WINDOW_N,
            CTM_MIN_N,
            CTM_BASELINE_WIN_RATE,
            CTM_SENSITIVITY,
            CTM_MAX_CAT_ADJ,
            CTM_MAX_HOUR_ADJ,
            CTM_THRESHOLD_FLOOR,
            CTM_THRESHOLD_CEIL,
            CTM_REFRESH_INTERVAL_SEC,
        )
        self._store = store
        self._base = base_threshold
        self._window_n = CTM_WINDOW_N
        self._min_n = CTM_MIN_N
        self._baseline_wr = CTM_BASELINE_WIN_RATE
        self._sensitivity = CTM_SENSITIVITY
        self._max_cat_adj = CTM_MAX_CAT_ADJ
        self._max_hour_adj = CTM_MAX_HOUR_ADJ
        self._floor = CTM_THRESHOLD_FLOOR
        self._ceil = CTM_THRESHOLD_CEIL
        self._refresh_interval = CTM_REFRESH_INTERVAL_SEC
        self._last_refresh: float = 0.0

        # In-memory rolling windows: category → deque of 0/1 outcomes
        self._cat_buf: dict[str, Deque[int]] = collections.defaultdict(
            lambda: collections.deque(maxlen=self._window_n)
        )
        # Hour buckets: 24 deques indexed by UTC hour
        self._hour_buf: list[Deque[int]] = [
            collections.deque(maxlen=self._window_n) for _ in range(24)
        ]

    # ── Initialization ───────────────────────────────────────────────

    def load(self) -> None:
        """Populate in-memory buffers from settled trades in SQLite.

        Called once on startup. Replays settled trades oldest-to-newest
        into the per-bucket deques so they reflect the most recent
        WINDOW_N outcomes per bucket.
        """
        try:
            rows = self._store.get_settled_trades_for_threshold(self._window_n)
            # Clear existing buffers (idempotent reload)
            self._cat_buf.clear()
            for buf in self._hour_buf:
                buf.clear()

            for row in rows:
                cat = row.get("category") or "unknown"
                opened_at = row.get("opened_at", 0)
                won = _WIN if (row.get("pnl_cents") or 0) > 0 else _LOSS
                utc_hour = int(opened_at / 3600) % 24

                self._cat_buf[cat].append(won)
                self._hour_buf[utc_hour].append(won)

            self._last_refresh = time.time()
            total_samples = sum(len(b) for b in self._cat_buf.values())
            log.info(
                f"[CTM] Loaded: {len(self._cat_buf)} categories, "
                f"{total_samples} category samples, "
                f"{sum(len(b) for b in self._hour_buf)} hour samples"
            )
        except Exception as e:
            log.warning(f"[CTM] Load failed: {e}")

    def maybe_refresh(self) -> None:
        """Re-load from DB if refresh interval has elapsed.

        Called at the top of on_anomaly() to keep thresholds current
        without a separate background task.
        """
        if time.time() - self._last_refresh > self._refresh_interval:
            self.load()

    # ── Core Logic ───────────────────────────────────────────────────

    def _win_rate(self, buf: Deque[int]) -> tuple[float, int]:
        """Returns (win_rate, n). Uses baseline if n < min_n (cold start)."""
        n = len(buf)
        if n < self._min_n:
            return self._baseline_wr, n
        return sum(buf) / n, n

    def _edge_to_adj(self, win_rate: float, max_adj: float) -> float:
        """Convert win rate to threshold adjustment.

        Positive edge (WR > baseline) → negative adj → lower threshold → easier entry.
        Negative edge (WR < baseline) → positive adj → higher threshold → harder entry.
        """
        delta = win_rate - self._baseline_wr
        raw_adj = -delta * self._sensitivity
        return max(-max_adj, min(max_adj, raw_adj))

    def effective_threshold(
        self, category: str | None, utc_hour: int | None = None
    ) -> float:
        """Return the adjusted ALERT entry threshold for this context.

        Combines category and hour-of-day adjustments additively on the
        base threshold, clamped to [FLOOR, CEIL].

        Args:
            category: Market category (e.g., "nba", "atp-tennis"). None → "unknown".
            utc_hour: UTC hour (0-23). None → current UTC hour.

        Returns:
            Effective threshold (float between FLOOR and CEIL).
        """
        cat_key = (category or "unknown").lower()
        if utc_hour is None:
            utc_hour = datetime.datetime.now(datetime.timezone.utc).hour

        cat_wr, cat_n = self._win_rate(self._cat_buf[cat_key])
        hour_wr, hour_n = self._win_rate(self._hour_buf[utc_hour])

        # Only apply adjustment if we have enough data (cold start guard)
        cat_adj = self._edge_to_adj(cat_wr, self._max_cat_adj) if cat_n >= self._min_n else 0.0
        hour_adj = self._edge_to_adj(hour_wr, self._max_hour_adj) if hour_n >= self._min_n else 0.0

        raw = self._base + cat_adj + hour_adj
        effective = max(self._floor, min(self._ceil, raw))

        log.debug(
            f"[CTM] threshold: base={self._base:.3f} "
            f"cat={cat_key}(wr={cat_wr:.2f},n={cat_n},adj={cat_adj:+.3f}) "
            f"hour={utc_hour}(wr={hour_wr:.2f},n={hour_n},adj={hour_adj:+.3f}) "
            f"→ effective={effective:.3f}"
        )
        return effective

    # ── Incremental Updates ──────────────────────────────────────────

    def on_settlement(
        self, category: str | None, utc_hour: int, won: bool
    ) -> None:
        """Incrementally update in-memory buffers on each settlement.

        Called from check_settlements() after each trade settles.
        Uses the entry hour (from opened_at), not settlement time —
        we attribute outcomes to the context at trade entry.
        """
        cat_key = (category or "unknown").lower()
        outcome = _WIN if won else _LOSS
        self._cat_buf[cat_key].append(outcome)
        self._hour_buf[utc_hour % 24].append(outcome)

    # ── Dashboard ────────────────────────────────────────────────────

    def get_dashboard_state(self) -> dict:
        """Returns serializable dict for dashboard rendering.

        Includes per-category and per-hour win rates, adjustments,
        and effective thresholds. Used by diamond_dashboard.py.
        """
        categories = {}
        for cat, buf in sorted(self._cat_buf.items()):
            wr, n = self._win_rate(buf)
            has_data = n >= self._min_n
            adj = self._edge_to_adj(wr, self._max_cat_adj) if has_data else 0.0
            categories[cat] = {
                "win_rate": round(wr, 3),
                "n": n,
                "adj": round(adj, 4),
                "effective": round(
                    max(self._floor, min(self._ceil, self._base + adj)), 3
                ),
                "has_data": has_data,
            }

        hours = []
        for h, buf in enumerate(self._hour_buf):
            wr, n = self._win_rate(buf)
            has_data = n >= self._min_n
            adj = self._edge_to_adj(wr, self._max_hour_adj) if has_data else 0.0
            hours.append({
                "hour": h,
                "win_rate": round(wr, 3),
                "n": n,
                "adj": round(adj, 4),
                "effective": round(
                    max(self._floor, min(self._ceil, self._base + adj)), 3
                ),
                "has_data": has_data,
            })

        return {
            "base_threshold": self._base,
            "categories": categories,
            "hours": hours,
            "last_refreshed": self._last_refresh,
            "baseline_win_rate": self._baseline_wr,
            "window_n": self._window_n,
            "min_n": self._min_n,
        }
