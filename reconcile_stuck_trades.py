"""
reconcile_stuck_trades.py
─────────────────────────
One-shot drain script to resolve paper_trades stuck in status='filled'.

Context
-------
As of 2026-04-15 the monitor had 13 open trades (oldest 16.5 days) trapped in
the settlement poll loop because:
  1. Kalshi added a 'determined' status not in the old whitelist.
  2. Exceptions in the settlement path were logged at DEBUG and swallowed.
  3. No timeout / quarantine ever terminated the loop.

diamond_paper.py has been patched to handle all three. This script simply
invokes the newly-patched check_settlements() once against the live store
to drain the backlog before the patched monitor restarts.

Usage (from /home/ubuntu/kalshi-diamond on the VM):
    PYTHONPATH=. python reconcile_stuck_trades.py          # dry-run: show only
    PYTHONPATH=. python reconcile_stuck_trades.py --apply  # actually resolve

The --apply flag is required to mutate the database, to prevent accidental
runs from booking P&L into a production DB without a manual green light.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from src.diamond_store import DiamondStore
from src.kalshi_client import KalshiRESTClient
from src.diamond_paper import PaperTradingEngine


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def main(apply: bool, verbose: bool) -> int:
    _configure_logging(verbose)
    log = logging.getLogger("reconcile")

    store = DiamondStore()
    store.connect()  # DiamondStore.__init__ does NOT open the connection; must call connect() explicitly
    rest = KalshiRESTClient()
    engine = PaperTradingEngine(store=store, rest_client=rest)

    # ── Snapshot the backlog BEFORE resolving so we can report the delta ──
    before = store.get_open_paper_trades()
    log.info("Found %d stuck trades (status='filled') before drain:", len(before))
    now = time.time()
    for t in before:
        age_hr = (now - (t.get("opened_at") or now)) / 3600.0
        log.info(
            "  id=%d  %-55s  side=%s  fill=%s¢  age=%.1fh",
            t["id"], t["ticker"], t.get("side"), t.get("fill_price"), age_hr,
        )
    if not before:
        log.info("Nothing to do. Exiting cleanly.")
        await rest.close()
        return 0

    if not apply:
        log.warning("DRY RUN — pass --apply to actually resolve. No DB writes performed.")
        await rest.close()
        return 0

    # ── Invoke the patched check_settlements() — single source of truth ──
    log.info("Invoking patched check_settlements()...")
    await engine.check_settlements()

    # ── Report what actually moved ──
    after = {t["id"]: t for t in store.get_open_paper_trades()}
    resolved_ids = [t["id"] for t in before if t["id"] not in after]

    # Pull the resolved rows to classify outcomes
    if resolved_ids:
        placeholders = ",".join("?" * len(resolved_ids))
        rows = store._conn.execute(
            f"SELECT id, ticker, status, settlement, pnl_cents "
            f"FROM paper_trades WHERE id IN ({placeholders})",
            resolved_ids,
        ).fetchall()
        by_status: dict[str, int] = {}
        pnl_total = 0.0
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["pnl_cents"] is not None:
                pnl_total += float(r["pnl_cents"])
            log.info(
                "  RESOLVED id=%d  %-55s  status=%-8s  settlement=%-6s  pnl=%s",
                r["id"], r["ticker"], r["status"], r["settlement"],
                f"{r['pnl_cents']:+.0f}¢" if r["pnl_cents"] is not None else "NULL",
            )
        log.info("Status breakdown: %s", dict(by_status))
        log.info("Realized P&L from drain: %+.0f¢ ($%+.2f)", pnl_total, pnl_total / 100.0)

    log.info(
        "Drain complete: %d/%d trades resolved, %d still stuck (will retry next poll).",
        len(resolved_ids), len(before), len(after),
    )

    await rest.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB (default: dry-run)")
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, verbose=args.verbose)))
