#!/usr/bin/env python3
"""Reconcile paper_trades DB with actual Kalshi fill/settlement data.

Fixes the field-name mismatch where our code read deprecated fields
(count_filled, yes_price) while Kalshi API returns _fp/_dollars fields
(fill_count_fp, yes_price_dollars).

This script:
1. Fetches all fills from Kalshi API
2. Fetches all executed orders
3. Matches them to paper_trades by order_id
4. Updates fill status, fill_price, fill_count
5. Checks settlement status for filled trades
6. Computes P&L for settled markets
"""
import asyncio
import sys
import sqlite3
import time

sys.path.insert(0, ".")
from src.kalshi_client import KalshiRESTClient


async def reconcile():
    rest = KalshiRESTClient()
    conn = sqlite3.connect("diamond_trades.db")
    conn.row_factory = sqlite3.Row

    try:
        # 1. Fetch ALL fills from Kalshi
        print("Fetching all fills from Kalshi...")
        all_fills = []
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await rest._request("GET", "/portfolio/fills", params=params)
            fills = resp.get("fills", [])
            if not fills:
                break
            all_fills.extend(fills)
            cursor = resp.get("cursor")
            if not cursor or len(fills) < 100:
                break
        print(f"  Found {len(all_fills)} fills on Kalshi")

        # 2. Build fill lookup by order_id
        # Multiple fills can map to one order (partial fills)
        fill_by_order = {}
        for f in all_fills:
            oid = f.get("order_id", "")
            if oid not in fill_by_order:
                fill_by_order[oid] = []
            fill_by_order[oid].append(f)

        # 3. Get all paper_trades from DB
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE order_id IS NOT NULL AND order_id != ''"
        ).fetchall()
        print(f"  Found {len(trades)} paper trades with order_ids")

        updated = 0
        settled = 0
        already_correct = 0

        for trade in trades:
            oid = trade["order_id"]
            trade_id = trade["id"]
            ticker = trade["ticker"]
            side = trade["side"]
            old_status = trade["status"]

            # Check if we have fills for this order
            if oid in fill_by_order:
                fills = fill_by_order[oid]
                total_count = sum(float(f.get("count_fp", 0)) for f in fills)
                fill_count = int(total_count)

                # Get fill price from the fill data
                if side == "yes":
                    fill_price_dollars = float(fills[0].get("yes_price_dollars", 0))
                else:
                    fill_price_dollars = float(fills[0].get("no_price_dollars", 0))
                fill_price_cents = int(fill_price_dollars * 100)

                if fill_count > 0 and old_status in ("pending", "unfilled"):
                    # This was filled but we didn't know!
                    conn.execute(
                        "UPDATE paper_trades SET status = 'filled', fill_count = ?, fill_price = ? WHERE id = ?",
                        (fill_count, fill_price_cents, trade_id),
                    )
                    updated += 1
                    print(f"  FIXED: {ticker} {side} → filled {fill_count}x @ {fill_price_cents}¢ (was {old_status})")
                elif fill_count > 0 and old_status == "filled":
                    already_correct += 1
                elif fill_count > 0 and old_status == "settled":
                    already_correct += 1
            else:
                # No fills found — if status is "unfilled" that's correct
                if old_status == "unfilled":
                    already_correct += 1

        conn.commit()
        print(f"\n  Updated: {updated}, Already correct: {already_correct}")

        # 4. Now check settlement for all filled trades
        print("\nChecking settlements for filled trades...")
        filled_trades = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'filled'"
        ).fetchall()
        print(f"  Found {len(filled_trades)} filled trades to check")

        for trade in filled_trades:
            ticker = trade["ticker"]
            side = trade["side"]
            fill_price = trade["fill_price"]
            fill_count = trade["fill_count"]
            trade_id = trade["id"]

            try:
                market = await rest.get_market(ticker)
                market_status = market.get("status", "")
                result = market.get("result", "")

                if market_status in ("settled", "finalized") and result:
                    # Compute P&L
                    if result == side:
                        # Won: payout is 100¢ per contract, minus what we paid
                        pnl = (100 - fill_price) * fill_count
                    else:
                        # Lost: lose what we paid
                        pnl = -fill_price * fill_count

                    conn.execute(
                        "UPDATE paper_trades SET status = 'settled', pnl_cents = ?, settled_at = ? WHERE id = ?",
                        (pnl, int(time.time()), trade_id),
                    )
                    settled += 1
                    outcome = "WIN" if pnl > 0 else "LOSS"
                    print(f"  SETTLED: {ticker} {side} @ {fill_price}¢ → result={result} P&L={pnl:+d}¢ ({outcome})")
                elif market_status in ("settled", "finalized") and not result:
                    # No result yet (rare)
                    print(f"  SETTLED but no result: {ticker} (status={market_status})")
                else:
                    print(f"  OPEN: {ticker} (status={market_status})")

                # Rate limit
                await asyncio.sleep(0.1)

            except Exception as e:
                print(f"  ERROR checking {ticker}: {e}")

        conn.commit()
        print(f"\n  Newly settled: {settled}")

        # 5. Summary
        print("\n" + "=" * 60)
        print("RECONCILIATION COMPLETE")
        print("=" * 60)
        for status in ["pending", "filled", "settled", "unfilled", "cancelled"]:
            n = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = ?", (status,)).fetchone()[0]
            if n > 0:
                print(f"  {status}: {n}")

        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades WHERE status = 'settled'"
        ).fetchone()[0]
        wins = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'settled' AND pnl_cents > 0"
        ).fetchone()[0]
        total_settled = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'settled'"
        ).fetchone()[0]
        if total_settled > 0:
            print(f"\n  Win rate: {wins}/{total_settled} ({wins/total_settled*100:.0f}%)")
            print(f"  Total P&L: {total_pnl:+d}¢ (${total_pnl/100:+.2f})")
            print(f"  Avg P&L per trade: {total_pnl/total_settled:+.1f}¢")
        else:
            print("\n  No settled trades yet")

    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await rest.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(reconcile())
