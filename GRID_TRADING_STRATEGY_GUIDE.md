# Grid Trading Strategy Guide for Perpetual Futures

**Version:** 1.0
**Based on:** 6 months of live trading on Aster DEX
**Applicable to:** HyperLiquid, Aster DEX, and similar perp DEXs

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Core Concepts](#core-concepts)
3. [Risk Management Framework](#risk-management-framework)
4. [Technical Indicators](#technical-indicators)
5. [Grid Configuration](#grid-configuration)
6. [Market Condition Detection](#market-condition-detection)
7. [Lessons Learned](#lessons-learned)
8. [Configuration Templates](#configuration-templates)
9. [Operational Playbook](#operational-playbook)

---

## Strategy Overview

### What is Grid Trading?

Grid trading places multiple limit orders at predetermined price levels (a "grid") to profit from price oscillations. For perpetual futures:

- **LONG Grid**: Place BUY orders below current price, sell (TP) when price rises
- **SHORT Grid**: Place SELL orders above current price, buy back (TP) when price drops

### Why Grid Trading on Perps?

| Advantage | Description |
|-----------|-------------|
| Range-bound profits | Profits from sideways markets where directional trading struggles |
| Automated execution | No need to time entries manually |
| Leverage efficiency | Small moves × leverage = meaningful returns |
| Reduced emotional trading | Systematic approach removes FOMO/panic |

### When Grid Trading Fails

| Condition | Problem |
|-----------|---------|
| Strong trends | Grid fills on wrong side, accumulates losing positions |
| High volatility | Positions get liquidated before TP |
| Low liquidity | Slippage eats profits |
| Funding rate spikes | Holding costs exceed grid profits |

---

## Core Concepts

### Order Flow Lifecycle

```
EMPTY → BUY_PLACED → POSITION_HELD → TP_PLACED → (TP fills) → EMPTY
         ↓              ↓                ↓
    Place BUY       BUY filled       Place TP
                  Store entry        Wait for profit
```

### Position Management

**Critical Rule**: Use TOTAL position average entry for TP calculation, not individual grid level entry.

```python
# WRONG: TP based on individual fill
tp_price = fill_price * 1.015  # 1.5% from this fill

# CORRECT: TP based on total position average
positions = get_position_risk(symbol)
avg_entry = positions["entryPrice"]  # Weighted average of ALL fills
tp_price = avg_entry * 1.015  # 1.5% from average
```

**Why?** When multiple grid levels fill, individual TPs may be underwater while the average position is profitable.

### Grid Side Selection

Never hardcode grid side. Determine dynamically at startup:

```python
def determine_grid_side():
    # 1. Check existing positions (highest priority)
    if has_long_position():
        return "LONG"
    if has_short_position():
        return "SHORT"

    # 2. Analyze market conditions
    trend_score = calculate_trend_score()
    if trend_score >= 2:
        return "LONG"
    if trend_score <= -2:
        return "SHORT"

    # 3. Default fallback
    return "LONG"
```

---

## Risk Management Framework

### 12-Layer Protection System

#### Layer 1: Circuit Breaker
```python
MAX_DRAWDOWN_PERCENT = 20  # Auto-pause if exceeded
MIN_BALANCE_GUARD = 100    # Stop everything if below
```

**Action**: Immediately halt all trading, alert operator.

#### Layer 2: Position Limits
```python
MAX_POSITIONS = 5              # Concurrent grid levels with positions
MAX_POSITION_PERCENT = 80      # Max % of balance in positions
```

**Action**: Skip new orders when limits reached.

#### Layer 3: Volatility Detection
```python
VOLATILITY_HIGH = 5%      # ATR threshold for caution
VOLATILITY_EXTREME = 10%  # ATR threshold for pause
```

| ATR % | Market State | Action |
|-------|--------------|--------|
| < 5% | STABLE | Normal operation |
| 5-10% | VOLATILE | Widen grid, reduce size |
| > 10% | EXTREME | Auto-pause |

#### Layer 4: Auto Re-Grid
```python
REGRID_THRESHOLD = 3.5%  # Price drift from grid center
```

When price moves too far from grid center:
1. Cancel all existing orders
2. Recalculate grid around new price
3. Place new orders

#### Layer 5: Trend Score System
```python
def calculate_trend_score():
    """
    Score from -4 to +4
    Components: EMA, MACD, RSI, Volume
    """
    score = 0
    score += ema_signal()    # -1, 0, +1
    score += macd_signal()   # -1, 0, +1
    score += rsi_signal()    # -1, 0, +1
    score += volume_signal() # -1, 0, +1
    return score

# Interpretation
# >= +2: LONG bias
# <= -2: SHORT bias
# else:  Neutral (use default)
```

#### Layer 6: Real-time Price Spike Detection
```python
PRICE_SPIKE_THRESHOLD = 3%   # In 5-minute window
EXTREME_SPIKE = 5%           # Critical alert
```

Use WebSocket for real-time monitoring. REST polling is too slow.

#### Layer 7: Funding Rate Monitoring
```python
FUNDING_WARNING = 0.1%   # Per 8-hour period
FUNDING_EXTREME = 0.3%   # Critical
```

| Your Position | Funding Rate | Impact |
|---------------|--------------|--------|
| LONG | Positive (>0) | You PAY |
| LONG | Negative (<0) | You RECEIVE |
| SHORT | Positive (>0) | You RECEIVE |
| SHORT | Negative (<0) | You PAY |

**Action**: Alert when approaching funding time with unfavorable rate.

#### Layer 8: BTC Correlation Analysis
```python
# Altcoins follow BTC. Monitor for divergence.
btc_score = analyze_btc()
sol_score = analyze_sol()

if grid_side == "LONG" and btc_score <= -2:
    alert("BTC bearish while LONG - high risk")
```

#### Layer 9: Liquidity Crisis Detection
```python
SPREAD_WARNING = 0.3%
SPREAD_CRITICAL = 0.5%
MIN_DEPTH = 5000  # USD
```

**Action**: Pause trading if spread too wide or depth too thin.

#### Layer 10: Position Size Coordination
```python
position_usage = current_exposure / max_allowed
if position_usage >= 0.9:
    alert("Position usage at 90% - reduce exposure")
```

#### Layer 11: Intelligent Drawdown Management

Three-level graduated protection:

| Level | Drawdown | Action |
|-------|----------|--------|
| 1 | 15% | Pause new BUY orders (TP still active) |
| 2 | 20% | Cut 30% of position |
| 3 | 25% | Close all positions (cut loss) |

**Auto Re-entry Logic** (after cut loss):
```python
def check_reentry_conditions():
    # Wait minimum time
    if time_since_cut < 30_minutes:
        return False

    # Check favorable conditions
    if rsi < 40 and rsi_rising:      # Oversold and bouncing
        if btc_score > -2:            # BTC not strongly bearish
            return True

    return False

# Re-enter with reduced size
REENTRY_SIZE_RATIO = 50%  # Start with half position
```

#### Layer 12: Side Switch Protection

**Critical Rule**: NEVER switch grid side while holding positions.

```python
def can_switch_side():
    if has_open_positions():
        alert("Cannot switch side - holding positions. Wait for TP.")
        return False
    return True
```

**Why?** Switching LONG→SHORT while holding LONG positions means:
1. Closing LONG at current price (potential loss)
2. Opening SHORT immediately
3. Double exposure to adverse move

---

## Technical Indicators

### Primary Indicators

#### 1. SuperTrend (Trailing TP)
```python
# Parameters
SUPERTREND_LENGTH = 10   # ATR period
SUPERTREND_MULTIPLIER = 3.0

# Output
SuperTrendResult:
    trend_line: float      # Current value
    direction: int         # 1=bullish, -1=bearish
    long_stop: float       # Trailing stop for LONG
    short_stop: float      # Trailing stop for SHORT
```

**Usage for Trailing TP:**
| Position | Condition | TP Price |
|----------|-----------|----------|
| LONG profitable | long_stop > entry | Use SuperTrend long_stop |
| LONG underwater | long_stop < entry | Use fixed 1.5% |
| SHORT profitable | short_stop < entry | Use SuperTrend short_stop |
| SHORT underwater | short_stop > entry | Use fixed 1.5% |

#### 2. StochRSI (Momentum)
```python
# Parameters
RSI_LENGTH = 14
STOCH_LENGTH = 14
K_SMOOTH = 3
D_SMOOTH = 3

# Output
StochRSIResult:
    k_line: float       # 0-100
    d_line: float       # 0-100
    is_oversold: bool   # K < 20
    is_overbought: bool # K > 80
```

**Interpretation:**
| K Value | State | LONG Grid | SHORT Grid |
|---------|-------|-----------|------------|
| > 80 | Overbought | Caution (pullback likely) | Entry signal |
| < 20 | Oversold | Entry signal | Caution (bounce likely) |
| 20-80 | Neutral | Normal operation | Normal operation |

#### 3. EMA Crossover
```python
EMA_FAST = 9
EMA_SLOW = 21

# Signal
if ema_fast > ema_slow:
    return +1  # Bullish
elif ema_fast < ema_slow:
    return -1  # Bearish
else:
    return 0
```

#### 4. MACD
```python
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Signal
if macd_line > signal_line and macd_line > 0:
    return +1  # Strong bullish
elif macd_line < signal_line and macd_line < 0:
    return -1  # Strong bearish
else:
    return 0
```

### Point-Based Trend Confirmation

For faster side-switching decisions:

```python
SWITCH_THRESHOLD_POINTS = 4
CHECK_INTERVAL = 300  # 5 minutes

def accumulate_points(target_side):
    points = 0

    # Strong trend signal
    if abs(trend_score) >= 3:
        points += 2
    elif abs(trend_score) == 2:
        points += 1

    # StochRSI confirmation
    if target_side == "LONG" and stochrsi.is_oversold:
        points += 1
    if target_side == "SHORT" and stochrsi.is_overbought:
        points += 1

    # Volume confirmation
    if volume > avg_volume * 1.3:
        points += 1

    # Decay on unclear signals
    if abs(trend_score) < 2:
        points -= 1

    return points

# Switch when accumulated points >= threshold
```

---

## Grid Configuration

### Dynamic Grid Sizing

Base grid range on ATR (Average True Range):

```python
def calculate_grid_range(atr_percent):
    """
    ATR-based dynamic grid range
    """
    multiplier = 2.5  # Adjust based on risk tolerance
    range_percent = atr_percent * multiplier

    # Apply bounds
    MIN_RANGE = 2.0   # Never tighter than ±2%
    MAX_RANGE = 6.0   # Never wider than ±6%

    return max(MIN_RANGE, min(MAX_RANGE, range_percent))
```

### Grid Level Calculation

```python
def calculate_grid_levels(center_price, range_percent, num_levels):
    half_range = range_percent / 100
    upper_bound = center_price * (1 + half_range)
    lower_bound = center_price * (1 - half_range)

    step = (upper_bound - lower_bound) / (num_levels - 1)

    levels = []
    for i in range(num_levels):
        price = lower_bound + (step * i)
        levels.append(round(price, 4))

    return levels
```

### Position Sizing

```python
def calculate_quantity(grid_price, usdt_per_grid, leverage):
    """
    Calculate order quantity for a grid level
    """
    notional = usdt_per_grid * leverage
    quantity = notional / grid_price

    # Round to exchange precision
    return round_to_precision(quantity, step_size=0.01)
```

### Recommended Settings by Account Size

| Account Size | Grid Count | USDT/Grid | Leverage | Max Positions |
|--------------|------------|-----------|----------|---------------|
| $500-1000 | 8-10 | $20-25 | 3-5x | 4-5 |
| $1000-5000 | 10-12 | $30-50 | 5x | 5-6 |
| $5000-10000 | 12-15 | $50-100 | 5x | 6-8 |
| $10000+ | 15-20 | $100-200 | 3-5x | 8-10 |

---

## Market Condition Detection

### Market State Classification

```python
def classify_market(atr_percent, trend_score):
    # Volatility classification
    if atr_percent > 10:
        volatility = "EXTREME"
    elif atr_percent > 5:
        volatility = "HIGH"
    elif atr_percent > 2:
        volatility = "MODERATE"
    else:
        volatility = "LOW"

    # Trend classification
    if abs(trend_score) >= 3:
        trend = "STRONG_TREND"
    elif abs(trend_score) >= 2:
        trend = "MODERATE_TREND"
    else:
        trend = "RANGING"

    return f"{trend}_{volatility}"
```

### Optimal Conditions for Grid Trading

| Market State | Grid Performance | Recommendation |
|--------------|------------------|----------------|
| RANGING_LOW | Excellent | Tighten grid, increase frequency |
| RANGING_MODERATE | Good | Standard settings |
| RANGING_HIGH | Risky | Widen grid, reduce size |
| MODERATE_TREND_* | Acceptable | Align grid side with trend |
| STRONG_TREND_* | Poor | Consider pausing or trend-following |
| *_EXTREME | Dangerous | Auto-pause |

---

## Lessons Learned

### Lesson 1: Respect BTC Correlation

**Scenario**: SOL showing bullish signals, BTC strongly bearish.
**Mistake**: Opened LONG grid on SOL.
**Result**: BTC dumped, SOL followed despite local bullish signals.

**Rule**: Always check BTC trend before opening altcoin positions. If BTC score ≤ -2, treat altcoin LONG signals with skepticism.

### Lesson 2: StochRSI Extremes are Warnings, Not Entries

**Scenario**: StochRSI at 95 (extremely overbought), price still rising.
**Mistake**: Assumed "overbought = immediate reversal", opened SHORT.
**Result**: Price continued higher, shorts trapped.

**Rule**: Overbought/oversold are warnings to reduce exposure or wait, not immediate reversal signals. Wait for confirmation (crossover, divergence).

### Lesson 3: Low Volume Rallies Fail

**Scenario**: Price spiked 3% on 0.5x average volume.
**Mistake**: Chased the rally with aggressive LONG.
**Result**: Rally reversed within hours, positions stopped out.

**Rule**: Volume < 0.7x average = weak conviction. Don't chase. Wait for pullback.

### Lesson 4: Funding Rate Compounds Silently

**Scenario**: Held LONG through 3 funding periods at 0.1% each.
**Mistake**: Ignored funding cost, focused only on price target.
**Result**: Paid 0.3% in funding, ate into grid profits.

**Rule**: Factor funding into TP calculation. If funding is unfavorable, either reduce hold time or increase TP target.

### Lesson 5: Position Sync on Restart is Critical

**Scenario**: Bot restarted, had existing positions from manual trades.
**Mistake**: Bot didn't detect existing positions, placed new orders.
**Result**: Over-leveraged, multiple positions in same direction.

**Rule**: Always sync positions from exchange on startup before placing new orders.

### Lesson 6: TP Must Use Average Entry, Not Individual Fill

**Scenario**: Multiple grid levels filled at different prices.
**Mistake**: Placed TP for each fill based on its individual entry.
**Result**: Some TPs were below breakeven for the total position.

**Rule**: Calculate TP from weighted average entry of entire position.

### Lesson 7: Never Switch Sides with Open Positions

**Scenario**: Market turned bearish while holding LONG.
**Mistake**: Switched to SHORT grid immediately.
**Result**: Closed LONG at loss, then SHORT got trapped when market bounced.

**Rule**: Wait for positions to close at TP before switching sides. Block side-switch while holding.

### Lesson 8: Pullbacks in Uptrends are Entries, Not Exits

**Scenario**: Strong uptrend, price pulled back 3%.
**Mistake**: Panicked, closed LONG positions, switched to SHORT.
**Result**: Price resumed uptrend, missed gains.

**Rule**: In confirmed uptrend, 3-5% pullbacks are normal. Let grid buy the dip. Only exit on trend break (e.g., below key support, trend score flip).

### Lesson 9: Liquidity Matters More Than Price

**Scenario**: Saw attractive entry price during low-liquidity period.
**Mistake**: Market order into thin orderbook.
**Result**: 0.5% slippage ate entire expected profit.

**Rule**: Check spread and depth before trading. If spread > 0.3% or depth < $5k, wait or reduce size.

### Lesson 10: Dynamic Grid > Static Grid

**Scenario**: Fixed 3% grid range during both calm and volatile markets.
**Mistake**: Grid too tight in volatile market, too wide in calm market.
**Result**: Unnecessary fills in volatility, missed opportunities in calm.

**Rule**: Adjust grid range based on ATR. Higher volatility = wider grid.

---

## Configuration Templates

### Conservative (Capital Preservation)

```python
# For accounts prioritizing safety over returns
LEVERAGE = 3
GRID_COUNT = 8
QUANTITY_PER_GRID_USDT = 20
MAX_POSITIONS = 3
MAX_DRAWDOWN_PERCENT = 15
DRAWDOWN_PAUSE_PERCENT = 10
DRAWDOWN_PARTIAL_CUT_PERCENT = 12
DRAWDOWN_FULL_CUT_PERCENT = 15
USE_TRAILING_TP = True
AUTO_SWITCH_SIDE_ENABLED = False  # Manual side selection
```

### Balanced (Default)

```python
# Standard settings for most conditions
LEVERAGE = 5
GRID_COUNT = 12
QUANTITY_PER_GRID_USDT = 25
MAX_POSITIONS = 5
MAX_DRAWDOWN_PERCENT = 20
DRAWDOWN_PAUSE_PERCENT = 15
DRAWDOWN_PARTIAL_CUT_PERCENT = 20
DRAWDOWN_FULL_CUT_PERCENT = 25
USE_TRAILING_TP = True
AUTO_SWITCH_SIDE_ENABLED = True
SWITCH_THRESHOLD_POINTS = 4
```

### Aggressive (Higher Risk/Reward)

```python
# For experienced traders in favorable conditions
LEVERAGE = 10
GRID_COUNT = 15
QUANTITY_PER_GRID_USDT = 40
MAX_POSITIONS = 8
MAX_DRAWDOWN_PERCENT = 25
DRAWDOWN_PAUSE_PERCENT = 18
DRAWDOWN_PARTIAL_CUT_PERCENT = 22
DRAWDOWN_FULL_CUT_PERCENT = 28
USE_TRAILING_TP = True
AUTO_SWITCH_SIDE_ENABLED = True
SWITCH_THRESHOLD_POINTS = 3  # Faster switching
```

---

## Operational Playbook

### Daily Checklist

- [ ] Check overnight funding payments
- [ ] Review BTC trend (leading indicator)
- [ ] Check StochRSI for extremes (>80 or <20)
- [ ] Verify grid alignment with current price (drift < 3%)
- [ ] Review position exposure vs limits
- [ ] Check liquidity conditions (spread, depth)

### When to Pause Trading

1. **ATR > 10%** (extreme volatility)
2. **Spread > 0.5%** (liquidity crisis)
3. **Drawdown > pause threshold**
4. **Major news event imminent** (FOMC, CPI, etc.)
5. **Exchange issues** (API errors, delayed fills)

### When to Switch Grid Side

**Switch to SHORT when:**
- Trend score ≤ -2 for 2+ consecutive checks
- BTC breaks below key support
- StochRSI overbought (>80) + bearish divergence
- Funding rate extremely positive (>0.3%)

**Switch to LONG when:**
- Trend score ≥ +2 for 2+ consecutive checks
- BTC breaks above key resistance
- StochRSI oversold (<20) + bullish divergence
- Funding rate extremely negative (<-0.3%)

### Emergency Procedures

**Flash Crash Protocol:**
1. Immediately cancel all open orders
2. Assess position exposure
3. If underwater, set tight stop-loss
4. Wait for volatility to subside
5. Re-evaluate before resuming

**API Failure Protocol:**
1. Log into exchange web interface
2. Manually verify order status
3. Cancel orphaned orders if needed
4. Investigate API issue before resuming bot

---

## HyperLiquid-Specific Notes

### Key Differences from Aster DEX

| Feature | Aster DEX | HyperLiquid |
|---------|-----------|-------------|
| API Style | Binance-compatible | Custom REST + WebSocket |
| Funding Interval | 8 hours | 1 hour |
| Margin Mode | Crossed/Isolated | Crossed only (per-asset) |
| Order Types | Limit, Market, Stop | Limit, Market, Trigger |

### HyperLiquid API Considerations

1. **Rate Limits**: More restrictive than CEXs. Batch operations when possible.
2. **WebSocket**: Use for real-time data; REST for order management.
3. **Funding**: Hourly funding requires more frequent monitoring.
4. **Margin**: All positions share margin per asset. Monitor total exposure.

### Recommended Adjustments for HyperLiquid

```python
# More frequent funding checks
FUNDING_CHECK_INTERVAL = 600  # 10 minutes (vs 15 for 8-hour funding)

# Tighter position management (shared margin)
MAX_POSITION_PERCENT = 70  # Lower than isolated margin

# Account for hourly funding
FUNDING_WARNING = 0.05%   # Lower threshold (1-hour rate)
FUNDING_EXTREME = 0.15%
```

---

## Appendix: Key Formulas

### Unrealized PnL
```python
# LONG
upnl = (current_price - entry_price) * quantity

# SHORT
upnl = (entry_price - current_price) * quantity
```

### Drawdown Percentage
```python
drawdown_pct = ((peak_balance - current_balance) / peak_balance) * 100
```

### Position Value
```python
position_value = quantity * current_price
margin_used = position_value / leverage
```

### Break-even Price (with fees)
```python
# LONG
breakeven = entry_price * (1 + (2 * fee_rate))  # Entry + exit fee

# SHORT
breakeven = entry_price * (1 - (2 * fee_rate))
```

### Grid Step Size
```python
step = (upper_bound - lower_bound) / (num_levels - 1)
step_percent = (step / center_price) * 100
```

---

*Document generated from Aster DEX Grid Trading Bot v1.0*
*Last updated: 2026-01-17*
