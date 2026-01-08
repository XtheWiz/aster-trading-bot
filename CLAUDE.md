# Aster DEX Grid Trading Bot

## Project Overview

Automated grid trading bot for Aster DEX futures market. Uses arithmetic grid strategy with smart take-profit, auto-side switching, and dynamic re-grid capabilities.

## Tech Stack

- **Language:** Python 3.11+
- **Async:** asyncio, aiohttp
- **Database:** SQLite (trade history)
- **Notifications:** Telegram Bot API
- **Deployment:** Railway (production)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        GridBot                               │
│  (Main orchestrator - order management, rebalancing)         │
├─────────────────────────────────────────────────────────────┤
│  StrategyManager          │  IndicatorAnalyzer              │
│  (Market analysis,        │  (RSI, MACD, SMA calculations,  │
│   trend scoring,          │   Smart TP recommendations)     │
│   auto-switch logic)      │                                 │
├─────────────────────────────────────────────────────────────┤
│  AsterClient              │  TelegramNotifier               │
│  (REST API + WebSocket)   │  (Real-time alerts + commands)  │
├─────────────────────────────────────────────────────────────┤
│  TradeLogger              │  Config                         │
│  (SQLite persistence)     │  (Centralized settings)         │
└─────────────────────────────────────────────────────────────┘
```

## Key Files

| File | Purpose |
|------|---------|
| `grid_bot.py` | Main bot class, order management, rebalancing logic |
| `strategy_manager.py` | Market analysis, trend scoring, auto-switch decisions |
| `indicator_analyzer.py` | Technical indicators, Smart TP calculation |
| `aster_client.py` | Aster DEX API client (REST + WebSocket) |
| `telegram_notifier.py` | Telegram notifications (alerts, summaries) |
| `telegram_commands.py` | Interactive Telegram commands (/status, /balance, etc.) |
| `trade_logger.py` | SQLite trade history and balance snapshots |
| `config.py` | All configuration (API, trading, grid, risk) |
| `cli.py` | Command-line interface for running the bot |

## Common Commands

```bash
# Run bot (production)
python cli.py run

# Run bot (dry run mode)
python cli.py run --dry-run

# Check status
python cli.py status

# View logs on Railway
railway logs -n 500
railway status
```

## Configuration

Key settings in `config.py` (loaded from `.env`):

```python
# Trading
SYMBOL = "SOLUSDT"
LEVERAGE = 5
MARGIN_TYPE = "CROSSED"
MARGIN_ASSET = "USDF"

# Grid
GRID_COUNT = 12
GRID_RANGE_PERCENT = 3.0  # ±3% (dynamic: 2-6%)
QUANTITY_PER_GRID_USDT = 25

# Risk
MAX_DRAWDOWN_PERCENT = 80
AUTO_TP_ENABLED = True
USE_SMART_TP = True

# Features
AUTO_SWITCH_SIDE_ENABLED = True
AUTO_REGRID_ENABLED = True
REGRID_ON_TP_ENABLED = True
```

## Grid Strategy

1. **Arithmetic Grid:** Evenly spaced price levels
2. **LONG-only mode:** Only BUY orders (configurable)
3. **Smart TP:** RSI/MACD-based take-profit (1.0% - 2.5%)
4. **Auto Re-Grid:** Repositions when price drifts >3.5%
5. **Auto Switch Side:** Changes LONG/SHORT based on trend score

## Trend Score System

```
Score = EMA + MACD + RSI + Volume (-4 to +4)

≥ +2  → LONG bias
≤ -2  → SHORT bias
else  → STAY current side
```

## Risk Management

- **Circuit Breaker:** Auto-pause at 80% drawdown
- **Min Balance:** $50 minimum
- **Volatility Detection:** Pause on extreme volatility (>10% ATR)
- **Position Limits:** Max open orders = 20

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Bot status, balance, PnL |
| `/balance` | Account balance details |
| `/position` | Current open positions |
| `/orders` | Active orders list |
| `/pnl` | Profit/loss summary |
| `/grid` | Grid configuration |
| `/stats` | Trading statistics |
| `/history` | Recent trade history |
| `/help` | All available commands |

## Grid Level State Machine

```
GridLevelState:
  EMPTY          → No order, no position
  BUY_PLACED     → BUY order placed, waiting fill
  POSITION_HELD  → BUY filled, holding position (entry_price, position_quantity set)
  TP_PLACED      → TP SELL order placed (tp_order_id set)
  SELL_PLACED    → Regular SELL order placed

Flow: EMPTY → BUY_PLACED → POSITION_HELD → TP_PLACED → (TP fills) → EMPTY
```

## Realized PnL Calculation

```python
# On SELL (TP) fill:
pnl = (fill_price - level.entry_price) * level.position_quantity
state.realized_pnl += pnl  # Accumulates total realized PnL
```

## Known Issues

1. ~~`send_message()` missing in TelegramNotifier~~ (Fixed 2026-01-08)
2. ~~`realized_pnl` always 0~~ (Fixed 2026-01-08)
3. ~~Position size not tracked accurately in GridLevel~~ (Fixed 2026-01-08)

## Development Notes

- Always test with `--dry-run` first
- Railway deployment auto-restarts on push to main
- Logs stored in `/app/logs/` on Railway (volume mounted)
- SQLite DB: `grid_bot_trades.db`

## Environment Variables

```
ASTER_API_KEY=
ASTER_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DRY_RUN=false
LOG_LEVEL=INFO
```

## Railway Deployment

```bash
# Check status
railway status

# View logs
railway logs -n 500

# Trigger redeploy
git push origin main
```
