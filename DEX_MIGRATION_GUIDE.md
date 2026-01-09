# DEX Migration Guide: Adapting the Grid Bot for New Exchanges

This guide outlines the process of taking the **Aster Grid Bot** codebase and adapting it for a new Decentralized Exchange (DEX) or Centralized Exchange (CEX).

The core logic of this bot (Grid Strategy, Risk Management, Indicator Analysis) is **Exchange-Agnostic**. This means you only need to rewrite the "Communication Layer" (The Client) to make it work anywhere.

---

## Phase 1: Project Setup (The "Clean Slate")

Do not modify the existing repository. Create a fresh one to keep configurations and dependencies clean.

### 1. Clone and Detach
Open your terminal and run the following to copy the code to a new directory and reset Git history:

```bash
# 1. Create new folder (e.g., for Hyperliquid)
cp -r aster-trading-bot hyperliquid-grid-bot

# 2. Enter the new folder
cd hyperliquid-grid-bot

# 3. Remove old Git history (Start fresh)
rm -rf .git
git init

# 4. Remove Aster-specific artifacts
rm aster_client.py          # You will rewrite this
rm grid_bot_trades.db       # Old database
rm -rf logs/*               # Old logs
rm .env                     # Old credentials
```

### 2. Clean Dependencies
Update `requirements.txt`.
- Remove: `aiohttp`, `websockets` (only if the new SDK doesn't use them).
- Add: The SDK for your new DEX (e.g., `hyperliquid-python-sdk`, `ccxt`, `web3`, `eth-account`).

---

## Phase 2: The Adapter Pattern (The Core Task)

You need to create a new file (e.g., `exchange_client.py`) that acts as a bridge.

**The Rule:** The `GridBot` class doesn't care *how* you talk to the exchange. It just expects specific methods to return specific data formats.

### 1. The Interface Contract (Complete Method List)

Your new Client class **MUST** implement these 13 async methods with these exact return formats:

#### Core Trading Methods

##### A. `get_ticker_price(symbol)`
**Must Return:**
```python
{'symbol': 'SOLUSDT', 'price': '135.50'}
```

##### B. `get_account_balance()`
**Must Return:** List of assets.
```python
[
    {
        'asset': 'USDC',
        'availableBalance': '500.00',
        'balance': '500.00'
    },
    ...
]
```

##### C. `get_position_risk(symbol)`
**CRITICAL:** This is where most errors happen. You must map the DEX's specific field names to these standard names.
**Must Return:** List of positions.
```python
[
    {
        'symbol': 'SOLUSDT',
        'positionAmt': '10.5',      # Positive = Long, Negative = Short
        'entryPrice': '130.00',
        'markPrice': '135.50',
        'unRealizedProfit': '55.00',
        'liquidationPrice': '105.00'
    }
]
```

##### D. `place_order(...)`
**Parameters:** `symbol`, `side` (BUY/SELL), `order_type` (LIMIT/MARKET), `quantity`, `price`.
**Note:** Ensure `quantity` and `price` are rounded to the correct precision before sending (see Phase 2.7).
**Must Return:**
```python
{'orderId': '12345', 'status': 'NEW', ...}
```

##### E. `cancel_order(symbol, order_id)`
**Parameters:** `symbol`, `order_id`.
**Must Return:**
```python
{'orderId': '12345', 'status': 'CANCELED'}
```

##### F. `get_open_orders(symbol)`
**Used for:** Checking existing orders, re-grid logic, order management.
**Must Return:**
```python
[
    {
        'orderId': '12345',
        'symbol': 'SOLUSDT',
        'side': 'BUY',
        'type': 'LIMIT',
        'price': '130.00',
        'origQty': '1.0',
        'executedQty': '0.0',
        'status': 'NEW',
        'time': 1704067200000
    },
    ...
]
```

##### G. `cancel_all_orders(symbol)`
**Used for:** Re-grid, emergency stop, cleanup.
**Must Return:**
```python
{'code': 200, 'msg': 'success'}
```

#### Market Data Methods

##### H. `get_klines(symbol, interval, limit)`
**Used for:** Technical indicators (RSI, MACD, EMA), StrategyManager analysis.
**Parameters:**
- `interval`: "1h", "4h", "1d" etc.
- `limit`: Number of candles (e.g., 100)
**Must Return:** List of OHLCV arrays.
```python
[
    [
        1704067200000,    # Open time (timestamp ms)
        '135.00',         # Open
        '136.50',         # High
        '134.00',         # Low
        '135.50',         # Close
        '10000.5',        # Volume
        1704070799999,    # Close time
        '1350000.00',     # Quote volume
        500,              # Number of trades
        '5000.25',        # Taker buy volume
        '675000.00',      # Taker buy quote volume
        '0'               # Ignore
    ],
    ...
]
```

##### I. `get_exchange_info(symbol)`
**Used for:** Getting precision, lot size, min notional for order validation.
**Must Return:**
```python
{
    'symbol': 'SOLUSDT',
    'pricePrecision': 2,      # Decimal places for price (e.g. 135.25)
    'quantityPrecision': 2,   # Decimal places for quantity (e.g. 1.05 SOL)
    'minQty': '0.01',         # Minimum order quantity
    'minNotional': '5.0'      # Minimum order value in USDT
}
```

#### Account Setup Methods

##### J. `set_leverage(symbol, leverage)`
**Used for:** Initial setup, setting leverage (e.g., 5x).
**Must Return:**
```python
{'leverage': 5, 'symbol': 'SOLUSDT'}
```

##### K. `set_margin_type(symbol, margin_type)`
**Parameters:** `margin_type` = "ISOLATED" or "CROSSED"
**Must Return:**
```python
{'code': 200, 'msg': 'success'}
```

##### L. `test_connection()`
**Used for:** Startup health check.
**Must Return:**
```python
True  # or False if connection failed
```

#### WebSocket Methods

##### M. `subscribe_user_data(callback)`
**CRITICAL for real-time operation.** See WebSocket section below.
**Used for:** Receiving order fill events, position updates.
**Callback receives:**
```python
{
    'e': 'ORDER_TRADE_UPDATE',
    'o': {
        'i': 12345,           # Order ID
        's': 'SOLUSDT',       # Symbol
        'S': 'BUY',           # Side
        'X': 'FILLED',        # Status
        'p': '135.00',        # Price
        'q': '1.0',           # Quantity
        'ap': '135.00',       # Average fill price
        'rp': '0.50'          # Realized profit
    }
}
```

### 2. Implementation Template (Pseudo-code)

Create `exchange_client.py`:

```python
import logging
# Import your DEX SDK here
# from hyperliquid.utils import ...

class ExchangeClient:
    def __init__(self):
        # Initialize SDK/Wallet here
        self.private_key = config.api.PRIVATE_KEY
        self.wallet_address = config.api.WALLET_ADDRESS

    async def get_ticker_price(self, symbol):
        # 1. Call DEX API
        raw_data = await self.dex_sdk.get_price(symbol)
        
        # 2. Normalize Data (The Adapter Step)
        return {
            "symbol": symbol,
            "price": str(raw_data['mid_price'])
        }

    # ... Implement other methods ...
```

### 3. WebSocket Implementation

WebSocket is **CRITICAL** for the bot to function properly. Without it, the bot won't know when orders are filled.

#### Implementation Pattern

```python
import asyncio
import websockets
import json

class ExchangeClient:
    def __init__(self):
        self._ws = None
        self._user_data_callback = None

    async def subscribe_user_data(self, callback):
        """Subscribe to user data stream (order updates, position changes)."""
        self._user_data_callback = callback
        asyncio.create_task(self._run_user_data_stream())

    async def _run_user_data_stream(self):
        """Maintain WebSocket connection with auto-reconnect."""
        ws_url = "wss://your-dex.com/ws/user"
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    await self._authenticate_ws(ws)
                    async for message in ws:
                        await self._handle_ws_message(message)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)  # Reconnect after 5 seconds
```

#### Event Types to Handle

| Event Type | Trigger | Bot Action |
|------------|---------|------------|
| `ORDER_TRADE_UPDATE` (FILLED) | BUY order filled | Place TP order |
| `ORDER_TRADE_UPDATE` (FILLED) | TP order filled | Update PnL, re-place BUY |
| `ORDER_TRADE_UPDATE` (CANCELED) | Order canceled externally | Update grid state |
| `ACCOUNT_UPDATE` | Position change | Update position tracking |

---

## Phase 2.5: Symbol Mapping (Crucial!)

Different DEXs use different symbol formats. You need a mapping layer.

**The Problem:** `grid_bot.py` expects standard symbols like `SOLUSDT`, but:
- Hyperliquid uses `SOL`
- dYdX uses `SOL-USD`
- GMX uses `SOL_USD`

**The Solution:**

```python
class SymbolMapper:
    """Convert between internal format and DEX format."""

    def __init__(self, dex_name: str):
        self.dex = dex_name

    def to_dex_format(self, internal_symbol: str) -> str:
        """Convert SOLUSDT → DEX format."""
        base = internal_symbol.replace('USDT', '').replace('USD', '')
        if self.dex == 'hyperliquid': return base  # SOL
        if self.dex == 'dydx': return f"{base}-USD"
        return internal_symbol

    def from_dex_format(self, dex_symbol: str) -> str:
        """Convert DEX format → SOLUSDT."""
        if self.dex == 'hyperliquid': return f"{dex_symbol}USDT"
        if self.dex == 'dydx': return f"{dex_symbol.replace('-USD', '')}USDT"
        return dex_symbol

# Use this mapper inside every method of ExchangeClient!
```

---

## Phase 2.6: Error Handling

Your client should handle DEX-specific errors and raise standard exceptions if possible, or log and retry.

```python
async def place_order(self, ...):
    try:
        # call sdk
    except RateLimitError:
        # wait and retry
    except InsufficientBalanceError:
        # log critical error
```

---

## Phase 2.7: Precision & Rounding (The Silent Killer)

APIs are strict. If the price precision is 2 decimals (135.25), sending 135.251 will cause a rejection.

**Requirement:**
Inside `place_order`, you MUST round the `quantity` and `price` based on the symbol's rules.

```python
# In ExchangeClient
async def place_order(self, symbol, quantity, price, ...):
    # 1. Get precision info
    info = await self.get_exchange_info(symbol)
    p_prec = info['pricePrecision']
    q_prec = info['quantityPrecision']
    
    # 2. Round down/match precision
    final_qty = f"{float(quantity):.{q_prec}f}"
    final_price = f"{float(price):.{p_prec}f}" if price else None
    
    # 3. Send
    return await self.sdk.place_order(..., size=final_qty, price=final_price)
```

---

## Phase 3: Configuration Adjustments

### 1. Update `config.py` (Validation Logic)
You likely need to change `APIConfig` to support Private Keys. **IMPORTANT:** You must also update the `validate()` method, otherwise the bot will refuse to start if `API_KEY` is missing.

```python
@dataclass(frozen=True)
class APIConfig:
    # Changed from API_KEY to PRIVATE_KEY for on-chain DEX
    PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    WALLET_ADDRESS: str = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))
    
    # Remove old fields if not needed, or keep optional
    API_KEY: str = "" 
    API_SECRET: str = ""

# In BotConfig.validate():
    # if not self.api.API_KEY:  <-- REMOVE THIS CHECK
    if not self.api.PRIVATE_KEY and not self.DRY_RUN: # <-- ADD THIS CHECK
        errors.append("PRIVATE_KEY is required")
```

### 2. Update `.env`
Create a new `.env` file with the credentials required for the new DEX.

---

## Phase 4: Integration & Wiring

1.  **Update `grid_bot.py`:**
    Change the import statement:
    ```python
    # FROM:
    from aster_client import AsterClient
    
    # TO:
    from exchange_client import ExchangeClient as AsterClient 
    ```

2.  **Update `cli.py`:**
    Similarly, update the imports in `cli.py` so you can use the command line tools with the new exchange.

---

## Phase 5: The "Safe Launch" Checklist

Before putting in real money ($300-$500), follow this sequence:

1.  **Read-Only Test:** `python cli.py balance` and `python cli.py price`.
2.  **Dry Run (Simulation):** `DRY_RUN=true python cli.py run`.
3.  **Minimum Size Test (Live):** Edit config to use min size ($1-$5) and run.
4.  **Websocket Verification:** Ensure the bot reacts to fills automatically.

---

## Appendix: DEX Comparison Matrix

| Feature | Aster | Hyperliquid | dYdX v4 | GMX v2 | Vertex |
|---------|-------|-------------|---------|--------|--------|
| **Auth** | API Key | Private Key | API Key | Wallet Sign | Private Key |
| **WebSocket** | Yes | Yes | Yes | No (polling) | Yes |
| **Symbol** | SOLUSDT | SOL | SOL-USD | SOL_USD | ID (2) |
| **Min Order** | ~$5 | ~$10 | ~$1 | ~$1 | ~$10 |
| **Best For** | Airdrop | Speed, Points | Decentralization | Simplicity | Low fees |

---

*Last updated: 2026-01-09*
*Compatible with: Aster Grid Bot v2.x*
