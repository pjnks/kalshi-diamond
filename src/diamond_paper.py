"""
diamond_paper.py
────────────────
Auto-trading engine for DIAMOND — places real micro-bets on anomaly signals.

When an ALERT+ anomaly fires, buys 1 contract on the detected "informed" side
via limit IOC order. Holds to settlement for clean signal quality measurement.

Lifecycle: on_anomaly() → place order → poll_loop() checks fills + settlements → P&L
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from diamond_config import (
    PAPER_CONTRACTS_PER_TRADE,
    PAPER_MAX_UNREALIZED_CENTS,
    PAPER_MAX_POSITIONS,
    PAPER_MIN_ALERT_LEVEL,
    PAPER_MIN_PRICE_CENTS,
    PAPER_NOTIFY_TRADES,
    PAPER_POLL_INTERVAL_SEC,
    PAPER_TRADING_ENABLED,
)

log = logging.getLogger(__name__)

# Alert level ordering for comparison
_LEVEL_ORDER = {"NONE": 0, "LOG": 1, "NOTABLE": 2, "ALERT": 3, "CRITICAL": 4}


class PaperTradingEngine:
    """Manages the lifecycle of auto-placed micro-bets on anomaly signals."""

    def __init__(self, store, rest_client, alert_module=None):
        """
        Args:
            store: DiamondStore instance.
            rest_client: KalshiRESTClient instance (with order methods).
            alert_module: Optional diamond_alerts module for trade notifications.
        """
        self._store = store
        self._rest = rest_client
        self._alerts = alert_module
        self._pending_tickers: set[str] = set()  # Race condition guard
        self._lock = asyncio.Lock()

    async def on_anomaly(
        self,
        ticker: str,
        taker_side: str,
        price_cents: float,
        score: float,
        level: str,
        features: dict,
        title: str,
    ):
        """Called from diamond_monitor when an anomaly fires.

        Checks all gates and places a trade if conditions are met.
        """
        if not PAPER_TRADING_ENABLED:
            return

        # Check minimum alert level
        if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(PAPER_MIN_ALERT_LEVEL, 3):
            return

        # Normalize price
        price_int = int(round(price_cents))
        if price_int < 1 or price_int > 99:
            log.debug(f"[PAPER] Skipping {ticker}: price {price_cents}¢ out of valid range [1-99]")
            return
        if price_int <= PAPER_MIN_PRICE_CENTS:
            log.debug(f"[PAPER] Skipping {ticker}: price {price_int}¢ ≤ min {PAPER_MIN_PRICE_CENTS}¢")
            return

        async with self._lock:
            # Duplicate check
            if ticker in self._pending_tickers:
                log.debug(f"[PAPER] Skipping {ticker}: already pending")
                return
            if self._store.has_open_position(ticker):
                log.debug(f"[PAPER] Skipping {ticker}: already have open position")
                return

            # Max positions check
            stats = self._store.get_paper_stats()
            if stats["open_positions"] >= PAPER_MAX_POSITIONS:
                log.info(f"[PAPER] Skipping {ticker}: max positions reached ({PAPER_MAX_POSITIONS})")
                return

            # Kill switch: total daily P&L (realized + unrealized) drops below -$20
            estimated_cost = price_int * PAPER_CONTRACTS_PER_TRADE
            total_daily_pnl = stats.get("total_daily_pnl_cents", 0)
            new_daily_pnl = total_daily_pnl - estimated_cost  # Conservative: assume new trade loses cost

            # Kill switch at -$20 daily loss
            if new_daily_pnl < -PAPER_MAX_UNREALIZED_CENTS:
                log.warning(f"[PAPER] KILL SWITCH: daily loss cap reached! "
                           f"Current P&L: {total_daily_pnl:+d}¢ - New cost: {estimated_cost}¢ = {new_daily_pnl:+d}¢ < -${PAPER_MAX_UNREALIZED_CENTS/100:.2f}")
                # Send CRITICAL alert via dispatch
                if self._alerts:
                    self._alerts.dispatch_alert(
                        ticker="DIAMOND-KILLSWITCH",
                        alert_level="CRITICAL",
                        composite_score=1.0,
                        features={"reason": "daily_loss_cap"},
                        market_title=f"Paper Trading Kill Switch — Daily P&L {new_daily_pnl:+d}¢ would exceed -${PAPER_MAX_UNREALIZED_CENTS/100:.2f} limit",
                    )
                return

            # Mark as pending to prevent race conditions
            self._pending_tickers.add(ticker)

        try:
            await self._place_trade(
                ticker=ticker,
                side=taker_side,
                price_cents=price_int,
                score=score,
                level=level,
                features=features,
                title=title,
            )
        finally:
            self._pending_tickers.discard(ticker)

    async def _place_trade(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        score: float,
        level: str,
        features: dict,
        title: str,
    ):
        """Place a limit IOC order and record the result."""
        client_order_id = f"diamond_{ticker}_{int(time.time())}"
        count = PAPER_CONTRACTS_PER_TRADE

        # Insert pending record first
        row_id = self._store.insert_paper_trade(
            ticker=ticker,
            title=title,
            side=side,
            action="buy",
            count=count,
            entry_price=price_cents,
            anomaly_score=score,
            anomaly_level=level,
            features=features,
            client_order_id=client_order_id,
        )

        # Will be logged again after price adjustment below, so skip here

        try:
            # Place limit order with price adjustment for thin market fill rates
            # Kalshi uses yes_price/no_price in cents (1-99)
            # Adjust price up by 3-8¢ (aggressive) to increase fill probability
            slippage_cents = min(8, max(3, price_cents // 5))  # 3-8¢ depending on price
            adjusted_price = min(99, price_cents + slippage_cents)

            order_kwargs = {
                "ticker": ticker,
                "side": side,
                "action": "buy",
                "count": count,
                "time_in_force": "good_till_canceled",  # Keep on books until filled
                "client_order_id": client_order_id,
            }
            if side == "yes":
                order_kwargs["yes_price"] = adjusted_price
            else:
                order_kwargs["no_price"] = adjusted_price

            log.info(f"[PAPER] Placing order: {ticker} buy {count}x {side} @ {adjusted_price}¢ "
                     f"(adjusted from {price_cents}¢, +{slippage_cents}¢ slippage, score={score:.2f}, level={level})")

            order = await self._rest.place_order(**order_kwargs)

            order_id = order.get("order_id", "")
            status = order.get("status", "")

            # Parse fill info — Kalshi returns fill details in the order response
            # Use float() first to handle string formats like "0.00" (int("0.00") crashes)
            raw_fill = order.get("count_filled", order.get("count_filled_fp", 0) or 0)
            fill_count = int(float(raw_fill))
            # Kalshi may return average fill price in various fields
            fill_price = adjusted_price  # Use adjusted price as default

            if fill_count > 0:
                self._store.update_paper_fill(row_id, order_id, fill_price, fill_count, "filled")
                log.info(f"[PAPER] ✓ Filled: {ticker} {side} {fill_count}x @ {fill_price}¢ "
                         f"(order {order_id})")

                # Send notification
                if PAPER_NOTIFY_TRADES and self._alerts:
                    self._alerts.notify_trade_placed(
                        ticker=ticker, side=side, price=fill_price, title=title,
                    )
            else:
                # Order queued but not yet filled — save order_id for tracking
                self._store.update_paper_fill(row_id, order_id, adjusted_price, 0, "pending")
                log.info(f"[PAPER] ⏳ Pending: {ticker} {side} @ {adjusted_price}¢ "
                         f"(GTC order {order_id})")

        except Exception as e:
            log.error(f"[PAPER] Order failed for {ticker}: {e}", exc_info=True)
            # Mark as unfilled on error — but the order may have gone through
            # on Kalshi's side. The dedup window in has_open_position will
            # prevent re-ordering for 30 min regardless.
            self._store.update_paper_fill(row_id, "", 0, 0, "unfilled")
            # Notify so we know orders are failing
            if PAPER_NOTIFY_TRADES and self._alerts:
                self._alerts.dispatch_alert(
                    ticker=ticker,
                    alert_level="CRITICAL",
                    composite_score=0.0,
                    features={"reason": "order_failed", "error": str(e)[:200]},
                    market_title=f"ORDER FAILED: {ticker} {side} @ {price_cents}¢ — {e}",
                )

    async def check_settlements(self):
        """Check if any open positions have settled and compute P&L."""
        open_trades = self._store.get_open_paper_trades()
        if not open_trades:
            return

        for trade in open_trades:
            ticker = trade["ticker"]
            try:
                market = await self._rest.get_market(ticker)
                status = market.get("status", "")

                if status in ("settled", "finalized", "closed"):
                    # Determine settlement result
                    result = market.get("result", "")  # "yes" or "no"
                    if not result:
                        # Try alternative fields
                        result = market.get("settlement_value", "")
                        if not result:
                            log.warning(f"[PAPER] Market {ticker} settled but no result found")
                            continue

                    # Compute P&L
                    fill_price = trade["fill_price"]
                    fill_count = trade["fill_count"]
                    side = trade["side"]

                    if side == "yes":
                        if result == "yes":
                            pnl = (100 - fill_price) * fill_count  # Won
                        else:
                            pnl = -fill_price * fill_count  # Lost
                    else:  # side == "no"
                        if result == "no":
                            pnl = (100 - fill_price) * fill_count  # Won
                        else:
                            pnl = -fill_price * fill_count  # Lost

                    self._store.update_paper_settlement(trade["id"], result, pnl)
                    log.info(f"[PAPER] Settled: {ticker} ({trade['title']}) "
                             f"side={side} result={result} pnl={pnl:+d}¢")

                    # Send notification
                    if PAPER_NOTIFY_TRADES and self._alerts:
                        self._alerts.notify_trade_settled(
                            ticker=ticker, pnl_cents=pnl, title=trade.get("title", ticker),
                        )

            except Exception as e:
                log.debug(f"[PAPER] Error checking settlement for {ticker}: {e}")

    async def check_pending_fills(self):
        """Check if any pending orders have been filled (fallback for missed fills)."""
        pending = self._store.get_pending_paper_trades()
        for trade in pending:
            order_id = trade.get("order_id")
            if not order_id:
                # No order_id means the API call probably failed — skip
                # Mark as unfilled if it's been pending too long (5 min)
                if time.time() - trade["opened_at"] > 300:
                    self._store.update_paper_fill(trade["id"], "", 0, 0, "unfilled")
                    log.info(f"[PAPER] Expired stale pending trade: {trade['ticker']}")
                continue

            try:
                order = await self._rest.get_order(order_id)
                fill_count = int(order.get("count_filled", 0) or 0)
                if fill_count > 0:
                    self._store.update_paper_fill(
                        trade["id"], order_id, trade["entry_price"], fill_count, "filled"
                    )
                    log.info(f"[PAPER] Late fill confirmed: {trade['ticker']} {fill_count}x")
                elif order.get("status") in ("canceled", "expired"):
                    self._store.update_paper_fill(trade["id"], order_id, 0, 0, "unfilled")
            except Exception as e:
                log.debug(f"[PAPER] Error checking order {order_id}: {e}")

    async def poll_loop(self):
        """Background loop: check fills and settlements periodically."""
        log.info(f"[PAPER] Poll loop started (interval={PAPER_POLL_INTERVAL_SEC}s)")
        while True:
            try:
                await self.check_pending_fills()
                await self.check_settlements()

                # Log summary periodically
                stats = self._store.get_paper_stats()
                if stats["total"] > 0:
                    log.info(
                        f"[PAPER] Status: {stats['open_positions']} open, "
                        f"{stats['settled']} settled, "
                        f"P&L={stats['total_pnl_cents']:+.0f}¢, "
                        f"win={stats['win_rate']:.0%}, "
                        f"daily_spend={stats['daily_spend_cents']}¢"
                    )
            except Exception as e:
                log.error(f"[PAPER] Poll loop error: {e}")

            await asyncio.sleep(PAPER_POLL_INTERVAL_SEC)
