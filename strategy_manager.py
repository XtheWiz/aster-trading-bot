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
"""
import logging
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal

import pandas as pd
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
    macd_histogram: float = 0.0


@dataclass
class TrendScore:
    """
    Multi-indicator trend scoring for auto-switch decisions.
    
    Score Components:
    - EMA: +1 if fast > slow (bullish), -1 if fast < slow (bearish)
    - MACD Histogram: +1 if positive, -1 if negative  
    - RSI: +1 if > 50, -1 if < 50
    
    Total Score Range: -3 (strong bearish) to +3 (strong bullish)
    """
    ema_score: int = 0  # -1, 0, or +1
    macd_score: int = 0  # -1, 0, or +1
    rsi_score: int = 0  # -1, 0, or +1
    
    @property
    def total(self) -> int:
        """Total trend score from -3 to +3."""
        return self.ema_score + self.macd_score + self.rsi_score
    
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
    
    def __str__(self) -> str:
        return f"TrendScore(EMA={self.ema_score:+d}, MACD={self.macd_score:+d}, RSI={self.rsi_score:+d}, Total={self.total:+d})"


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
        
        # Strategy Parameters - reduced to 30 min for faster response
        self.check_interval = 1800  # Check every 30 minutes (was 1 hour)
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
        
    async def start_monitoring(self):
        """Start the background monitoring loop."""
        self.is_running = True
        logger.info("Strategy Manager started - monitoring market conditions...")
        
        while self.is_running:
            try:
                analysis = await self.analyze_market()
                await self.evaluate_safety(analysis)
                
                # Sleep for interval
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in strategy monitoring loop: {e}")
                await asyncio.sleep(60) # Short retry on error

    async def stop(self):
        """Stop the monitoring loop."""
        self.is_running = False
        logger.info("Strategy Manager stopped")

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
            
            # SMA (Simple Moving Averages) - keep for backward compatibility
            df['ma_fast'] = df['close'].rolling(window=self.ma_fast_period).mean()
            df['ma_slow'] = df['close'].rolling(window=self.ma_slow_period).mean()
            
            # RSI (14-period)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # MACD
            ema_12 = df['close'].ewm(span=12, adjust=False).mean()
            ema_26 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = ema_12 - ema_26
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']
            
            # Get latest values
            latest = df.iloc[-1]
            current_price = Decimal(str(latest['close']))
            atr = Decimal(str(latest['atr'])) if not pd.isna(latest['atr']) else Decimal("0")
            ma_fast = Decimal(str(latest['ma_fast']))
            ma_slow = Decimal(str(latest['ma_slow']))
            ema_fast = Decimal(str(latest['ema_fast']))
            ema_slow = Decimal(str(latest['ema_slow']))
            rsi = float(latest['rsi']) if not pd.isna(latest['rsi']) else 50.0
            macd_hist = float(latest['macd_hist']) if not pd.isna(latest['macd_hist']) else 0.0
            
            # 4. Calculate Trend Score for Auto-Switch
            trend_score = self._calculate_trend_score(ema_fast, ema_slow, macd_hist, rsi)
            self.current_trend_score = trend_score
            
            # 5. Determine State
            
            # Volatility Ratio (ATR / Price)
            vol_ratio = atr / current_price if current_price > 0 else Decimal("0")
            
            # Trend Check (using SMA for state classification)
            if ma_fast > ma_slow * Decimal("1.02"):  # 2% buffer
                trend = "UP"
            elif ma_fast < ma_slow * Decimal("0.98"):
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
                macd_histogram=macd_hist,
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
        rsi: float
    ) -> TrendScore:
        """
        Calculate trend score from multiple indicators.
        
        Scoring:
        - EMA: +1 if fast > slow × 1.01 (bullish), -1 if fast < slow × 0.99 (bearish)
        - MACD Histogram: +1 if > 0, -1 if < 0
        - RSI: +1 if > 50, -1 if < 50
        
        Returns:
            TrendScore with individual and total scores
        """
        # EMA Score (with 1% buffer to avoid noise)
        if ema_fast > ema_slow * Decimal("1.01"):
            ema_score = 1
        elif ema_fast < ema_slow * Decimal("0.99"):
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
        
        # RSI Score
        if rsi > 50:
            rsi_score = 1
        elif rsi < 50:
            rsi_score = -1
        else:
            rsi_score = 0
        
        return TrendScore(
            ema_score=ema_score,
            macd_score=macd_score,
            rsi_score=rsi_score
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

