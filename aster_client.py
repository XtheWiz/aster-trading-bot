"""
Aster DEX Grid Trading Bot - Async API Client

This module provides a robust asynchronous client for interacting with
Aster DEX Futures API. It handles:
- HMAC-SHA256 signature generation for authenticated endpoints
- Rate limit handling with exponential backoff
- WebSocket connection for real-time data streams
- Proper error handling and retry logic

The API follows Binance Futures API patterns with some Aster-specific endpoints.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any, Callable, Literal
from urllib.parse import urlencode

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from config import config

# Configure module logger
logger = logging.getLogger(__name__)


class AsterAPIError(Exception):
    """
    Custom exception for Aster DEX API errors.
    
    Attributes:
        status_code: HTTP status code (if applicable)
        error_code: Aster-specific error code
        message: Human-readable error message
    """
    def __init__(self, status_code: int, error_code: int | None, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(f"[{status_code}] Error {error_code}: {message}")


class RateLimitError(AsterAPIError):
    """Raised when API rate limit (HTTP 429) is exceeded."""
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(429, None, f"Rate limit exceeded. Retry after {retry_after}s")


class AsterClient:
    """
    Async client for Aster DEX Futures API.
    
    This client provides methods for:
    - Account management (balance, positions)
    - Order management (place, cancel, query)
    - Market data (ticker, depth, trades)
    - WebSocket streams (user data, market data)
    
    Usage:
        async with AsterClient() as client:
            balance = await client.get_account_balance()
            order = await client.place_order(...)
    
    The client uses HMAC-SHA256 for request signing as required by
    authenticated endpoints. Signature is computed over:
        signature = HMAC-SHA256(secret, query_string + request_body)
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        ws_url: str | None = None,
    ):
        """
        Initialize the Aster DEX client.
        
        Args:
            api_key: API key (defaults to config)
            api_secret: API secret (defaults to config)
            base_url: REST API base URL (defaults to config)
            ws_url: WebSocket base URL (defaults to config)
        """
        self.api_key = api_key or config.api.API_KEY
        self.api_secret = api_secret or config.api.API_SECRET
        self.base_url = base_url or config.api.BASE_URL
        self.ws_url = ws_url or config.api.WS_URL
        
        self._session: aiohttp.ClientSession | None = None
        self._ws_connection: websockets.WebSocketClientProtocol | None = None
        
        # Rate limit handling with exponential backoff
        self._backoff_base = 1.0  # Base delay in seconds
        self._backoff_max = 60.0  # Maximum delay
        self._backoff_factor = 2.0  # Exponential factor
        self._current_backoff = 0.0
        
        # Request statistics for monitoring
        self._request_count = 0
        self._error_count = 0
    
    async def __aenter__(self) -> "AsterClient":
        """Async context manager entry - creates HTTP session."""
        await self._ensure_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - closes connections."""
        await self.close()
    
    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create or return existing HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=config.api.REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self) -> None:
        """Close all connections gracefully."""
        if self._ws_connection:
            await self._ws_connection.close()
            self._ws_connection = None
        
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    # =========================================================================
    # AUTHENTICATION & SIGNING
    # =========================================================================
    
    def _generate_signature(self, params: dict[str, Any]) -> str:
        """
        Generate HMAC-SHA256 signature for request authentication.
        
        The signature process:
        1. Sort parameters alphabetically (for consistency)
        2. URL-encode parameters to query string
        3. Compute HMAC-SHA256(secret, query_string)
        4. Return hex-encoded signature
        
        Args:
            params: Request parameters to sign
            
        Returns:
            Hex-encoded signature string
        """
        # Create sorted query string for consistent signing
        query_string = urlencode(sorted(params.items()))
        
        # Compute HMAC-SHA256
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _get_timestamp(self) -> int:
        """Get current timestamp in milliseconds for API requests."""
        return int(time.time() * 1000)
    
    def _add_auth_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Add authentication parameters to request.
        
        Adds:
        - timestamp: Current time in milliseconds
        - recvWindow: Tolerance for timestamp difference
        - signature: HMAC-SHA256 signature
        
        Args:
            params: Original request parameters
            
        Returns:
            Parameters with auth fields added
        """
        # Add required timestamp
        params["timestamp"] = self._get_timestamp()
        params["recvWindow"] = config.api.RECV_WINDOW
        
        # Generate and add signature (must be last)
        params["signature"] = self._generate_signature(params)
        
        return params
    
    # =========================================================================
    # HTTP REQUEST HANDLING
    # =========================================================================
    
    async def _request(
        self,
        method: Literal["GET", "POST", "DELETE", "PUT"],
        endpoint: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        retry_count: int = 3,
    ) -> dict[str, Any]:
        """
        Execute HTTP request with error handling and retry logic.
        
        Features:
        - Automatic signature generation for signed endpoints
        - Exponential backoff for rate limits (HTTP 429)
        - Retry on transient failures
        - Structured logging
        
        Args:
            method: HTTP method
            endpoint: API endpoint path (e.g., "/fapi/v1/order")
            params: Request parameters
            signed: Whether endpoint requires signature
            retry_count: Number of retries on failure
            
        Returns:
            JSON response as dictionary
            
        Raises:
            AsterAPIError: On API errors
            RateLimitError: On rate limit exceeded
        """
        session = await self._ensure_session()
        params = params or {}
        
        if signed:
            # For signed requests, we MUST ensure the query string sent is EXACTLY
            # what we signed. aiohttp might reorder params or encode differently.
            # So we construct the URL manually.
            params = self._add_auth_params(params.copy())
            query_string = urlencode(sorted(params.items()))
            url = f"{self.base_url}{endpoint}?{query_string}"
            request_params = None # Already in URL
        else:
            url = f"{self.base_url}{endpoint}"
            request_params = params

        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        
        # Rate limit backoff - respect previous limit
        if self._current_backoff > 0:
            logger.warning(f"Rate limit backoff: waiting {self._current_backoff:.1f}s")
            await asyncio.sleep(self._current_backoff)
        
        for attempt in range(retry_count):
            try:
                self._request_count += 1
                
                # Log request (mask sensitive data)
                safe_params = {k: v for k, v in params.items() if k != "signature"}
                logger.debug(f"Request: {method} {endpoint} params={safe_params}")
                
                async with session.request(
                    method,
                    url,
                    params=request_params, # Only for unsigned (GET/POST)
                    headers=headers,
                ) as response:
                    # Reset backoff on successful request
                    self._current_backoff = 0
                    
                    response_text = await response.text()
                    
                    # Handle rate limit
                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        self._current_backoff = min(
                            self._backoff_base * (self._backoff_factor ** attempt),
                            self._backoff_max,
                            retry_after
                        )
                        logger.warning(f"Rate limit hit. Retry after {self._current_backoff}s")
                        
                        if attempt < retry_count - 1:
                            await asyncio.sleep(self._current_backoff)
                            continue
                        raise RateLimitError(retry_after)
                    
                    # Parse JSON response
                    try:
                        data = json.loads(response_text) if response_text else {}
                    except json.JSONDecodeError:
                        data = {"raw_response": response_text}
                    
                    # Handle API errors
                    if response.status >= 400:
                        self._error_count += 1
                        error_code = data.get("code")
                        error_msg = data.get("msg", response_text)
                        logger.error(f"API Error: {response.status} - {error_msg}")
                        raise AsterAPIError(response.status, error_code, error_msg)
                    
                    logger.debug(f"Response: {response.status} - {data}")
                    return data
                    
            except aiohttp.ClientError as e:
                self._error_count += 1
                logger.warning(f"Network error (attempt {attempt + 1}/{retry_count}): {e}")
                
                if attempt < retry_count - 1:
                    await asyncio.sleep(self._backoff_base * (self._backoff_factor ** attempt))
                    continue
                raise
        
        raise AsterAPIError(0, None, "Max retries exceeded")
    
    # =========================================================================
    # MARKET DATA ENDPOINTS (Public - No signature required)
    # =========================================================================
    
    async def get_ticker_price(self, symbol: str | None = None) -> dict[str, Any]:
        """
        Get current price for a symbol.
        
        Args:
            symbol: Trading pair (e.g., "ASTERUSDT"). If None, uses config.
            
        Returns:
            {"symbol": "ASTERUSDT", "price": "0.9683", ...}
        """
        symbol = symbol or config.trading.SYMBOL
        return await self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
    
    async def get_exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        """
        Get exchange trading rules and symbol information.
        
        Returns tick size, lot size, min notional, and other trading constraints.
        These are critical for placing valid orders.
        
        Args:
            symbol: Trading pair (optional filter)
            
        Returns:
            Exchange info including symbol filters
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/exchangeInfo", params)
    
    async def get_depth(
        self, 
        symbol: str | None = None, 
        limit: int = 20
    ) -> dict[str, Any]:
        """
        Get order book depth.
        
        Args:
            symbol: Trading pair
            limit: Depth limit (5, 10, 20, 50, 100, 500, 1000)
            
        Returns:
            {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        """
        symbol = symbol or config.trading.SYMBOL
        return await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
    
    async def get_klines(
        self,
        symbol: str | None = None,
        interval: str = "1h",
        limit: int = 100,
    ) -> list[list]:
        """
        Get candlestick/kline data.

        Args:
            symbol: Trading pair
            interval: Kline interval (1m, 5m, 15m, 1h, 4h, 1d, etc.)
            limit: Number of klines (max 1500)

        Returns:
            List of klines: [open_time, open, high, low, close, volume, ...]
        """
        symbol = symbol or config.trading.SYMBOL
        return await self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit}
        )

    async def get_funding_rate(self, symbol: str | None = None) -> dict[str, Any]:
        """
        Get current funding rate and next funding time.

        Funding rate is charged/paid every 8 hours.
        Positive rate = longs pay shorts
        Negative rate = shorts pay longs

        Args:
            symbol: Trading pair

        Returns:
            {
                "symbol": "SOLUSDT",
                "markPrice": "135.5",
                "indexPrice": "135.4",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1704067200000,
                ...
            }
        """
        symbol = symbol or config.trading.SYMBOL
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def get_funding_rate_history(
        self,
        symbol: str | None = None,
        limit: int = 10,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get historical funding rate data.

        Args:
            symbol: Trading pair
            limit: Number of records (max 1000)
            start_time: Start timestamp in milliseconds
            end_time: End timestamp in milliseconds

        Returns:
            List of funding rate records:
            [{"symbol": "SOLUSDT", "fundingRate": "0.0001", "fundingTime": 1704067200000}, ...]
        """
        symbol = symbol or config.trading.SYMBOL
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._request("GET", "/fapi/v1/fundingRate", params)
    
    # =========================================================================
    # ACCOUNT ENDPOINTS (Signed - Requires authentication)
    # =========================================================================
    
    async def get_account_balance(self) -> list[dict[str, Any]]:
        """
        Get futures account balance for all assets.
        
        Returns balance information including:
        - accountAlias: Account identifier
        - asset: Asset symbol (USDT, USDF, BNB, etc.)
        - balance: Total balance
        - availableBalance: Available for trading
        - crossUnPnl: Unrealized PnL for cross positions
        
        Returns:
            List of asset balances
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] get_account_balance - returning mock data")
            return [{
                "asset": "USDT",
                "balance": str(config.INITIAL_CAPITAL_USDT),
                "availableBalance": str(config.INITIAL_CAPITAL_USDT),
                "crossUnPnl": "0",
            }]
        
        return await self._request("GET", "/fapi/v2/balance", signed=True)
    
    async def get_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Get current position information.
        
        Returns:
        - symbol: Trading pair
        - positionAmt: Position quantity (+ for long, - for short)
        - entryPrice: Average entry price
        - markPrice: Current mark price
        - unRealizedProfit: Unrealized PnL
        - liquidationPrice: Position liquidation price
        
        Args:
            symbol: Trading pair (optional, None returns all)
            
        Returns:
            List of position info
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] get_position_risk - returning mock data")
            return [{
                "symbol": symbol or config.trading.SYMBOL,
                "positionAmt": "0",
                "entryPrice": "0",
                "markPrice": "0.9683",
                "unRealizedProfit": "0",
                "liquidationPrice": "0",
                "leverage": str(config.trading.LEVERAGE),
            }]
        
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
    
    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Get all open orders for a symbol.
        
        Args:
            symbol: Trading pair (optional)
            
        Returns:
            List of open orders
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] get_open_orders - returning empty list")
            return []
        
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    async def get_user_trades(
        self,
        symbol: str,
        limit: int = 50,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get account trade history.

        Returns filled trades with PnL information:
        - symbol: Trading pair
        - id: Trade ID
        - orderId: Order ID
        - side: BUY or SELL
        - price: Execution price
        - qty: Quantity
        - realizedPnl: Realized PnL for this trade
        - commission: Trading fee
        - time: Trade timestamp

        Args:
            symbol: Trading pair
            limit: Number of trades to return (default 50, max 1000)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)

        Returns:
            List of trade records
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] get_user_trades - returning empty list")
            return []

        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", "/fapi/v1/userTrades", params, signed=True)

    async def get_income_history(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        limit: int = 50,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get income history (PnL, funding fees, commissions).

        Income types:
        - REALIZED_PNL: Realized profit/loss
        - FUNDING_FEE: Funding fee
        - COMMISSION: Trading commission
        - TRANSFER: Transfer in/out

        Args:
            symbol: Trading pair (optional)
            income_type: Filter by income type (optional)
            limit: Number of records (default 50, max 1000)
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional)

        Returns:
            List of income records
        """
        if config.DRY_RUN:
            logger.info("[DRY RUN] get_income_history - returning empty list")
            return []

        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", "/fapi/v3/income", params, signed=True)

    # =========================================================================
    # ORDER MANAGEMENT ENDPOINTS (Signed)
    # =========================================================================
    
    async def place_order(
        self,
        symbol: str,
        side: Literal["BUY", "SELL"],
        order_type: Literal["LIMIT", "MARKET", "STOP", "STOP_MARKET", 
                            "TAKE_PROFIT", "TAKE_PROFIT_MARKET"],
        quantity: Decimal,
        price: Decimal | None = None,
        time_in_force: Literal["GTC", "IOC", "FOK"] = "GTC",
        reduce_only: bool = False,
        position_side: Literal["LONG", "SHORT", "BOTH"] = "BOTH",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Place a new order.
        
        Order types:
        - LIMIT: Limit order (requires price)
        - MARKET: Market order (executes at best available price)
        - STOP/STOP_MARKET: Stop loss orders
        - TAKE_PROFIT/TAKE_PROFIT_MARKET: Take profit orders
        
        Time in force:
        - GTC: Good til canceled (default for grid trading)
        - IOC: Immediate or cancel
        - FOK: Fill or kill
        
        Args:
            symbol: Trading pair
            side: BUY or SELL
            order_type: Order type
            quantity: Order quantity (in base asset)
            price: Limit price (required for LIMIT orders)
            time_in_force: Order lifetime
            reduce_only: Only reduce position (no new entry)
            position_side: Position direction (for hedge mode)
            client_order_id: Custom order ID for tracking
            
        Returns:
            Order response including orderId, status, etc.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": str(quantity),
            "positionSide": position_side,
        }
        
        # Add price for limit orders
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("Price is required for LIMIT orders")
            params["price"] = str(price)
            params["timeInForce"] = time_in_force
        
        if reduce_only:
            params["reduceOnly"] = "true"
        
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] place_order: {side} {order_type} {quantity} @ {price}")
            return {
                "orderId": int(time.time() * 1000),
                "symbol": symbol,
                "status": "NEW",
                "clientOrderId": client_order_id or f"dry_{int(time.time())}",
                "price": str(price) if price else "0",
                "origQty": str(quantity),
                "executedQty": "0",
                "side": side,
                "type": order_type,
            }
        
        return await self._request("POST", "/fapi/v1/order", params, signed=True)
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Cancel an active order.
        
        Args:
            symbol: Trading pair
            order_id: Exchange order ID
            client_order_id: Custom client order ID
            
        Returns:
            Canceled order info
        """
        if not order_id and not client_order_id:
            raise ValueError("Either order_id or client_order_id is required")
        
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] cancel_order: {order_id or client_order_id}")
            return {"orderId": order_id, "status": "CANCELED"}
        
        return await self._request("DELETE", "/fapi/v1/order", params, signed=True)
    
    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        """
        Cancel all open orders for a symbol.
        
        This is useful for:
        - Emergency shutdown
        - Grid recalculation
        - Circuit breaker trigger
        
        Args:
            symbol: Trading pair
            
        Returns:
            Cancellation response
        """
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] cancel_all_orders: {symbol}")
            return {"code": 200, "msg": "All orders canceled (dry run)"}
        
        return await self._request(
            "DELETE", 
            "/fapi/v1/allOpenOrders", 
            {"symbol": symbol}, 
            signed=True
        )
    
    # =========================================================================
    # LEVERAGE & MARGIN MANAGEMENT
    # =========================================================================
    
    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """
        Set leverage for a symbol.
        
        Args:
            symbol: Trading pair
            leverage: Leverage multiplier (1-125, depending on symbol)
            
        Returns:
            New leverage setting
        """
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] set_leverage: {symbol} -> {leverage}x")
            return {"leverage": leverage, "maxNotionalValue": "1000000"}
        
        return await self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )
    
    async def set_margin_type(
        self, 
        symbol: str, 
        margin_type: Literal["ISOLATED", "CROSSED"]
    ) -> dict[str, Any]:
        """
        Set margin type for a symbol.
        
        ISOLATED: Margin is separate for each position
        CROSSED: Margin is shared across all positions
        
        Args:
            symbol: Trading pair
            margin_type: ISOLATED or CROSSED
            
        Returns:
            Result message
        """
        if config.DRY_RUN:
            logger.info(f"[DRY RUN] set_margin_type: {symbol} -> {margin_type}")
            return {"code": 200, "msg": f"Margin type set to {margin_type}"}
        
        return await self._request(
            "POST",
            "/fapi/v1/marginType",
            {"symbol": symbol, "marginType": margin_type},
            signed=True,
        )
    
    # =========================================================================
    # WEBSOCKET STREAMS
    # =========================================================================
    
    async def create_listen_key(self) -> str:
        """
        Create a listen key for user data stream.
        
        The listen key is required to subscribe to user-specific WebSocket streams
        including order updates, position changes, and balance updates.
        
        Listen key expires after 60 minutes and must be kept alive.
        
        Returns:
            Listen key string
        """
        if config.DRY_RUN:
            return "dry_run_listen_key"
        
        response = await self._request("POST", "/fapi/v1/listenKey", signed=True)
        return response.get("listenKey", "")
    
    async def keepalive_listen_key(self) -> dict[str, Any]:
        """
        Keep user data stream alive.
        
        Call this every 30-50 minutes to prevent listen key expiration.
        
        Returns:
            Empty dict on success
        """
        if config.DRY_RUN:
            return {}
        
        return await self._request("PUT", "/fapi/v1/listenKey", signed=True)
    
    async def subscribe_user_data(
        self,
        on_order_update: Callable[[dict], None] | None = None,
        on_position_update: Callable[[dict], None] | None = None,
        on_balance_update: Callable[[dict], None] | None = None,
    ) -> None:
        """
        Subscribe to user data stream via WebSocket.
        
        Provides real-time updates for:
        - ORDER_TRADE_UPDATE: Order fills, cancellations, new orders
        - ACCOUNT_UPDATE: Position and balance changes
        
        This is the core event loop for the grid bot, triggering
        rebalancing when orders are filled.
        
        Args:
            on_order_update: Callback for order updates
            on_position_update: Callback for position updates
            on_balance_update: Callback for balance updates
        """
        listen_key = await self.create_listen_key()
        ws_url = f"{self.ws_url}/ws/{listen_key}"
        
        logger.info(f"Connecting to user data stream: {ws_url}")
        
        # Keep-alive task
        async def keepalive():
            while True:
                await asyncio.sleep(30 * 60)  # Every 30 minutes
                try:
                    await self.keepalive_listen_key()
                    logger.debug("Listen key refreshed")
                except Exception as e:
                    logger.error(f"Failed to refresh listen key: {e}")
        
        keepalive_task = asyncio.create_task(keepalive())
        
        try:
            async with websockets.connect(ws_url) as ws:
                self._ws_connection = ws
                logger.info("User data stream connected")
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        event_type = data.get("e")
                        
                        if event_type == "ORDER_TRADE_UPDATE" and on_order_update:
                            on_order_update(data.get("o", {}))
                        
                        elif event_type == "ACCOUNT_UPDATE":
                            update_data = data.get("a", {})
                            
                            # Position updates
                            if on_position_update and "P" in update_data:
                                for position in update_data["P"]:
                                    on_position_update(position)
                            
                            # Balance updates
                            if on_balance_update and "B" in update_data:
                                for balance in update_data["B"]:
                                    on_balance_update(balance)
                        
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse WebSocket message: {e}")
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}")
                        
        except ConnectionClosed as e:
            logger.warning(f"WebSocket connection closed: {e}")
        finally:
            keepalive_task.cancel()
            self._ws_connection = None
    
    async def subscribe_market_data(
        self,
        symbol: str,
        streams: list[Literal["trade", "markPrice", "depth", "kline"]],
        on_message: Callable[[str, dict], None],
    ) -> None:
        """
        Subscribe to market data streams.
        
        Available streams:
        - trade: Real-time trades
        - markPrice: Mark price updates (every 3s or real-time)
        - depth: Order book updates
        - kline: Candlestick updates
        
        Args:
            symbol: Trading pair (lowercase for stream name)
            streams: List of stream types
            on_message: Callback(stream_name, data)
        """
        stream_names = [f"{symbol.lower()}@{s}" for s in streams]
        stream_param = "/".join(stream_names)
        ws_url = f"{self.ws_url}/stream?streams={stream_param}"
        
        logger.info(f"Connecting to market data: {streams}")
        
        async with websockets.connect(ws_url) as ws:
            async for message in ws:
                try:
                    data = json.loads(message)
                    stream = data.get("stream", "")
                    payload = data.get("data", {})
                    on_message(stream, payload)
                except Exception as e:
                    logger.error(f"Error processing market data: {e}")
    
    # =========================================================================
    # UTILITY METHODS
    # =========================================================================
    
    async def test_connection(self) -> bool:
        """
        Test API connectivity.
        
        Returns:
            True if connection successful
        """
        try:
            await self._request("GET", "/fapi/v1/ping")
            logger.info("API connection successful")
            return True
        except Exception as e:
            logger.error(f"API connection failed: {e}")
            return False
    
    async def get_server_time(self) -> int:
        """
        Get server time.
        
        Useful for diagnosing timestamp synchronization issues.
        
        Returns:
            Server time in milliseconds
        """
        response = await self._request("GET", "/fapi/v1/time")
        return response.get("serverTime", 0)
    
    def get_stats(self) -> dict[str, int]:
        """
        Get client statistics.
        
        Returns:
            {"requests": count, "errors": count}
        """
        return {
            "requests": self._request_count,
            "errors": self._error_count,
        }


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def create_client() -> AsterClient:
    """
    Factory function to create and initialize an AsterClient.
    
    Usage:
        client = await create_client()
        # ... use client ...
        await client.close()
    
    Or with context manager:
        async with AsterClient() as client:
            # ... use client ...
    """
    client = AsterClient()
    await client._ensure_session()
    return client


if __name__ == "__main__":
    # Quick connection test
    async def main():
        logging.basicConfig(level=logging.INFO)
        
        async with AsterClient() as client:
            # Test connection
            connected = await client.test_connection()
            print(f"Connected: {connected}")
            
            if connected:
                # Get current price
                ticker = await client.get_ticker_price()
                print(f"Ticker: {ticker}")
                
                # Get balance (will return mock in DRY_RUN mode)
                balance = await client.get_account_balance()
                print(f"Balance: {balance}")
    
    asyncio.run(main())
