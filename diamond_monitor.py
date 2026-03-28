"""
diamond_monitor.py
──────────────────
Main entry point for DIAMOND — Kalshi unusual volume tracker.

Async main loop:
1. Connect WebSocket → subscribe to trade channel
2. Start REST poller for order book snapshots
3. On each trade: store → compute features → check alert thresholds
4. Every 5 min: refresh market metadata, recompute profiles
5. Graceful shutdown on SIGINT/SIGTERM

Usage:
  python diamond_monitor.py                      # Monitor all markets
  python diamond_monitor.py --category politics   # Filter by category
  python diamond_monitor.py --test               # Dry run (no alerts)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import fcntl
import logging
import os
import signal
import sys
import time

from diamond_config import (
    ALERT_THRESHOLD_LOG,
    CONVICTION_ENABLED,
    CONVICTION_FLIP_THRESHOLD,
    CONVICTION_HALF_LIFE_SEC,
    METADATA_REFRESH_SEC,
    PAPER_TRADING_ENABLED,
    REST_POLL_INTERVAL_SEC,
)
from src.bounded_dict import BoundedDict
from src.diamond_alerts import dispatch_alert
from src.diamond_features import FeatureEngine
from src.diamond_paper import PaperTradingEngine
from src.diamond_store import DiamondStore
from src.kalshi_client import KalshiRESTClient, KalshiWSClient

log = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────────────

store = DiamondStore()
rest = KalshiRESTClient()
features = None  # Initialized after store.connect()
paper_engine: PaperTradingEngine | None = None  # Initialized if PAPER_TRADING_ENABLED
market_cache = BoundedDict(max_size=3000)  # ticker → market metadata
running = True
test_mode = False
trade_count = 0
anomaly_count = 0
startup_ts = 0.0  # Set when live streaming begins
last_trade_ts = 0.0  # Timestamp of most recent trade received (for stale detection)
WARMUP_SEC = 180  # 3 min warmup — score trades but suppress alerts
DATA_STALE_SEC = 300  # 5 min with no trades → log STALE warning + force reconnect


# ── Helpers ───────────────────────────────────────────────────────────

_cache_fetch_failures: dict[str, float] = {}  # ticker → fail_timestamp (time-based expiry)
_CACHE_FAILURE_EXPIRY_SEC = 3600  # Re-try failed fetches after 60 minutes


async def _ensure_cached(ticker: str) -> None:
    """Lazy-fetch market details into market_cache if missing."""
    global market_cache
    if ticker in market_cache:
        return
    # Check if failure is still fresh (skip if < 60 min old)
    fail_ts = _cache_fetch_failures.get(ticker)
    if fail_ts is not None and (time.time() - fail_ts) < _CACHE_FAILURE_EXPIRY_SEC:
        return
    try:
        market = await rest.get_market(ticker)
        if market:
            market_cache[ticker] = market
            # Remove from failures if previously failed
            _cache_fetch_failures.pop(ticker, None)
            title = _market_display_name(market)
            store.update_market_profile(ticker, title=title)
            log.info(f"Lazy-cached market: {ticker} → {title}")
    except Exception as e:
        _cache_fetch_failures[ticker] = time.time()
        log.debug(f"Lazy-cache miss for {ticker}: {e}")


def _market_display_name(market: dict) -> str:
    """Build a human-readable display name from a Kalshi market object.

    Regular markets: "LA L at Houston Winner? — Houston"
    MVE/parlays:     "Parlay: PSG, Real Madrid, Bodoe/Glimt"
    """
    title = market.get("title", "")
    yes_sub = market.get("yes_sub_title", "")

    # MVE/parlay markets have titles like "yes PSG,yes Real Madrid,yes Bodoe/Glimt"
    if title.startswith("yes ") or title.startswith("no "):
        # Clean up: strip "yes "/"no " prefix from each leg, join with ", "
        legs = [leg.strip().removeprefix("yes ").removeprefix("no ")
                for leg in title.split(",")]
        return "Parlay: " + ", ".join(legs)

    # Regular markets: combine title + yes_sub for specificity
    if title and yes_sub and yes_sub != title:
        return f"{title} — {yes_sub}"
    return title


# ── Trade Handler ─────────────────────────────────────────────────────


def _normalize_trade(raw: dict) -> dict:
    """Normalize a Kalshi WebSocket trade message into our internal format."""
    trade = {}
    trade["trade_id"] = raw.get("trade_id", "")
    trade["ticker"] = raw.get("market_ticker") or raw.get("ticker", "")
    trade["taker_side"] = raw.get("taker_side", "")

    # count: WS sends "count_fp" as string, REST sends "count" as int
    count_fp = raw.get("count_fp")
    if count_fp:
        trade["count"] = int(float(count_fp))
    else:
        trade["count"] = raw.get("count", raw.get("contracts", 1))

    # prices: WS sends "*_dollars" as strings, REST sends cents as ints
    yes_p = raw.get("yes_price_dollars") or raw.get("yes_price")
    no_p = raw.get("no_price_dollars") or raw.get("no_price")
    trade["yes_price"] = float(yes_p) * 100 if yes_p and float(yes_p) < 10 else (float(yes_p) if yes_p else None)
    trade["no_price"] = float(no_p) * 100 if no_p and float(no_p) < 10 else (float(no_p) if no_p else None)

    # timestamp
    trade["ts"] = raw.get("ts", time.time())

    return trade


async def on_trade(msg: dict):
    """Process an incoming WebSocket trade message."""
    global trade_count, anomaly_count, last_trade_ts

    # WS format: {"type": "trade", "msg": {single trade dict}}
    raw_trade = msg.get("msg", msg)
    trade = _normalize_trade(raw_trade)

    ticker = trade.get("ticker")
    if not ticker:
        return

    # Store the trade
    store.insert_trade(trade)
    trade_count += 1
    last_trade_ts = time.time()

    # Compute features
    result = features.compute(trade)
    alert_level = result["alert_level"]

    if alert_level != "NONE":
        # Lazy-fetch market details if not in cache
        await _ensure_cached(ticker)

        # Get market display name for alert + storage
        market_title = ""
        if ticker in market_cache:
            market_title = _market_display_name(market_cache[ticker])

        # Store anomaly with human-readable title
        store.insert_anomaly(
            ticker=ticker,
            score=result["composite"],
            features=result,
            alert_level=alert_level,
            title=market_title,
        )
        anomaly_count += 1

        # Record signal in conviction tracker (all levels, including during warmup)
        # so conviction builds from full signal history
        if paper_engine is not None and paper_engine._conviction is not None:
            paper_engine._conviction.record_signal(
                ticker, result["composite"], time.time()
            )

        # Suppress push notifications during warmup (still store anomalies)
        in_warmup = (time.time() - startup_ts) < WARMUP_SEC
        if in_warmup:
            if anomaly_count % 50 == 1:
                remaining = int(WARMUP_SEC - (time.time() - startup_ts))
                log.info(f"[WARMUP] {remaining}s remaining — alerts suppressed ({anomaly_count} anomalies logged)")
        elif not test_mode:
            # Dispatch alert (respects cooldown)
            dispatch_alert(
                ticker=ticker,
                alert_level=alert_level,
                composite_score=result["composite"],
                features=result,
                market_title=market_title,
            )
            # Paper trading: auto-bet on anomaly signals
            if paper_engine is not None:
                # Determine entry price based on taker side
                taker_side = trade.get("taker_side", "")
                if taker_side == "yes":
                    price_cents = trade.get("yes_price", 0)
                elif taker_side == "no":
                    price_cents = trade.get("no_price", 0)
                else:
                    price_cents = 0
                if taker_side and price_cents:
                    # Get category from market cache for portfolio intelligence
                    market_category = None
                    if ticker in market_cache:
                        market_category = market_cache[ticker].get("category") or None
                    await paper_engine.on_anomaly(
                        ticker=ticker,
                        taker_side=taker_side,
                        price_cents=float(price_cents),
                        score=result["composite"],
                        level=alert_level,
                        features=result,
                        title=market_title,
                        category=market_category,
                    )
        else:
            log.info(
                f"[TEST] {alert_level} {ticker} score={result['composite']:.2f}"
            )


# ── Background Tasks ──────────────────────────────────────────────────


async def refresh_markets(category: str | None = None):
    """Discover active markets via recent trades + individual market lookups.

    The /markets list endpoint returns volume_fp=0 for all markets (Kalshi doesn't
    populate volume in bulk listings). So we discover active tickers from recent
    trades instead, then fetch individual market details for metadata.
    """
    global market_cache

    # Step 1: Discover active tickers from recent trades (up to 3 pages)
    active_tickers: dict[str, int] = {}  # ticker → trade count
    cursor = None
    for page in range(3):
        data = await rest.get_trades(limit=100, cursor=cursor)
        trades = data.get("trades", [])
        for t in trades:
            ticker = t.get("market_ticker") or t.get("ticker", "")
            if ticker:
                active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
        cursor = data.get("cursor")
        if not cursor or not trades:
            break
        await asyncio.sleep(0.5)

    log.info(f"Discovered {len(active_tickers)} active tickers from recent trades")

    # Step 2: Fetch individual market details for each active ticker
    new_cache = BoundedDict(max_size=3000)
    for i, ticker in enumerate(active_tickers):
        try:
            market = await rest.get_market(ticker)
            if market.get("status") not in ("open", "active"):
                continue
            if category and category.lower() not in (market.get("category", "") or "").lower():
                continue
            new_cache[ticker] = market
        except Exception as e:
            log.debug(f"Failed to fetch market {ticker}: {e}")
        if (i + 1) % 20 == 0:
            log.info(f"  ... fetched details for {i+1}/{len(active_tickers)} tickers")
        await asyncio.sleep(0.3)  # Rate-limit safe

    market_cache = new_cache
    log.info(f"Market cache: {len(market_cache)} active open markets")
    return list(market_cache.keys())


async def metadata_refresh_loop(category: str | None = None):
    """Periodically refresh market metadata and recompute profiles."""
    while running:
        try:
            # Prune expired fetch failures (older than 60 min) instead of clearing all
            now = time.time()
            stale_keys = [k for k, ts in _cache_fetch_failures.items()
                          if (now - ts) > _CACHE_FAILURE_EXPIRY_SEC]
            for k in stale_keys:
                del _cache_fetch_failures[k]
            await refresh_markets(category)

            # Update market profiles for active tickers (with display names from cache)
            active_tickers = store.get_all_active_tickers()
            for ticker in active_tickers:
                title = _market_display_name(market_cache[ticker]) if ticker in market_cache else ""
                store.update_market_profile(ticker, title=title)

            # Prune old data
            store.prune_old_data()

        except Exception as e:
            log.error(f"Metadata refresh error: {e}")

        await asyncio.sleep(METADATA_REFRESH_SEC)


async def orderbook_poll_loop():
    """Periodically poll order books for active markets."""
    while running:
        try:
            active_tickers = store.get_all_active_tickers()
            # Limit polling to most active markets to respect rate limits
            tickers_to_poll = active_tickers[:30]

            for ticker in tickers_to_poll:
                if not running:
                    break
                try:
                    book = await rest.get_orderbook(ticker)
                    store.insert_book_snapshot(ticker, book.get("orderbook", book))
                except Exception as e:
                    log.debug(f"Orderbook fetch failed for {ticker}: {e}")
                await asyncio.sleep(0.5)  # 0.5s between requests

        except Exception as e:
            log.error(f"Orderbook poll error: {e}")

        await asyncio.sleep(REST_POLL_INTERVAL_SEC)


async def status_report_loop():
    """Print periodic status and detect stale data.

    If no trades arrive for DATA_STALE_SEC (5 min), logs a WARNING-level
    STALE message (visible to the watchdog as a problem signal) and cancels
    the WebSocket task to force a reconnect.
    """
    _prev_trade_count = 0
    while running:
        await asyncio.sleep(60)
        stats = store.get_db_stats()

        # Detect stale WebSocket: no new trades for DATA_STALE_SEC
        now = time.time()
        since_last_trade = now - last_trade_ts if last_trade_ts > 0 else now - startup_ts
        is_stale = since_last_trade > DATA_STALE_SEC and (now - startup_ts) > WARMUP_SEC

        if is_stale:
            stale_min = int(since_last_trade / 60)
            log.warning(
                f"STALE: no trades for {stale_min}min — WebSocket likely hung. "
                f"trades={trade_count}, anomalies={anomaly_count}"
            )
            # Force-cancel the WebSocket task so it reconnects
            if _ws_task_ref is not None and not _ws_task_ref.done():
                log.warning("Cancelling stale WebSocket task to force reconnect")
                _ws_task_ref.cancel()
        else:
            new_trades = trade_count - _prev_trade_count
            status_msg = (
                f"Status: {trade_count} trades (+{new_trades}/min), "
                f"{anomaly_count} anomalies, "
                f"DB: {stats['trades']} trades / {stats['anomalies']} anomalies / "
                f"{stats['market_profiles']} profiles"
            )
            # Conviction tracker cleanup + status
            if paper_engine is not None and paper_engine._conviction is not None:
                paper_engine._conviction.cleanup(max_age_sec=1800)
                if paper_engine._conviction._store is not None:
                    paper_engine._conviction._store.prune_conviction_signals(max_age_sec=7200)
                status_msg += f", conviction_events={paper_engine._conviction.active_events}"
            log.info(status_msg)

        _prev_trade_count = trade_count


# ── Main ──────────────────────────────────────────────────────────────


_ws_task_ref: asyncio.Task | None = None  # Reference for stale-detection cancel

async def main(category: str | None = None):
    global features, running, startup_ts, paper_engine, _ws_task_ref

    # Initialize store
    store.connect()
    features = FeatureEngine(store)

    # Initialize paper trading engine if enabled
    if PAPER_TRADING_ENABLED:
        import src.diamond_alerts as alerts_module

        conviction_tracker = None
        if CONVICTION_ENABLED:
            from src.diamond_conviction import ConvictionTracker
            conviction_tracker = ConvictionTracker(
                half_life=CONVICTION_HALF_LIFE_SEC,
                flip_threshold=CONVICTION_FLIP_THRESHOLD,
                store=store,
            )
            # Restore conviction state from SQLite (survives restarts)
            conviction_tracker.load_recent(max_age_sec=3600)
            log.info(
                f"[CONVICTION] Enabled (half_life={CONVICTION_HALF_LIFE_SEC}s, "
                f"flip_threshold={CONVICTION_FLIP_THRESHOLD})"
            )

        paper_engine = PaperTradingEngine(
            store, rest, alert_module=alerts_module,
            conviction_tracker=conviction_tracker,
        )
        log.info("[PAPER] Auto-trading enabled")

    # Initial market fetch
    tickers = await refresh_markets(category)
    if not tickers:
        log.error("No markets found. Check your category filter or API connection.")
        return

    log.info(f"Starting DIAMOND monitor for {len(tickers)} markets")
    if test_mode:
        log.info("TEST MODE — alerts will be logged but not sent")

    # Backfill recent trades to seed market profiles (cold start)
    backfill_tickers = tickers[:20]  # Limit to top 20 to avoid rate limits
    log.info(f"Backfilling recent trades for {len(backfill_tickers)} tickers...")
    for i, ticker in enumerate(backfill_tickers):
        try:
            data = await rest.get_trades(ticker=ticker, limit=100)
            rest_trades = data.get("trades", [])
            for rt in rest_trades:
                nt = _normalize_trade(rt)
                if nt.get("ticker"):
                    store.insert_trade(nt)
            if (i + 1) % 5 == 0:
                log.info(f"  ... backfilled {i+1}/{len(backfill_tickers)} tickers")
        except Exception as e:
            log.debug(f"Backfill failed for {ticker}: {e}")
        await asyncio.sleep(0.5)  # 0.5s between requests (safe for 20 req/sec limit)

    # Build initial profiles (with display names from market cache)
    for ticker in backfill_tickers:
        title = _market_display_name(market_cache[ticker]) if ticker in market_cache else ""
        store.update_market_profile(ticker, title=title)
    log.info(f"Baseline profiles built for {len(backfill_tickers)} tickers. Starting live stream...")

    # Mark startup time — alerts suppressed during warmup
    startup_ts = time.time()
    log.info(f"Warmup: alerts suppressed for {WARMUP_SEC}s while baselines stabilize")

    # Start background tasks
    tasks = [
        asyncio.create_task(metadata_refresh_loop(category)),
        asyncio.create_task(orderbook_poll_loop()),
        asyncio.create_task(status_report_loop()),
    ]
    if paper_engine is not None:
        tasks.append(asyncio.create_task(paper_engine.poll_loop()))

    # ML scorer daily retrain (if enabled)
    try:
        from diamond_config import ML_SCORER_ENABLED, ML_MODEL_PATH, DB_PATH, ML_MIN_SAMPLES
        if ML_SCORER_ENABLED:
            async def ml_retrain_loop():
                """Retrain ML model daily as more trades settle."""
                await asyncio.sleep(300)  # Wait 5 min after startup
                while running:
                    try:
                        from src.diamond_ml import DiamondMLScorer
                        scorer = DiamondMLScorer(db_path=DB_PATH, model_path=ML_MODEL_PATH)
                        metrics = scorer.train(min_samples=ML_MIN_SAMPLES)
                        if "error" not in metrics:
                            log.info(f"[ML] Retrained: {metrics['model_type']}, "
                                     f"Brier={metrics['cv_brier']:.4f}, "
                                     f"AUC={metrics['cv_auc']:.3f}, "
                                     f"n={metrics['n_samples']}")
                            # Reload in feature engine
                            if features and hasattr(features, '_ml_scorer') and features._ml_scorer:
                                features._ml_scorer.load_model()
                        else:
                            log.info(f"[ML] Retrain skipped: {metrics.get('error')}")
                    except Exception as e:
                        log.error(f"[ML] Retrain failed: {e}")
                    await asyncio.sleep(86400)  # Daily
            tasks.append(asyncio.create_task(ml_retrain_loop()))
    except ImportError:
        pass

    # Start WebSocket — subscribe in batches (WS may have limits)
    ws = KalshiWSClient(on_trade=on_trade)
    BATCH_SIZE = 100
    first_batch = tickers[:BATCH_SIZE]
    remaining = tickers[BATCH_SIZE:]

    async def ws_with_subscribe():
        """WebSocket connection loop — auto-restarts if cancelled by stale detection."""
        while running:
            try:
                # connect() handles reconnection internally for normal errors;
                # stale detection cancels this task to force a full restart
                await ws.connect(tickers=first_batch, channels=["trade"])
            except asyncio.CancelledError:
                if not running:
                    raise  # Clean shutdown — propagate
                log.warning("WebSocket task cancelled (stale detection) — restarting in 5s")
                await asyncio.sleep(5)
                continue

    ws_task = asyncio.create_task(ws_with_subscribe())
    _ws_task_ref = ws_task
    tasks.append(ws_task)

    # Handle shutdown — use asyncio-safe signal handlers (not signal.signal)
    loop = asyncio.get_running_loop()

    def shutdown(sig_name: str):
        global running
        running = False
        log.info(f"Received {sig_name}, shutting down...")
        for t in tasks:
            t.cancel()

    for sig_enum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_enum, shutdown, sig_enum.name)

    # Wait for all tasks
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        await ws.disconnect()
        await rest.close()
        store.close()
        log.info(
            f"DIAMOND stopped. Processed {trade_count} trades, "
            f"flagged {anomaly_count} anomalies."
        )


def cli():
    parser = argparse.ArgumentParser(description="DIAMOND — Kalshi Unusual Volume Tracker")
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Filter markets by category (e.g., politics, economics, sports)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: log alerts but don't send notifications",
    )
    args = parser.parse_args()

    global test_mode
    test_mode = args.test

    # ── PID lock: prevent duplicate monitor instances ──
    lockfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".diamond_monitor.lock")
    lock_fd = open(lockfile_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("FATAL: Another diamond_monitor is already running. Exiting.", file=sys.stderr)
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    atexit.register(lambda: os.unlink(lockfile_path))

    # Suppress sklearn feature-name warnings that flood the logs
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(main(category=args.category))


if __name__ == "__main__":
    cli()
