"""
Tuning session: run for 60s, collect trades + feature scores, then print diagnostics.
Helps calibrate thresholds and feature weights.
"""

import asyncio
import logging
import time
from collections import Counter, defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tuning")

from diamond_monitor import _normalize_trade
from src.diamond_store import DiamondStore
from src.diamond_features import FeatureEngine
from src.kalshi_client import KalshiRESTClient, KalshiWSClient

DURATION = 60  # seconds

store = DiamondStore()
store.connect()
engine = FeatureEngine(store)
rest = KalshiRESTClient()

# Collection buffers
all_scores = []
feature_scores = defaultdict(list)  # feature_name → [scores]
ticker_counts = Counter()
level_counts = Counter()
big_trades = []  # trades with count >= 100
alert_feature_contributions = defaultdict(lambda: defaultdict(list))  # level → feat → [scores]


async def on_trade(msg):
    raw = msg.get("msg", msg)
    trade = _normalize_trade(raw)
    ticker = trade.get("ticker")
    if not ticker:
        return

    store.insert_trade(trade)
    result = engine.compute(trade)

    # Store anomalies so cross_market_correlation can fire
    if result["alert_level"] != "NONE":
        store.insert_anomaly(ticker, result["composite"], result, result["alert_level"])

    # Collect stats
    composite = result["composite"]
    all_scores.append(composite)
    ticker_counts[ticker] += 1
    level_counts[result["alert_level"]] += 1

    feat_names = ["trade_size_zscore", "volume_spike_ratio", "order_book_imbalance",
                   "taker_side_skew", "price_impact", "cross_market_correlation"]
    for feat in feat_names:
        feature_scores[feat].append(result[feat])

    # Track feature contributions per alert level (for flagged trades)
    level = result["alert_level"]
    if level != "NONE":
        for feat in feat_names:
            alert_feature_contributions[level][feat].append(result[feat])

    if trade["count"] >= 100:
        big_trades.append({
            "ticker": ticker,
            "count": trade["count"],
            "composite": composite,
            "level": result["alert_level"],
            "top_feature": max(
                [(k, v) for k, v in result.items()
                 if k not in ("composite", "alert_level")],
                key=lambda x: x[1]
            ),
        })


async def main():
    # Get actively-traded tickers
    data = await rest.get_trades(limit=50)
    active_tickers = list(set(t["ticker"] for t in data.get("trades", [])))
    log.info(f"Subscribing to {len(active_tickers)} active tickers")

    # Backfill: fetch recent REST trades to seed profiles
    log.info("Backfilling recent trades from REST API...")
    for ticker in active_tickers:
        try:
            data2 = await rest.get_trades(ticker=ticker, limit=100)
            rest_trades = data2.get("trades", [])
            for rt in rest_trades:
                from diamond_monitor import _normalize_trade
                nt = _normalize_trade(rt)
                if nt.get("ticker"):
                    store.insert_trade(nt)
        except Exception as e:
            log.debug(f"Backfill failed for {ticker}: {e}")
        await asyncio.sleep(0.1)

    # Build initial profiles from backfilled data
    for ticker in active_tickers:
        store.update_market_profile(ticker)
    log.info(f"Profiles built for {len(active_tickers)} tickers")

    ws = KalshiWSClient(on_trade=on_trade)
    ws_task = asyncio.create_task(ws.connect(tickers=active_tickers, channels=["trade"]))

    log.info(f"Collecting data for {DURATION}s...")

    # Background: order book polling + profile updates
    async def book_poller():
        """Poll order books every 15s for active tickers."""
        while True:
            try:
                for t in active_tickers[:20]:
                    try:
                        book = await rest.get_orderbook(t)
                        store.insert_book_snapshot(t, book.get("orderbook", book))
                    except Exception:
                        pass
                    await asyncio.sleep(0.15)
            except Exception as e:
                log.debug(f"Book poll error: {e}")
            await asyncio.sleep(15)

    async def profile_updater():
        """Update profiles every 20s."""
        while True:
            await asyncio.sleep(20)
            for t in store.get_all_active_tickers():
                store.update_market_profile(t)
            log.info("Profiles updated")

    book_task = asyncio.create_task(book_poller())
    profile_task = asyncio.create_task(profile_updater())
    await asyncio.sleep(DURATION)
    await ws.disconnect()
    ws_task.cancel()
    book_task.cancel()
    profile_task.cancel()
    await rest.close()

    # ── Diagnostics ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  DIAMOND TUNING REPORT — {len(all_scores)} trades in {DURATION}s")
    print("=" * 70)

    if not all_scores:
        print("No trades captured. Markets may be closed.")
        store.close()
        return

    # Score distribution
    import statistics
    print(f"\n--- Composite Score Distribution ---")
    print(f"  Min:    {min(all_scores):.3f}")
    print(f"  Max:    {max(all_scores):.3f}")
    print(f"  Mean:   {statistics.mean(all_scores):.3f}")
    print(f"  Median: {statistics.median(all_scores):.3f}")
    print(f"  Stdev:  {statistics.stdev(all_scores):.3f}" if len(all_scores) > 1 else "")

    # Score histogram
    buckets = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    print(f"\n--- Score Histogram ---")
    for i in range(len(buckets) - 1):
        lo, hi = buckets[i], buckets[i + 1]
        count = sum(1 for s in all_scores if lo <= s < hi)
        bar = "#" * (count * 50 // max(len(all_scores), 1))
        print(f"  [{lo:.1f}-{hi:.1f}) {count:5d}  {bar}")

    # Alert level breakdown
    print(f"\n--- Alert Levels ---")
    for level in ["NONE", "LOG", "NOTABLE", "ALERT", "CRITICAL"]:
        cnt = level_counts.get(level, 0)
        pct = cnt * 100 / len(all_scores)
        print(f"  {level:10s}: {cnt:5d} ({pct:.1f}%)")

    # Per-feature stats
    print(f"\n--- Feature Firing Rates (score > 0) ---")
    for feat, scores in sorted(feature_scores.items()):
        firing = sum(1 for s in scores if s > 0)
        avg_when_firing = (
            statistics.mean([s for s in scores if s > 0])
            if firing > 0 else 0
        )
        print(
            f"  {feat:30s}: fires {firing:4d}/{len(scores):4d} "
            f"({firing * 100 / max(len(scores), 1):.0f}%)  "
            f"avg={avg_when_firing:.3f}"
        )

    # Top tickers by volume
    print(f"\n--- Top 10 Tickers by Trade Count ---")
    for ticker, cnt in ticker_counts.most_common(10):
        print(f"  {ticker:55s}: {cnt:4d} trades")

    # Large trades
    if big_trades:
        print(f"\n--- Large Trades (count >= 100) ---")
        for bt in sorted(big_trades, key=lambda x: -x["count"])[:15]:
            feat_name, feat_val = bt["top_feature"]
            print(
                f"  {bt['ticker']:50s} count={bt['count']:6d}  "
                f"score={bt['composite']:.3f} [{bt['level']}]  "
                f"top: {feat_name}={feat_val:.2f}"
            )

    # Feature contributions per alert level
    if alert_feature_contributions:
        print(f"\n--- Feature Contributions by Alert Level ---")
        feat_names = ["trade_size_zscore", "volume_spike_ratio", "order_book_imbalance",
                       "taker_side_skew", "price_impact", "cross_market_correlation"]
        for level in ["LOG", "NOTABLE", "ALERT", "CRITICAL"]:
            if level not in alert_feature_contributions:
                continue
            feats = alert_feature_contributions[level]
            n = len(feats[feat_names[0]]) if feat_names[0] in feats else 0
            print(f"\n  {level} ({n} trades):")
            for feat in feat_names:
                scores = feats.get(feat, [])
                if not scores:
                    print(f"    {feat:30s}: n/a")
                    continue
                fires = sum(1 for s in scores if s > 0)
                avg = statistics.mean(scores) if scores else 0
                avg_f = statistics.mean([s for s in scores if s > 0]) if fires > 0 else 0
                print(
                    f"    {feat:30s}: fires {fires:3d}/{len(scores):3d} "
                    f"({fires * 100 / max(len(scores), 1):4.0f}%)  "
                    f"avg_all={avg:.3f}  avg_firing={avg_f:.3f}"
                )

    # Feature co-firing analysis (which features fire together)
    print(f"\n--- Feature Co-firing (when 2+ features fire) ---")
    from itertools import combinations
    feat_names = ["trade_size_zscore", "volume_spike_ratio", "order_book_imbalance",
                   "taker_side_skew", "price_impact", "cross_market_correlation"]
    cofires = Counter()
    total_trades = len(feature_scores.get(feat_names[0], []))
    for i in range(total_trades):
        firing = [f for f in feat_names if feature_scores[f][i] > 0]
        if len(firing) >= 2:
            for pair in combinations(sorted(firing), 2):
                cofires[pair] += 1
    for pair, cnt in cofires.most_common(10):
        pct = cnt * 100 / max(total_trades, 1)
        print(f"  {pair[0]:25s} + {pair[1]:25s}: {cnt:4d} ({pct:.1f}%)")

    # DB stats
    stats = store.get_db_stats()
    print(f"\n--- DB Stats ---")
    print(f"  Trades: {stats['trades']}, Anomalies: {stats['anomalies']}, Profiles: {stats['market_profiles']}")

    store.close()
    print("\nDone. Use these results to adjust thresholds in diamond_config.py")


asyncio.run(main())
