# Grid Trading Strategy Improvements

> **Purpose**: Bot-agnostic strategy documentation for enhanced grid trading.
> **Applicable to**: Any perpetual futures grid trading bot (Hyperliquid, Binance, Bybit, etc.)

---

## Table of Contents

1. [Dependencies](#dependencies)
2. [New Indicators](#new-indicators)
3. [Enhancement 1: Multi-Timeframe Analysis](#enhancement-1-multi-timeframe-analysis)
4. [Enhancement 2: Momentum Breakout Detection](#enhancement-2-momentum-breakout-detection)
5. [Enhancement 3: Trailing Take-Profit (SuperTrend)](#enhancement-3-trailing-take-profit-supertrend)
6. [Enhancement 4: Faster Trend Confirmation](#enhancement-4-faster-trend-confirmation)
7. [Enhancement 5: Volatility Regime Adaptation](#enhancement-5-volatility-regime-adaptation)
8. [Optional Enhancements](#optional-enhancements)
9. [Configuration Reference](#configuration-reference)
10. [Backtesting Guidelines](#backtesting-guidelines)

---

## Dependencies

```
pandas>=2.0.0
pandas-ta>=0.3.14b
```

**Why pandas-ta?**
- Pure Python, no C compilation required
- Easy deployment on cloud platforms (Railway, Heroku, etc.)
- 130+ indicators with consistent API
- Active maintenance

---

## New Indicators

### 1. Bollinger Bands (BBands)

**Purpose**: Dynamic support/resistance, volatility measurement

**Calculation**:
```python
import pandas_ta as ta

# Standard Bollinger Bands (20-period, 2 std dev)
bbands = df.ta.bbands(length=20, std=2)
# Returns: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0

upper_band = bbands['BBU_20_2.0']  # Upper band (resistance)
lower_band = bbands['BBL_20_2.0']  # Lower band (support)
middle_band = bbands['BBM_20_2.0'] # Middle band (SMA)
bandwidth = bbands['BBB_20_2.0']   # Bandwidth % (volatility)
percent_b = bbands['BBP_20_2.0']   # %B (price position within bands)
```

**Usage**:
| Condition | Interpretation |
|-----------|----------------|
| Bandwidth < 4% | Low volatility (squeeze) - breakout imminent |
| Bandwidth > 10% | High volatility - widen grid spacing |
| Price at upper band | Potential resistance - TP target |
| Price at lower band | Potential support - entry zone |
| %B < 0 | Price below lower band (oversold) |
| %B > 1 | Price above upper band (overbought) |

---

### 2. Average Directional Index (ADX)

**Purpose**: Trend strength measurement (NOT direction)

**Calculation**:
```python
# ADX with +DI and -DI (14-period default)
adx = df.ta.adx(length=14)
# Returns: ADX_14, DMP_14 (+DI), DMN_14 (-DI)

adx_value = adx['ADX_14']
plus_di = adx['DMP_14']   # Positive directional indicator
minus_di = adx['DMN_14']  # Negative directional indicator
```

**Usage**:
| ADX Value | Market State | Grid Strategy |
|-----------|--------------|---------------|
| < 20 | Weak/No trend (ranging) | Optimal for grid trading |
| 20-25 | Developing trend | Use with caution |
| 25-40 | Strong trend | Consider pausing grid |
| > 40 | Very strong trend | Pause grid, ride momentum |

**Trend Direction** (when ADX > 25):
- `+DI > -DI` = Bullish trend
- `-DI > +DI` = Bearish trend

---

### 3. SuperTrend

**Purpose**: Trend-following indicator with built-in trailing stop

**Calculation**:
```python
# SuperTrend (10-period, 3x ATR multiplier)
supertrend = df.ta.supertrend(length=10, multiplier=3.0)
# Returns: SUPERT_10_3.0, SUPERTd_10_3.0, SUPERTl_10_3.0, SUPERTs_10_3.0

trend_line = supertrend['SUPERT_10_3.0']      # SuperTrend line value
direction = supertrend['SUPERTd_10_3.0']       # 1 = bullish, -1 = bearish
long_stop = supertrend['SUPERTl_10_3.0']       # Long trailing stop
short_stop = supertrend['SUPERTs_10_3.0']      # Short trailing stop
```

**Usage**:
| Signal | Condition | Action |
|--------|-----------|--------|
| Buy | Direction changes from -1 to 1 | Enter long / close short |
| Sell | Direction changes from 1 to -1 | Exit long / enter short |
| Trailing Stop (Long) | Price < long_stop | Exit long position |
| Trailing Stop (Short) | Price > short_stop | Exit short position |

**For Grid Bot TP**:
```python
# Use SuperTrend as trailing TP instead of fixed %
if position_side == "LONG":
    trailing_tp = supertrend['SUPERTl_10_3.0']  # Long stop as trailing TP
else:
    trailing_tp = supertrend['SUPERTs_10_3.0']  # Short stop as trailing TP
```

---

### 4. Stochastic RSI (StochRSI)

**Purpose**: Faster overbought/oversold detection than standard RSI

**Calculation**:
```python
# Stochastic RSI (14-period RSI, 14-period Stoch, 3-period smoothing)
stochrsi = df.ta.stochrsi(length=14, rsi_length=14, k=3, d=3)
# Returns: STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3

stochrsi_k = stochrsi['STOCHRSIk_14_14_3_3']  # Fast line (0-100)
stochrsi_d = stochrsi['STOCHRSId_14_14_3_3']  # Signal line (0-100)
```

**Usage**:
| Condition | Interpretation | Action |
|-----------|----------------|--------|
| K < 20 | Oversold | Potential buy zone |
| K > 80 | Overbought | Potential sell zone |
| K < 20 AND RSI < 40 | Strong oversold | High-confidence buy signal |
| K > 80 AND RSI > 60 | Strong overbought | High-confidence sell signal |
| K crosses above D (< 20) | Bullish crossover | Entry signal |
| K crosses below D (> 80) | Bearish crossover | Exit signal |

---

### 5. VWAP (Volume Weighted Average Price)

**Purpose**: Institutional reference price, trend bias

**Calculation**:
```python
# VWAP (resets daily by default)
vwap = df.ta.vwap()
# Returns: VWAP_D

vwap_value = vwap['VWAP_D']
```

**Usage**:
| Condition | Interpretation | Grid Bias |
|-----------|----------------|-----------|
| Price > VWAP | Bullish bias (buyers in control) | Favor LONG grid |
| Price < VWAP | Bearish bias (sellers in control) | Favor SHORT grid |
| Price = VWAP | Fair value | Neutral |

**Note**: VWAP is most useful for intraday/session-based trading. For multi-day positions, use with caution.

---

## Enhancement 1: Multi-Timeframe Analysis

### Concept

Use higher timeframe (4H) for trend direction, lower timeframe (1H) for entry timing.

### Decision Tree

```
┌─────────────────────────────────────────────────────────────┐
│                    4H TIMEFRAME ANALYSIS                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Calculate trend score on 4H candles:                        │
│  - EMA(7) vs EMA(25)                                         │
│  - MACD histogram direction                                  │
│  - RSI position (>55 bullish, <45 bearish)                  │
│  - ADX strength                                              │
│                                                              │
│  4H_TREND = BULLISH if score >= 2                           │
│  4H_TREND = BEARISH if score <= -2                          │
│  4H_TREND = NEUTRAL otherwise                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    1H TIMEFRAME ANALYSIS                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Calculate same indicators on 1H candles                     │
│                                                              │
│  1H_TREND = BULLISH if score >= 2                           │
│  1H_TREND = BEARISH if score <= -2                          │
│  1H_TREND = NEUTRAL otherwise                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    ALIGNMENT DECISION                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  IF 4H_TREND == 1H_TREND:                                   │
│      alignment = "ALIGNED"                                   │
│      → AGGRESSIVE mode: +30% position size                   │
│                                                              │
│  ELIF 4H_TREND != NEUTRAL AND 1H_TREND == NEUTRAL:          │
│      alignment = "CAUTIOUS"                                  │
│      → REDUCED mode: -30% position size                      │
│                                                              │
│  ELIF 4H_TREND opposite to 1H_TREND:                        │
│      alignment = "CONFLICTING"                               │
│      → PAUSE: No new grid orders                             │
│                                                              │
│  ELSE:                                                       │
│      alignment = "NEUTRAL"                                   │
│      → NORMAL mode: standard position size                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Pseudocode

```python
class MTFAlignment(Enum):
    ALIGNED = "ALIGNED"           # Both timeframes agree
    CAUTIOUS = "CAUTIOUS"         # Higher TF trending, lower TF neutral
    CONFLICTING = "CONFLICTING"   # Timeframes disagree
    NEUTRAL = "NEUTRAL"           # Both unclear

def analyze_multi_timeframe(symbol: str) -> MTFAnalysis:
    # Get 4H candles
    h4_klines = get_klines(symbol, interval="4h", limit=50)
    h4_score = calculate_trend_score(h4_klines)

    # Get 1H candles
    h1_klines = get_klines(symbol, interval="1h", limit=100)
    h1_score = calculate_trend_score(h1_klines)

    # Determine trends
    h4_trend = "BULLISH" if h4_score >= 2 else "BEARISH" if h4_score <= -2 else "NEUTRAL"
    h1_trend = "BULLISH" if h1_score >= 2 else "BEARISH" if h1_score <= -2 else "NEUTRAL"

    # Determine alignment
    if h4_trend == h1_trend and h4_trend != "NEUTRAL":
        alignment = MTFAlignment.ALIGNED
        size_multiplier = 1.3  # +30%
    elif h4_trend != "NEUTRAL" and h1_trend == "NEUTRAL":
        alignment = MTFAlignment.CAUTIOUS
        size_multiplier = 0.7  # -30%
    elif h4_trend != h1_trend and h4_trend != "NEUTRAL" and h1_trend != "NEUTRAL":
        alignment = MTFAlignment.CONFLICTING
        size_multiplier = 0.0  # Pause
    else:
        alignment = MTFAlignment.NEUTRAL
        size_multiplier = 1.0  # Normal

    return MTFAnalysis(
        h4_trend=h4_trend,
        h4_score=h4_score,
        h1_trend=h1_trend,
        h1_score=h1_score,
        alignment=alignment,
        size_multiplier=size_multiplier
    )
```

### Configuration

```python
MTF_CONFIG = {
    "higher_timeframe": "4h",
    "lower_timeframe": "1h",
    "bullish_threshold": 2,
    "bearish_threshold": -2,
    "aligned_size_multiplier": 1.3,
    "cautious_size_multiplier": 0.7,
    "conflicting_action": "PAUSE",
}
```

---

## Enhancement 2: Momentum Breakout Detection

### Concept

Detect strong directional moves that warrant pausing the grid to avoid accumulating against the trend.

### Detection Criteria

A breakout is confirmed when ALL conditions are met:
1. **ADX > 40** (strong trend strength)
2. **Price move > 2x ATR** in short period (15-30 min)
3. **Volume > 2x average** (volume confirmation)
4. **RSI extreme** (>70 bullish, <30 bearish)

### Decision Tree

```
┌─────────────────────────────────────────────────────────────┐
│                  BREAKOUT DETECTION                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  price_move = abs(current_price - price_15min_ago)          │
│  atr_multiple = price_move / ATR_value                       │
│  volume_ratio = current_volume / avg_volume_20               │
│                                                              │
│  breakout_detected = (                                       │
│      ADX > 40 AND                                            │
│      atr_multiple > 2.0 AND                                  │
│      volume_ratio > 2.0 AND                                  │
│      (RSI > 70 OR RSI < 30)                                  │
│  )                                                           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  BREAKOUT RESPONSE                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  IF breakout_detected:                                       │
│      direction = "UP" if current > price_15min_ago else "DOWN"│
│                                                              │
│      IF grid_side == "LONG" AND direction == "UP":          │
│          → HOLD position, widen TP by 50%                    │
│          → Cancel pending BUY orders (don't add more)        │
│                                                              │
│      ELIF grid_side == "LONG" AND direction == "DOWN":      │
│          → PAUSE grid immediately                            │
│          → Consider partial exit if loss > 5%                │
│                                                              │
│      ELIF grid_side == "SHORT" AND direction == "DOWN":     │
│          → HOLD position, widen TP by 50%                    │
│          → Cancel pending SELL orders                        │
│                                                              │
│      ELIF grid_side == "SHORT" AND direction == "UP":       │
│          → PAUSE grid immediately                            │
│          → Consider partial exit if loss > 5%                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Pseudocode

```python
@dataclass
class BreakoutSignal:
    detected: bool
    direction: Literal["UP", "DOWN", None]
    strength: Literal["WEAK", "MODERATE", "STRONG"]
    atr_multiple: float
    volume_ratio: float
    recommendation: Literal["HOLD", "PAUSE", "EXIT_PARTIAL"]

def detect_breakout(
    current_price: Decimal,
    price_15min_ago: Decimal,
    atr_value: Decimal,
    adx_value: float,
    rsi_value: float,
    volume_ratio: float,
    grid_side: str
) -> BreakoutSignal:

    # Calculate move magnitude
    price_move = abs(current_price - price_15min_ago)
    atr_multiple = float(price_move / atr_value)

    # Check breakout conditions
    breakout_detected = (
        adx_value > 40 and
        atr_multiple > 2.0 and
        volume_ratio > 2.0 and
        (rsi_value > 70 or rsi_value < 30)
    )

    if not breakout_detected:
        return BreakoutSignal(detected=False, direction=None, ...)

    # Determine direction
    direction = "UP" if current_price > price_15min_ago else "DOWN"

    # Determine strength
    if atr_multiple >= 4.0 and volume_ratio >= 3.0:
        strength = "STRONG"
    elif atr_multiple >= 3.0 and volume_ratio >= 2.5:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    # Determine recommendation
    position_aligned = (
        (grid_side == "LONG" and direction == "UP") or
        (grid_side == "SHORT" and direction == "DOWN")
    )

    if position_aligned:
        recommendation = "HOLD"  # Ride the momentum
    elif strength == "STRONG":
        recommendation = "EXIT_PARTIAL"  # Strong counter-move
    else:
        recommendation = "PAUSE"  # Wait and see

    return BreakoutSignal(
        detected=True,
        direction=direction,
        strength=strength,
        atr_multiple=atr_multiple,
        volume_ratio=volume_ratio,
        recommendation=recommendation
    )
```

### Configuration

```python
BREAKOUT_CONFIG = {
    "adx_threshold": 40,
    "atr_multiple_threshold": 2.0,
    "volume_ratio_threshold": 2.0,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "tp_widen_multiplier": 1.5,  # Widen TP by 50% on aligned breakout
    "partial_exit_percent": 0.3,  # Exit 30% on strong counter-breakout
}
```

---

## Enhancement 3: Trailing Take-Profit (SuperTrend)

### Concept

Replace fixed-percentage TP with dynamic trailing using SuperTrend indicator.

### Why SuperTrend?

- **Adapts to volatility**: ATR-based, automatically widens in volatile markets
- **Built-in trailing logic**: No manual calculation needed
- **Clear signals**: Direction changes provide clean exit points
- **Battle-tested**: Widely used in systematic trading

### Implementation

```
┌─────────────────────────────────────────────────────────────┐
│                 SUPERTREND TRAILING TP                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  LONG POSITION:                                              │
│  ─────────────────                                           │
│  initial_tp = entry_price * (1 + base_tp_percent)           │
│  supertrend_stop = SuperTrend_Long_Stop (SUPERTl)           │
│                                                              │
│  IF supertrend_stop > entry_price:                          │
│      trailing_tp = supertrend_stop  # Use SuperTrend        │
│  ELSE:                                                       │
│      trailing_tp = initial_tp       # Use fixed TP          │
│                                                              │
│  EXIT when: price < trailing_tp OR SuperTrend direction = -1│
│                                                              │
│  ─────────────────────────────────────────────────────────  │
│                                                              │
│  SHORT POSITION:                                             │
│  ─────────────────                                           │
│  initial_tp = entry_price * (1 - base_tp_percent)           │
│  supertrend_stop = SuperTrend_Short_Stop (SUPERTs)          │
│                                                              │
│  IF supertrend_stop < entry_price:                          │
│      trailing_tp = supertrend_stop  # Use SuperTrend        │
│  ELSE:                                                       │
│      trailing_tp = initial_tp       # Use fixed TP          │
│                                                              │
│  EXIT when: price > trailing_tp OR SuperTrend direction = 1 │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Pseudocode

```python
@dataclass
class TrailingTPState:
    entry_price: Decimal
    position_side: Literal["LONG", "SHORT"]
    initial_tp: Decimal
    current_trailing_tp: Decimal
    supertrend_active: bool
    highest_price: Decimal  # For LONG
    lowest_price: Decimal   # For SHORT

def calculate_trailing_tp(
    state: TrailingTPState,
    current_price: Decimal,
    supertrend_long_stop: Decimal,
    supertrend_short_stop: Decimal,
    supertrend_direction: int
) -> Decimal:

    if state.position_side == "LONG":
        # Update highest price seen
        state.highest_price = max(state.highest_price, current_price)

        # Check if SuperTrend stop is above entry (profitable)
        if supertrend_long_stop > state.entry_price:
            state.supertrend_active = True
            state.current_trailing_tp = supertrend_long_stop
        elif not state.supertrend_active:
            # Use fixed TP until SuperTrend catches up
            state.current_trailing_tp = state.initial_tp

        # SuperTrend direction flip = immediate exit signal
        if supertrend_direction == -1:
            return current_price  # Market exit

    else:  # SHORT
        state.lowest_price = min(state.lowest_price, current_price)

        if supertrend_short_stop < state.entry_price:
            state.supertrend_active = True
            state.current_trailing_tp = supertrend_short_stop
        elif not state.supertrend_active:
            state.current_trailing_tp = state.initial_tp

        if supertrend_direction == 1:
            return current_price  # Market exit

    return state.current_trailing_tp

def should_exit_position(
    state: TrailingTPState,
    current_price: Decimal
) -> bool:
    if state.position_side == "LONG":
        return current_price <= state.current_trailing_tp
    else:
        return current_price >= state.current_trailing_tp
```

### Configuration

```python
TRAILING_TP_CONFIG = {
    "supertrend_length": 10,
    "supertrend_multiplier": 3.0,
    "base_tp_percent": 1.5,  # Fallback fixed TP
    "min_profit_for_trailing": 0.5,  # Start trailing after 0.5% profit
}
```

---

## Enhancement 4: Faster Trend Confirmation

### Concept

Replace fixed 2-check (30 min) confirmation with weighted point system for faster response.

### Point System

| Signal Source | Condition | Points |
|---------------|-----------|--------|
| **Trend Score** | Strong (±3 or ±4) | 2 points |
| **Trend Score** | Moderate (±2) | 1 point |
| **StochRSI** | Extreme (<20 or >80) | 1 point |
| **VWAP** | Price aligned with trend | 1 point |
| **Volume** | Above average (>1.3x) | 1 point |

**Threshold**: 4 points to switch grid side

### Decision Tree

```
┌─────────────────────────────────────────────────────────────┐
│              FAST TREND CONFIRMATION                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  CHECK EVERY 5 MINUTES (vs 15 min before)                   │
│                                                              │
│  points = 0                                                  │
│  recommended_side = current_side                             │
│                                                              │
│  # Trend score points                                        │
│  IF trend_score >= 3:                                        │
│      points += 2                                             │
│      recommended_side = "LONG"                               │
│  ELIF trend_score == 2:                                      │
│      points += 1                                             │
│      recommended_side = "LONG"                               │
│  ELIF trend_score <= -3:                                     │
│      points += 2                                             │
│      recommended_side = "SHORT"                              │
│  ELIF trend_score == -2:                                     │
│      points += 1                                             │
│      recommended_side = "SHORT"                              │
│  ELSE:                                                       │
│      # Unclear - decay accumulated points                    │
│      accumulated_points = max(0, accumulated_points - 1)     │
│      RETURN                                                  │
│                                                              │
│  # StochRSI bonus                                            │
│  IF stochrsi_k < 20 AND recommended_side == "LONG":         │
│      points += 1                                             │
│  ELIF stochrsi_k > 80 AND recommended_side == "SHORT":      │
│      points += 1                                             │
│                                                              │
│  # VWAP bonus                                                │
│  IF price > vwap AND recommended_side == "LONG":            │
│      points += 1                                             │
│  ELIF price < vwap AND recommended_side == "SHORT":         │
│      points += 1                                             │
│                                                              │
│  # Volume bonus                                              │
│  IF volume_ratio > 1.3:                                      │
│      points += 1                                             │
│                                                              │
│  # Accumulate or reset                                       │
│  IF recommended_side == pending_direction:                   │
│      accumulated_points += points                            │
│  ELSE:                                                       │
│      pending_direction = recommended_side                    │
│      accumulated_points = points                             │
│                                                              │
│  # Check threshold                                           │
│  IF accumulated_points >= 4:                                 │
│      IF recommended_side != current_side:                    │
│          EXECUTE SIDE SWITCH                                 │
│      accumulated_points = 0                                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Pseudocode

```python
class FastTrendConfirmation:
    def __init__(self):
        self.accumulated_points = 0
        self.pending_direction: Optional[str] = None
        self.last_check_time: Optional[datetime] = None

    def check_confirmation(
        self,
        trend_score: int,
        stochrsi_k: float,
        price: Decimal,
        vwap: Decimal,
        volume_ratio: float,
        current_side: str
    ) -> Optional[str]:
        """Returns new side if switch should happen, None otherwise."""

        points = 0
        recommended_side = None

        # Trend score points
        if trend_score >= 3:
            points += 2
            recommended_side = "LONG"
        elif trend_score == 2:
            points += 1
            recommended_side = "LONG"
        elif trend_score <= -3:
            points += 2
            recommended_side = "SHORT"
        elif trend_score == -2:
            points += 1
            recommended_side = "SHORT"
        else:
            # Unclear trend - decay points
            self.accumulated_points = max(0, self.accumulated_points - 1)
            return None

        # StochRSI bonus
        if stochrsi_k < 20 and recommended_side == "LONG":
            points += 1
        elif stochrsi_k > 80 and recommended_side == "SHORT":
            points += 1

        # VWAP bonus
        if price > vwap and recommended_side == "LONG":
            points += 1
        elif price < vwap and recommended_side == "SHORT":
            points += 1

        # Volume bonus
        if volume_ratio > 1.3:
            points += 1

        # Accumulate or reset
        if recommended_side == self.pending_direction:
            self.accumulated_points += points
        else:
            self.pending_direction = recommended_side
            self.accumulated_points = points

        # Check threshold
        if self.accumulated_points >= 4:
            if recommended_side != current_side:
                self.accumulated_points = 0
                return recommended_side
            self.accumulated_points = 0

        return None
```

### Configuration

```python
FAST_CONFIRMATION_CONFIG = {
    "check_interval_seconds": 300,  # 5 minutes
    "strong_signal_points": 2,
    "moderate_signal_points": 1,
    "stochrsi_bonus_threshold_low": 20,
    "stochrsi_bonus_threshold_high": 80,
    "volume_bonus_threshold": 1.3,
    "switch_threshold_points": 4,
    "decay_rate": 1,  # Points to decay on unclear signal
}
```

---

## Enhancement 5: Volatility Regime Adaptation

### Concept

Dynamically adjust grid parameters based on current volatility regime.

### Regime Detection

```
┌─────────────────────────────────────────────────────────────┐
│              VOLATILITY REGIME DETECTION                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  atr_percent = (ATR / current_price) * 100                  │
│  bb_bandwidth = Bollinger_Bandwidth                          │
│  adx_value = ADX_14                                          │
│                                                              │
│  IF atr_percent < 1.5 OR bb_bandwidth < 4:                  │
│      regime = "LOW"        # Tight range, squeeze           │
│                                                              │
│  ELIF atr_percent < 3.0:                                     │
│      regime = "NORMAL"     # Standard conditions             │
│                                                              │
│  ELIF atr_percent < 5.0:                                     │
│      regime = "HIGH"       # Elevated volatility             │
│                                                              │
│  ELSE: # atr_percent >= 5.0 OR adx > 40                     │
│      regime = "EXTREME"    # Crisis or strong trend          │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Regime Parameters

| Regime | Grid Levels | Grid Range | Base TP | Qty Multiplier |
|--------|-------------|------------|---------|----------------|
| LOW | 15 | 2.0% | 0.8% | 1.2x |
| NORMAL | 12 | 3.5% | 1.5% | 1.0x |
| HIGH | 10 | 5.0% | 2.0% | 0.8x |
| EXTREME | 8 | 7.0% | 2.5% | 0.5x |

### Pseudocode

```python
class VolatilityRegime(Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"

@dataclass
class RegimeParameters:
    grid_levels: int
    grid_range_percent: Decimal
    base_tp_percent: Decimal
    quantity_multiplier: Decimal

REGIME_PARAMS = {
    VolatilityRegime.LOW: RegimeParameters(
        grid_levels=15,
        grid_range_percent=Decimal("2.0"),
        base_tp_percent=Decimal("0.8"),
        quantity_multiplier=Decimal("1.2")
    ),
    VolatilityRegime.NORMAL: RegimeParameters(
        grid_levels=12,
        grid_range_percent=Decimal("3.5"),
        base_tp_percent=Decimal("1.5"),
        quantity_multiplier=Decimal("1.0")
    ),
    VolatilityRegime.HIGH: RegimeParameters(
        grid_levels=10,
        grid_range_percent=Decimal("5.0"),
        base_tp_percent=Decimal("2.0"),
        quantity_multiplier=Decimal("0.8")
    ),
    VolatilityRegime.EXTREME: RegimeParameters(
        grid_levels=8,
        grid_range_percent=Decimal("7.0"),
        base_tp_percent=Decimal("2.5"),
        quantity_multiplier=Decimal("0.5")
    ),
}

def detect_volatility_regime(
    atr_value: Decimal,
    current_price: Decimal,
    bb_bandwidth: float,
    adx_value: float
) -> VolatilityRegime:

    atr_percent = float(atr_value / current_price * 100)

    # Check for squeeze (low volatility)
    if atr_percent < 1.5 or bb_bandwidth < 4:
        return VolatilityRegime.LOW

    # Check for extreme (crisis or strong trend)
    if atr_percent >= 5.0 or adx_value > 40:
        return VolatilityRegime.EXTREME

    # Normal vs High
    if atr_percent < 3.0:
        return VolatilityRegime.NORMAL
    else:
        return VolatilityRegime.HIGH

def get_regime_parameters(regime: VolatilityRegime) -> RegimeParameters:
    return REGIME_PARAMS[regime]
```

### Regime Transition Rules

1. **Smooth transitions**: Don't immediately switch on single reading
2. **Hysteresis**: Require 2-3 consecutive readings to confirm regime change
3. **No mid-grid changes**: Only adjust parameters on full grid reset
4. **Log regime changes**: For analysis and tuning

---

## Optional Enhancements

### 6. Session-Based Adjustments

```python
SESSIONS = {
    "ASIA": {"start_utc": 0, "end_utc": 8, "qty_mult": 0.8, "tp_mult": 0.9},
    "EUROPE": {"start_utc": 8, "end_utc": 14, "qty_mult": 1.0, "tp_mult": 1.0},
    "US": {"start_utc": 14, "end_utc": 21, "qty_mult": 1.2, "tp_mult": 1.1},
    "OVERLAP": {"start_utc": 21, "end_utc": 24, "qty_mult": 1.0, "tp_mult": 1.0},
}

def get_session_multipliers() -> dict:
    utc_hour = datetime.utcnow().hour
    for session, config in SESSIONS.items():
        if config["start_utc"] <= utc_hour < config["end_utc"]:
            return config
    return SESSIONS["OVERLAP"]
```

### 7. Order Book Imbalance

```python
def analyze_order_book(bids: list, asks: list) -> dict:
    bid_depth = sum(price * qty for price, qty in bids[:10])
    ask_depth = sum(price * qty for price, qty in asks[:10])

    imbalance = bid_depth / ask_depth if ask_depth > 0 else 999

    if imbalance > 1.5:
        signal = "STRONG_BUY"
    elif imbalance > 1.2:
        signal = "BUY"
    elif imbalance < 0.67:
        signal = "STRONG_SELL"
    elif imbalance < 0.83:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    return {"imbalance": imbalance, "signal": signal}
```

### 8. Smart DCA

```python
def should_dca(
    position_entry: Decimal,
    current_price: Decimal,
    rsi: float,
    stochrsi_k: float,
    h4_trend_score: int,
    dca_count: int
) -> bool:
    # Check if underwater enough
    drawdown = (position_entry - current_price) / position_entry * 100
    if drawdown < 5.0:
        return False

    # Check max DCA count
    if dca_count >= 2:
        return False

    # Double oversold confirmation
    if not (rsi < 40 and stochrsi_k < 20):
        return False

    # Don't DCA into strong downtrend
    if h4_trend_score <= -3:
        return False

    return True
```

---

## Configuration Reference

### Complete Configuration Object

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

@dataclass
class StrategyConfig:
    # Multi-Timeframe
    mtf_higher_tf: str = "4h"
    mtf_lower_tf: str = "1h"
    mtf_bullish_threshold: int = 2
    mtf_bearish_threshold: int = -2
    mtf_aligned_multiplier: Decimal = Decimal("1.3")
    mtf_cautious_multiplier: Decimal = Decimal("0.7")

    # Breakout Detection
    breakout_adx_threshold: float = 40.0
    breakout_atr_multiple: float = 2.0
    breakout_volume_ratio: float = 2.0
    breakout_rsi_overbought: float = 70.0
    breakout_rsi_oversold: float = 30.0
    breakout_tp_widen_multiplier: Decimal = Decimal("1.5")

    # Trailing TP (SuperTrend)
    supertrend_length: int = 10
    supertrend_multiplier: float = 3.0
    trailing_base_tp_percent: Decimal = Decimal("1.5")

    # Fast Confirmation
    fast_confirm_interval_sec: int = 300
    fast_confirm_threshold: int = 4
    fast_confirm_stochrsi_low: float = 20.0
    fast_confirm_stochrsi_high: float = 80.0
    fast_confirm_volume_bonus: float = 1.3

    # Volatility Regimes
    regime_low_atr_pct: float = 1.5
    regime_low_bb_bandwidth: float = 4.0
    regime_normal_atr_pct: float = 3.0
    regime_high_atr_pct: float = 5.0
    regime_extreme_adx: float = 40.0

    # Optional: Session Adjustments
    enable_session_adjustments: bool = False

    # Optional: Order Book
    enable_order_book_filter: bool = False
    ob_imbalance_threshold: float = 1.5

    # Optional: Smart DCA
    enable_smart_dca: bool = False
    dca_trigger_drawdown_pct: float = 5.0
    dca_max_count: int = 2
    dca_rsi_threshold: float = 40.0
    dca_stochrsi_threshold: float = 20.0
```

---

## Backtesting Guidelines

### Data Requirements

- **Minimum**: 3 months of 1H OHLCV data
- **Recommended**: 12 months covering different market conditions
- **Include**: Bull market, bear market, ranging periods

### Metrics to Track

| Metric | Target |
|--------|--------|
| Win Rate | > 70% |
| Profit Factor | > 1.5 |
| Max Drawdown | < 20% |
| Sharpe Ratio | > 1.3 |
| Avg Trade Duration | 4-24 hours |
| Trades per Day | 5-10 |

### Backtest Process

1. **Split data**: 70% training, 30% validation
2. **Optimize on training**: Find best parameters
3. **Validate on holdout**: Confirm not overfit
4. **Walk-forward**: Test on rolling windows
5. **Monte Carlo**: Simulate parameter uncertainty

### Common Pitfalls

- **Overfitting**: Too many parameters tuned to historical data
- **Look-ahead bias**: Using future data in calculations
- **Survivorship bias**: Only testing on assets that still exist
- **Ignoring costs**: Fees, slippage, funding rates
- **Ignoring liquidity**: Assuming infinite liquidity

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2026-01-15 | Initial strategy documentation |

---

## License

This strategy documentation is provided for educational purposes. Use at your own risk. Past performance does not guarantee future results.

