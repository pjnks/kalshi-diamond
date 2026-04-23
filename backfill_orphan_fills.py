"""
backfill_orphan_fills.py
────────────────────────
Recover fill_price, fill_count, and settlement outcome for paper_trades rows
that were marked 'stuck' due to the line-544 bug in diamond_paper.py (fixed
2026-04-16). The bug hardcoded `fill_price=None` on the GTC-cancel-window
path when a pending order filled during the 15-30s wait, producing orphan
rows with status='filled' but no price data.

This script is the data-recovery counterpart to that patch: it walks the
stuck rows, queries Kalshi for the original order + market data, and
restores each row to its correct final state (settled with P&L, voided, or
re-filled pending settlement).

Recovery algorithm
------------------
For each row with status='stuck' AND order_id IS NOT NULL:
  1. GET /portfolio/orders/{order_id} — extract maker_fill_cost_dollars
     and taker_fill_cost_dollars. Divide total cost by fill_count to get
     average fill price in cents. This is the ONLY source of truth for
     fill price — what we actually paid, not what we bid.
  2. GET /markets/{ticker} — check status and result. If market resolved,
     compute P&L from fill_price vs settlement_value (100 if won, 0 if lost).
  3. UPDATE the row: set fill_price, fill_count, and drive through the
     existing store settlement methods (update_paper_settlement,
     mark_paper_voided) so realized_edge and settled_at are computed
     consistently with live-traffic settlements.

Usage
-----
    PYTHONPATH=. python backfill_orphan_fills.py          # dry-run: report only
    PYTHONPATH=. python backfill_orphan_fills.py --apply  # mutate the DB

The --apply gate is MANDATORY for DB writes. Dry-run prints exactly what
would change, so the collaborator can audit P&L attribution before
committing the row resurrections.

Idempotency
-----------
Safe to re-run. Once a row transitions stuck→settled/voided/unfilled, it
no longer matches the SELECT predicate. Re-running finds nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Any

from src.diamond_store import DiamondStore
from src.kalshi_client import KalshiRESTClient


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _recover_one(rest: KalshiRESTClient, row: Any, log: logging.Logger) -> dict | None:
    """Query Kalshi for one stuck row's true state. Returns a recovery plan
    dict, or None if unrecoverable (missing order, API error, etc.)."""
    ticker = row["ticker"]
    order_id = row["order_id"]
    side = row["side"]

    if not order_id:
        log.warning(f"  {ticker}: no order_id — cannot backfill")
        return None

    # ── Step 1: Order data → fill_price, fill_count ──
    try:
        order = await rest.get_order(order_id)
    except Exception as e:
        log.error(f"  {ticker}: order fetch failed: {type(e).__name__}: {e}")
        return None

    raw_fill = (order.get("fill_count_fp")
                or order.get("count_filled_fp")
                or order.get("count_filled")
                or order.get("fill_count")
                or "0")
    try:
        fill_count = int(float(raw_fill))
    except (TypeError, ValueError):
        fill_count = 0

    if fill_count <= 0:
        log.info(f"  {ticker}: order never filled on Kalshi → downgrade to 'unfilled'")
        return {"action": "unfilled", "fill_price": 0, "fill_count": 0}

    try:
        maker_cost = float(order.get("maker_fill_cost_dollars", "0") or "0")
        taker_cost = float(order.get("taker_fill_cost_dollars", "0") or "0")
    except (TypeError, ValueError):
        log.warning(f"  {ticker}: cost-field parse error — skipping")
        return None

    total_cost = maker_cost + taker_cost
    if total_cost <= 0:
        log.warning(f"  {ticker}: fill_count={fill_count} but total_cost=0 — anomalous, skipping")
        return None

    fill_price = int(round(total_cost * 100 / fill_count))

    # ── Step 2: Market data → settlement outcome ──
    try:
        market = await rest.get_market(ticker)
    except Exception as e:
        log.warning(
            f"  {ticker}: market fetch failed ({type(e).__name__}) — "
            f"restoring fill data only, poll will settle later"
        )
        return {"action": "refill", "fill_price": fill_price, "fill_count": fill_count}

    m_status = (market.get("status") or "").strip().lower()
    result = (market.get("result") or "").strip().lower()

    # Voided / cancelled / refunded: refund at fill_price, P&L = 0
    TERMINAL_VOID = {"voided", "canceled", "cancelled", "refunded"}
    if m_status in TERMINAL_VOID or result == "void":
        log.info(
            f"  {ticker}: side={side} fill={fill_price}¢ x{fill_count} "
            f"→ VOIDED (refund, pnl=0)"
        )
        return {
            "action": "voided",
            "fill_price": fill_price,
            "fill_count": fill_count,
        }

    # Still pending: restore fill data, let live poll handle settlement
    if result not in ("yes", "no"):
        log.info(
            f"  {ticker}: market not yet resolved (status={m_status}, result={result!r}) "
            f"— restore fill, poll will settle"
        )
        return {"action": "refill", "fill_price": fill_price, "fill_count": fill_count}

    # Resolved — compute P&L
    won = (side == "yes" and result == "yes") or (side == "no" and result == "no")
    settlement_value_cents = 100 if won else 0
    pnl_cents = (settlement_value_cents - fill_price) * fill_count

    log.info(
        f"  {ticker}: side={side} fill={fill_price}¢ x{fill_count} "
        f"result={result} won={won} pnl={pnl_cents:+d}¢"
    )

    return {
        "action": "settled",
        "fill_price": fill_price,
        "fill_count": fill_count,
        "settlement": result,
        "pnl_cents": pnl_cents,
    }


def _apply_recovery(store: DiamondStore, row_id: int, ticker: str, plan: dict, log: logging.Logger) -> None:
    """Execute a recovery plan atomically. Uses direct SQL for fill-data restore
    (preserves original timestamps), then drives settlement via the existing
    store methods so realized_edge is computed consistently."""
    action = plan["action"]

    if action == "unfilled":
        # Nothing filled on Kalshi — just clear the corrupt state
        store._conn.execute(
            "UPDATE paper_trades SET fill_price = 0, fill_count = 0, "
            "status = 'unfilled', settled_at = NULL WHERE id = ?",
            (row_id,),
        )
        store._conn.commit()
        log.info(f"  [APPLIED] {ticker}: marked unfilled")
        return

    # All other actions require valid fill data first
    # Use filled_at ≈ opened_at (we don't know exact fill time, conservative estimate)
    store._conn.execute(
        """UPDATE paper_trades
           SET fill_price = ?, fill_count = ?,
               filled_at = COALESCE(filled_at, opened_at)
           WHERE id = ?""",
        (plan["fill_price"], plan["fill_count"], row_id),
    )
    store._conn.commit()

    if action == "refill":
        # Market not yet resolved — re-open as 'filled' for live poll to settle
        store._conn.execute(
            "UPDATE paper_trades SET status = 'filled', settled_at = NULL, "
            "settlement = NULL, pnl_cents = NULL WHERE id = ?",
            (row_id,),
        )
        store._conn.commit()
        log.info(f"  [APPLIED] {ticker}: restored fill, status='filled' (poll will settle)")
    elif action == "voided":
        # Use existing mark_paper_voided method (added to store in stuck-trade fix)
        store.mark_paper_voided(row_id)
        log.info(f"  [APPLIED] {ticker}: marked voided")
    elif action == "settled":
        # Use existing update_paper_settlement — computes realized_edge correctly
        store.update_paper_settlement(row_id, plan["settlement"], plan["pnl_cents"])
        log.info(
            f"  [APPLIED] {ticker}: settled, settlement={plan['settlement']} "
            f"pnl={plan['pnl_cents']:+d}¢"
        )
    else:
        log.error(f"  [APPLIED] {ticker}: unknown action {action!r} — no-op")


async def main(apply: bool, verbose: bool) -> int:
    _configure_logging(verbose)
    log = logging.getLogger("backfill")

    store = DiamondStore()
    store.connect()  # DiamondStore.__init__ does NOT open connection
    rest = KalshiRESTClient()

    rows = store._conn.execute(
        """SELECT id, ticker, side, entry_price, order_id, opened_at,
                  anomaly_score, anomaly_level
           FROM paper_trades
           WHERE status = 'stuck' AND order_id IS NOT NULL
           ORDER BY opened_at"""
    ).fetchall()

    if not rows:
        log.info("No stuck rows with order_id found. Nothing to do.")
        await rest.close()
        return 0

    log.info(f"Found {len(rows)} stuck row(s) with order_id. Probing Kalshi for recovery data...")
    plans: list[tuple[int, str, dict]] = []
    for row in rows:
        log.info(f"id={row['id']} {row['ticker']}")
        plan = await _recover_one(rest, row, log)
        if plan is not None:
            plans.append((row["id"], row["ticker"], plan))
        # Rate-limit-friendly pacing (20 req/sec Basic tier, 2 calls per row)
        await asyncio.sleep(0.3)

    # ── Summary ──
    log.info("")
    log.info(f"Recoverable: {len(plans)} of {len(rows)}")
    action_counts: dict[str, int] = {}
    pnl_total = 0
    for _, _, p in plans:
        action_counts[p["action"]] = action_counts.get(p["action"], 0) + 1
        if p["action"] == "settled":
            pnl_total += p["pnl_cents"]
    log.info(f"  Actions: {action_counts}")
    if action_counts.get("settled"):
        log.info(f"  Net P&L from settled recoveries: {pnl_total:+d}¢ (${pnl_total/100:+.2f})")

    if not apply:
        log.warning("DRY RUN — pass --apply to mutate the DB. No writes performed.")
        await rest.close()
        return 0

    # ── Apply ──
    log.info("")
    log.info("Applying recoveries...")
    for row_id, ticker, plan in plans:
        try:
            _apply_recovery(store, row_id, ticker, plan, log)
        except Exception as e:
            log.error(f"  [APPLY FAILED] id={row_id} {ticker}: {type(e).__name__}: {e}", exc_info=True)

    # Final status check
    log.info("")
    log.info("Post-backfill status distribution:")
    for r in store._conn.execute(
        "SELECT status, COUNT(*) n FROM paper_trades GROUP BY status ORDER BY n DESC"
    ):
        log.info(f"  {r['status']:12s} {r['n']}")

    await rest.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually mutate the DB (default: dry-run)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG-level logging")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, verbose=args.verbose)))
