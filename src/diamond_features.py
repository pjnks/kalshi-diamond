"""
diamond_features.py
───────────────────
Ten anomaly detection features for DIAMOND.

Each feature produces a score 0-1. Composite score is a weighted combination.
Features are computed per-trade against rolling market profiles.

Features 1-6: Original (trade size, volume spike, book imbalance, taker skew,
              price impact, cross-market correlation)
Features 7-10: Advanced (sweep detection, trade velocity, size concentration,
               book pressure delta)
"""

from __future__ import annotations

import logging
import math
import time

from diamond_config import (
    FEATURE_ENABLED,
    FEATURE_WEIGHTS,
    ROLLING_WINDOW_1H,
    ROLLING_WINDOW_24H,
)

log = logging.getLogger(__name__)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ── Individual Features ───────────────────────────────────────────────


def trade_size_zscore(trade_count: int, mean_size: float, std_size: float) -> float:
    """
    Feature 1: How unusual is this trade size relative to the market's history?

    z = (trade_size - mean) / std, normalized to 0-1 via sigmoid-like mapping.
    z >= 3 → score ~1.0 (very unusual)
    z <= 0 → score ~0.0 (normal)

    Returns 0 if no baseline (mean=0), since we can't judge without history.
    """
    if mean_size <= 0:
        return 0.0  # No baseline yet
    if std_size <= 0:
        std_size = 1.0
    z = (trade_count - mean_size) / std_size
    if z <= 0:
        return 0.0
    # Map z-score to 0-1: z=1→0.25, z=2→0.5, z=3→0.75, z=4→0.9
    score = 1.0 - 1.0 / (1.0 + 0.5 * z)
    return _clamp(score)


def volume_spike_ratio(current_1h_volume: int, avg_hourly_volume: float) -> float:
    """
    Feature 2: Is the last hour's volume unusually high?

    ratio = current_1h / avg_hourly. Normalized so ratio=3x→0.5, 5x→0.75, 10x→0.9.
    Returns 0 if no baseline — can't judge spikes without history.
    Requires ratio > 1.5x to fire (slight above-average is normal).
    """
    if avg_hourly_volume <= 0:
        return 0.0  # No baseline = can't judge, don't flag
    ratio = current_1h_volume / avg_hourly_volume
    if ratio <= 1.5:
        return 0.0  # Up to 1.5x is normal variance
    # Log scale: ratio 2→0.25, 3→0.45, 5→0.65, 10→0.85, 15→1.0
    score = math.log(ratio) / math.log(15)  # log15(ratio): ratio=15→1.0
    return _clamp(score)


def order_book_imbalance(yes_depth: float, no_depth: float) -> float:
    """
    Feature 3: Is the order book lopsided?

    imbalance = |yes_depth - no_depth| / (yes_depth + no_depth)
    Range: 0 (perfectly balanced) to 1 (completely one-sided).
    Requires minimum total depth of 100 to avoid noise from thin books.
    """
    total = yes_depth + no_depth
    if total < 200:
        return 0.0  # Thin books are unreliable — ignore
    imbalance = abs(yes_depth - no_depth) / total
    # Kalshi markets are naturally lopsided; only flag extreme imbalance > 0.7
    if imbalance < 0.7:
        return 0.0
    score = (imbalance - 0.7) / 0.3  # Rescale 0.7-1.0 → 0-1
    return _clamp(score)


def taker_side_skew(yes_taker_ratio: float, trades_in_window: int = 0) -> float:
    """
    Feature 4: Is one side dominating as taker?

    Normal: ~0.5. Anomalous: heavily skewed to yes or no.
    Requires at least 30 trades in window to be meaningful.
    High threshold (0.65 deviation) because Kalshi markets are naturally directional —
    most markets have a natural lean. Only flag when skew is extreme.
    """
    if trades_in_window < 50:
        return 0.0  # Not enough data to judge skew
    deviation = abs(yes_taker_ratio - 0.5) * 2.0
    # Kalshi markets are naturally directional, especially during live games.
    # Only flag extreme skew > 0.8 (yes_ratio >= 0.90 or <= 0.10)
    if deviation < 0.8:
        return 0.0
    score = (deviation - 0.8) / 0.2  # 0.8→0, 0.9→0.5, 1.0→1.0
    return _clamp(score)


def price_impact(price_before: float, price_after: float) -> float:
    """
    Feature 5: Did a large trade move the price significantly?

    Measured as absolute price change (Kalshi prices are 0-100 cents).
    5+ cent move → moderate, 10+ → high, 20+ → very high.
    """
    if price_before is None or price_after is None:
        return 0.0
    change = abs(price_after - price_before)
    if change < 2:
        return 0.0
    # Scale: 2→0.1, 5→0.25, 10→0.5, 20→0.8, 30+→1.0
    score = change / 30.0
    return _clamp(score)


def cross_market_correlation(
    ticker: str,
    recent_anomaly_counts: dict[str, int],
    event_series: str | None = None,
) -> float:
    """
    Feature 6: Are related markets also showing anomalies simultaneously?

    Counts *distinct tickers* (not total anomalies) with recent flags.
    Requires >= 8 other tickers with anomalies to fire — high bar to avoid
    noise from normal market activity where many tickers always have some anomalies.
    This prevents feedback loops where one anomaly cascades.
    """
    if not recent_anomaly_counts:
        return 0.0

    # Count distinct other tickers with anomalies
    # Only count tickers with 3+ anomalies in the window (filters noise)
    other_tickers = sum(1 for t, cnt in recent_anomaly_counts.items()
                        if t != ticker and cnt >= 3)

    # High bar: need 15+ other tickers with concentrated anomalies
    # Prevents feedback loops where normal multi-market activity cascades
    if other_tickers < 15:
        return 0.0

    # 15 tickers → 0.1, 20 → 0.33, 25 → 0.56, 30+ → 1.0
    score = (other_tickers - 12) / 18.0
    return _clamp(score)


def sweep_score(recent_trades: list[dict], current_side: str) -> float:
    """
    Feature 7: Is someone eating through multiple price levels rapidly?

    A sweep occurs when a taker lifts through consecutive ask levels in quick
    succession — the hallmark of an informed trader who needs size NOW.
    Scores by number of distinct price levels crossed on the same taker side.
    """
    if not recent_trades or not current_side:
        return 0.0

    now = time.time()
    # Filter to matching taker side in the last 60 seconds
    side_trades = [
        t for t in recent_trades
        if t.get("taker_side") == current_side and (now - t.get("ts", 0)) <= 60
    ]

    if len(side_trades) < 3:
        return 0.0  # Need at least 3 trades to detect a sweep

    # Sort by timestamp, extract prices
    side_trades.sort(key=lambda t: t.get("ts", 0))
    price_key = "yes_price" if current_side == "yes" else "no_price"
    prices = []
    for t in side_trades:
        p = t.get(price_key)
        if p is not None:
            prices.append(float(p))

    if len(prices) < 3:
        return 0.0

    # Count consecutive price level increases (sweep up) or decreases (sweep down)
    # Take the longer of up-sweep or down-sweep
    up_levels = 1
    down_levels = 1
    max_up = 1
    max_down = 1
    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            up_levels += 1
            max_up = max(max_up, up_levels)
            down_levels = 1
        elif prices[i] < prices[i - 1]:
            down_levels += 1
            max_down = max(max_down, down_levels)
            up_levels = 1
        # Equal prices don't break the streak

    sweep_levels = max(max_up, max_down)
    if sweep_levels < 2:
        return 0.0

    # Score by sweep depth: 2=0.2, 3=0.5, 4=0.8, 5+=1.0
    score = min(1.0, (sweep_levels - 1) / 4.0)

    # Speed bonus: if trades are < 2s apart on average, boost signal
    if len(side_trades) >= 2:
        avg_interval = (side_trades[-1]["ts"] - side_trades[0]["ts"]) / (len(side_trades) - 1)
        if avg_interval < 2.0:
            score = min(1.0, score * 1.2)

    return _clamp(score)


def trade_velocity(trades_last_30s: int, avg_trades_per_30s: float) -> float:
    """
    Feature 8: Is the trade frequency abnormally high right now?

    Detects burst activity — many trades in a very short window. Different
    from volume_spike_ratio which measures 1-hour contract volume.
    This catches rapid-fire small trades that indicate algo activity or panic.
    """
    if avg_trades_per_30s <= 0:
        return 0.0
    ratio = trades_last_30s / max(avg_trades_per_30s, 0.5)
    if ratio < 3.0:
        return 0.0  # Up to 3x is normal variance
    # Log scale: 3x→0.3, 5x→0.5, 10x→0.8, 20x→1.0
    score = math.log(ratio) / math.log(20)
    return _clamp(score)


def size_concentration(recent_trades: list[dict], top_n: int = 3) -> float:
    """
    Feature 9: Is volume concentrated in a few large trades (whale detection)?

    Normal markets have distributed trade sizes. When 80%+ of volume comes
    from 2-3 trades, that's likely a single large participant — informed money.
    """
    if len(recent_trades) < 10:
        return 0.0  # Need enough trades for concentration to be meaningful

    sizes = sorted([t.get("count", 1) for t in recent_trades], reverse=True)
    total_vol = sum(sizes)
    if total_vol <= 0:
        return 0.0

    top_vol = sum(sizes[:top_n])
    concentration = top_vol / total_vol

    if concentration < 0.5:
        return 0.0  # Normal distribution
    # Rescale 0.5-1.0 → 0-1
    score = (concentration - 0.5) / 0.5
    return _clamp(score)


def book_pressure_delta(
    current_yes_depth: float, current_no_depth: float,
    prev_yes_depth: float, prev_no_depth: float,
) -> float:
    """
    Feature 10: Is the order book balance CHANGING rapidly?

    Static imbalance can be normal (market lean). But a large *shift* in
    imbalance over a short period indicates new positioning activity —
    someone is building a wall or pulling liquidity.
    """
    cur_total = current_yes_depth + current_no_depth
    prev_total = prev_yes_depth + prev_no_depth

    # Require some depth in both snapshots (lowered from 200 to 20 —
    # most sports markets have thin books but shifts are still meaningful)
    if cur_total < 20 or prev_total < 20:
        return 0.0

    imbalance_now = (current_yes_depth - current_no_depth) / cur_total
    imbalance_prev = (prev_yes_depth - prev_no_depth) / prev_total
    delta = abs(imbalance_now - imbalance_prev)

    if delta < 0.10:
        return 0.0  # Normal fluctuation (lowered from 0.15)
    # Rescale: 0.10→0.0, 0.25→0.43, 0.45→1.0
    score = (delta - 0.10) / 0.35
    return _clamp(score)


# ── Composite Score ───────────────────────────────────────────────────


def compute_composite_score(features: dict[str, float]) -> float:
    """
    Weighted combination of feature scores, with mild redistribution from
    dead features to active ones.

    Redistribution is capped at 1.5× to prevent two low-weight features
    from inflating the score to CRITICAL. This means you need at least 3+
    strong features or 2+ high-weight features to reach ALERT/CRITICAL.
    """
    active_weight = 0.0
    active_count = 0
    raw_score = 0.0
    for name, weight in FEATURE_WEIGHTS.items():
        val = features.get(name, 0.0)
        raw_score += weight * val
        if val > 0:
            active_weight += weight
            active_count += 1

    if active_weight <= 0:
        return 0.0

    # Redistribute: scale up, but cap at 1.5× to prevent inflation
    # from just 1-2 weak features firing
    total_weight = sum(FEATURE_WEIGHTS.values())
    redistribution = min(total_weight / active_weight, 1.5)

    # Mild penalty: need 3+ features firing for full redistribution
    if active_count < 3:
        redistribution = min(redistribution, 1.3)

    score = raw_score * redistribution
    return _clamp(score)


def classify_alert_level(score: float) -> str:
    """Map composite score to alert level."""
    from diamond_config import (
        ALERT_THRESHOLD_CRITICAL,
        ALERT_THRESHOLD_ALERT,
        ALERT_THRESHOLD_NOTABLE,
        ALERT_THRESHOLD_LOG,
    )
    if score >= ALERT_THRESHOLD_CRITICAL:
        return "CRITICAL"
    elif score >= ALERT_THRESHOLD_ALERT:
        return "ALERT"
    elif score >= ALERT_THRESHOLD_NOTABLE:
        return "NOTABLE"
    elif score >= ALERT_THRESHOLD_LOG:
        return "LOG"
    return "NONE"


# ── Feature Computation Pipeline ──────────────────────────────────────


class FeatureEngine:
    """
    Computes all 6 features for a trade against stored market data.

    Requires a DiamondStore instance for lookups.
    Tracks last known price per ticker for price_impact computation.
    """

    def __init__(self, store):
        self._store = store
        self._last_price: dict[str, float] = {}  # ticker → last yes_price

    def compute(self, trade: dict) -> dict:
        """
        Compute all features for a single trade.

        Returns: {feature_name: score, ..., "composite": score, "alert_level": str}
        """
        ticker = trade["ticker"]
        trade_count = trade.get("count", trade.get("contracts", 1))

        # Get market profile (cached stats)
        profile = self._store.get_market_profile(ticker)
        has_baseline = profile is not None and profile.get("trade_count_24h", 0) >= 50
        mean_size = profile["mean_size"] if has_baseline else 0
        std_size = profile["std_size"] if has_baseline else 1

        # Get rolling volumes
        vol_1h = self._store.get_volume_in_window(ticker, ROLLING_WINDOW_1H)
        avg_hourly = profile["avg_hourly_volume"] if has_baseline else 0

        # Get taker ratio + recent trade count
        yes_ratio = self._store.get_taker_side_ratio(ticker, ROLLING_WINDOW_1H)
        recent_trades = self._store.get_recent_trades(ticker, ROLLING_WINDOW_1H)
        trades_in_1h = len(recent_trades)

        # Get order book imbalance
        # Book levels are [[price_str, qty_str], ...] from Kalshi orderbook_fp
        book = self._store.get_latest_book(ticker)
        if book:
            yes_depth = sum(float(level[1]) for level in book["yes_bids"] if level)
            no_depth = sum(float(level[1]) for level in book["no_bids"] if level)
        else:
            yes_depth = 0
            no_depth = 0

        # Get recent anomalies for cross-market correlation (60s window)
        # Short window prevents stale anomalies from inflating correlation
        recent_anomalies = self._store.count_recent_anomalies(window_sec=60)

        # ── Compute original 6 features ──────────────────────────────
        features = {}

        if FEATURE_ENABLED.get("trade_size_zscore", True):
            features["trade_size_zscore"] = trade_size_zscore(trade_count, mean_size, std_size)
        if FEATURE_ENABLED.get("volume_spike_ratio", True):
            features["volume_spike_ratio"] = volume_spike_ratio(vol_1h, avg_hourly)
        if FEATURE_ENABLED.get("order_book_imbalance", True):
            features["order_book_imbalance"] = order_book_imbalance(yes_depth, no_depth)
        if FEATURE_ENABLED.get("taker_side_skew", True):
            features["taker_side_skew"] = taker_side_skew(yes_ratio, trades_in_1h)
        if FEATURE_ENABLED.get("price_impact", True):
            features["price_impact"] = price_impact(
                self._last_price.get(ticker), trade.get("yes_price"),
            )
        if FEATURE_ENABLED.get("cross_market_correlation", True):
            features["cross_market_correlation"] = cross_market_correlation(
                ticker, recent_anomalies
            )

        # ── Compute advanced features (7-10) ─────────────────────────
        now = time.time()
        taker_side = trade.get("taker_side", "")

        if FEATURE_ENABLED.get("sweep_score", True):
            features["sweep_score"] = sweep_score(recent_trades, taker_side)

        if FEATURE_ENABLED.get("trade_velocity", True):
            trades_30s = sum(1 for t in recent_trades if (now - t.get("ts", 0)) <= 30)
            # Baseline: average trades per 30s from 24h profile
            trade_count_24h = profile.get("trade_count_24h", 0) if has_baseline else 0
            avg_per_30s = trade_count_24h / 2880.0 if trade_count_24h > 0 else 0
            features["trade_velocity"] = trade_velocity(trades_30s, avg_per_30s)

        if FEATURE_ENABLED.get("size_concentration", True):
            # Use last 5 minutes of trades for concentration
            trades_5m = [t for t in recent_trades if (now - t.get("ts", 0)) <= 300]
            features["size_concentration"] = size_concentration(trades_5m)

        if FEATURE_ENABLED.get("book_pressure_delta", True):
            prev_book = self._store.get_book_snapshot_at(ticker, now - 300)
            if book and prev_book:
                prev_yes = sum(float(l[1]) for l in prev_book.get("yes_bids", []) if l)
                prev_no = sum(float(l[1]) for l in prev_book.get("no_bids", []) if l)
                features["book_pressure_delta"] = book_pressure_delta(
                    yes_depth, no_depth, prev_yes, prev_no
                )
            else:
                features["book_pressure_delta"] = 0.0

        composite = compute_composite_score(features)
        features["composite"] = composite
        features["alert_level"] = classify_alert_level(composite)

        # Track last price for price_impact on next trade
        yes_p = trade.get("yes_price")
        if yes_p is not None:
            self._last_price[ticker] = float(yes_p)

        return features
