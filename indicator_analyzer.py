"""
Indicator Analyzer Module
=========================
Technical analysis indicators for smart trading decisions.

This module can use pre-calculated indicators from StrategyManager
to avoid duplicate calculations and API calls.

Uses 'ta' library for calculating RSI, MACD, ATR, and other indicators
to determine optimal Take-Profit levels.

Includes manual implementations of:
- SuperTrend: Trailing stop-based TP (ATR-adaptive)
- StochRSI: Faster overbought/oversold detection
"""

import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Optional, Literal, TYPE_CHECKING

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator
from ta.volatility import AverageTrueRange

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


@dataclass
class SuperTrendResult:
    """
    SuperTrend indicator result for trailing TP.

    SuperTrend is an ATR-based trend-following indicator that provides:
    - Trend direction (bullish/bearish)
    - Dynamic trailing stop levels that adapt to volatility
    """
    trend_line: float          # Current SuperTrend line value
    direction: int             # 1 = bullish, -1 = bearish
    long_stop: float           # Trailing stop for LONG positions
    short_stop: float          # Trailing stop for SHORT positions
    atr_value: float           # Current ATR value (for reference)

    @property
    def is_bullish(self) -> bool:
        return self.direction == 1

    @property
    def is_bearish(self) -> bool:
        return self.direction == -1


@dataclass
class StochRSIResult:
    """
    Stochastic RSI result for faster momentum detection.

    StochRSI applies Stochastic formula to RSI values, creating
    a faster oscillator that reaches extremes more often.
    """
    k_line: float              # Fast line (0-100)
    d_line: float              # Signal line (0-100, smoothed K)

    @property
    def is_oversold(self) -> bool:
        """K < 20 indicates oversold condition."""
        return self.k_line < 20

    @property
    def is_overbought(self) -> bool:
        """K > 80 indicates overbought condition."""
        return self.k_line > 80

    @property
    def bullish_crossover(self) -> bool:
        """K crossing above D in oversold zone = buy signal."""
        return self.k_line > self.d_line and self.k_line < 30

    @property
    def bearish_crossover(self) -> bool:
        """K crossing below D in overbought zone = sell signal."""
        return self.k_line < self.d_line and self.k_line > 70


@dataclass
class TrailingTPResult:
    """
    Combined result for SuperTrend-based trailing TP decision.
    """
    use_trailing: bool          # Whether to use trailing TP
    trailing_stop: Decimal      # The trailing stop price
    fixed_tp: Decimal           # Fallback fixed TP price
    supertrend: Optional[SuperTrendResult]
    reason: str                 # Explanation for the decision


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

        # SuperTrend parameters (configurable via config.py)
        self.supertrend_length = 10
        self.supertrend_multiplier = 3.0

        # StochRSI parameters
        self.stochrsi_length = 14
        self.stochrsi_rsi_length = 14
        self.stochrsi_k = 3
        self.stochrsi_d = 3

    def calculate_supertrend(
        self,
        candles: list[dict],
        length: int = None,
        multiplier: float = None
    ) -> Optional[SuperTrendResult]:
        """
        Calculate SuperTrend indicator from candle data.

        SuperTrend uses ATR to create dynamic support/resistance levels
        that trail the price during a trend.

        Manual implementation using ta library for ATR calculation.

        Args:
            candles: List of candle dicts with OHLCV data
            length: ATR period (default: 10)
            multiplier: ATR multiplier (default: 3.0)

        Returns:
            SuperTrendResult with trend direction and stop levels
        """
        try:
            length = length or self.supertrend_length
            multiplier = multiplier or self.supertrend_multiplier

            if len(candles) < length + 10:
                logger.warning(f"Not enough candles for SuperTrend: {len(candles)}")
                return None

            # Convert to DataFrame
            df = self._candles_to_dataframe(candles)
            if df is None:
                return None

            # Calculate ATR using ta library
            atr_indicator = AverageTrueRange(
                high=df['high'],
                low=df['low'],
                close=df['close'],
                window=length
            )
            df['atr'] = atr_indicator.average_true_range()

            # Calculate HL2 (midpoint of high and low)
            df['hl2'] = (df['high'] + df['low']) / 2

            # Calculate basic upper and lower bands
            df['basic_upper'] = df['hl2'] + (multiplier * df['atr'])
            df['basic_lower'] = df['hl2'] - (multiplier * df['atr'])

            # Initialize SuperTrend columns
            df['final_upper'] = df['basic_upper']
            df['final_lower'] = df['basic_lower']
            df['supertrend'] = np.nan
            df['direction'] = 1  # 1 = bullish, -1 = bearish

            # Calculate SuperTrend with proper logic
            for i in range(length, len(df)):
                # Final upper band: lower of current basic_upper and previous final_upper
                # (only if previous close was above previous final_upper)
                if df['close'].iloc[i-1] > df['final_upper'].iloc[i-1]:
                    df.loc[df.index[i], 'final_upper'] = df['basic_upper'].iloc[i]
                else:
                    df.loc[df.index[i], 'final_upper'] = min(
                        df['basic_upper'].iloc[i],
                        df['final_upper'].iloc[i-1]
                    )

                # Final lower band: higher of current basic_lower and previous final_lower
                # (only if previous close was below previous final_lower)
                if df['close'].iloc[i-1] < df['final_lower'].iloc[i-1]:
                    df.loc[df.index[i], 'final_lower'] = df['basic_lower'].iloc[i]
                else:
                    df.loc[df.index[i], 'final_lower'] = max(
                        df['basic_lower'].iloc[i],
                        df['final_lower'].iloc[i-1]
                    )

                # Determine direction
                if i == length:
                    # First calculation - use simple logic
                    if df['close'].iloc[i] > df['final_upper'].iloc[i]:
                        df.loc[df.index[i], 'direction'] = 1
                    else:
                        df.loc[df.index[i], 'direction'] = -1
                else:
                    prev_dir = df['direction'].iloc[i-1]
                    prev_st = df['supertrend'].iloc[i-1]

                    if prev_dir == 1:
                        # Was bullish
                        if df['close'].iloc[i] < df['final_lower'].iloc[i]:
                            df.loc[df.index[i], 'direction'] = -1  # Flip to bearish
                        else:
                            df.loc[df.index[i], 'direction'] = 1  # Stay bullish
                    else:
                        # Was bearish
                        if df['close'].iloc[i] > df['final_upper'].iloc[i]:
                            df.loc[df.index[i], 'direction'] = 1  # Flip to bullish
                        else:
                            df.loc[df.index[i], 'direction'] = -1  # Stay bearish

                # Set SuperTrend value based on direction
                if df['direction'].iloc[i] == 1:
                    df.loc[df.index[i], 'supertrend'] = df['final_lower'].iloc[i]
                else:
                    df.loc[df.index[i], 'supertrend'] = df['final_upper'].iloc[i]

            # Get latest values
            latest = df.iloc[-1]

            result = SuperTrendResult(
                trend_line=float(latest['supertrend']) if pd.notna(latest['supertrend']) else 0.0,
                direction=int(latest['direction']),
                long_stop=float(latest['final_lower']) if pd.notna(latest['final_lower']) else 0.0,
                short_stop=float(latest['final_upper']) if pd.notna(latest['final_upper']) else float('inf'),
                atr_value=float(latest['atr']) if pd.notna(latest['atr']) else 0.0
            )

            logger.debug(
                f"SuperTrend: direction={result.direction}, "
                f"long_stop={result.long_stop:.4f}, short_stop={result.short_stop:.4f}"
            )

            return result

        except Exception as e:
            logger.error(f"Error calculating SuperTrend: {e}")
            return None

    def calculate_stochrsi(
        self,
        candles: list[dict],
        length: int = None,
        rsi_length: int = None,
        k: int = None,
        d: int = None
    ) -> Optional[StochRSIResult]:
        """
        Calculate Stochastic RSI indicator.

        StochRSI is more sensitive than regular RSI, reaching
        overbought/oversold levels more frequently.

        Manual implementation using ta library for RSI calculation.

        Formula:
        1. Calculate RSI
        2. StochRSI = (RSI - min(RSI, length)) / (max(RSI, length) - min(RSI, length))
        3. K = SMA(StochRSI, k)
        4. D = SMA(K, d)

        Args:
            candles: List of candle dicts with OHLCV data
            length: Stochastic period (default: 14)
            rsi_length: RSI period (default: 14)
            k: K smoothing period (default: 3)
            d: D smoothing period (default: 3)

        Returns:
            StochRSIResult with K and D line values (0-100 scale)
        """
        try:
            length = length or self.stochrsi_length
            rsi_length = rsi_length or self.stochrsi_rsi_length
            k_period = k or self.stochrsi_k
            d_period = d or self.stochrsi_d

            min_candles = max(length, rsi_length) + 20
            if len(candles) < min_candles:
                logger.warning(f"Not enough candles for StochRSI: {len(candles)}")
                return None

            # Convert to DataFrame
            df = self._candles_to_dataframe(candles)
            if df is None:
                return None

            # Step 1: Calculate RSI
            rsi_indicator = RSIIndicator(close=df['close'], window=rsi_length)
            df['rsi'] = rsi_indicator.rsi()

            # Step 2: Calculate Stochastic RSI
            # StochRSI = (RSI - min(RSI)) / (max(RSI) - min(RSI))
            df['rsi_min'] = df['rsi'].rolling(window=length).min()
            df['rsi_max'] = df['rsi'].rolling(window=length).max()

            # Avoid division by zero
            df['rsi_range'] = df['rsi_max'] - df['rsi_min']
            df['stochrsi'] = np.where(
                df['rsi_range'] > 0,
                (df['rsi'] - df['rsi_min']) / df['rsi_range'],
                0.5  # Default to middle if no range
            )

            # Step 3: Calculate K line (smoothed StochRSI)
            df['k_line'] = df['stochrsi'].rolling(window=k_period).mean()

            # Step 4: Calculate D line (smoothed K)
            df['d_line'] = df['k_line'].rolling(window=d_period).mean()

            # Get latest values (convert to 0-100 scale)
            latest = df.iloc[-1]

            k_value = float(latest['k_line']) * 100 if pd.notna(latest['k_line']) else 50.0
            d_value = float(latest['d_line']) * 100 if pd.notna(latest['d_line']) else 50.0

            result = StochRSIResult(
                k_line=k_value,
                d_line=d_value
            )

            logger.debug(f"StochRSI: K={result.k_line:.1f}, D={result.d_line:.1f}")

            return result

        except Exception as e:
            logger.error(f"Error calculating StochRSI: {e}")
            return None

    def get_trailing_tp(
        self,
        candles: list[dict],
        entry_price: Decimal,
        position_side: Literal["LONG", "SHORT"],
        fallback_tp_percent: Decimal = Decimal("1.5")
    ) -> TrailingTPResult:
        """
        Get trailing TP recommendation based on SuperTrend.

        Logic:
        - LONG: Use SuperTrend long_stop if it's above entry price (profitable)
        - SHORT: Use SuperTrend short_stop if it's below entry price (profitable)
        - Fallback to fixed TP% if SuperTrend stop is not yet profitable

        Args:
            candles: Candle data for indicator calculation
            entry_price: Position entry price
            position_side: "LONG" or "SHORT"
            fallback_tp_percent: Fallback TP% if trailing not applicable

        Returns:
            TrailingTPResult with trailing stop or fixed TP
        """
        # Calculate fixed TP as fallback
        if position_side == "LONG":
            fixed_tp = entry_price * (1 + fallback_tp_percent / 100)
        else:
            fixed_tp = entry_price * (1 - fallback_tp_percent / 100)

        # Try to calculate SuperTrend
        supertrend = self.calculate_supertrend(candles)

        if supertrend is None:
            return TrailingTPResult(
                use_trailing=False,
                trailing_stop=Decimal("0"),
                fixed_tp=fixed_tp,
                supertrend=None,
                reason="SuperTrend calculation failed, using fixed TP"
            )

        # Determine if SuperTrend stop is profitable
        if position_side == "LONG":
            st_stop = Decimal(str(supertrend.long_stop))
            is_profitable = st_stop > entry_price

            if is_profitable:
                return TrailingTPResult(
                    use_trailing=True,
                    trailing_stop=st_stop,
                    fixed_tp=fixed_tp,
                    supertrend=supertrend,
                    reason=f"SuperTrend trailing stop above entry ({st_stop:.4f} > {entry_price:.4f})"
                )
            else:
                return TrailingTPResult(
                    use_trailing=False,
                    trailing_stop=st_stop,
                    fixed_tp=fixed_tp,
                    supertrend=supertrend,
                    reason=f"SuperTrend stop below entry, using fixed TP until profit"
                )

        else:  # SHORT
            st_stop = Decimal(str(supertrend.short_stop))
            is_profitable = st_stop < entry_price

            if is_profitable:
                return TrailingTPResult(
                    use_trailing=True,
                    trailing_stop=st_stop,
                    fixed_tp=fixed_tp,
                    supertrend=supertrend,
                    reason=f"SuperTrend trailing stop below entry ({st_stop:.4f} < {entry_price:.4f})"
                )
            else:
                return TrailingTPResult(
                    use_trailing=False,
                    trailing_stop=st_stop,
                    fixed_tp=fixed_tp,
                    supertrend=supertrend,
                    reason=f"SuperTrend stop above entry, using fixed TP until profit"
                )

    def _candles_to_dataframe(self, candles: list[dict]) -> Optional[pd.DataFrame]:
        """Convert candle list to pandas DataFrame with proper column names."""
        try:
            # Aster API returns candles as arrays:
            # [timestamp, open, high, low, close, volume, ...]
            df = pd.DataFrame(candles, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_volume',
                'taker_buy_quote_volume', 'ignore'
            ])

            # Ensure numeric types for OHLCV
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            return df

        except Exception as e:
            logger.error(f"Error converting candles to DataFrame: {e}")
            return None

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
