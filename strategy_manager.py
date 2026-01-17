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
from indicator_analyzer import IndicatorAnalyzer

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


@dataclass
class FastTrendConfirmation:
    """
    Point-based trend confirmation for faster side switching.

    Instead of waiting for 2 consecutive checks (30+ min), this system
    accumulates points based on multiple signal strengths. Strong signals
    can trigger a switch in as little as 5-10 minutes.

    Point Sources:
    - Trend Score >= 3: +2 points
    - Trend Score == 2: +1 point
    - StochRSI < 20 (for LONG) or > 80 (for SHORT): +1 point
    - Volume > 1.3x average: +1 point

    Threshold: 4 points to trigger switch
    Decay: -1 point per check when signal is unclear
    """
    accumulated_points: int = 0
    pending_direction: str | None = None  # "LONG" or "SHORT"
    last_check_time: datetime | None = None
    points_history: list[int] = None  # Track point changes for debugging

    def __post_init__(self):
        if self.points_history is None:
            self.points_history = []

    def reset(self):
        """Reset confirmation state."""
        self.accumulated_points = 0
        self.pending_direction = None
        self.points_history = []

    def add_check(
        self,
        trend_score: int,
        recommended_side: str,
        stochrsi_k: float | None,
        volume_ratio: float,
        current_side: str
    ) -> str | None:
        """
        Process a confirmation check and return new side if switch should happen.

        Args:
            trend_score: Current trend score (-4 to +4)
            recommended_side: "LONG", "SHORT", "STAY", or "PAUSE"
            stochrsi_k: StochRSI K value (0-100) or None if not calculated
            volume_ratio: Current volume / average volume
            current_side: Current grid side

        Returns:
            New side to switch to, or None if no switch needed
        """
        points = 0
        direction = None

        # If signal is unclear, decay points
        if recommended_side in ("STAY", "PAUSE"):
            decay = config.grid.POINT_DECAY_RATE
            self.accumulated_points = max(0, self.accumulated_points - decay)
            self.points_history.append(-decay)
            return None

        # Already on recommended side
        if recommended_side == current_side:
            self.reset()
            return None

        direction = recommended_side

        # Calculate points from trend score
        if abs(trend_score) >= 3:
            points += config.grid.STRONG_SIGNAL_POINTS
        elif abs(trend_score) >= 2:
            points += config.grid.MODERATE_SIGNAL_POINTS

        # StochRSI bonus
        if stochrsi_k is not None:
            if direction == "LONG" and stochrsi_k < config.grid.STOCHRSI_BONUS_LOW:
                points += 1
            elif direction == "SHORT" and stochrsi_k > config.grid.STOCHRSI_BONUS_HIGH:
                points += 1

        # Volume bonus
        if volume_ratio > config.grid.VOLUME_BONUS_THRESHOLD:
            points += 1

        # Accumulate or reset based on direction consistency
        if direction == self.pending_direction:
            self.accumulated_points += points
        else:
            # Direction changed - start fresh
            self.pending_direction = direction
            self.accumulated_points = points

        self.points_history.append(points)
        self.last_check_time = datetime.now()

        # Check if threshold reached
        if self.accumulated_points >= config.grid.SWITCH_THRESHOLD_POINTS:
            if direction != current_side:
                result = direction
                self.reset()
                return result

        return None

    def get_status(self) -> str:
        """Get human-readable status."""
        if self.pending_direction is None:
            return "No pending signal"
        return (
            f"Pending: {self.pending_direction} | "
            f"Points: {self.accumulated_points}/{config.grid.SWITCH_THRESHOLD_POINTS}"
        )


class StrategyManager:
    """
    Quantitative analysis engine for the grid bot.

    Enhanced features:
    - Telegram notifications when market state changes
    - Auto-pause on EXTREME_VOLATILITY
    - Auto Switch Side based on multi-indicator trend scoring
    - 15-min check interval for faster response
    - Real-time price spike detection via WebSocket
    - Funding rate monitoring with alerts
    - BTC correlation analysis (leading indicator)
    - Liquidity monitoring (spread and depth)
    - Position size coordination (exposure limits)
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

        # Fast Trend Confirmation (point-based system)
        self.fast_trend_confirmation = FastTrendConfirmation()
        self.indicator_analyzer = IndicatorAnalyzer()
        self.last_stochrsi_k: float | None = None  # Cached StochRSI value
        
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

        # Trend signal alert cooldown (prevents repeated alerts during oscillation)
        self.last_trend_signal_alert: datetime | None = None
        self.last_trend_signal_direction: str | None = None
        self.trend_signal_alert_cooldown = 900  # 15 minutes between same-direction alerts
        self._price_monitor_task: asyncio.Task | None = None

        # BTC Correlation Analysis
        self.btc_symbol = "BTCUSDT"
        self.last_btc_analysis: MarketAnalysis | None = None
        self.btc_trend_score: TrendScore | None = None
        self.btc_rsi_danger_high = 70  # BTC overbought - danger for LONG
        self.btc_rsi_danger_low = 30   # BTC oversold - danger for SHORT
        self.btc_divergence_alert_sent = False

        # Liquidity Crisis Detection
        self.spread_warning_threshold = Decimal("0.003")  # 0.3% spread = warning
        self.spread_danger_threshold = Decimal("0.005")   # 0.5% spread = danger
        self.min_depth_usdt = Decimal("5000")  # Minimum $5000 depth on each side
        self.last_spread: Decimal = Decimal("0")
        self.last_bid_depth: Decimal = Decimal("0")
        self.last_ask_depth: Decimal = Decimal("0")
        self.liquidity_warning_sent = False

        # Max Position Size Coordination
        self.position_warning_threshold = Decimal("0.7")  # 70% of max = warning
        self.position_danger_threshold = Decimal("0.9")   # 90% of max = danger
        self.last_position_size: Decimal = Decimal("0")
        self.last_position_value: Decimal = Decimal("0")
        self.max_position_alert_sent = False
        self.exposure_mismatch_alerted = False

        # ==========================================================================
        # Intelligent Drawdown Management
        # ==========================================================================
        self.drawdown_state: Literal["NORMAL", "PAUSED", "PARTIAL_CUT", "FULL_CUT", "WAITING_REENTRY"] = "NORMAL"
        self.last_position_entry_price: Decimal = Decimal("0")
        self.last_position_drawdown: Decimal = Decimal("0")
        self.partial_cut_executed: bool = False
        self.full_cut_executed: bool = False
        self.cut_loss_time: datetime | None = None
        self.daily_loss_usdt: Decimal = Decimal("0")
        self.daily_loss_reset_time: datetime = datetime.now()
        self.reentry_check_count: int = 0

    async def start_monitoring(self):
        """Start the background monitoring loop."""
        self.is_running = True
        logger.info("Strategy Manager started - monitoring market conditions...")

        # Start real-time price monitoring in background
        self._price_monitor_task = asyncio.create_task(self._monitor_price_spikes())

        while self.is_running:
            try:
                # Analyze main trading symbol (SOL)
                analysis = await self.analyze_market()
                await self.evaluate_safety(analysis)

                # Check funding rate
                await self._check_funding_rate()

                # Check BTC correlation (leading indicator)
                await self._check_btc_correlation()

                # Check liquidity (spread and depth)
                await self._check_liquidity()

                # Check position size coordination
                await self._check_position_size()

                # Check drawdown and execute protection actions
                await self._check_drawdown()

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

    async def _check_btc_correlation(self) -> None:
        """
        Analyze BTC as a leading indicator for altcoin movements.

        BTC typically leads the crypto market:
        - If BTC is bearish while we're LONG on altcoin → danger
        - If BTC is overbought (RSI > 70) → potential reversal
        - If BTC breaks key support/resistance → altcoins follow

        This method compares BTC trend with current grid side and alerts
        if there's a dangerous divergence.
        """
        try:
            # Analyze BTC market
            btc_analysis = await self.analyze_market(symbol=self.btc_symbol)
            self.last_btc_analysis = btc_analysis

            # Calculate BTC trend score
            btc_score = self._calculate_trend_score(
                btc_analysis.ema_fast,
                btc_analysis.ema_slow,
                btc_analysis.macd_histogram,
                btc_analysis.rsi,
                btc_analysis.volume_ratio
            )
            self.btc_trend_score = btc_score

            # Get current grid side and SOL trend
            grid_side = config.grid.GRID_SIDE
            sol_score = self.current_trend_score.total if self.current_trend_score else 0

            logger.info(
                f"BTC Correlation Check: BTC={btc_score.total:+d} RSI={btc_analysis.rsi:.1f} | "
                f"SOL={sol_score:+d} | Grid={grid_side}"
            )

            # Check for dangerous divergences
            await self._evaluate_btc_divergence(btc_analysis, btc_score, grid_side)

        except Exception as e:
            logger.error(f"Error checking BTC correlation: {e}")

    async def _evaluate_btc_divergence(
        self,
        btc_analysis: "MarketAnalysis",
        btc_score: TrendScore,
        grid_side: str
    ) -> None:
        """
        Evaluate if BTC signals danger for current grid position.

        Danger scenarios:
        1. LONG grid + BTC strongly bearish (score <= -2)
        2. LONG grid + BTC RSI > 70 (overbought, reversal likely)
        3. SHORT grid + BTC strongly bullish (score >= +2)
        4. SHORT grid + BTC RSI < 30 (oversold, bounce likely)
        """
        btc_rsi = btc_analysis.rsi
        btc_total = btc_score.total
        danger_detected = False
        danger_reason = ""
        severity = "WARNING"  # or "CRITICAL"

        if grid_side == "LONG":
            # Danger for LONG positions
            if btc_total <= -2:
                danger_detected = True
                danger_reason = f"BTC strongly BEARISH (score: {btc_total:+d})"
                severity = "CRITICAL" if btc_total <= -3 else "WARNING"

            elif btc_rsi >= self.btc_rsi_danger_high:
                danger_detected = True
                danger_reason = f"BTC OVERBOUGHT (RSI: {btc_rsi:.1f})"
                severity = "WARNING"

            elif btc_analysis.macd_histogram < 0 and btc_rsi > 65:
                danger_detected = True
                danger_reason = f"BTC showing reversal signals (MACD-, RSI={btc_rsi:.1f})"
                severity = "WARNING"

        elif grid_side == "SHORT":
            # Danger for SHORT positions
            if btc_total >= 2:
                danger_detected = True
                danger_reason = f"BTC strongly BULLISH (score: {btc_total:+d})"
                severity = "CRITICAL" if btc_total >= 3 else "WARNING"

            elif btc_rsi <= self.btc_rsi_danger_low:
                danger_detected = True
                danger_reason = f"BTC OVERSOLD (RSI: {btc_rsi:.1f})"
                severity = "WARNING"

        # Send alert if danger detected (with cooldown)
        if danger_detected:
            # Avoid spam - only alert once per divergence
            if not self.btc_divergence_alert_sent:
                self.btc_divergence_alert_sent = True

                icon = "🚨" if severity == "CRITICAL" else "⚠️"
                sol_price = self.last_analysis.current_price if self.last_analysis else Decimal("0")
                btc_price = btc_analysis.current_price

                await self.telegram.send_message(
                    f"{icon} BTC Correlation Alert!\n\n"
                    f"Your Grid: {grid_side}\n"
                    f"Danger: {danger_reason}\n\n"
                    f"BTC Analysis:\n"
                    f"  Price: ${btc_price:,.0f}\n"
                    f"  RSI: {btc_rsi:.1f}\n"
                    f"  Trend Score: {btc_total:+d}\n"
                    f"  {btc_score}\n\n"
                    f"SOL Price: ${sol_price:.2f}\n\n"
                    f"{'🔴 Consider reducing exposure!' if severity == 'CRITICAL' else '⚠️ Monitor closely!'}"
                )

                logger.warning(f"BTC Divergence Alert: {danger_reason}")
        else:
            # Reset alert flag when divergence clears
            if self.btc_divergence_alert_sent:
                self.btc_divergence_alert_sent = False
                logger.info("BTC correlation normalized - divergence alert cleared")

    async def _check_liquidity(self) -> None:
        """
        Check order book liquidity and spread.

        Liquidity crisis indicators:
        - Wide spread (> 0.3% warning, > 0.5% danger)
        - Low depth (< $5000 on bid or ask side)

        Low liquidity means:
        - Orders may not fill at expected prices
        - Slippage on market orders
        - Difficulty exiting positions in panic
        """
        try:
            symbol = config.trading.SYMBOL

            # Get order book depth
            depth = await self.client.get_depth(symbol=symbol, limit=20)

            if not depth or "bids" not in depth or "asks" not in depth:
                logger.warning("Could not fetch order book depth")
                return

            bids = depth.get("bids", [])
            asks = depth.get("asks", [])

            if not bids or not asks:
                logger.warning("Empty order book")
                return

            # Calculate spread
            best_bid = Decimal(bids[0][0])
            best_ask = Decimal(asks[0][0])
            mid_price = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / mid_price
            self.last_spread = spread

            # Calculate depth (total value in USDT for top 20 levels)
            bid_depth = sum(Decimal(b[0]) * Decimal(b[1]) for b in bids)
            ask_depth = sum(Decimal(a[0]) * Decimal(a[1]) for a in asks)
            self.last_bid_depth = bid_depth
            self.last_ask_depth = ask_depth

            spread_percent = float(spread * 100)
            logger.info(
                f"Liquidity Check: Spread={spread_percent:.3f}% | "
                f"Bid Depth=${bid_depth:,.0f} | Ask Depth=${ask_depth:,.0f}"
            )

            # Evaluate liquidity conditions
            await self._evaluate_liquidity(spread, bid_depth, ask_depth, mid_price)

        except Exception as e:
            logger.error(f"Error checking liquidity: {e}")

    async def _evaluate_liquidity(
        self,
        spread: Decimal,
        bid_depth: Decimal,
        ask_depth: Decimal,
        mid_price: Decimal
    ) -> None:
        """
        Evaluate liquidity conditions and alert if dangerous.

        Args:
            spread: Current bid-ask spread as decimal (e.g., 0.003 = 0.3%)
            bid_depth: Total bid depth in USDT
            ask_depth: Total ask depth in USDT
            mid_price: Current mid price
        """
        issues = []
        severity = "WARNING"
        grid_side = config.grid.GRID_SIDE

        # Check spread
        if spread >= self.spread_danger_threshold:
            issues.append(f"WIDE SPREAD: {float(spread * 100):.2f}%")
            severity = "CRITICAL"
        elif spread >= self.spread_warning_threshold:
            issues.append(f"High spread: {float(spread * 100):.2f}%")

        # Check depth based on grid side
        if grid_side == "LONG":
            # For LONG grid, we need good ask depth (to buy) and bid depth (to sell/TP)
            if ask_depth < self.min_depth_usdt:
                issues.append(f"LOW ASK DEPTH: ${ask_depth:,.0f}")
                severity = "CRITICAL"
            if bid_depth < self.min_depth_usdt:
                issues.append(f"Low bid depth: ${bid_depth:,.0f} (TP may slip)")
        else:
            # For SHORT grid, we need good bid depth (to sell) and ask depth (to buy/TP)
            if bid_depth < self.min_depth_usdt:
                issues.append(f"LOW BID DEPTH: ${bid_depth:,.0f}")
                severity = "CRITICAL"
            if ask_depth < self.min_depth_usdt:
                issues.append(f"Low ask depth: ${ask_depth:,.0f} (TP may slip)")

        # Send alert if issues detected
        if issues:
            if not self.liquidity_warning_sent:
                self.liquidity_warning_sent = True

                icon = "🚨" if severity == "CRITICAL" else "⚠️"
                issues_text = "\n".join(f"  • {issue}" for issue in issues)

                await self.telegram.send_message(
                    f"{icon} Liquidity Warning!\n\n"
                    f"Symbol: {config.trading.SYMBOL}\n"
                    f"Mid Price: ${mid_price:.2f}\n\n"
                    f"Issues:\n{issues_text}\n\n"
                    f"Grid: {grid_side}\n\n"
                    f"{'🔴 Consider pausing trading!' if severity == 'CRITICAL' else '⚠️ Monitor order fills closely!'}"
                )

                logger.warning(f"Liquidity warning: {issues}")

                # Auto-pause on critical liquidity crisis
                if severity == "CRITICAL" and self.bot and hasattr(self.bot, 'pause'):
                    logger.critical("CRITICAL liquidity crisis - auto-pausing bot!")
                    await self.telegram.send_message(
                        "🚨 AUTO-PAUSE TRIGGERED\n\n"
                        "Bot paused due to critical liquidity conditions.\n"
                        "Manual intervention required."
                    )
                    await self.bot.pause()
        else:
            # Reset warning flag when liquidity normalizes
            if self.liquidity_warning_sent:
                self.liquidity_warning_sent = False
                logger.info("Liquidity normalized - warning cleared")

                await self.telegram.send_message(
                    "✅ Liquidity Normalized\n\n"
                    f"Spread: {float(spread * 100):.3f}%\n"
                    f"Bid Depth: ${bid_depth:,.0f}\n"
                    f"Ask Depth: ${ask_depth:,.0f}\n\n"
                    "Trading conditions improved."
                )

    async def _check_position_size(self) -> None:
        """
        Check current position size against max limits.

        Monitors:
        1. Current position vs MAX_POSITION_PERCENT of balance
        2. Grid configuration vs actual exposure potential
        3. Warns if approaching limits

        This prevents over-exposure scenarios where all grid levels
        filling at once would exceed safe position limits.
        """
        try:
            symbol = config.trading.SYMBOL

            # Get current balance
            balances = await self.client.get_account_balance()
            available_balance = Decimal("0")
            for bal in balances:
                if bal.get("asset") == config.trading.MARGIN_ASSET:
                    available_balance = Decimal(bal.get("availableBalance", "0"))
                    break

            if available_balance <= 0:
                return

            # Get current position
            positions = await self.client.get_position_risk(symbol)
            current_position_size = Decimal("0")
            current_position_value = Decimal("0")
            entry_price = Decimal("0")

            for pos in positions:
                if pos.get("symbol") == symbol:
                    pos_amt = abs(Decimal(pos.get("positionAmt", "0")))
                    if pos_amt > 0:
                        current_position_size = pos_amt
                        entry_price = Decimal(pos.get("entryPrice", "0"))
                        current_position_value = pos_amt * entry_price
                        break

            self.last_position_size = current_position_size
            self.last_position_value = current_position_value

            # Calculate max allowed position value
            max_position_percent = config.risk.MAX_POSITION_PERCENT / 100
            leverage = Decimal(config.trading.LEVERAGE)
            max_position_value = available_balance * max_position_percent * leverage

            # Calculate potential exposure from grid config
            grid_count = config.grid.GRID_COUNT
            qty_per_grid = config.grid.QUANTITY_PER_GRID_USDT
            max_positions = config.risk.MAX_POSITIONS
            potential_exposure = qty_per_grid * min(grid_count, max_positions) * leverage

            # Check for config mismatch (one-time alert)
            if potential_exposure > max_position_value and not self.exposure_mismatch_alerted:
                self.exposure_mismatch_alerted = True
                await self.telegram.send_message(
                    f"⚠️ Grid Exposure Mismatch!\n\n"
                    f"Grid Config:\n"
                    f"  • {grid_count} levels × ${qty_per_grid} × {leverage}x\n"
                    f"  • Potential: ${potential_exposure:,.0f}\n\n"
                    f"Balance Limit:\n"
                    f"  • Balance: ${available_balance:,.2f}\n"
                    f"  • Max {max_position_percent*100:.0f}%: ${max_position_value:,.0f}\n\n"
                    f"⚠️ Consider reducing QUANTITY_PER_GRID_USDT\n"
                    f"or GRID_COUNT to match limits."
                )
                logger.warning(
                    f"Grid exposure mismatch: potential ${potential_exposure:,.0f} > "
                    f"max ${max_position_value:,.0f}"
                )

            # Check current position vs limits
            if current_position_value > 0:
                usage_ratio = current_position_value / max_position_value

                logger.info(
                    f"Position Size Check: ${current_position_value:,.0f} / "
                    f"${max_position_value:,.0f} ({usage_ratio*100:.1f}%)"
                )

                await self._evaluate_position_usage(
                    current_position_value,
                    max_position_value,
                    usage_ratio,
                    current_position_size,
                    entry_price
                )

        except Exception as e:
            logger.error(f"Error checking position size: {e}")

    async def _evaluate_position_usage(
        self,
        current_value: Decimal,
        max_value: Decimal,
        usage_ratio: Decimal,
        position_size: Decimal,
        entry_price: Decimal
    ) -> None:
        """
        Evaluate position usage and alert if approaching limits.

        Args:
            current_value: Current position value in USDT
            max_value: Maximum allowed position value
            usage_ratio: Current / Max ratio
            position_size: Position size in base asset
            entry_price: Average entry price
        """
        grid_side = config.grid.GRID_SIDE
        severity = None

        if usage_ratio >= self.position_danger_threshold:
            severity = "CRITICAL"
        elif usage_ratio >= self.position_warning_threshold:
            severity = "WARNING"

        if severity:
            if not self.max_position_alert_sent:
                self.max_position_alert_sent = True

                icon = "🚨" if severity == "CRITICAL" else "⚠️"
                usage_percent = float(usage_ratio * 100)

                await self.telegram.send_message(
                    f"{icon} Position Size Alert!\n\n"
                    f"Grid: {grid_side}\n"
                    f"Symbol: {config.trading.SYMBOL}\n\n"
                    f"Current Position:\n"
                    f"  • Size: {position_size:.4f}\n"
                    f"  • Entry: ${entry_price:.2f}\n"
                    f"  • Value: ${current_value:,.0f}\n\n"
                    f"Limit:\n"
                    f"  • Max: ${max_value:,.0f}\n"
                    f"  • Usage: {usage_percent:.1f}%\n\n"
                    f"{'🔴 Near maximum! New orders may be blocked.' if severity == 'CRITICAL' else '⚠️ Approaching limit. Monitor closely.'}"
                )

                logger.warning(f"Position usage alert: {usage_percent:.1f}% of max")

                # If critical, could auto-reduce exposure
                if severity == "CRITICAL":
                    await self.telegram.send_message(
                        "💡 Suggestions:\n"
                        "• Wait for TP orders to fill\n"
                        "• Manually close some positions\n"
                        "• Reduce QUANTITY_PER_GRID_USDT\n"
                        "• Or pause new BUY orders"
                    )
        else:
            # Reset alert when usage drops
            if self.max_position_alert_sent:
                self.max_position_alert_sent = False
                usage_percent = float(usage_ratio * 100)
                logger.info(f"Position usage normalized: {usage_percent:.1f}%")

    # ==========================================================================
    # Intelligent Drawdown Management
    # ==========================================================================

    async def _check_drawdown(self) -> None:
        """
        Check position drawdown and execute protection actions.

        Drawdown Levels (Moderate Mode):
        - 15%: Pause new BUY orders (keep existing TP orders)
        - 20%: Partial cut 30% of position
        - 25%: Full cut loss (close all positions)

        After full cut: Wait for market stabilization then auto re-entry.
        """
        try:
            symbol = config.trading.SYMBOL

            # Reset daily loss if new day
            if datetime.now().date() > self.daily_loss_reset_time.date():
                self.daily_loss_usdt = Decimal("0")
                self.daily_loss_reset_time = datetime.now()
                logger.info("Daily loss counter reset")

            # Check if waiting for re-entry
            if self.drawdown_state == "WAITING_REENTRY":
                await self._check_reentry_conditions()
                return

            # Get current position
            positions = await self.client.get_position_risk(symbol)
            position_amt = Decimal("0")
            entry_price = Decimal("0")
            current_price = Decimal("0")
            unrealized_pnl = Decimal("0")

            for pos in positions:
                if pos.get("symbol") == symbol:
                    position_amt = Decimal(pos.get("positionAmt", "0"))
                    entry_price = Decimal(pos.get("entryPrice", "0"))
                    unrealized_pnl = Decimal(pos.get("unRealizedProfit", "0"))
                    break

            # Get current price
            ticker = await self.client.get_ticker_price(symbol)
            current_price = Decimal(ticker.get("price", "0"))

            # No position = reset state
            if position_amt == 0 or entry_price == 0:
                if self.drawdown_state != "NORMAL" and self.drawdown_state != "WAITING_REENTRY":
                    self.drawdown_state = "NORMAL"
                    self.partial_cut_executed = False
                    self.full_cut_executed = False
                    logger.info("No position - drawdown state reset to NORMAL")
                return

            self.last_position_entry_price = entry_price

            # Calculate drawdown from entry price
            # For LONG: drawdown = (entry - current) / entry * 100
            if position_amt > 0:  # LONG position
                drawdown_percent = (entry_price - current_price) / entry_price * 100
            else:  # SHORT position
                drawdown_percent = (current_price - entry_price) / entry_price * 100

            # Only care about losses (positive drawdown)
            if drawdown_percent <= 0:
                drawdown_percent = Decimal("0")
                # Reset state if we're back in profit
                if self.drawdown_state == "PAUSED":
                    self.drawdown_state = "NORMAL"
                    self.partial_cut_executed = False
                    if self.bot:
                        await self.bot.resume()
                    await self.telegram.send_message(
                        "✅ Drawdown Recovered!\n\n"
                        f"Price back above entry.\n"
                        f"Entry: ${entry_price:.4f}\n"
                        f"Current: ${current_price:.4f}\n\n"
                        "BUY orders resumed."
                    )
                    logger.info("Drawdown recovered - resumed buying")

            self.last_position_drawdown = drawdown_percent

            logger.info(
                f"Drawdown Check: {drawdown_percent:.2f}% | "
                f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | "
                f"Position: {position_amt} | uPnL: ${unrealized_pnl:.2f}"
            )

            # Check balance guard
            balances = await self.client.get_account_balance()
            current_balance = Decimal("0")
            for bal in balances:
                if bal.get("asset") == config.trading.MARGIN_ASSET:
                    current_balance = Decimal(bal.get("balance", "0"))
                    break

            if current_balance < config.risk.MIN_BALANCE_GUARD:
                logger.critical(f"BALANCE GUARD TRIGGERED: ${current_balance:.2f} < ${config.risk.MIN_BALANCE_GUARD}")
                await self._execute_full_cut(position_amt, entry_price, current_price, "BALANCE_GUARD")
                return

            # Check daily loss limit
            if self.daily_loss_usdt >= config.risk.DAILY_LOSS_LIMIT_USDT:
                logger.warning(f"Daily loss limit reached: ${self.daily_loss_usdt:.2f}")
                if self.bot and not self.bot.is_paused:
                    await self.bot.pause()
                    await self.telegram.send_message(
                        f"⏸️ Daily Loss Limit Reached!\n\n"
                        f"Daily Loss: ${self.daily_loss_usdt:.2f}\n"
                        f"Limit: ${config.risk.DAILY_LOSS_LIMIT_USDT:.2f}\n\n"
                        "Bot paused for 24 hours."
                    )
                return

            # Execute actions based on drawdown level
            await self._execute_drawdown_actions(
                drawdown_percent,
                position_amt,
                entry_price,
                current_price,
                unrealized_pnl
            )

        except Exception as e:
            logger.error(f"Error checking drawdown: {e}")

    async def _execute_drawdown_actions(
        self,
        drawdown_percent: Decimal,
        position_amt: Decimal,
        entry_price: Decimal,
        current_price: Decimal,
        unrealized_pnl: Decimal
    ) -> None:
        """
        Execute protection actions based on drawdown level.

        Actions are cumulative:
        - 15%+: Pause BUY
        - 20%+: Pause BUY + Partial Cut 30%
        - 25%+: Pause BUY + Full Cut
        """
        pause_threshold = config.risk.DRAWDOWN_PAUSE_PERCENT
        partial_threshold = config.risk.DRAWDOWN_PARTIAL_CUT_PERCENT
        full_threshold = config.risk.DRAWDOWN_FULL_CUT_PERCENT

        # Level 3: Full Cut (25%+)
        if drawdown_percent >= full_threshold and not self.full_cut_executed:
            await self._execute_full_cut(position_amt, entry_price, current_price, "DRAWDOWN_25%")
            return

        # Level 2: Partial Cut (20%+)
        if drawdown_percent >= partial_threshold and not self.partial_cut_executed:
            await self._execute_partial_cut(position_amt, entry_price, current_price)

        # Level 1: Pause BUY (15%+)
        if drawdown_percent >= pause_threshold and self.drawdown_state == "NORMAL":
            await self._execute_pause_buying(drawdown_percent, entry_price, current_price)

    async def _execute_pause_buying(
        self,
        drawdown_percent: Decimal,
        entry_price: Decimal,
        current_price: Decimal
    ) -> None:
        """Pause new BUY orders while keeping existing TP orders."""
        self.drawdown_state = "PAUSED"

        if self.bot:
            # Cancel only BUY orders, keep TP (SELL) orders
            await self.bot.pause_buying()

        await self.telegram.send_message(
            f"⚠️ Drawdown Alert - Level 1\n\n"
            f"Drawdown: {drawdown_percent:.1f}%\n"
            f"Entry: ${entry_price:.4f}\n"
            f"Current: ${current_price:.4f}\n\n"
            f"Action: Paused new BUY orders\n"
            f"TP orders still active.\n\n"
            f"Next level at 20%: Partial cut 30%"
        )

        logger.warning(f"DRAWDOWN LEVEL 1: {drawdown_percent:.1f}% - Paused buying")

    async def _execute_partial_cut(
        self,
        position_amt: Decimal,
        entry_price: Decimal,
        current_price: Decimal
    ) -> None:
        """Cut 30% of position to reduce risk."""
        self.partial_cut_executed = True
        self.drawdown_state = "PARTIAL_CUT"

        cut_ratio = config.risk.PARTIAL_CUT_RATIO / 100
        cut_quantity = abs(position_amt) * cut_ratio

        # Round to appropriate precision
        cut_quantity = cut_quantity.quantize(Decimal("0.01"))

        if cut_quantity <= 0:
            logger.warning("Partial cut quantity too small, skipping")
            return

        try:
            # Determine sell side based on position direction
            side = "SELL" if position_amt > 0 else "BUY"

            # Execute market order to close partial position
            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side=side,
                order_type="MARKET",
                quantity=cut_quantity,
            )

            order_id = response.get("orderId")

            # Calculate realized loss
            loss_per_unit = abs(entry_price - current_price)
            realized_loss = loss_per_unit * cut_quantity
            self.daily_loss_usdt += realized_loss

            remaining = abs(position_amt) - cut_quantity

            await self.telegram.send_message(
                f"✂️ Drawdown Alert - Level 2\n\n"
                f"Action: Partial Cut 30%\n"
                f"Sold: {cut_quantity} @ ${current_price:.4f}\n"
                f"Realized Loss: -${realized_loss:.2f}\n\n"
                f"Remaining Position: {remaining:.4f}\n"
                f"Entry: ${entry_price:.4f}\n\n"
                f"Next level at 25%: Full cut\n"
                f"OrderID: {order_id}"
            )

            logger.warning(
                f"DRAWDOWN LEVEL 2: Partial cut executed | "
                f"Sold {cut_quantity} @ ${current_price:.4f} | "
                f"Loss: -${realized_loss:.2f}"
            )

        except Exception as e:
            logger.error(f"Failed to execute partial cut: {e}")
            await self.telegram.send_message(
                f"❌ Partial Cut Failed!\n\n"
                f"Error: {e}\n\n"
                f"Please manually reduce position."
            )

    async def _execute_full_cut(
        self,
        position_amt: Decimal,
        entry_price: Decimal,
        current_price: Decimal,
        reason: str
    ) -> None:
        """Close all positions to prevent further losses."""
        self.full_cut_executed = True
        self.drawdown_state = "WAITING_REENTRY"
        self.cut_loss_time = datetime.now()
        self.reentry_check_count = 0

        cut_quantity = abs(position_amt)

        if cut_quantity <= 0:
            logger.warning("No position to cut")
            return

        try:
            # Cancel all orders first
            if self.bot:
                await self.bot.cancel_all_orders()

            # Determine sell side based on position direction
            side = "SELL" if position_amt > 0 else "BUY"

            # Execute market order to close all
            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side=side,
                order_type="MARKET",
                quantity=cut_quantity,
            )

            order_id = response.get("orderId")

            # Calculate realized loss
            loss_per_unit = abs(entry_price - current_price)
            realized_loss = loss_per_unit * cut_quantity
            self.daily_loss_usdt += realized_loss

            await self.telegram.send_message(
                f"🛑 Drawdown Alert - Level 3\n\n"
                f"Reason: {reason}\n"
                f"Action: FULL CUT LOSS\n\n"
                f"Closed: {cut_quantity} @ ${current_price:.4f}\n"
                f"Entry was: ${entry_price:.4f}\n"
                f"Realized Loss: -${realized_loss:.2f}\n\n"
                f"Daily Loss Total: -${self.daily_loss_usdt:.2f}\n\n"
                f"🔄 Auto Re-entry enabled.\n"
                f"Waiting for market to stabilize...\n"
                f"OrderID: {order_id}"
            )

            logger.critical(
                f"DRAWDOWN LEVEL 3: Full cut executed | "
                f"Reason: {reason} | "
                f"Closed {cut_quantity} @ ${current_price:.4f} | "
                f"Loss: -${realized_loss:.2f}"
            )

            # Pause the bot
            if self.bot:
                await self.bot.pause()

        except Exception as e:
            logger.error(f"Failed to execute full cut: {e}")
            await self.telegram.send_message(
                f"❌ Full Cut Failed!\n\n"
                f"Error: {e}\n\n"
                f"URGENT: Please manually close all positions!"
            )

    async def _check_reentry_conditions(self) -> None:
        """
        Check if conditions are met for auto re-entry after full cut.

        Conditions:
        1. Minimum wait time passed
        2. RSI is oversold and bouncing
        3. BTC is not strongly bearish
        4. Price showing signs of reversal
        """
        if not config.risk.AUTO_REENTRY_ENABLED:
            return

        # Check minimum wait time
        if self.cut_loss_time:
            wait_minutes = (datetime.now() - self.cut_loss_time).total_seconds() / 60
            if wait_minutes < config.risk.REENTRY_MIN_WAIT_MINUTES:
                return

        self.reentry_check_count += 1

        # Get current analysis
        analysis = self.last_analysis
        if not analysis:
            return

        # Check RSI - looking for oversold bounce
        rsi = analysis.rsi
        rsi_threshold = float(config.risk.REENTRY_RSI_THRESHOLD)

        # We want RSI to have been below threshold and now rising
        rsi_condition = rsi < 40 and rsi > rsi_threshold  # Was oversold, now recovering

        # Check BTC correlation
        btc_ok = True
        if self.btc_trend_score:
            btc_ok = self.btc_trend_score.total > -2  # Not strongly bearish

        # Check trend - looking for reversal signs
        trend_ok = analysis.trend_score.total > -2  # Not strongly bearish

        logger.info(
            f"Re-entry Check #{self.reentry_check_count}: "
            f"RSI={rsi:.1f} (need <40 & >{rsi_threshold}) | "
            f"BTC OK: {btc_ok} | Trend OK: {trend_ok}"
        )

        # All conditions met
        if rsi_condition and btc_ok and trend_ok:
            await self._execute_reentry()
        elif self.reentry_check_count >= 12:  # After 3 hours (15min × 12)
            # Force check with relaxed conditions
            if rsi < 50 and btc_ok:
                await self._execute_reentry()

    async def _execute_reentry(self) -> None:
        """Execute auto re-entry with reduced position size."""
        self.drawdown_state = "NORMAL"
        self.partial_cut_executed = False
        self.full_cut_executed = False
        self.cut_loss_time = None
        self.reentry_check_count = 0

        # Get current price
        ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
        current_price = Decimal(ticker.get("price", "0"))

        # Calculate reduced position size
        size_ratio = config.risk.REENTRY_POSITION_SIZE_RATIO / 100

        await self.telegram.send_message(
            f"🔄 Auto Re-entry Triggered!\n\n"
            f"Market conditions improved:\n"
            f"• RSI showing recovery\n"
            f"• BTC not bearish\n"
            f"• Trend stabilizing\n\n"
            f"Re-entry Price: ${current_price:.4f}\n"
            f"Position Size: {size_ratio*100:.0f}% of normal\n\n"
            f"Resuming grid trading..."
        )

        logger.info(
            f"AUTO RE-ENTRY: Market stabilized | "
            f"Price: ${current_price:.4f} | "
            f"Size ratio: {size_ratio*100:.0f}%"
        )

        # Resume bot with reduced size
        if self.bot:
            # Temporarily reduce quantity per grid
            original_qty = config.grid.QUANTITY_PER_GRID_USDT
            config.grid.QUANTITY_PER_GRID_USDT = original_qty * size_ratio

            await self.bot.resume()

            # Schedule restoration of original size after successful trades
            # (This will be handled by gradual increase logic)

    async def analyze_market(self, symbol: str | None = None) -> MarketAnalysis:
        """
        Fetch data and perform technical analysis.
        """
        symbol = symbol or config.trading.SYMBOL
        logger.info(f"Analyzing market for {symbol}...")
        
        # 1. Fetch K-lines (Candles) - 15m interval for faster signal detection
        try:
            klines = await self.client.get_klines(
                symbol=symbol,
                interval="15m",
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

        Supports two modes:
        1. Legacy 2-Check: Wait for 2 consecutive confirmations (30+ min)
        2. Point-Based (NEW): Accumulate points, faster for strong signals

        Point-based system uses:
        - Trend score strength (+1 or +2 points)
        - StochRSI extreme levels (+1 point)
        - Volume confirmation (+1 point)
        - 4 points = switch
        """
        if not config.grid.AUTO_SWITCH_SIDE_ENABLED:
            return

        if self.current_trend_score is None:
            return

        recommended = self.current_trend_score.recommended_side
        current_side = config.grid.GRID_SIDE
        trend_score = self.current_trend_score.total
        volume_ratio = self.current_trend_score.volume_ratio

        # Use point-based confirmation if enabled
        if config.grid.USE_POINT_CONFIRMATION:
            await self._check_auto_switch_points(analysis, recommended, trend_score, volume_ratio)
        else:
            await self._check_auto_switch_legacy(analysis, recommended)

    async def _check_auto_switch_points(
        self,
        analysis: MarketAnalysis,
        recommended: str,
        trend_score: int,
        volume_ratio: float
    ) -> None:
        """
        Point-based auto-switch confirmation (faster for strong signals).

        Points accumulate based on signal strength:
        - Trend Score >= 3: +2 points
        - Trend Score == 2: +1 point
        - StochRSI extreme: +1 point
        - High volume: +1 point
        - 4 points threshold = switch
        """
        current_side = config.grid.GRID_SIDE

        # Get StochRSI if available (cached or calculate)
        stochrsi_k = self.last_stochrsi_k

        # Try to calculate StochRSI if not cached or stale
        if stochrsi_k is None:
            try:
                candles = await self.client.get_klines(
                    symbol=config.trading.SYMBOL,
                    interval="1h",
                    limit=50
                )
                if candles:
                    stochrsi = self.indicator_analyzer.calculate_stochrsi(candles)
                    if stochrsi:
                        stochrsi_k = stochrsi.k_line
                        self.last_stochrsi_k = stochrsi_k
            except Exception as e:
                logger.warning(f"Could not calculate StochRSI: {e}")

        # Run point-based check
        new_side = self.fast_trend_confirmation.add_check(
            trend_score=trend_score,
            recommended_side=recommended,
            stochrsi_k=stochrsi_k,
            volume_ratio=volume_ratio,
            current_side=current_side
        )

        # Log status
        ftc = self.fast_trend_confirmation
        if ftc.pending_direction:
            stochrsi_str = f"{stochrsi_k:.1f}" if stochrsi_k else "N/A"
            logger.info(
                f"Point Confirmation: {ftc.get_status()} | "
                f"Score={trend_score}, StochRSI={stochrsi_str}, Vol={volume_ratio:.2f}"
            )

            # Send Telegram notification on first signal (with cooldown to prevent spam)
            should_alert = False
            now = datetime.now()

            if len(ftc.points_history) == 1:
                # First point for this direction - check cooldown
                if self.last_trend_signal_alert is None:
                    should_alert = True
                elif ftc.pending_direction != self.last_trend_signal_direction:
                    # Different direction - always alert
                    should_alert = True
                else:
                    # Same direction - check cooldown
                    seconds_since = (now - self.last_trend_signal_alert).total_seconds()
                    if seconds_since >= self.trend_signal_alert_cooldown:
                        should_alert = True

            if should_alert:
                self.last_trend_signal_alert = now
                self.last_trend_signal_direction = ftc.pending_direction
                await self.telegram.send_message(
                    f"🔄 Trend Signal (Point System)\n\n"
                    f"Current: {current_side}\n"
                    f"Recommended: {recommended}\n"
                    f"Score: {self.current_trend_score}\n"
                    f"StochRSI: {stochrsi_str}\n"
                    f"Volume: {volume_ratio:.2f}x\n\n"
                    f"Points: {ftc.accumulated_points}/{config.grid.SWITCH_THRESHOLD_POINTS}"
                )

        # Execute switch if threshold reached
        if new_side:
            await self._execute_switch(new_side, analysis)

    async def _check_auto_switch_legacy(
        self,
        analysis: MarketAnalysis,
        recommended: str
    ) -> None:
        """
        Legacy 2-check auto-switch confirmation.

        Uses confirmation period to prevent whipsaw:
        - If recommended side is same for 2 consecutive checks, switch
        - If signal changes, reset confirmation counter
        """
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

