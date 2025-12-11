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


class StrategyManager:
    """
    Quantitative analysis engine for the grid bot.
    """
    
    def __init__(self, client: AsterClient):
        self.client = client
        self.last_analysis: MarketAnalysis | None = None
        self.is_running = False
        
        # Strategy Parameters
        self.check_interval = 3600  # Check every 1 hour
        self.atr_period = 14
        self.ma_fast_period = 7
        self.ma_slow_period = 25
        
        # Thresholds
        self.volatility_threshold_high = Decimal("0.05") # 5% ATR relative to price
        self.volatility_threshold_extreme = Decimal("0.10") # 10% ATR
        
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
            
            # Moving Averages (for Trend)
            df['ma_fast'] = df['close'].rolling(window=self.ma_fast_period).mean()
            df['ma_slow'] = df['close'].rolling(window=self.ma_slow_period).mean()
            
            # Get latest values
            latest = df.iloc[-1]
            current_price = Decimal(str(latest['close']))
            atr = Decimal(str(latest['atr'])) if not pd.isna(latest['atr']) else Decimal("0")
            ma_fast = Decimal(str(latest['ma_fast']))
            ma_slow = Decimal(str(latest['ma_slow']))
            
            # 4. Determine State
            
            # Volatility Ratio (ATR / Price)
            vol_ratio = atr / current_price if current_price > 0 else Decimal("0")
            
            # Trend Check
            if ma_fast > ma_slow * Decimal("1.02"): # 2% buffer
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
            elif trend == "flat":
                state = MarketState.RANGING_STABLE
            elif trend == "UP":
                state = MarketState.TRENDING_UP
            elif trend == "DOWN":
                state = MarketState.TRENDING_DOWN
            else:
                state = MarketState.RANGING_STABLE # Default to stable if flat and low vol
            
            analysis = MarketAnalysis(
                state=state,
                current_price=current_price,
                atr_value=atr,
                trend_direction=trend,
                volatility_score=float(vol_ratio * 100)
            )
            
            self.last_analysis = analysis
            
            logger.info(
                f"Market Analysis: {state.value} | Price: {current_price} | "
                f"ATR: {atr:.4f} ({vol_ratio*100:.2f}%) | Trend: {trend}"
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

    async def evaluate_safety(self, analysis: MarketAnalysis):
        """
        Check if current bot config is safe for market conditions.
        """
        if analysis.state == MarketState.EXTREME_VOLATILITY:
            logger.critical("🚨 EXTREME VOLATILITY DETECTED! Recommendation: PAUSE BOT")
            # In a fully autonomous system, this would trigger self.bot.pause()
            # For now, we log critical warning.
            
        elif analysis.state == MarketState.RANGING_VOLATILE:
            # Check grid range
            current_range = config.grid.GRID_RANGE_PERCENT
            if current_range < Decimal("10.0"):
                logger.warning(
                    f"⚠️ High Volatility ({analysis.volatility_score:.1f}%) "
                    f"but Grid Range is tight ({current_range}%). Recommendation: Widen to 15%+"
                )
        
        elif analysis.state == MarketState.TRENDING_DOWN:
             logger.warning("📉 Strong DOWNTREND. Long-biased grids may be trapped.")
