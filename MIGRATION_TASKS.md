# Hyperliquid Migration Tasks

> ✅ = Done | 🔄 = In Progress | ⬜ = Todo | ❌ = Blocked

## Phase 1: Setup & Environment (Day 1)

- [ ] Copy project folder to new location
- [ ] Rename `HYPERLIQUID_MIGRATION.md` → `CLAUDE.md`
- [ ] Delete `aster_client.py`
- [ ] Update `requirements.txt` with `hyperliquid-python-sdk`
- [ ] Install dependencies: `pip install -r requirements.txt`
- [ ] Create Ethereum wallet for trading
- [ ] Get testnet funds from Hyperliquid faucet
- [ ] Configure `.env` with wallet details

## Phase 2: Create Hyperliquid Client (Day 2-4)

### Core Client (`hyperliquid_client.py`)

- [ ] Create `HyperliquidClient` class skeleton
- [ ] Implement initialization with SDK
- [ ] Add context manager support (`async with`)

### Market Data Methods

- [ ] `get_ticker_price(symbol)` - Current mid price
- [ ] `get_depth(symbol, limit)` - Order book
- [ ] `get_klines(symbol, interval, limit)` - OHLCV data
- [ ] `get_funding_rate(symbol)` - Funding rate info
- [ ] `get_exchange_info(symbol)` - Symbol precision

### Account Methods

- [ ] `get_account_balance()` - Account balance
- [ ] `get_position_risk(symbol)` - Position info
- [ ] `get_open_orders(symbol)` - Open orders list
- [ ] `get_user_trades(symbol, limit)` - Trade history

### Trading Methods

- [ ] `place_order(symbol, side, type, qty, price, ...)` - Place order
- [ ] `cancel_order(symbol, order_id)` - Cancel single order
- [ ] `cancel_all_orders(symbol)` - Cancel all orders
- [ ] `set_leverage(symbol, leverage)` - Set leverage

### WebSocket

- [ ] Implement WebSocket connection
- [ ] Subscribe to user data stream
- [ ] Handle order fill events
- [ ] Handle connection reconnection

### Utility

- [ ] DRY_RUN mode support
- [ ] Error handling and logging
- [ ] Rate limit handling

## Phase 3: Update Grid Bot (Day 5)

### Import Updates (`grid_bot.py`)

- [ ] Change import from `aster_client` to `hyperliquid_client`
- [ ] Update client instantiation

### Symbol Updates

- [ ] Replace `SOLUSDT` with `SOL` references
- [ ] Update symbol format in logging

### Order Placement

- [ ] Verify `place_order()` calls work
- [ ] Verify order types (LIMIT, MARKET)
- [ ] Test `reduce_only` parameter

### Position Management

- [ ] Verify position query works
- [ ] Verify entry price calculation
- [ ] Verify unrealized PnL

### WebSocket Events

- [ ] Verify order fill detection
- [ ] Verify price update handling

## Phase 4: Update Config (Day 5)

### Config.py

- [ ] Add `HL_WALLET_ADDRESS` config
- [ ] Add `HL_PRIVATE_KEY` config
- [ ] Add `HL_API_URL` config
- [ ] Add `HL_WS_URL` config
- [ ] Add `HL_TESTNET` flag
- [ ] Update default `SYMBOL` to `SOL`
- [ ] Remove/comment Aster configs

### .env.example

- [ ] Add Hyperliquid environment variables
- [ ] Remove Aster variables
- [ ] Add testnet flag

## Phase 5: CLI Updates (Day 6)

- [ ] Update `cli.py` client imports
- [ ] Test `balance` command
- [ ] Test `position` command
- [ ] Test `orders` command
- [ ] Test `price` command
- [ ] Test `run` command (dry-run)

## Phase 6: Testing (Day 7-8)

### Unit Tests

- [ ] Test `HyperliquidClient` initialization
- [ ] Test market data methods
- [ ] Test order placement (mock)
- [ ] Test order cancellation (mock)

### Integration Tests (Testnet)

- [ ] Connect to testnet successfully
- [ ] Query account balance
- [ ] Place test LIMIT order
- [ ] Cancel test order
- [ ] Query positions
- [ ] Test WebSocket connection

### Grid Bot Tests

- [ ] Initialize grid correctly
- [ ] Place grid orders
- [ ] Detect order fills
- [ ] Place TP orders
- [ ] Auto re-grid works

### Risk Management Tests

- [ ] Drawdown detection works
- [ ] Position limits work
- [ ] Volatility detection works

## Phase 7: Documentation (Day 9)

- [ ] Update CLAUDE.md for production
- [ ] Update README.md
- [ ] Document any API differences
- [ ] Create troubleshooting guide

## Phase 8: Deployment (Day 10)

- [ ] Final testnet verification
- [ ] Switch to mainnet config
- [ ] Deploy to Railway/VPS
- [ ] Monitor first trades
- [ ] Verify Telegram notifications

---

## Notes

_Add notes here during migration..._
