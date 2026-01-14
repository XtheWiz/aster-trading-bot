# Aster DEX Grid Trading Bot

## Project Overview

Automated grid trading bot for Aster DEX futures market. Uses arithmetic grid strategy with smart take-profit, auto-side switching, and dynamic re-grid capabilities.

**Codebase:** ~6,200 lines of Python

## Tech Stack

- **Language:** Python 3.11+
- **Async:** asyncio, aiohttp, websockets
- **Database:** SQLite (trade history)
- **Indicators:** pandas, ta library
- **Notifications:** Telegram Bot API
- **Deployment:** Railway (production)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        GridBot                               │
│            (Order management, Rebalancing)                   │
├──────────────────────────┬──────────────────────────────────┤
│    StrategyManager       │       IndicatorAnalyzer          │
│    (Risk & Analysis)     │       (Technical Indicators)     │
├──────────────────────────┼──────────────────────────────────┤
│    AsterClient           │       TelegramNotifier           │
│    (REST + WebSocket)    │       (Alerts + Commands)        │
├──────────────────────────┴──────────────────────────────────┤
│              TradeLogger (SQLite) + Config                   │
└─────────────────────────────────────────────────────────────┘
```

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `grid_bot.py` | ~2,400 | Main bot, order management, trailing TP |
| `strategy_manager.py` | ~1,900 | Risk analysis, point-based confirmation |
| `aster_client.py` | ~1,040 | API client (REST + WebSocket) |
| `cli.py` | ~800 | Command-line interface |
| `indicator_analyzer.py` | ~700 | Technical indicators, SuperTrend, StochRSI |
| `config.py` | ~450 | All configurations |
| `telegram_notifier.py` | - | Telegram notifications |
| `telegram_commands.py` | - | Interactive Telegram commands |
| `trade_logger.py` | - | SQLite trade history |

## Common Commands

```bash
# Run bot (production)
python cli.py run

# Run bot (dry run mode)
python cli.py run --dry-run

# Check status
python cli.py status

# View trade history
python cli.py history

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
GRID_RANGE_PERCENT = 3.0  # ±3%
QUANTITY_PER_GRID_USDT = 25

# Risk
MAX_DRAWDOWN_PERCENT = 20
MAX_POSITIONS = 5
MAX_POSITION_PERCENT = 80
AUTO_TP_ENABLED = True
USE_SMART_TP = True

# Intelligent Drawdown Management (Moderate Mode)
DRAWDOWN_PAUSE_PERCENT = 15      # Pause BUY orders
DRAWDOWN_PARTIAL_CUT_PERCENT = 20  # Cut 30% of position
DRAWDOWN_FULL_CUT_PERCENT = 25   # Cut all positions
PARTIAL_CUT_RATIO = 30           # % to cut at level 2
MIN_BALANCE_GUARD = 100          # Stop everything if below
DAILY_LOSS_LIMIT_USDT = 50       # Pause for 24h if exceeded
AUTO_REENTRY_ENABLED = True      # Auto re-enter after cut loss
REENTRY_POSITION_SIZE_RATIO = 50 # Start with 50% size

# Features
AUTO_SWITCH_SIDE_ENABLED = True
AUTO_REGRID_ENABLED = True
REGRID_ON_TP_ENABLED = True

# Trailing TP (SuperTrend-based)
USE_TRAILING_TP = True
SUPERTREND_LENGTH = 10
SUPERTREND_MULTIPLIER = 3.0
FALLBACK_TP_PERCENT = 1.5

# Point-Based Confirmation (Fast Switch)
USE_POINT_CONFIRMATION = True
CONFIRMATION_CHECK_INTERVAL = 300  # 5 minutes
SWITCH_THRESHOLD_POINTS = 4
```

## Grid Strategy

### Order Flow
```
EMPTY → BUY_PLACED → POSITION_HELD → TP_PLACED → (TP fills) → EMPTY
         ↓              ↓                ↓
    Place BUY       BUY filled       Place TP
                  Store entry price   Wait for profit
```

### Trailing Take-Profit (SuperTrend-based)

Replaces fixed % TP with dynamic trailing stop based on SuperTrend indicator.

```python
# Config (in RiskConfig)
USE_TRAILING_TP = True           # Enable SuperTrend trailing
SUPERTREND_LENGTH = 10           # ATR period
SUPERTREND_MULTIPLIER = 3.0      # ATR multiplier
FALLBACK_TP_PERCENT = 1.5%       # Used until position is profitable
TRAILING_TP_UPDATE_INTERVAL = 300  # Update every 5 minutes
```

**How it works:**
| Position | SuperTrend Stop | Action |
|----------|-----------------|--------|
| LONG profitable | long_stop > entry | Use SuperTrend as trailing TP |
| LONG not profitable | long_stop < entry | Use fixed 1.5% TP |
| SHORT profitable | short_stop < entry | Use SuperTrend as trailing TP |
| SHORT not profitable | short_stop > entry | Use fixed 1.5% TP |

**Benefits:**
- Captures more profit in trending moves
- Adapts to volatility (ATR-based)
- Protects gains with trailing stop

### Legacy Smart TP (Fallback)
| Condition | TP% | Reason |
|-----------|-----|--------|
| RSI > 65 (near overbought) | 1.0% | Take profit quickly |
| RSI < 40 (oversold) | 2.5% | Hold for bigger move |
| MACD+ & Trend bullish | 2.0% | Medium TP |
| Default | 1.5% | Standard |

### Trend Score System
```
Score = EMA + MACD + RSI + Volume (-4 to +4)

≥ +2  → LONG bias
≤ -2  → SHORT bias
else  → Default to LONG

Startup: Grid side set dynamically from analysis (not config)
```

### Point-Based Trend Confirmation (Fast Switch)

Replaces old 2-check system (30 min) with point accumulation (can switch in 5-10 min).

```python
# Config (in GridConfig)
USE_POINT_CONFIRMATION = True    # Enable point system
CONFIRMATION_CHECK_INTERVAL = 300  # Check every 5 minutes
SWITCH_THRESHOLD_POINTS = 4      # Points needed to switch
```

**Point Sources:**
| Signal | Points | Condition |
|--------|--------|-----------|
| Strong trend | +2 | Trend score >= 3 |
| Moderate trend | +1 | Trend score == 2 |
| StochRSI oversold | +1 | K < 20 (for LONG) |
| StochRSI overbought | +1 | K > 80 (for SHORT) |
| High volume | +1 | Volume > 1.3x average |
| Unclear signal | -1 | Decay on neutral |

**Example:**
- Strong LONG signal (score=3) + oversold (StochRSI=15) + high volume = 4 points → Immediate switch
- Moderate signals accumulate over 2-3 checks before switching

### Dynamic Initialization
On bot startup:
1. **Initial Capital**: Fetched from exchange (after cancelling orders)
2. **Grid Side**: Determined by real-time analysis, not config value
3. **Position Check**: Existing positions force matching side
4. **Fallback**: Config values used only if analysis fails

## Risk Management (12 Layers)

### Layer 1: Circuit Breaker
- **Max Drawdown 20%** → Auto-pause bot
- **Min Balance $50** → Stop if balance too low
- Prevents catastrophic losses

### Layer 2: Position Limits
- **MAX_POSITIONS = 5** → Limit concurrent positions
- **MAX_POSITION_PERCENT = 80%** → Don't use more than 80% of balance
- Reduces exposure during volatility

### Layer 3: Volatility Detection
- **ATR > 5%** = RANGING_VOLATILE → Recommend wider grid
- **ATR > 10%** = EXTREME_VOLATILITY → **Auto-pause**
- Prevents trading during extreme swings

### Layer 4: Auto Re-Grid
- When price moves **> 3.5%** from grid center
- **Auto-cancel** old orders and place new grid
- Prevents grid from drifting away from price

### Layer 5: Trend Score & Auto-Switch
- Calculates **Trend Score** from 4 indicators
- Score ≥ +2 → Recommend LONG
- Score ≤ -2 → Recommend SHORT
- Requires **2 confirmations** before switch

### Layer 6: Real-time Price Spike Detection
- **WebSocket** monitors price in real-time
- Alert when price moves **≥ 3%** in 5 minutes
- **Extreme Alert** when ≥ 5% + unfavorable direction
- Faster response than 15-min check

### Layer 7: Funding Rate Monitoring
- Check funding rate every 15 minutes
- **Warning** when rate ≥ 0.1% and near funding time
- **Extreme Alert** when rate ≥ 0.3%
- Calculates favorable/unfavorable based on grid side

### Layer 8: BTC Correlation Analysis
- Analyzes **BTC** as leading indicator
- **LONG grid + BTC bearish (score ≤ -2)** → CRITICAL
- **LONG grid + BTC RSI > 70** → WARNING
- Prevents altcoin losses from BTC dumps

### Layer 9: Liquidity Crisis Detection
- Check **Spread** and **Order Book Depth**
- Spread > 0.3% = WARNING, > 0.5% = CRITICAL
- Depth < $5,000 = CRITICAL + **Auto-pause**
- Prevents slippage during low liquidity

### Layer 10: Position Size Coordination
- Check **Grid Config vs Balance** mismatch
- Alert when position usage ≥ 70% of limit
- **CRITICAL** when ≥ 90% + suggestions
- Prevents over-exposure

### Layer 11: Intelligent Drawdown Management (Moderate Mode)
Graduated protection against position drawdowns:

| Drawdown | Price (from $135) | Action |
|----------|-------------------|--------|
| **15%** | ~$115 | Pause new BUY orders (TP still active) |
| **20%** | ~$108 | Partial cut 30% of position |
| **25%** | ~$102 | Full cut loss (close all) |

**Auto Re-entry Logic:**
- Wait 30 minutes after cut loss
- Check RSI < 40 and bouncing up
- BTC not strongly bearish (score > -2)
- Re-enter with **50% position size**
- Gradually increase size on profitable trades

**Safety Net:**
- **Min Balance Guard $100** → Stop everything immediately
- **Daily Loss Limit $50** → Pause for 24 hours

### Layer 12: Side Switch Protection
- **Block side switch** if holding position
- Prevents selling at loss when switching LONG → SHORT
- Must wait for TP to fill before switching
- Telegram alert when switch is blocked

## Monitoring Schedule

| Interval | Checks |
|----------|--------|
| **Real-time** | Price spike detection (WebSocket) |
| **Every 15 min** | Market analysis, funding rate, BTC correlation, liquidity, position size, **drawdown** |

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

## Alert Types

| Icon | Level | Meaning |
|------|-------|---------|
| `🚨` | CRITICAL | Immediate action required |
| `⚠️` | WARNING | Monitor closely |
| `📈📉` | INFO | Price movement |
| `✅` | OK | Condition normalized |

## StrategyManager Features

```python
class StrategyManager:
    # Check interval
    check_interval = 900  # 15 minutes

    # Volatility thresholds
    volatility_threshold_high = 5%    # ATR
    volatility_threshold_extreme = 10%

    # Funding rate thresholds
    funding_rate_threshold = 0.1%   # Warning
    funding_rate_extreme = 0.3%     # Critical

    # Price spike detection
    price_spike_threshold = 3%      # In 5-min window

    # BTC correlation
    btc_rsi_danger_high = 70        # Overbought
    btc_rsi_danger_low = 30         # Oversold

    # Liquidity thresholds
    spread_warning = 0.3%
    spread_danger = 0.5%
    min_depth = $5,000

    # Position size thresholds
    position_warning = 70%          # Of max
    position_danger = 90%

    # Drawdown Management (Moderate Mode)
    drawdown_state = "NORMAL"  # NORMAL, PAUSED, PARTIAL_CUT, FULL_CUT, WAITING_REENTRY
    drawdown_pause = 15%       # Level 1: Pause BUY
    drawdown_partial = 20%     # Level 2: Cut 30%
    drawdown_full = 25%        # Level 3: Cut all
```

## Realized PnL Calculation

```python
# TP uses TOTAL position avg entry (not individual level)
# This ensures TP is always profitable

positions = await client.get_position_risk(symbol)
total_entry_price = positions[0]["entryPrice"]  # Weighted average

tp_price = total_entry_price * (1 + tp_percent)

# On SELL (TP) fill:
pnl = (fill_price - entry_price) * quantity
state.realized_pnl += pnl
```

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

## Development Notes

- Always test with `--dry-run` first
- Railway auto-restarts on push to main
- Logs stored in `/app/logs/` on Railway (volume mounted)
- SQLite DB: `grid_bot_trades.db`

## Recent Updates (2026-01-15)

| Feature | Description |
|---------|-------------|
| **Trailing TP (SuperTrend)** | Dynamic trailing stop replaces fixed % TP |
| **Point-Based Confirmation** | Faster trend switching (5-10 min vs 30 min) |
| **SuperTrend Indicator** | Manual implementation using `ta` library ATR |
| **StochRSI Indicator** | Manual implementation for faster overbought/oversold |
| No pandas-ta dependency | Removed numba dependency, works on Python 3.14 |
| **Bug Fix** | Added missing `indicator_analyzer` init in GridBot (was causing TP not placed) |

### New Indicators in `indicator_analyzer.py`

```python
# SuperTrend - ATR-based trailing stop
SuperTrendResult:
    trend_line: float      # Current SuperTrend value
    direction: int         # 1 = bullish, -1 = bearish
    long_stop: float       # Trailing stop for LONG
    short_stop: float      # Trailing stop for SHORT
    atr_value: float       # Current ATR

# StochRSI - Faster momentum detection
StochRSIResult:
    k_line: float          # Fast line (0-100)
    d_line: float          # Signal line (0-100)
    is_oversold: bool      # K < 20
    is_overbought: bool    # K > 80
```

## Updates (2026-01-12)

| Feature | Description |
|---------|-------------|
| **Dynamic Initialization** | Capital & grid side from real data, not config |
| **Analysis-based Side** | Grid side set by trend analysis at startup |
| Scaled Position | $25/grid for $550 balance |

## Updates (2026-01-08)

| Feature | Description |
|---------|-------------|
| **Drawdown Management** | 3-level protection (15%/20%/25%) with auto re-entry |
| **Side Switch Block** | Prevent switching while holding position |
| **Safety Net** | Min balance $100 + daily loss limit $50 |
| TP Fix | Use total position avg entry instead of level entry |
| Sync Positions | Place TP for existing positions after restart |
| 15-min Check | Reduced from 30 minutes |
| Funding Rate | Monitor and alert on high funding |
| Price Spike | Real-time WebSocket detection |
| BTC Correlation | Leading indicator analysis |
| Liquidity Check | Spread + depth monitoring |
| Position Size | Exposure coordination |

## Drawdown States

```
NORMAL → PAUSED → PARTIAL_CUT → FULL_CUT → WAITING_REENTRY → NORMAL
  ↑                                              ↓
  └──────────────── Auto Re-entry ───────────────┘
```

## Known Issues (All Fixed)

1. ~~`send_message()` missing in TelegramNotifier~~ (Fixed 2026-01-08)
2. ~~`realized_pnl` always 0~~ (Fixed 2026-01-08)
3. ~~Position size not tracked accurately~~ (Fixed 2026-01-08)
4. ~~TP calculated from level entry instead of total position~~ (Fixed 2026-01-08)
5. ~~Side switch causes realized loss~~ (Fixed 2026-01-08)
6. ~~TP not placed after BUY fill - `indicator_analyzer` not initialized~~ (Fixed 2026-01-15)
