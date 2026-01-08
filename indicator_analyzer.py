"""
Indicator Analyzer Module
=========================
Technical analysis indicators for smart trading decisions.

This module can use pre-calculated indicators from StrategyManager
to avoid duplicate calculations and API calls.

Uses 'ta' library as fallback for calculating RSI, MACD, and other indicators
to determine optimal Take-Profit levels.
"""

import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import pandas as pd
import ta
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator

if TYPE_CHECKING:
    from strategy_manager import MarketAnalysis

logger = logging.getLogger(__name__)


@dataclass
class MarketSignal:
    """Market analysis signal with recommended actions."""
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    sma_20: float
    sma_50: float
    current_price: float
    trend: str  # "BULLISH", "BEARISH", "NEUTRAL"
    tp_percent: Decimal
    recommendation: str


class IndicatorAnalyzer:
    """
    Analyzes market indicators to provide smart TP recommendations.

    Uses:
    - RSI (Relative Strength Index) for overbought/oversold
    - MACD for trend direction
    - SMA for support/resistance

    Can use pre-calculated values from StrategyManager to avoid duplicate calculations.
    """

    def __init__(self):
        self.rsi_overbought = 70
        self.rsi_oversold = 30
        self.rsi_high = 65
        self.rsi_low = 40

    def from_market_analysis(self, analysis: "MarketAnalysis") -> Optional[MarketSignal]:
        """
        Create MarketSignal from pre-calculated MarketAnalysis.

        This avoids duplicate indicator calculations by reusing values
        already computed by StrategyManager.

        Args:
            analysis: MarketAnalysis from StrategyManager

        Returns:
            MarketSignal with indicator values and recommendations
        """
        try:
            rsi = analysis.rsi
            macd = analysis.macd
            macd_signal = analysis.macd_signal
            macd_hist = analysis.macd_histogram
            sma_20 = analysis.sma_20
            sma_50 = analysis.sma_50
            current_price = float(analysis.current_price)

            # Determine trend using existing logic
            trend = self._determine_trend(rsi, macd, macd_hist, current_price, sma_20, sma_50)

            # Calculate recommended TP
            tp_percent = self._get_tp_recommendation(rsi, macd_hist, trend)

            # Build recommendation text
            recommendation = self._build_recommendation(rsi, macd_hist, trend, tp_percent)

            logger.info(f"Using cached indicators: RSI={rsi:.1f}, MACD={macd_hist:.4f}")

            return MarketSignal(
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
                macd_histogram=macd_hist,
                sma_20=sma_20,
                sma_50=sma_50,
                current_price=current_price,
                trend=trend,
                tp_percent=tp_percent,
                recommendation=recommendation,
            )

        except Exception as e:
            logger.error(f"Error creating signal from analysis: {e}")
            return None
    
    def calculate_indicators(self, candles: list[dict]) -> Optional[MarketSignal]:
        """
        Calculate technical indicators from candle data.
        
        Args:
            candles: List of candle dicts with 'open', 'high', 'low', 'close', 'volume'
        
        Returns:
            MarketSignal with indicator values and recommendations
        """
        try:
            if len(candles) < 50:
                logger.warning(f"Not enough candles for analysis: {len(candles)}")
                return None
            
            # Aster API returns candles as arrays:
            # [timestamp, open, high, low, close, volume, close_time, quote_volume, trades, ...]
            # Convert to DataFrame with proper column names
            df = pd.DataFrame(candles, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_volume',
                'taker_buy_quote_volume', 'ignore'
            ])
            
            # Ensure numeric types for OHLCV
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Calculate RSI
            rsi_indicator = RSIIndicator(close=df['close'], window=14)
            df['rsi'] = rsi_indicator.rsi()
            
            # Calculate MACD
            macd_indicator = MACD(close=df['close'], window_fast=12, window_slow=26, window_sign=9)
            df['macd'] = macd_indicator.macd()
            df['macd_signal'] = macd_indicator.macd_signal()
            df['macd_hist'] = macd_indicator.macd_diff()
            
            # Calculate SMA
            sma_20_indicator = SMAIndicator(close=df['close'], window=20)
            sma_50_indicator = SMAIndicator(close=df['close'], window=50)
            df['sma_20'] = sma_20_indicator.sma_indicator()
            df['sma_50'] = sma_50_indicator.sma_indicator()
            
            # Get latest values
            latest = df.iloc[-1]
            
            rsi = float(latest['rsi']) if pd.notna(latest['rsi']) else 50.0
            macd = float(latest['macd']) if pd.notna(latest['macd']) else 0.0
            macd_signal = float(latest['macd_signal']) if pd.notna(latest['macd_signal']) else 0.0
            macd_hist = float(latest['macd_hist']) if pd.notna(latest['macd_hist']) else 0.0
            sma_20 = float(latest['sma_20']) if pd.notna(latest['sma_20']) else 0.0
            sma_50 = float(latest['sma_50']) if pd.notna(latest['sma_50']) else 0.0
            current_price = float(latest['close'])
            
            # Determine trend
            trend = self._determine_trend(rsi, macd, macd_hist, current_price, sma_20, sma_50)
            
            # Calculate recommended TP
            tp_percent = self._get_tp_recommendation(rsi, macd_hist, trend)
            
            # Build recommendation text
            recommendation = self._build_recommendation(rsi, macd_hist, trend, tp_percent)
            
            return MarketSignal(
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
                macd_histogram=macd_hist,
                sma_20=sma_20,
                sma_50=sma_50,
                current_price=current_price,
                trend=trend,
                tp_percent=tp_percent,
                recommendation=recommendation,
            )
            
        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")
            return None
    
    def _determine_trend(
        self, 
        rsi: float, 
        macd: float, 
        macd_hist: float,
        price: float,
        sma_20: float,
        sma_50: float
    ) -> str:
        """Determine overall market trend."""
        bullish_signals = 0
        bearish_signals = 0
        
        # RSI signals
        if rsi > 50:
            bullish_signals += 1
        elif rsi < 50:
            bearish_signals += 1
        
        # MACD histogram
        if macd_hist > 0:
            bullish_signals += 1
        elif macd_hist < 0:
            bearish_signals += 1
        
        # Price vs SMA
        if sma_20 > 0 and price > sma_20:
            bullish_signals += 1
        elif sma_20 > 0 and price < sma_20:
            bearish_signals += 1
        
        if sma_50 > 0 and price > sma_50:
            bullish_signals += 1
        elif sma_50 > 0 and price < sma_50:
            bearish_signals += 1
        
        # Determine trend
        if bullish_signals >= 3:
            return "BULLISH"
        elif bearish_signals >= 3:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    def _get_tp_recommendation(
        self, 
        rsi: float, 
        macd_hist: float,
        trend: str
    ) -> Decimal:
        """
        Get recommended TP percentage based on indicators.
        
        Logic:
        - RSI > 65 (near overbought): TP quickly at 1.0%
        - RSI < 40 (oversold): Hold longer, TP at 2.5%
        - MACD bullish + trend bullish: TP at 2.0%
        - Default: 1.5%
        """
        # Near overbought - take profit quickly
        if rsi > self.rsi_high:
            logger.info(f"RSI {rsi:.1f} > {self.rsi_high}: Near overbought, quick TP")
            return Decimal("1.0")
        
        # Oversold - hold for bigger move
        if rsi < self.rsi_low:
            logger.info(f"RSI {rsi:.1f} < {self.rsi_low}: Oversold, hold for bigger TP")
            return Decimal("2.5")
        
        # Strong bullish momentum
        if macd_hist > 0 and trend == "BULLISH":
            logger.info(f"MACD bullish + trend bullish: Medium TP")
            return Decimal("2.0")
        
        # Bearish momentum - quick TP
        if macd_hist < 0 or trend == "BEARISH":
            logger.info(f"Bearish signals detected: Quick TP")
            return Decimal("1.0")
        
        # Default
        logger.info(f"Neutral market: Default TP")
        return Decimal("1.5")
    
    def _build_recommendation(
        self,
        rsi: float,
        macd_hist: float,
        trend: str,
        tp_percent: Decimal
    ) -> str:
        """Build human-readable recommendation."""
        parts = []
        
        if rsi > self.rsi_overbought:
            parts.append(f"⚠️ RSI {rsi:.1f} OVERBOUGHT")
        elif rsi > self.rsi_high:
            parts.append(f"🔶 RSI {rsi:.1f} High")
        elif rsi < self.rsi_oversold:
            parts.append(f"🟢 RSI {rsi:.1f} OVERSOLD")
        elif rsi < self.rsi_low:
            parts.append(f"🔵 RSI {rsi:.1f} Low")
        else:
            parts.append(f"RSI {rsi:.1f} Neutral")
        
        if macd_hist > 0:
            parts.append("MACD+ Bullish")
        else:
            parts.append("MACD- Bearish")
        
        parts.append(f"Trend: {trend}")
        parts.append(f"→ TP: {tp_percent}%")
        
        return " | ".join(parts)


# Singleton instance
analyzer = IndicatorAnalyzer()


async def get_smart_tp(candles: list[dict] = None, market_analysis: "MarketAnalysis" = None) -> Decimal:
    """
    Quick helper to get smart TP recommendation.

    Prefers using cached market_analysis if available to avoid duplicate calculations.

    Args:
        candles: Candle data from API (fallback)
        market_analysis: Pre-calculated MarketAnalysis from StrategyManager (preferred)

    Returns:
        Recommended TP percentage
    """
    signal = None

    # Prefer cached analysis
    if market_analysis is not None:
        signal = analyzer.from_market_analysis(market_analysis)
        if signal:
            logger.info(f"Smart TP (cached): {signal.recommendation}")
            return signal.tp_percent

    # Fallback to calculating from candles
    if candles:
        signal = analyzer.calculate_indicators(candles)
        if signal:
            logger.info(f"Smart TP (calculated): {signal.recommendation}")
            return signal.tp_percent

    logger.warning("Could not calculate indicators, using default TP")
    return Decimal("1.5")
