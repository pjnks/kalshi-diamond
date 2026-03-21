"""
kalshi_client.py
────────────────
Kalshi API client — WebSocket + REST for DIAMOND unusual volume tracker.

Auth: RSA-PSS signature (SHA-256) per Kalshi API v2 spec.
WebSocket: trade channel (public) + orderbook_delta (private).
REST: markets, orderbook snapshots, historical trades.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Coroutine

import aiohttp
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from diamond_config import (
    KALSHI_API_KEY,
    KALSHI_PRIVATE_KEY_PATH,
    KALSHI_REST_BASE,
    KALSHI_WS_URL,
)

log = logging.getLogger(__name__)


# ── Auth Helpers ──────────────────────────────────────────────────────────


def _load_private_key_from_file(path):
    """Load RSA private key from PEM file."""
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    """
    RSA-PSS sign: timestamp + method + path (no query params).
    Returns base64-encoded signature.
    """
    message = f"{timestamp_ms}{method}{path}"
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _auth_headers(private_key, method: str, path: str) -> dict[str, str]:
    """Build Kalshi auth headers for a request."""
    ts = int(time.time() * 1000)
    sig = _sign(private_key, ts, method, path)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


# ── REST Client ───────────────────────────────────────────────────────────


SESSION_RECYCLE_SEC = 1800  # Recreate aiohttp session every 30 minutes


class KalshiRESTClient:
    """Async REST client for Kalshi API v2."""

    def __init__(self):
        self._private_key = _load_private_key_from_file(KALSHI_PRIVATE_KEY_PATH)
        self._session: aiohttp.ClientSession | None = None
        self._session_created_at: float = 0

    async def _ensure_session(self):
        now = time.time()
        if (
            self._session is None
            or self._session.closed
            or (now - self._session_created_at) > SESSION_RECYCLE_SEC
        ):
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession()
            self._session_created_at = now
            log.debug("Created new aiohttp session")

    async def _force_new_session(self):
        """Force-close and recreate the session (used after connection failures)."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = aiohttp.ClientSession()
        self._session_created_at = time.time()
        log.info("Force-recycled aiohttp session")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self, method: str, path: str, params: dict | None = None,
        json_body: dict | None = None, max_retries: int = 6,
    ) -> dict:
        """Make authenticated request to Kalshi REST API with retry on 429 + connection errors."""
        await self._ensure_session()
        url = f"{KALSHI_REST_BASE}{path}"
        # Sign with full path (including /trade-api/v2 prefix) per Kalshi API spec
        sign_path = f"/trade-api/v2{path}"

        for attempt in range(max_retries + 1):
            try:
                headers = _auth_headers(self._private_key, method, sign_path)
                if json_body is not None:
                    headers["Content-Type"] = "application/json"
                async with self._session.request(
                    method, url, headers=headers, params=params, json=json_body,
                ) as resp:
                    if resp.status == 429:
                        wait = min(2 + 2 ** attempt, 30)  # 3s, 4s, 6s, 10s, 18s, 30s
                        log.warning(f"Rate limited (429). Retrying in {wait}s... (attempt {attempt+1}/{max_retries+1})")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status not in (200, 201, 204):
                        body = await resp.text()
                        log.error(f"Kalshi API {method} {path} → {resp.status}: {body}")
                        resp.raise_for_status()
                    if resp.status == 204:
                        return {}
                    return await resp.json()
            except (aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                    aiohttp.ServerDisconnectedError, OSError) as e:
                wait = min(5 + 2 ** attempt, 30)
                log.warning(f"Connection error: {e}. Recycling session and retrying in {wait}s... "
                            f"(attempt {attempt+1}/{max_retries+1})")
                await self._force_new_session()
                await asyncio.sleep(wait)
                continue
        raise RuntimeError(f"Max retries exceeded for {method} {path}")

    # ── Public endpoints ──────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 200,
        cursor: str | None = None,
        status: str = "open",
    ) -> dict:
        """Fetch markets list. Returns {markets: [...], cursor: ...}."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_all_markets(
        self, status: str = "open", max_pages: int = 25,
    ) -> list[dict]:
        """Paginate through markets with rate-limit-safe delays.

        Args:
            status: Market status filter (default: "open").
            max_pages: Stop after this many pages (default 25 = ~5,000 markets).
                       Kalshi has 80k+ markets but most are dormant.
        """
        all_markets = []
        cursor = None
        page = 0
        while True:
            data = await self.get_markets(limit=200, cursor=cursor, status=status)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            page += 1
            cursor = data.get("cursor")
            if not cursor or not markets:
                break
            if page >= max_pages:
                log.info(f"  Reached page cap ({max_pages}), stopping pagination")
                break
            await asyncio.sleep(1.5)  # Conservative delay between pages
            if page % 5 == 0:
                log.info(f"  ... fetched page {page} ({len(all_markets)} markets so far)")
        log.info(f"Fetched {len(all_markets)} markets from Kalshi ({page} pages)")
        return all_markets

    async def get_market(self, ticker: str) -> dict:
        """Fetch single market details."""
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Fetch order book snapshot for a market."""
        return await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )

    async def get_trades(
        self,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Fetch recent trades. Optionally filter by ticker."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets/trades", params=params)

    async def get_exchange_status(self) -> dict:
        """Check API health / exchange status."""
        return await self._request("GET", "/exchange/status")

    # ── Trading endpoints (portfolio) ──────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str = "buy",
        count: int = 1,
        yes_price: int | None = None,
        no_price: int | None = None,
        time_in_force: str = "immediate_or_cancel",
        client_order_id: str | None = None,
    ) -> dict:
        """Place an order on Kalshi.

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            action: "buy" or "sell".
            count: Number of contracts.
            yes_price: Price in cents (1-99) for yes side.
            no_price: Price in cents (1-99) for no side.
            time_in_force: "immediate_or_cancel", "fill_or_kill", or "good_till_canceled".
            client_order_id: Optional client-supplied order ID.

        Returns:
            Order dict from Kalshi API.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id

        data = await self._request("POST", "/portfolio/orders", json_body=body)
        order = data.get("order", data)
        log.info(f"Order placed: {ticker} {action} {count}x {side} @ "
                 f"yes={yes_price} no={no_price} → {order.get('order_id', '?')}")
        return order

    async def get_order(self, order_id: str) -> dict:
        """Get order status by ID."""
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_positions(self, ticker: str | None = None) -> list[dict]:
        """Get portfolio positions. Optionally filter by ticker."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/positions", params=params)
        return data.get("market_positions", [])

    async def get_balance(self) -> dict:
        """Get portfolio balance."""
        return await self._request("GET", "/portfolio/balance")


# ── WebSocket Client ──────────────────────────────────────────────────────

TradeCallback = Callable[[dict], Coroutine[Any, Any, None]]


class KalshiWSClient:
    """
    Async WebSocket client for Kalshi trade stream.

    Usage:
        ws = KalshiWSClient(on_trade=my_handler)
        await ws.connect(tickers=["TICKER1", "TICKER2"])
    """

    def __init__(self, on_trade: TradeCallback | None = None):
        self._private_key = _load_private_key_from_file(KALSHI_PRIVATE_KEY_PATH)
        self._on_trade = on_trade
        self._ws = None
        self._msg_id = 1
        self._running = False

    async def connect(
        self,
        tickers: list[str] | None = None,
        channels: list[str] | None = None,
    ):
        """
        Connect to Kalshi WebSocket and subscribe to channels.

        Args:
            tickers: Market tickers to subscribe to. None = must subscribe later.
            channels: Channels to subscribe. Default: ["trade"].
        """
        if channels is None:
            channels = ["trade"]

        self._running = True

        while self._running:
            try:
                headers = _auth_headers(self._private_key, "GET", "/trade-api/ws/v2")
                # websockets <11 uses extra_headers, >=11 uses additional_headers
                ws_version = int(websockets.__version__.split(".")[0])
                hdr_kwarg = "additional_headers" if ws_version >= 11 else "extra_headers"
                async with websockets.connect(
                    KALSHI_WS_URL,
                    **{hdr_kwarg: headers},
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    log.info("WebSocket connected to Kalshi")

                    if tickers:
                        await self.subscribe(channels, tickers)

                    await self._listen()

            except websockets.ConnectionClosed as e:
                log.warning(f"WebSocket closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"WebSocket error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)

    async def subscribe(self, channels: list[str], tickers: list[str]):
        """Subscribe to channels for given market tickers."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            },
        }
        await self._ws.send(json.dumps(msg))
        log.info(f"Subscribed to {channels} for {len(tickers)} tickers")
        self._msg_id += 1

    async def unsubscribe(self, channels: list[str], tickers: list[str]):
        """Unsubscribe from channels."""
        if not self._ws:
            return
        msg = {
            "id": self._msg_id,
            "cmd": "unsubscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            },
        }
        await self._ws.send(json.dumps(msg))
        self._msg_id += 1

    async def disconnect(self):
        """Gracefully close the WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
            log.info("WebSocket disconnected")

    async def _listen(self):
        """Listen for incoming messages and dispatch to handlers."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Non-JSON message: {raw[:200]}")
                continue

            msg_type = msg.get("type")

            if msg_type == "trade":
                if self._on_trade:
                    await self._on_trade(msg)
            elif msg_type == "subscribed":
                log.debug(f"Subscription confirmed: {msg}")
            elif msg_type == "error":
                log.error(f"WebSocket error message: {msg}")
            else:
                log.debug(f"Unhandled message type '{msg_type}': {msg}")


# ── Quick connectivity test ───────────────────────────────────────────────


async def test_connection():
    """Quick test: fetch exchange status + a few markets."""
    client = KalshiRESTClient()
    try:
        print("Testing Kalshi API connectivity...\n")

        # 1. Exchange status
        status = await client.get_exchange_status()
        print(f"Exchange status: {json.dumps(status, indent=2)}\n")

        # 2. Fetch a page of markets
        data = await client.get_markets(limit=5)
        markets = data.get("markets", [])
        print(f"Sample markets ({len(markets)}):")
        for m in markets:
            ticker = m.get("ticker", "?")
            title = m.get("title", "?")
            volume = float(m.get("volume_24h_fp") or 0)
            print(f"  {ticker}: {title} (vol_24h: {volume})")

        print("\nKalshi API connection OK!")
    except Exception as e:
        print(f"\nConnection FAILED: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_connection())
