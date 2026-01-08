"""
Strategy Manager for Aster DEX Grid Bot

This module acts as the "Quantitative Supervisor" for the grid bot.
It periodically analyzes market conditions (Trend, Volatility) and
recommends configuration changes or safety actions.

Core Responsibilities:
1. Fetch historical candle data (K-lines)
2. Calculate technical indicators (ATR, SMA/EMA)
3. Determine Market State (Ranging, Trending, Volatile)
4. Trigger circuit breakers or config updates if conditions are dangerous
5. Real-time price spike detection via WebSocket
6. Funding rate monitoring
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Literal

import pandas as pd
import websockets

from aster_client import AsterClient
from config import config
from telegram_notifier import TelegramNotifier

logger = logging.getLogger("StrategyManager")


class MarketState(Enum):
    """Market condition classification."""
    UNKNOWN = "UNKNOWN"
    RANGING_STABLE = "RANGING_STABLE"    # Safe for Grid
    RANGING_VOLATILE = "RANGING_VOLATILE" # Risky, needs wide grid
    TRENDING_UP = "TRENDING_UP"          # Long only or Pause
    TRENDING_DOWN = "TRENDING_DOWN"      # Short only or Pause
    EXTREME_VOLATILITY = "EXTREME_VOLATILITY" # STOP TRADING


@dataclass
class MarketAnalysis:
    """Result of market analysis."""
    state: MarketState
    current_price: Decimal
    atr_value: Decimal
    trend_direction: Literal["UP", "DOWN", "FLAT"]
    volatility_score: float  # 0.0 to 100.0
    trend_score: int = 0  # -3 to +3 for auto-switch
    ema_fast: Decimal = Decimal("0")
    ema_slow: Decimal = Decimal("0")
    rsi: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    sma_20: float = 0.0
    sma_50: float = 0.0
    volume_ratio: float = 1.0  # Current volume / avg volume


@dataclass
class TrendScore:
    """
    Multi-indicator trend scoring for auto-switch decisions.

    Score Components:
    - EMA: +1 if fast > slow (bullish), -1 if fast < slow (bearish)
    - MACD Histogram: +1 if positive, -1 if negative
    - RSI: +1 if > 55, -1 if < 45
    - Volume: +1 if volume confirms trend, 0 if neutral/weak

    Total Score Range: -4 (strong bearish) to +4 (strong bullish)
    """
    ema_score: int = 0  # -1, 0, or +1
    macd_score: int = 0  # -1, 0, or +1
    rsi_score: int = 0  # -1, 0, or +1
    volume_score: int = 0  # -1, 0, or +1
    volume_ratio: float = 1.0  # For display purposes

    @property
    def total(self) -> int:
        """Total trend score from -4 to +4."""
        return self.ema_score + self.macd_score + self.rsi_score + self.volume_score

    @property
    def recommended_side(self) -> Literal["LONG", "SHORT", "STAY", "PAUSE"]:
        """Get recommended grid side based on score."""
        score = self.total
        min_score = config.grid.MIN_SWITCH_SCORE

        if score >= min_score:
            return "LONG"
        elif score <= -min_score:
            return "SHORT"
        else:
            # Unclear signal
            return config.grid.UNCLEAR_TREND_ACTION

    @property
    def volume_confirmed(self) -> bool:
        """Check if volume confirms the trend direction."""
        return self.volume_score != 0

    def __str__(self) -> str:
        vol_str = f"Vol={self.volume_score:+d}" if self.volume_score != 0 else "Vol=0"
        return f"TrendScore(EMA={self.ema_score:+d}, MACD={self.macd_score:+d}, RSI={self.rsi_score:+d}, {vol_str}, Total={self.total:+d})"


class StrategyManager:
    """
    Quantitative analysis engine for the grid bot.
    
    Enhanced features:
    - Telegram notifications when market state changes
    - Auto-pause on EXTREME_VOLATILITY
    - Auto Switch Side based on multi-indicator trend scoring
    - 30-min check interval for faster response
    """
    
    def __init__(self, client: AsterClient, bot_reference=None):
        self.client = client
        self.bot = bot_reference  # Reference to GridBot for pause/resume/switch
        self.telegram = TelegramNotifier()
        self.last_analysis: MarketAnalysis | None = None
        self.previous_state: MarketState | None = None  # Track state changes
        self.is_running = False
        
        # Strategy Parameters - reduced to 15 min for faster response
        self.check_interval = 900  # Check every 15 minutes (was 30 min)
        self.atr_period = 14
        self.ma_fast_period = 7
        self.ma_slow_period = 25
        
        # Thresholds
        self.volatility_threshold_high = Decimal("0.05")  # 5% ATR relative to price
        self.volatility_threshold_extreme = Decimal("0.10")  # 10% ATR
        
        # Auto Switch Side tracking
        self.pending_switch_side: Literal["LONG", "SHORT"] | None = None
        self.switch_confirmation_count: int = 0
        self.current_trend_score: TrendScore | None = None
        
        # Dynamic Re-Grid on TP tracking
        self.grid_placement_score: int = 0  # Score when grid was placed
        self.last_analysis_time: datetime | None = None
        self.last_regrid_time: datetime | None = None
        self.pending_regrid_count: int = 0

        # Funding rate monitoring
        self.funding_rate_threshold = Decimal("0.001")  # 0.1% - alert if higher
        self.funding_rate_extreme = Decimal("0.003")  # 0.3% - consider pausing
        self.last_funding_rate: Decimal = Decimal("0")
        self.next_funding_time: datetime | None = None

        # Real-time price spike detection
        self.price_spike_threshold = Decimal("0.03")  # 3% move triggers alert
        self.price_spike_window = 300  # 5 minutes window for spike detection
        self.price_history: list[tuple[datetime, Decimal]] = []
        self.last_spike_alert: datetime | None = None
        self.spike_alert_cooldown = 60  # Seconds between alerts
        self._price_monitor_task: asyncio.Task | None = None
        
    async def start_monitoring(self):
        """Start the background monitoring loop."""
        self.is_running = True
        logger.info("Strategy Manager started - monitoring market conditions...")

        # Start real-time price monitoring in background
        self._price_monitor_task = asyncio.create_task(self._monitor_price_spikes())

        while self.is_running:
            try:
                analysis = await self.analyze_market()
                await self.evaluate_safety(analysis)

                # Check funding rate
                await self._check_funding_rate()

                # Sleep for interval
                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in strategy monitoring loop: {e}")
                await asyncio.sleep(60)  # Short retry on error

    async def stop(self):
        """Stop the monitoring loop."""
        self.is_running = False

        # Stop price monitor
        if self._price_monitor_task:
            self._price_monitor_task.cancel()
            try:
                await self._price_monitor_task
            except asyncio.CancelledError:
                pass
            self._price_monitor_task = None

        logger.info("Strategy Manager stopped")

    async def _check_funding_rate(self) -> None:
        """
        Check current funding rate and alert if extreme.

        High funding rate considerations:
        - Positive rate > 0.1%: Longs pay shorts, unfavorable for LONG grid
        - Negative rate < -0.1%: Shorts pay longs, favorable for LONG grid
        - Extreme rate > 0.3%: Consider closing positions before funding
        """
        try:
            symbol = config.trading.SYMBOL
            funding_data = await self.client.get_funding_rate(symbol)

            if not funding_data:
                return

            rate_str = funding_data.get("lastFundingRate", "0")
            self.last_funding_rate = Decimal(rate_str)
            next_funding_ms = funding_data.get("nextFundingTime", 0)
            self.next_funding_time = datetime.fromtimestamp(next_funding_ms / 1000) if next_funding_ms else None

            # Calculate minutes until next funding
            minutes_until_funding = 0
            if self.next_funding_time:
                delta = self.next_funding_time - datetime.now()
                minutes_until_funding = max(0, delta.total_seconds() / 60)

            rate_percent = float(self.last_funding_rate * 100)
            logger.info(f"Funding Rate: {rate_percent:.4f}% | Next funding in {minutes_until_funding:.0f} min")

            # Get current grid side
            grid_side = config.grid.GRID_SIDE

            # Alert if funding rate is significant
            abs_rate = abs(self.last_funding_rate)

            if abs_rate >= self.funding_rate_extreme:
                # Extreme funding rate
                direction = "LONGS pay" if self.last_funding_rate > 0 else "SHORTS pay"
                impact = "UNFAVORABLE" if (
                    (grid_side == "LONG" and self.last_funding_rate > 0) or
                    (grid_side == "SHORT" and self.last_funding_rate < 0)
                ) else "FAVORABLE"

                await self.telegram.send_message(
                    f"🚨 EXTREME Funding Rate Alert!\n\n"
                    f"Rate: {rate_percent:.4f}%\n"
                    f"Direction: {direction}\n"
                    f"Grid Side: {grid_side}\n"
                    f"Impact: {impact}\n"
                    f"Next Funding: {minutes_until_funding:.0f} min\n\n"
                    f"⚠️ Consider closing positions before funding!"
                )

            elif abs_rate >= self.funding_rate_threshold:
                # High but not extreme
                direction = "LONGS pay" if self.last_funding_rate > 0 else "SHORTS pay"
                impact = "unfavorable" if (
                    (grid_side == "LONG" and self.last_funding_rate > 0) or
                    (grid_side == "SHORT" and self.last_funding_rate < 0)
                ) else "favorable"

                logger.warning(f"High funding rate ({rate_percent:.4f}%) - {impact} for {grid_side}")

                # Only alert if close to funding time (< 30 min) and unfavorable
                if minutes_until_funding <= 30 and impact == "unfavorable":
                    await self.telegram.send_message(
                        f"⚠️ High Funding Rate Warning\n\n"
                        f"Rate: {rate_percent:.4f}%\n"
                        f"Direction: {direction}\n"
                        f"Grid Side: {grid_side}\n"
                        f"Next Funding: {minutes_until_funding:.0f} min\n\n"
                        f"Consider reducing position size."
                    )

        except Exception as e:
            logger.error(f"Error checking funding rate: {e}")

    async def _monitor_price_spikes(self) -> None:
        """
        Real-time price monitoring via WebSocket to detect sudden spikes.

        Connects to mark price stream and tracks price changes over
        a 5-minute window. Alerts if price moves more than 3%.
        """
        symbol = config.trading.SYMBOL.lower()
        logger.info(f"Starting real-time price spike monitor for {symbol}")

        while self.is_running:
            try:
                # Connect to mark price stream (updates every second)
                ws_url = f"{self.client.ws_url}/ws/{symbol}@markPrice"
                logger.debug(f"Connecting to price stream: {ws_url}")

                async with websockets.connect(ws_url) as ws:
                    logger.info("Price spike monitor connected")

                    async for message in ws:
                        if not self.is_running:
                            break

                        try:
                            data = json.loads(message)
                            mark_price = Decimal(data.get("p", "0"))

                            if mark_price > 0:
                                await self._process_price_update(mark_price)

                        except Exception as e:
                            logger.debug(f"Error processing price message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Price monitor connection error: {e}")
                await asyncio.sleep(5)  # Retry after 5 seconds

    async def _process_price_update(self, current_price: Decimal) -> None:
        """
        Process price update and check for spikes.

        Args:
            current_price: Current mark price
        """
        now = datetime.now()

        # Add to history
        self.price_history.append((now, current_price))

        # Clean old entries (older than window)
        cutoff = now - timedelta(seconds=self.price_spike_window)
        self.price_history = [
            (ts, price) for ts, price in self.price_history
            if ts >= cutoff
        ]

        # Need at least 2 points to detect spike
        if len(self.price_history) < 2:
            return

        # Get oldest price in window
        oldest_price = self.price_history[0][1]

        # Calculate change percentage
        if oldest_price > 0:
            change = (current_price - oldest_price) / oldest_price
            abs_change = abs(change)

            # Check if spike threshold exceeded
            if abs_change >= self.price_spike_threshold:
                await self._handle_price_spike(current_price, oldest_price, change)

    async def _handle_price_spike(
        self,
        current_price: Decimal,
        reference_price: Decimal,
        change: Decimal
    ) -> None:
        """
        Handle detected price spike.

        Args:
            current_price: Current price
            reference_price: Price at start of window
            change: Percentage change (as decimal, e.g., 0.03 = 3%)
        """
        now = datetime.now()

        # Check cooldown to avoid alert spam
        if self.last_spike_alert:
            seconds_since_alert = (now - self.last_spike_alert).total_seconds()
            if seconds_since_alert < self.spike_alert_cooldown:
                return

        self.last_spike_alert = now
        change_percent = float(change * 100)
        direction = "📈 UP" if change > 0 else "📉 DOWN"

        logger.warning(
            f"Price spike detected! {direction}: {change_percent:+.2f}% "
            f"(${reference_price:.2f} → ${current_price:.2f})"
        )

        # Determine impact on current grid
        grid_side = config.grid.GRID_SIDE
        if grid_side == "LONG":
            impact = "FAVORABLE" if change > 0 else "UNFAVORABLE"
        else:
            impact = "FAVORABLE" if change < 0 else "UNFAVORABLE"

        await self.telegram.send_message(
            f"🚨 Price Spike Alert!\n\n"
            f"Direction: {direction}\n"
            f"Change: {change_percent:+.2f}%\n"
            f"Price: ${reference_price:.2f} → ${current_price:.2f}\n"
            f"Window: {self.price_spike_window // 60} min\n\n"
            f"Grid: {grid_side}\n"
            f"Impact: {impact}\n\n"
            f"⚠️ Check your positions!"
        )

        # If extreme move (>5%) and unfavorable, trigger safety check
        if abs(change) >= Decimal("0.05") and impact == "UNFAVORABLE":
            logger.critical(f"EXTREME price move detected: {change_percent:+.2f}%")
            await self.telegram.send_message(
                f"🚨🚨 EXTREME MOVE ALERT 🚨🚨\n\n"
                f"Price moved {change_percent:+.2f}% against your {grid_side} grid!\n\n"
                f"Consider immediate action!"
            )

    async def analyze_market(self, symbol: str | None = None) -> MarketAnalysis:
        """
        Fetch data and perform technical analysis.
        """
        symbol = symbol or config.trading.SYMBOL
        logger.info(f"Analyzing market for {symbol}...")
        
        # 1. Fetch K-lines (Candles) - 1h interval
        try:
            klines = await self.client.get_klines(
                symbol=symbol,
                interval="1h",
                limit=100
            )
            
            if not klines:
                logger.warning("No kline data received")
                return self._get_default_analysis()

            # 2. Convert to DataFrame
            # API returns: [open_time, open, high, low, close, volume, ...]
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume', 
                'close_time', 'quote_vol', 'trades', 'taker_buy_base', 
                'taker_buy_quote', 'ignore'
            ])
            
            # Type conversion
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric)
            
            # 3. Calculate Indicators
            
            # True Range (TR)
            df['tr1'] = df['high'] - df['low']
            df['tr2'] = abs(df['high'] - df['close'].shift(1))
            df['tr3'] = abs(df['low'] - df['close'].shift(1))
            df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
            
            # ATR (Average True Range)
            df['atr'] = df['tr'].rolling(window=self.atr_period).mean()
            
            # EMA (Exponential Moving Averages) - more responsive than SMA
            df['ema_fast'] = df['close'].ewm(span=self.ma_fast_period, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=self.ma_slow_period, adjust=False).mean()
            
            # RSI (14-period) - Wilder's RSI using EMA
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # MACD
            ema_12 = df['close'].ewm(span=12, adjust=False).mean()
            ema_26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = ema_12 - ema_26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']

            # SMA for support/resistance (used by indicator_analyzer)
            df['sma_20'] = df['close'].rolling(window=20).mean()
            df['sma_50'] = df['close'].rolling(window=50).mean()

            # Volume analysis (use completed candle, not current partial candle)
            df['volume_sma'] = df['volume'].rolling(window=20).mean()
            df['volume_ratio'] = df['volume'].shift(1) / df['volume_sma'].shift(1)

            # Get latest values
            latest = df.iloc[-1]
            current_price = Decimal(str(latest['close']))
            atr = Decimal(str(latest['atr'])) if not pd.isna(latest['atr']) else Decimal("0")
            ema_fast = Decimal(str(latest['ema_fast']))
            ema_slow = Decimal(str(latest['ema_slow']))
            rsi = float(latest['rsi']) if not pd.isna(latest['rsi']) else 50.0
            macd_val = float(latest['macd']) if not pd.isna(latest['macd']) else 0.0
            macd_signal_val = float(latest['macd_signal']) if not pd.isna(latest['macd_signal']) else 0.0
            macd_hist = float(latest['macd_hist']) if not pd.isna(latest['macd_hist']) else 0.0
            sma_20 = float(latest['sma_20']) if not pd.isna(latest['sma_20']) else 0.0
            sma_50 = float(latest['sma_50']) if not pd.isna(latest['sma_50']) else 0.0
            volume_ratio = float(latest['volume_ratio']) if not pd.isna(latest['volume_ratio']) else 1.0
            
            # 4. Calculate Trend Score for Auto-Switch (with volume confirmation)
            trend_score = self._calculate_trend_score(ema_fast, ema_slow, macd_hist, rsi, volume_ratio)
            self.current_trend_score = trend_score
            
            # 5. Determine State

            # Volatility Ratio (ATR / Price)
            vol_ratio = atr / current_price if current_price > 0 else Decimal("0")

            # Trend Check (using EMA with 1.5% buffer for consistency)
            if ema_fast > ema_slow * Decimal("1.015"):  # 1.5% buffer
                trend = "UP"
            elif ema_fast < ema_slow * Decimal("0.985"):
                trend = "DOWN"
            else:
                trend = "FLAT"
                
            # State classification
            state = MarketState.UNKNOWN
            if vol_ratio > self.volatility_threshold_extreme:
                state = MarketState.EXTREME_VOLATILITY
            elif vol_ratio > self.volatility_threshold_high:
                state = MarketState.RANGING_VOLATILE
            elif trend == "FLAT":
                state = MarketState.RANGING_STABLE
            elif trend == "UP":
                state = MarketState.TRENDING_UP
            elif trend == "DOWN":
                state = MarketState.TRENDING_DOWN
            else:
                state = MarketState.RANGING_STABLE
            
            analysis = MarketAnalysis(
                state=state,
                current_price=current_price,
                atr_value=atr,
                trend_direction=trend,
                volatility_score=float(vol_ratio * 100),
                trend_score=trend_score.total,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                rsi=rsi,
                macd=macd_val,
                macd_signal=macd_signal_val,
                macd_histogram=macd_hist,
                sma_20=sma_20,
                sma_50=sma_50,
                volume_ratio=volume_ratio,
            )
            
            self.last_analysis = analysis
            
            logger.info(
                f"Market Analysis: {state.value} | Price: {current_price} | "
                f"ATR: {atr:.4f} ({vol_ratio*100:.2f}%) | Trend: {trend} | "
                f"TrendScore: {trend_score}"
            )
            
            return analysis
            
        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            return self._get_default_analysis()

    def _get_default_analysis(self) -> MarketAnalysis:
        return MarketAnalysis(
            state=MarketState.UNKNOWN,
            current_price=Decimal("0"),
            atr_value=Decimal("0"),
            trend_direction="FLAT",
            volatility_score=0.0
        )
    
    def _calculate_trend_score(
        self,
        ema_fast: Decimal,
        ema_slow: Decimal,
        macd_hist: float,
        rsi: float,
        volume_ratio: float = 1.0
    ) -> TrendScore:
        """
        Calculate trend score from multiple indicators.

        Scoring:
        - EMA: +1 if fast > slow × 1.005 (bullish), -1 if fast < slow × 0.995 (bearish)
        - MACD Histogram: +1 if > 0, -1 if < 0
        - RSI: +1 if > 55, -1 if < 45 (with neutral zone 45-55)
        - Volume: +1/-1 if volume confirms trend direction (ratio > 1.2)

        Returns:
            TrendScore with individual and total scores
        """
        # EMA Score (with 0.5% buffer - more sensitive for trend scoring)
        if ema_fast > ema_slow * Decimal("1.005"):
            ema_score = 1
        elif ema_fast < ema_slow * Decimal("0.995"):
            ema_score = -1
        else:
            ema_score = 0

        # MACD Histogram Score
        if macd_hist > 0:
            macd_score = 1
        elif macd_hist < 0:
            macd_score = -1
        else:
            macd_score = 0

        # RSI Score (with neutral zone 45-55)
        if rsi > 55:
            rsi_score = 1
        elif rsi < 45:
            rsi_score = -1
        else:
            rsi_score = 0

        # Volume Confirmation Score
        # Volume confirms trend if ratio > 1.2 (20% above average)
        # Direction determined by other indicators
        volume_score = 0
        if volume_ratio > 1.2:
            # High volume - determine if it confirms bullish or bearish
            other_score = ema_score + macd_score + rsi_score
            if other_score > 0:
                volume_score = 1  # High volume confirms bullish
                logger.debug(f"Volume confirms bullish: ratio={volume_ratio:.2f}")
            elif other_score < 0:
                volume_score = -1  # High volume confirms bearish
                logger.debug(f"Volume confirms bearish: ratio={volume_ratio:.2f}")
            # If other_score == 0, volume is neutral (no clear direction to confirm)

        return TrendScore(
            ema_score=ema_score,
            macd_score=macd_score,
            rsi_score=rsi_score,
            volume_score=volume_score,
            volume_ratio=volume_ratio
        )
    
    async def _check_auto_switch(self, analysis: MarketAnalysis) -> None:
        """
        Check if grid side should be switched based on trend score.
        
        Uses confirmation period to prevent whipsaw:
        - If recommended side is same for 2 consecutive checks, switch
        - If signal changes, reset confirmation counter
        """
        if not config.grid.AUTO_SWITCH_SIDE_ENABLED:
            return
        
        if self.current_trend_score is None:
            return
        
        recommended = self.current_trend_score.recommended_side
        current_side = config.grid.GRID_SIDE
        
        # If recommendation is STAY or PAUSE, don't switch
        if recommended in ("STAY", "PAUSE"):
            if self.pending_switch_side is not None:
                logger.info(f"Trend unclear (score={self.current_trend_score.total}), canceling pending switch")
                self.pending_switch_side = None
                self.switch_confirmation_count = 0
            return
        
        # If already on recommended side, nothing to do
        if recommended == current_side:
            self.pending_switch_side = None
            self.switch_confirmation_count = 0
            return
        
        # Check if this is a new pending switch or continuation
        if self.pending_switch_side == recommended:
            # Same recommendation as before, increment counter
            self.switch_confirmation_count += 1
            logger.info(
                f"Switch to {recommended} confirmed: {self.switch_confirmation_count}/"
                f"{config.grid.SWITCH_CONFIRMATION_CHECKS}"
            )
            
            # Check if we have enough confirmations
            if self.switch_confirmation_count >= config.grid.SWITCH_CONFIRMATION_CHECKS:
                await self._execute_switch(recommended, analysis)
        else:
            # New recommendation, start fresh
            self.pending_switch_side = recommended
            self.switch_confirmation_count = 1
            logger.info(
                f"New switch signal: {current_side} → {recommended} "
                f"(score={self.current_trend_score.total}, need {config.grid.SWITCH_CONFIRMATION_CHECKS} confirmations)"
            )
            
            await self.telegram.send_message(
                f"🔄 Trend Signal Detected\n\n"
                f"Current: {current_side}\n"
                f"Recommended: {recommended}\n"
                f"Score: {self.current_trend_score}\n\n"
                f"Waiting for confirmation ({self.switch_confirmation_count}/{config.grid.SWITCH_CONFIRMATION_CHECKS})..."
            )
    
    async def _execute_switch(self, new_side: Literal["LONG", "SHORT"], analysis: MarketAnalysis) -> None:
        """Execute the grid side switch."""
        old_side = config.grid.GRID_SIDE
        
        logger.warning(f"🔄 AUTO-SWITCH: {old_side} → {new_side}")
        
        await self.telegram.send_message(
            f"🔄 Auto-Switch Executing!\n\n"
            f"Old Side: {old_side}\n"
            f"New Side: {new_side}\n"
            f"Score: {self.current_trend_score}\n"
            f"Price: ${analysis.current_price:.2f}\n\n"
            f"Canceling orders and repositioning grid..."
        )
        
        # Execute switch via bot reference
        if self.bot and hasattr(self.bot, 'switch_grid_side'):
            await self.bot.switch_grid_side(new_side)
            
            await self.telegram.send_message(
                f"✅ Switch Complete!\n\n"
                f"Grid now: {new_side}-only\n"
                f"New orders placed."
            )
        else:
            logger.error("Cannot switch: bot reference not available")
        
        # Reset tracking
        self.pending_switch_side = None
        self.switch_confirmation_count = 0
    
    def record_grid_placement(self) -> None:
        """Record the current trend score when grid is placed."""
        if self.current_trend_score:
            self.grid_placement_score = self.current_trend_score.total
        self.last_regrid_time = datetime.now()
        logger.info(f"Grid placement recorded: score={self.grid_placement_score}")
    
    async def should_regrid_on_tp(self) -> Literal["REPLACE", "REGRID", "WAIT"]:
        """
        Decide action after TP fill.
        
        Returns:
            "REPLACE" - Re-place BUY at original level (same trend)
            "REGRID" - Do full re-grid (trend changed + confirmed)
            "WAIT" - Wait for more confirmation (trend changed but not confirmed)
        """
        if not config.grid.REGRID_ON_TP_ENABLED:
            return "REPLACE"  # Default behavior
        
        # Check rate limit
        if self.last_regrid_time:
            minutes_since_regrid = (datetime.now() - self.last_regrid_time).total_seconds() / 60
            if minutes_since_regrid < config.grid.REGRID_MIN_INTERVAL_MINUTES:
                logger.info(f"Rate limited: only {minutes_since_regrid:.1f} min since last re-grid")
                return "REPLACE"
        
        # Check if we need fresh analysis or can use cache
        need_fresh_analysis = True
        if self.last_analysis_time:
            minutes_since_analysis = (datetime.now() - self.last_analysis_time).total_seconds() / 60
            if minutes_since_analysis < config.grid.REGRID_ANALYSIS_CACHE_MINUTES:
                need_fresh_analysis = False
                logger.info(f"Using cached analysis ({minutes_since_analysis:.1f} min old)")
        
        # Run analysis if needed
        if need_fresh_analysis:
            await self.analyze_market()
            self.last_analysis_time = datetime.now()
        
        if not self.current_trend_score:
            return "REPLACE"
        
        current_score = self.current_trend_score.total
        
        # Compare direction: positive = bullish, negative = bearish
        same_direction = (
            (current_score >= 0 and self.grid_placement_score >= 0) or
            (current_score < 0 and self.grid_placement_score < 0)
        )
        
        if same_direction:
            # Same direction → just re-place the BUY
            self.pending_regrid_count = 0
            logger.info(f"Same trend direction (score: {self.grid_placement_score} -> {current_score}), replacing BUY")
            return "REPLACE"
        else:
            # Different direction → need confirmation
            self.pending_regrid_count += 1
            logger.info(
                f"Trend changed (score: {self.grid_placement_score} -> {current_score}), "
                f"confirmation {self.pending_regrid_count}/2"
            )
            
            if self.pending_regrid_count >= 2:
                self.pending_regrid_count = 0
                return "REGRID"
            else:
                await self.telegram.send_message(
                    f"🔄 Trend Change Detected\n\n"
                    f"Old Score: {self.grid_placement_score:+d}\n"
                    f"New Score: {current_score:+d}\n\n"
                    f"Waiting for confirmation..."
                )
                return "WAIT"

    async def evaluate_safety(self, analysis: MarketAnalysis):
        """
        Check if current bot config is safe for market conditions.
        Now with Telegram notifications and auto-pause on extreme conditions.
        """
        current_state = analysis.state
        state_changed = self.previous_state is not None and current_state != self.previous_state
        
        # Send Telegram notification if state changed
        if state_changed:
            await self._notify_state_change(analysis)
        
        # Handle each market state
        if current_state == MarketState.EXTREME_VOLATILITY:
            logger.critical("🚨 EXTREME VOLATILITY DETECTED! Auto-pausing bot...")
            
            await self.telegram.send_message(
                f"🚨 EXTREME VOLATILITY ALERT!\n\n"
                f"Volatility: {analysis.volatility_score:.1f}%\n"
                f"Price: ${analysis.current_price:.2f}\n"
                f"ATR: ${analysis.atr_value:.4f}\n\n"
                f"⚠️ Bot is being PAUSED for safety!"
            )
            
            # Auto-pause the bot if reference exists
            if self.bot and hasattr(self.bot, 'pause'):
                await self.bot.pause()
            
        elif current_state == MarketState.RANGING_VOLATILE:
            current_range = config.grid.GRID_RANGE_PERCENT
            if current_range < Decimal("10.0"):
                msg = (
                    f"⚠️ High Volatility Warning\n\n"
                    f"Volatility: {analysis.volatility_score:.1f}%\n"
                    f"Current Grid Range: {current_range}%\n\n"
                    f"Recommendation: Widen to 15%+"
                )
                logger.warning(msg)
                if state_changed:
                    await self.telegram.send_message(msg)
        
        elif current_state == MarketState.TRENDING_DOWN:
            msg = "📉 Strong DOWNTREND detected. Long-biased grids may be trapped."
            logger.warning(msg)
            if state_changed:
                await self.telegram.send_message(
                    f"📉 Market Trend Change\n\n"
                    f"Trend: DOWNTREND\n"
                    f"Price: ${analysis.current_price:.2f}\n\n"
                    f"⚠️ Long-only grids may accumulate losses."
                )
        
        elif current_state == MarketState.TRENDING_UP:
            if state_changed:
                await self.telegram.send_message(
                    f"📈 Market Trend Change\n\n"
                    f"Trend: UPTREND\n"
                    f"Price: ${analysis.current_price:.2f}\n\n"
                    f"✅ Favorable for LONG-only grid strategy."
                )
        
        elif current_state == MarketState.RANGING_STABLE:
            if state_changed:
                await self.telegram.send_message(
                    f"📊 Market Stabilized\n\n"
                    f"State: RANGING_STABLE\n"
                    f"Volatility: {analysis.volatility_score:.1f}%\n"
                    f"Price: ${analysis.current_price:.2f}\n\n"
                    f"✅ Ideal conditions for grid trading!"
                )
        
        # Update previous state
        self.previous_state = current_state
        
        # Check for auto-switch based on trend score
        await self._check_auto_switch(analysis)

    async def _notify_state_change(self, analysis: MarketAnalysis):
        """Send Telegram notification when market state changes."""
        state_icons = {
            MarketState.UNKNOWN: "❓",
            MarketState.RANGING_STABLE: "✅",
            MarketState.RANGING_VOLATILE: "⚠️",
            MarketState.TRENDING_UP: "📈",
            MarketState.TRENDING_DOWN: "📉",
            MarketState.EXTREME_VOLATILITY: "🚨",
        }
        
        icon = state_icons.get(analysis.state, "📊")
        prev_state = self.previous_state.value if self.previous_state else "UNKNOWN"
        
        logger.info(
            f"Market state changed: {prev_state} → {analysis.state.value}"
        )

