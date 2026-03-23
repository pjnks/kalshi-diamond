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
    CONVICTION_ENABLED,
    PAPER_CONTRACTS_PER_TRADE,
    PAPER_MAX_PER_CATEGORY,
    PAPER_MAX_PER_EVENT,
    PAPER_MAX_TRADES_PER_5MIN,
    PAPER_MAX_UNREALIZED_CENTS,
    PAPER_MAX_POSITIONS,
    PAPER_MIN_ALERT_LEVEL,
    PAPER_MIN_PRICE_CENTS,
    PAPER_NOTIFY_TRADES,
    PAPER_POLL_INTERVAL_SEC,
    PAPER_STALE_ORDER_SEC,
    PAPER_TRADING_ENABLED,
)

log = logging.getLogger(__name__)

# Alert level ordering for comparison
_LEVEL_ORDER = {"NONE": 0, "LOG": 1, "NOTABLE": 2, "ALERT": 3, "CRITICAL": 4}


class PaperTradingEngine:
    """Manages the lifecycle of auto-placed micro-bets on anomaly signals."""

    def __init__(self, store, rest_client, alert_module=None, conviction_tracker=None):
        """
        Args:
            store: DiamondStore instance.
            rest_client: KalshiRESTClient instance (with order methods).
            alert_module: Optional diamond_alerts module for trade notifications.
            conviction_tracker: Optional ConvictionTracker for event-aware trade management.
        """
        self._store = store
        self._rest = rest_client
        self._alerts = alert_module
        self._conviction = conviction_tracker
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
        category: str = "",
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

            # Event-level conviction check: block or flip opposing signals
            if self._conviction is not None:
                decision = self._conviction.evaluate(ticker, score)
                if decision.action == "block":
                    log.info(
                        f"[CONVICTION] BLOCK {ticker} — {decision.old_ticker} has stronger "
                        f"conviction ({decision.old_conviction:.2f} vs {decision.new_conviction:.2f})"
                    )
                    return
                elif decision.action == "flip":
                    # Check if we actually have a position to exit
                    old_position = self._store.get_open_position_for_ticker(decision.old_ticker)
                    if old_position is None:
                        # No position to exit — also check the event for ANY open position
                        # (might be on a different sibling ticker)
                        event_id = self._conviction.extract_event_id(ticker)
                        event_positions = self._store.get_open_positions_for_event(event_id)
                        if event_positions:
                            # Found position on a different sibling — try to exit that
                            old_position = event_positions[0]
                            decision.old_ticker = old_position["ticker"]

                    if old_position is None:
                        # Can't find any position to exit on this event.
                        # Check the 30-min dedup window — if there was a recent order
                        # attempt on the opposing ticker, treat as BLOCK to avoid
                        # ending up on both sides.
                        if decision.old_ticker and self._store.has_open_position(decision.old_ticker):
                            log.info(
                                f"[CONVICTION] BLOCK {ticker} — recent order on "
                                f"{decision.old_ticker} (within dedup window)"
                            )
                            return
                        # No recent activity on opposing side — allow the trade
                        log.debug(
                            f"[CONVICTION] FLIP decision but no position to exit on "
                            f"{decision.old_ticker} — allowing {ticker} as new entry"
                        )
                    else:
                        log.info(
                            f"[CONVICTION] FLIP {decision.old_ticker} → {ticker} — "
                            f"conviction {decision.new_conviction:.2f} > "
                            f"{decision.old_conviction:.2f} + {self._conviction._flip_threshold}"
                        )
                        exited = await self._exit_position(
                            decision.old_ticker,
                            new_ticker=ticker,
                            conviction_old=decision.old_conviction,
                            conviction_new=decision.new_conviction,
                        )
                # "allow" falls through to normal trade placement

            # ── Portfolio intelligence gates ──────────────────────────
            # Max positions check
            stats = self._store.get_paper_stats()
            if stats["open_positions"] >= PAPER_MAX_POSITIONS:
                log.info(f"[PAPER] Skipping {ticker}: max positions reached ({PAPER_MAX_POSITIONS})")
                return

            # Category exposure limit
            if category:
                cat_count = self._store.count_open_positions_by_category(category)
                if cat_count >= PAPER_MAX_PER_CATEGORY:
                    log.info(f"[PAPER] Skipping {ticker}: category '{category}' at limit "
                             f"({cat_count}/{PAPER_MAX_PER_CATEGORY})")
                    return

            # Event exposure limit
            event_id = self._conviction.extract_event_id(ticker) if self._conviction else ticker.rsplit("-", 1)[0]
            event_positions = self._store.get_open_positions_for_event(event_id)
            if len(event_positions) >= PAPER_MAX_PER_EVENT:
                log.info(f"[PAPER] Skipping {ticker}: event at limit "
                         f"({len(event_positions)}/{PAPER_MAX_PER_EVENT})")
                return

            # Burst throttle
            recent_trade_count = self._store.count_recent_paper_trades(300)
            if recent_trade_count >= PAPER_MAX_TRADES_PER_5MIN:
                log.info(f"[PAPER] Burst throttle: {recent_trade_count} trades in last 5 min "
                         f"(limit {PAPER_MAX_TRADES_PER_5MIN})")
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
                category=category,
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
        category: str = "",
    ):
        """Place a limit order with order-book-aware pricing."""
        client_order_id = f"diamond_{ticker}_{int(time.time())}"
        count = PAPER_CONTRACTS_PER_TRADE

        # Compute event_id for conviction tracking
        event_id = None
        if self._conviction is not None:
            event_id = self._conviction.extract_event_id(ticker)

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
            event_id=event_id,
            category=category,
        )

        # Will be logged again after price adjustment below, so skip here

        try:
            # ── Order-book-aware pricing ─────────────────────────────
            # Fetch a FRESH order book (not stale snapshot) since we're
            # about to place real money. One extra API call is worth it.
            #
            # Kalshi book format:
            #   yes_dollars = [[price_str, qty_str], ...] — resting YES bids (ascending)
            #   no_dollars  = [[price_str, qty_str], ...] — resting NO bids (ascending)
            #
            # To BUY YES: we need to cross the YES ask. The YES ask is the
            # complement of NO bids: ask_price = 100 - no_bid_price.
            # The cheapest YES ask = 100 - highest_no_bid.
            #
            # To BUY NO: same logic reversed — cheapest NO ask = 100 - highest_yes_bid.
            adjusted_price = None
            price_source = "fallback"

            # ── Adaptive parameters by score tier ──────────────────
            # Higher conviction → more aggressive crossing + wider spread tolerance
            if score >= 0.78:       # CRITICAL
                cross_margin = 3     # Cross ask by 3¢ — high conviction, ensure fill
                max_spread_cents = 25
            elif score >= 0.65:     # High ALERT
                cross_margin = 2     # Cross ask by 2¢
                max_spread_cents = 20
            else:                   # Low ALERT (0.55-0.64)
                cross_margin = 1     # Cross ask by 1¢ — minimum to actually fill
                max_spread_cents = 15

            try:
                raw_book = await self._rest.get_orderbook(ticker)
                book = raw_book.get("orderbook_fp", raw_book.get("orderbook", raw_book))
                yes_levels = book.get("yes_dollars", book.get("yes", []))
                no_levels = book.get("no_dollars", book.get("no", []))

                if side == "yes" and no_levels:
                    # Best YES ask = 100 - highest NO bid
                    # NO bids are sorted ascending, so last entry is highest
                    highest_no_bid = int(round(float(no_levels[-1][0]) * 100))
                    best_ask = 100 - highest_no_bid
                    spread = best_ask - price_cents

                    if spread > max_spread_cents:
                        log.info(f"[PAPER] Skipping {ticker}: spread too wide "
                                 f"(best ask {best_ask}¢ vs signal {price_cents}¢, spread={spread}¢)")
                        self._store.update_paper_fill(row_id, "", 0, 0, "unfilled")
                        return

                    adjusted_price = min(99, best_ask + cross_margin)
                    price_source = f"book (ask={best_ask}¢, +{cross_margin}¢)"

                elif side == "no" and yes_levels:
                    # Best NO ask = 100 - highest YES bid
                    highest_yes_bid = int(round(float(yes_levels[-1][0]) * 100))
                    best_ask = 100 - highest_yes_bid
                    spread = best_ask - price_cents

                    if spread > max_spread_cents:
                        log.info(f"[PAPER] Skipping {ticker}: spread too wide "
                                 f"(best ask {best_ask}¢ vs signal {price_cents}¢, spread={spread}¢)")
                        self._store.update_paper_fill(row_id, "", 0, 0, "unfilled")
                        return

                    adjusted_price = min(99, best_ask + cross_margin)
                    price_source = f"book (ask={best_ask}¢, +{cross_margin}¢)"

            except Exception as e:
                log.debug(f"[PAPER] Book fetch failed for {ticker}, using fallback: {e}")

            # Fallback: static slippage if book unavailable or empty
            if adjusted_price is None:
                slippage_cents = min(8, max(3, price_cents // 5))
                adjusted_price = min(99, price_cents + slippage_cents)
                price_source = f"slippage (+{slippage_cents}¢)"

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
                     f"(signal={price_cents}¢, pricing={price_source}, score={score:.2f}, level={level})")

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

    async def _exit_position(
        self,
        ticker: str,
        new_ticker: str = "",
        conviction_old: float = 0.0,
        conviction_new: float = 0.0,
    ):
        """Exit an existing position by cancelling or selling.

        For pending (unfilled) orders: cancel the GTC order.
        For filled positions: place a sell order.
        """
        trade = self._store.get_open_position_for_ticker(ticker)
        if not trade:
            log.debug(f"[CONVICTION] Cannot exit {ticker}: no open position found")
            return False

        # For pending orders: cancel instead of selling
        if trade["status"] == "pending":
            order_id = trade.get("order_id")
            if order_id:
                try:
                    await self._rest.cancel_order(order_id)
                    self._store.update_paper_fill(trade["id"], order_id, 0, 0, "cancelled")
                    log.info(f"[CONVICTION] Cancelled pending order for {ticker} (order {order_id})")
                except Exception as e:
                    log.error(f"[CONVICTION] Failed to cancel {ticker}: {e}")
            else:
                # No order_id — mark as cancelled directly
                self._store.update_paper_fill(trade["id"], "", 0, 0, "cancelled")
                log.info(f"[CONVICTION] Cancelled pending trade for {ticker} (no order_id)")
            return True

        # For filled positions: sell
        side = trade["side"]
        fill_count = trade["fill_count"]
        fill_price = trade["fill_price"]

        if fill_count <= 0:
            log.warning(f"[CONVICTION] Cannot sell {ticker}: fill_count={fill_count}")
            return

        try:
            # Get current price estimate from recent trades
            recent = self._store.get_recent_trades(ticker, window_sec=300)
            if recent:
                last = recent[-1]
                current_price = int(float(last.get("yes_price" if side == "yes" else "no_price", fill_price) or fill_price))
            else:
                current_price = fill_price

            # Sell with slippage DOWN (mirror of buy slippage) to ensure fill
            slippage_cents = min(8, max(3, current_price // 5))
            sell_price = max(1, current_price - slippage_cents)

            client_order_id = f"diamond_exit_{ticker}_{int(time.time())}"
            order_kwargs = {
                "ticker": ticker,
                "side": side,
                "action": "sell",
                "count": fill_count,
                "time_in_force": "good_till_canceled",
                "client_order_id": client_order_id,
            }
            if side == "yes":
                order_kwargs["yes_price"] = sell_price
            else:
                order_kwargs["no_price"] = sell_price

            log.info(
                f"[CONVICTION] Selling {ticker} {side} {fill_count}x @ {sell_price}¢ "
                f"(entry was {fill_price}¢, slippage -{slippage_cents}¢)"
            )

            order = await self._rest.place_order(**order_kwargs)
            exit_order_id = order.get("order_id", "")

            # Compute realized P&L on the exit
            pnl = (sell_price - fill_price) * fill_count
            if side == "no":
                pnl = (sell_price - fill_price) * fill_count  # same formula for both sides

            # Mark original position as settled with "flipped" settlement
            self._store.update_paper_settlement(trade["id"], "flipped", pnl)

            log.info(
                f"[CONVICTION] EXIT complete: {ticker} {side} {fill_count}x — "
                f"P&L: {pnl:+d}¢ (sold @ {sell_price}¢, bought @ {fill_price}¢)"
            )

            # Send flip notification
            if PAPER_NOTIFY_TRADES and self._alerts:
                self._alerts.notify_trade_flipped(
                    ticker_exited=ticker,
                    ticker_entered=new_ticker,
                    exit_pnl_cents=pnl,
                    entry_price=sell_price,
                    conviction_old=conviction_old,
                    conviction_new=conviction_new,
                    title=trade.get("title", ""),
                )

        except Exception as e:
            log.error(f"[CONVICTION] Failed to sell {ticker}: {e}", exc_info=True)
            # Don't mark as settled on failure — position stays open
            if PAPER_NOTIFY_TRADES and self._alerts:
                self._alerts.dispatch_alert(
                    ticker=ticker,
                    alert_level="CRITICAL",
                    composite_score=0.0,
                    features={"reason": "exit_failed", "error": str(e)[:200]},
                    market_title=f"EXIT FAILED: {ticker} {side} @ {sell_price}¢ — {e}",
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
        """Check if any pending orders have been filled or expired.

        Handles:
        - Late fills (GTC order eventually matched)
        - Canceled/expired orders
        - 'executed' with 0 fills (market settled while order was on book)
        - Pending orders on settled markets (cancel + compute P&L)
        """
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
                order_status = order.get("status", "")
                fill_count = int(float(order.get("count_filled", 0) or 0))

                if fill_count > 0:
                    # Order filled (or partially filled)
                    self._store.update_paper_fill(
                        trade["id"], order_id, trade["entry_price"], fill_count, "filled"
                    )
                    log.info(f"[PAPER] Late fill confirmed: {trade['ticker']} {fill_count}x")

                    # Send notification for late fills
                    if PAPER_NOTIFY_TRADES and self._alerts:
                        self._alerts.notify_trade_placed(
                            ticker=trade["ticker"], side=trade["side"],
                            price=trade["entry_price"], title=trade.get("title", ""),
                        )

                elif order_status in ("canceled", "expired"):
                    self._store.update_paper_fill(trade["id"], order_id, 0, 0, "unfilled")
                    log.info(f"[PAPER] Order {order_status}: {trade['ticker']}")

                elif order_status == "executed" and fill_count == 0:
                    # Market settled while order was on book — unfilled
                    self._store.update_paper_fill(trade["id"], order_id, 0, 0, "unfilled")
                    log.info(f"[PAPER] Order executed unfilled (market settled): {trade['ticker']}")

                else:
                    # Cancel stale GTC orders that have been pending too long
                    age_sec = time.time() - trade["opened_at"]
                    if age_sec > PAPER_STALE_ORDER_SEC:
                        try:
                            await self._rest.cancel_order(order_id)
                        except Exception:
                            pass  # May already be cancelled
                        self._store.update_paper_fill(trade["id"], order_id, 0, 0, "unfilled")
                        log.info(f"[PAPER] Cancelled stale GTC order: {trade['ticker']} "
                                 f"(age={int(age_sec)}s > {PAPER_STALE_ORDER_SEC}s)")
                        continue

                    # Still open/pending — check if the market itself has settled
                    # (catches cases where order status hasn't updated yet)
                    try:
                        market = await self._rest.get_market(trade["ticker"])
                        market_status = market.get("status", "")
                        if market_status in ("settled", "finalized", "closed"):
                            # Market settled but order still open — cancel it
                            try:
                                await self._rest.cancel_order(order_id)
                            except Exception:
                                pass  # May already be cancelled/executed
                            self._store.update_paper_fill(trade["id"], order_id, 0, 0, "unfilled")
                            log.info(f"[PAPER] Cancelled pending order on settled market: {trade['ticker']}")
                    except Exception:
                        pass  # Market lookup failed, will retry next cycle

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
