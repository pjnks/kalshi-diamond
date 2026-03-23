"""
diamond_conviction.py
─────────────────────
Event-aware conviction tracking for DIAMOND.

Groups sibling tickers by event, tracks directional conviction with time decay,
and makes flip/block decisions when opposing signals arrive on the same event.

Core formula:
  conviction_side = Σ (score_i × e^(-age_i / half_life))

Persistence vs intensity handled naturally:
  - Three 0.65 alerts ≈ one 0.90 alert (at same recency)
  - Fresh intensity wins over stale persistence as old signals decay
  - Single tuning knob (half_life) controls the tradeoff
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────


@dataclass
class ConvictionEntry:
    """A single anomaly signal contributing to conviction."""
    score: float        # composite anomaly score (0-1)
    timestamp: float    # unix time when the signal was recorded
    ticker: str         # specific ticker (e.g., KXNCAAMBGAME-26MAR20MIZZMIA-MIZZ)


@dataclass
class EventConviction:
    """Conviction state for a single event (all sibling tickers)."""
    event_id: str
    entries: list[ConvictionEntry] = field(default_factory=list)

    def conviction_for_ticker(self, ticker: str, now: float, half_life: float) -> float:
        """Compute decayed conviction sum for a specific ticker."""
        total = 0.0
        for entry in self.entries:
            if entry.ticker == ticker:
                age = now - entry.timestamp
                decay = math.exp(-age / half_life) if half_life > 0 else 0.0
                total += entry.score * decay
        return total

    def strongest_opposing_ticker(
        self, ticker: str, now: float, half_life: float
    ) -> tuple[Optional[str], float]:
        """Find the sibling ticker with the highest current conviction.

        Returns (ticker, conviction) for the strongest ticker that is NOT
        the given ticker. Returns (None, 0.0) if no opposing tickers exist.
        """
        # Collect all unique tickers that aren't the query ticker
        opposing_tickers: set[str] = set()
        for entry in self.entries:
            if entry.ticker != ticker:
                opposing_tickers.add(entry.ticker)

        if not opposing_tickers:
            return None, 0.0

        best_ticker = None
        best_conviction = 0.0
        for opp_ticker in opposing_tickers:
            conv = self.conviction_for_ticker(opp_ticker, now, half_life)
            if conv > best_conviction:
                best_conviction = conv
                best_ticker = opp_ticker

        return best_ticker, best_conviction

    def all_tickers(self) -> set[str]:
        """Return all unique tickers that have contributed signals."""
        return {entry.ticker for entry in self.entries}


@dataclass
class ConvictionDecision:
    """Result of a conviction evaluation."""
    action: str          # "allow", "block", "flip"
    reason: str          # human-readable explanation
    event_id: str
    new_ticker: str
    new_conviction: float
    old_ticker: Optional[str] = None
    old_conviction: float = 0.0


# ── Conviction Tracker ───────────────────────────────────────────────


class ConvictionTracker:
    """Tracks decayed conviction per side per event.

    When an opposing signal arrives on the same event:
    - If new side conviction > old side conviction + flip_threshold → FLIP
    - If new side conviction ≤ old side conviction + flip_threshold → BLOCK
    - If no existing signals for other tickers in the event → ALLOW
    """

    def __init__(self, half_life: float = 420.0, flip_threshold: float = 0.4, store=None):
        """
        Args:
            half_life: Decay half-life in seconds (default 420 = 7 minutes).
                       After half_life seconds, a signal's contribution is reduced
                       to ~37% (1/e). After 2× half_life, ~13.5%.
            flip_threshold: Minimum conviction advantage required to flip an
                           existing position. Accounts for spread cost of
                           exiting + re-entering.
            store: Optional DiamondStore for persisting signals across restarts.
        """
        self._half_life = half_life
        self._flip_threshold = flip_threshold
        self._store = store
        self._events: dict[str, EventConviction] = {}  # event_id → EventConviction

    @staticmethod
    def extract_event_id(ticker: str) -> str:
        """Extract the event identifier from a Kalshi ticker.

        Convention: everything before the last '-' segment is the event.
        Examples:
          KXNCAAMBGAME-26MAR20MIZZMIA-MIZZ  → KXNCAAMBGAME-26MAR20MIZZMIA
          KXNCAAMBGAME-26MAR20MIZZMIA-MIA   → KXNCAAMBGAME-26MAR20MIZZMIA  (same event)
          KXBTCD-26MAR2023-T70799.99        → KXBTCD-26MAR2023
          KXMARMAD-26-KU                    → KXMARMAD-26
          CONTROLH-2026-D                   → CONTROLH-2026

        MVE parlays have hash suffixes, so each parlay is its own "event"
        (correct — parlays are independent bets).
        """
        if "-" not in ticker:
            return ticker
        return ticker.rsplit("-", 1)[0]

    def record_signal(self, ticker: str, score: float, timestamp: float) -> None:
        """Record an anomaly signal for conviction tracking.

        Called for ALL anomalies ≥ LOG level (not just traded ones) so conviction
        builds from the full signal history.
        """
        event_id = self.extract_event_id(ticker)
        if event_id not in self._events:
            self._events[event_id] = EventConviction(event_id=event_id)

        self._events[event_id].entries.append(
            ConvictionEntry(score=score, timestamp=timestamp, ticker=ticker)
        )

        # Persist to SQLite for restart recovery
        if self._store is not None:
            try:
                self._store.insert_conviction_signal(event_id, ticker, score, timestamp)
            except Exception as e:
                log.debug(f"[CONVICTION] Failed to persist signal: {e}")

    def evaluate(self, ticker: str, score: float) -> ConvictionDecision:
        """Evaluate whether a new trade should be allowed, blocked, or should flip.

        This is a read-only decision — it does NOT record the signal.
        The signal should already have been recorded via record_signal() before
        calling evaluate().

        Decision logic:
          1. No prior signals for other tickers in this event → ALLOW
          2. Compute conviction for new ticker and strongest opposing ticker
          3. new_conviction > old_conviction + flip_threshold → FLIP
          4. Otherwise → BLOCK
        """
        event_id = self.extract_event_id(ticker)
        now = time.time()

        event = self._events.get(event_id)
        if event is None:
            return ConvictionDecision(
                action="allow",
                reason="no prior signals for this event",
                event_id=event_id,
                new_ticker=ticker,
                new_conviction=score,
            )

        # Check if there are any opposing tickers with signals
        old_ticker, old_conviction = event.strongest_opposing_ticker(
            ticker, now, self._half_life
        )

        if old_ticker is None:
            # Only signals for this same ticker (or no signals at all)
            return ConvictionDecision(
                action="allow",
                reason="no opposing signals in this event",
                event_id=event_id,
                new_ticker=ticker,
                new_conviction=event.conviction_for_ticker(ticker, now, self._half_life),
            )

        # Compute conviction for the new ticker (includes already-recorded signals)
        new_conviction = event.conviction_for_ticker(ticker, now, self._half_life)

        if new_conviction > old_conviction + self._flip_threshold:
            return ConvictionDecision(
                action="flip",
                reason=(
                    f"new conviction {new_conviction:.2f} > "
                    f"old {old_conviction:.2f} + threshold {self._flip_threshold}"
                ),
                event_id=event_id,
                new_ticker=ticker,
                new_conviction=new_conviction,
                old_ticker=old_ticker,
                old_conviction=old_conviction,
            )
        else:
            return ConvictionDecision(
                action="block",
                reason=(
                    f"new conviction {new_conviction:.2f} ≤ "
                    f"old {old_conviction:.2f} + threshold {self._flip_threshold}"
                ),
                event_id=event_id,
                new_ticker=ticker,
                new_conviction=new_conviction,
                old_ticker=old_ticker,
                old_conviction=old_conviction,
            )

    def load_recent(self, max_age_sec: float = 3600.0) -> int:
        """Restore conviction state from SQLite on startup.

        Returns number of signals loaded.
        """
        if self._store is None:
            return 0
        try:
            signals = self._store.get_recent_conviction_signals(max_age_sec)
            for sig in signals:
                event_id = sig["event_id"]
                if event_id not in self._events:
                    self._events[event_id] = EventConviction(event_id=event_id)
                self._events[event_id].entries.append(
                    ConvictionEntry(
                        score=sig["score"],
                        timestamp=sig["ts"],
                        ticker=sig["ticker"],
                    )
                )
            if signals:
                log.info(f"[CONVICTION] Restored {len(signals)} signals across "
                         f"{len(self._events)} events from SQLite")
            return len(signals)
        except Exception as e:
            log.warning(f"[CONVICTION] Failed to load state from SQLite: {e}")
            return 0

    def get_event_state(self, event_id: str) -> Optional[EventConviction]:
        """Get conviction state for a specific event (for dashboard/debugging)."""
        return self._events.get(event_id)

    def cleanup(self, max_age_sec: float = 3600.0) -> int:
        """Remove stale events with no recent entries.

        Args:
            max_age_sec: Remove events where ALL entries are older than this.

        Returns:
            Number of events removed.
        """
        now = time.time()
        stale_events = []
        for event_id, event in self._events.items():
            if not event.entries:
                stale_events.append(event_id)
                continue
            newest = max(e.timestamp for e in event.entries)
            if now - newest > max_age_sec:
                stale_events.append(event_id)

        for event_id in stale_events:
            del self._events[event_id]

        if stale_events:
            log.debug(f"[CONVICTION] Cleaned up {len(stale_events)} stale events")

        return len(stale_events)

    @property
    def active_events(self) -> int:
        """Number of events currently being tracked."""
        return len(self._events)

    def __repr__(self) -> str:
        return (
            f"ConvictionTracker(half_life={self._half_life}s, "
            f"flip_threshold={self._flip_threshold}, "
            f"events={len(self._events)})"
        )
