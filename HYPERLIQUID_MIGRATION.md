# Hyperliquid Grid Trading Bot - Migration Project

> ⚠️ **RENAME THIS FILE TO `CLAUDE.md` AFTER COPYING TO NEW FOLDER**

## 🎯 Project Goal

**Migrate the Aster DEX Grid Trading Bot to Hyperliquid DEX** using Option A (Full Migration).

**Timeline**: ~7-10 days  
**Approach**: Replace `aster_client.py` → `hyperliquid_client.py`, minimal changes elsewhere

---

## 📋 Migration Overview

### What Changes

| Component               | Action                                       | Notes                        |
| ----------------------- | -------------------------------------------- | ---------------------------- |
| `aster_client.py`       | **DELETE** → **NEW** `hyperliquid_client.py` | Use official SDK             |
| `grid_bot.py`           | **MODIFY**                                   | Update imports, client calls |
| `config.py`             | **MODIFY**                                   | Add Hyperliquid settings     |
| `.env.example`          | **MODIFY**                                   | New env vars                 |
| `strategy_manager.py`   | **NO CHANGE**                                | TA logic unchanged           |
| `indicator_analyzer.py` | **NO CHANGE**                                | Unchanged                    |
| `telegram_*.py`         | **NO CHANGE**                                | Unchanged                    |
| `trade_logger.py`       | **NO CHANGE**                                | Unchanged                    |

### What's Different

| Aspect        | Aster             | Hyperliquid                      |
| ------------- | ----------------- | -------------------------------- |
| **Signing**   | HMAC-SHA256       | EIP-712 (SDK handles)            |
| **Symbol**    | `SOLUSDT`         | `SOL`                            |
| **API Style** | Binance-like REST | Custom POST `/info`, `/exchange` |
| **SDK**       | None (custom)     | `hyperliquid-python-sdk`         |
| **Wallet**    | API key/secret    | Ethereum private key             |

---

## 🤖 Agent Roles

### Agent 1: API Client Developer (`hyperliquid_client.py`)

**Responsibility**: Create the new exchange client using official SDK.

**Tasks**:

1. Create `hyperliquid_client.py` with same interface as `aster_client.py`
2. Implement all trading methods:
   - `get_ticker_price()`
   - `get_account_balance()`
   - `get_position_risk()`
   - `get_open_orders()`
   - `place_order()` (LIMIT, MARKET, reduce_only)
   - `cancel_order()`
   - `cancel_all_orders()`
   - `set_leverage()`
   - `get_klines()` (for indicators)
   - `get_funding_rate()`
3. Implement WebSocket subscriptions:
   - User data stream (order fills)
   - Trade stream (price updates)
4. Handle DRY_RUN mode

**Key Reference**:

- [Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- Review `aster_client.py` for method signatures

---

### Agent 2: Grid Bot Integrator (`grid_bot.py`)

**Responsibility**: Update GridBot to use new client.

**Tasks**:

1. Update imports: `from hyperliquid_client import HyperliquidClient`
2. Update symbol references: `SOLUSDT` → `SOL`
3. Verify order placement works with new client
4. Test WebSocket event handling
5. Ensure grid state machine works correctly

**Dependencies**: Agent 1 must complete first.

---

### Agent 3: Config & Environment (`config.py`, `.env`)

**Responsibility**: Configure for Hyperliquid.

**Tasks**:

1. Add Hyperliquid config section:
   ```python
   # Hyperliquid
   HL_WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
   HL_PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
   HL_API_URL = "https://api.hyperliquid.xyz"
   HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
   HL_TESTNET = os.getenv("HL_TESTNET", "false").lower() == "true"
   ```
2. Update `.env.example`:
   ```
   # Hyperliquid
   HL_WALLET_ADDRESS=0x...
   HL_PRIVATE_KEY=0x...
   HL_TESTNET=true
   ```
3. Remove/comment Aster-specific configs
4. Update symbol default: `SYMBOL = "SOL"`

---

### Agent 4: Testing & Verification

**Responsibility**: Ensure everything works on testnet.

**Tasks**:

1. Setup testnet environment
2. Test order placement (LIMIT buy/sell)
3. Test order cancellation
4. Test position queries
5. Test WebSocket order fills
6. Test grid initialization
7. Test risk management triggers
8. Run full dry-run cycle
9. Document any issues

---

## 📁 New Project Structure

```
hyperliquid-trading-bot/
├── CLAUDE.md                    ← THIS FILE (renamed)
├── MIGRATION_TASKS.md           ← Migration checklist
├── .env.example                 ← Updated env vars
├── config.py                    ← Updated config
├── hyperliquid_client.py        ← NEW (replaces aster_client.py)
├── grid_bot.py                  ← Modified
├── strategy_manager.py          ← No change
├── indicator_analyzer.py        ← No change
├── telegram_notifier.py         ← No change
├── telegram_commands.py         ← No change
├── trade_logger.py              ← No change
├── cli.py                       ← Minor updates
├── requirements.txt             ← Add hyperliquid-python-sdk
└── tests/                       ← Add new tests
```

---

## 🔑 Key API Mappings

### Order Placement

**Aster (Current)**:

```python
await client.place_order(
    symbol="SOLUSDT",
    side="BUY",
    order_type="LIMIT",
    quantity=Decimal("1.0"),
    price=Decimal("100.0"),
    time_in_force="GTC"
)
```

**Hyperliquid (New)**:

```python
from hyperliquid.exchange import Exchange

exchange.order(
    coin="SOL",
    is_buy=True,
    sz=1.0,
    limit_px=100.0,
    order_type={"limit": {"tif": "Gtc"}}  # or "Alo" for post-only
)
```

### Position Query

**Aster**: `GET /fapi/v2/positionRisk`  
**Hyperliquid**: `POST /info` with `{"type": "clearinghouseState", "user": "0x..."}`

### Order Book

**Aster**: `GET /fapi/v1/depth`  
**Hyperliquid**: `POST /info` with `{"type": "l2Book", "coin": "SOL"}`

---

## ⚙️ Environment Setup

### 1. Install SDK

```bash
pip install hyperliquid-python-sdk
```

### 2. Create Wallet

You need an Ethereum wallet (private key) with funds on Hyperliquid.

```bash
# For testnet, get test funds from:
# https://app.hyperliquid-testnet.xyz/faucet
```

### 3. Configure `.env`

```bash
# Hyperliquid
HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=0xYourPrivateKey
HL_TESTNET=true

# Keep these
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
DRY_RUN=true
```

---

## 🚀 Post-Copy Instructions

After copying the folder:

1. **Rename this file**: `HYPERLIQUID_MIGRATION.md` → `CLAUDE.md`

2. **Delete old files**:

   ```bash
   rm aster_client.py
   rm GEMINI.md  # Optional, or update
   ```

3. **Install dependencies**:

   ```bash
   pip install hyperliquid-python-sdk
   ```

4. **Create wallet** and get testnet funds:
   - Go to https://app.hyperliquid-testnet.xyz
   - Connect wallet, get testnet USDC from faucet
5. **Configure `.env`** with wallet details

6. **Start development** following `MIGRATION_TASKS.md`

7. **Test on testnet** before mainnet deployment

---

## 📊 Fee Advantage

| Metric    | Aster  | Hyperliquid   | Savings |
| --------- | ------ | ------------- | ------- |
| Maker Fee | ~0.05% | 0.015%        | **70%** |
| Taker Fee | ~0.05% | 0.045%        | 10%     |
| Rebate    | None   | Up to -0.003% | ✅      |

**Estimated monthly savings** (with $100k volume):

- Old: $50 in fees
- New: $15 in fees
- **Savings: $35/month**

---

## 🔗 References

- [Hyperliquid Docs](https://hyperliquid.gitbook.io/hyperliquid-docs)
- [Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- [Exchange Endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
- [Info Endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [WebSocket API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)
