"""Quick 20-second live test of the full DIAMOND pipeline."""

import asyncio
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from diamond_monitor import _normalize_trade
from src.diamond_store import DiamondStore
from src.diamond_features import FeatureEngine
from src.kalshi_client import KalshiRESTClient, KalshiWSClient

store = DiamondStore()
store.connect()
features = FeatureEngine(store)
rest = KalshiRESTClient()
trade_count = 0


async def on_trade(msg):
    global trade_count
    raw = msg.get("msg", msg)
    trade = _normalize_trade(raw)
    ticker = trade.get("ticker")
    if not ticker:
        return
    store.insert_trade(trade)
    trade_count += 1
    result = features.compute(trade)
    level = result["alert_level"]
    tag = "ANOMALY" if level != "NONE" else "trade"
    print(
        f"  [{tag}] {ticker} count={trade['count']} "
        f"score={result['composite']:.3f} level={level}"
    )


async def main():
    global trade_count
    # Subscribe to known-active tickers
    data = await rest.get_trades(limit=20)
    active_tickers = list(set(t["ticker"] for t in data.get("trades", [])))
    print(f"Subscribing to {len(active_tickers)} active tickers...")

    ws = KalshiWSClient(on_trade=on_trade)
    ws_task = asyncio.create_task(ws.connect(tickers=active_tickers, channels=["trade"]))

    print("Listening for trades (20s)...")
    await asyncio.sleep(20)
    await ws.disconnect()
    ws_task.cancel()

    stats = store.get_db_stats()
    print(f"\nResults: {trade_count} trades captured in 20s")
    print(f"DB stats: {stats}")

    await rest.close()
    store.close()


asyncio.run(main())
