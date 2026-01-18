"""
Aster DEX Grid Trading Bot - Configuration

This module manages all configuration parameters for the grid trading bot.
Environment variables are loaded from .env file for security.

IMPORTANT: Never commit .env file with real API credentials!
"""
import os
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Literal
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass(frozen=True)
class APIConfig:
    """
    API connection settings for Aster DEX.
    
    These URLs follow the Binance Futures API pattern as Aster DEX
    is compatible with this standard.
    """
    API_KEY: str = field(default_factory=lambda: os.getenv("ASTER_API_KEY", ""))
    API_SECRET: str = field(default_factory=lambda: os.getenv("ASTER_API_SECRET", ""))
    
    # REST API base URL for Futures
    BASE_URL: str = "https://fapi.asterdex.com"
    
    # WebSocket base URL for Futures real-time data
    WS_URL: str = "wss://fstream.asterdex.com"
    
    # Request timeout in seconds
    REQUEST_TIMEOUT: int = 30
    
    # Recv window for signature (milliseconds) - tolerance for timestamp difference
    RECV_WINDOW: int = 5000


@dataclass
class TradingConfig:
    """
    Core trading parameters.
    
    Why we use low leverage (2x-3x):
    - Grid trading holds multiple positions across price levels
    - Higher leverage increases liquidation risk during volatile swings
    - Lower leverage allows the bot to survive larger price movements
    - With 500 USDT capital, 2x leverage gives effective 1000 USDT buying power
      while maintaining safe distance from liquidation price
    """
    # Trading symbol - SOLUSDT for long-term run (Post-Dec 15 Strategy)
    SYMBOL: str = "SOLUSDT"
    
    # Leverage multiplier - 5x MODERATE (balanced risk/reward)
    # Moderate risk: Liq distance ~20%, good for trending markets
    LEVERAGE: int = 5
    
    # Margin type: ISOLATED or CROSSED
    # For Multi-Asset Mode (using USDF as collateral), CROSSED is required
    # CROSSED also provides 20x Airdrop Multiplier benefit when using USDF
    MARGIN_TYPE: Literal["ISOLATED", "CROSSED"] = "CROSSED"
    
    # Primary margin asset
    MARGIN_ASSET: Literal["USDT", "USDF"] = "USDF"


@dataclass
class GridConfig:
    """
    Grid strategy parameters.
    
    Grid trading places buy orders below current price and sell orders above.
    When a buy fills, we place a sell one level higher (take profit).
    When a sell fills, we place a buy one level lower (re-entry).
    
    Arithmetic spacing formula:
        grid_step = (upper_price - lower_price) / (grid_count - 1)
        level[i] = lower_price + (i * grid_step)
    
    Example with BTCUSDT at $90,000, ±5% range, 10 grids:
        lower = 85,500, upper = 94,500, step = 1,000
        levels = [85500, 86500, 87500, ..., 94500]
    """
    # 12 grids for more frequent, smaller profits
    GRID_COUNT: int = 12
    
    # Price boundaries - will be calculated dynamically based on current price
    # if not specified (using GRID_RANGE_PERCENT)
    LOWER_PRICE: Decimal | None = None
    UPPER_PRICE: Decimal | None = None
    
    # If LOWER/UPPER not set, use this percentage range around current price
    # ±10% means grid spans from -10% to +10% of entry price
    # Tighter Range: ±4% for closer grid (~$127 - $138 at current price)
    # Grid closer to price for faster fills in trending market
    GRID_RANGE_PERCENT: Decimal = Decimal("3.0")

    # ==========================================================================
    # Dynamic Grid Spacing: Adjust grid range based on ATR (volatility)
    # ==========================================================================

    # Enable dynamic grid spacing based on ATR
    # When enabled, GRID_RANGE_PERCENT is adjusted based on market volatility
    DYNAMIC_GRID_SPACING_ENABLED: bool = True

    # ATR multiplier for grid range calculation
    # Grid Range = ATR% × multiplier (e.g., 1.5% ATR × 2.0 = 3% grid range)
    ATR_GRID_MULTIPLIER: Decimal = Decimal("2.5")

    # Minimum grid range (even in low volatility)
    MIN_GRID_RANGE_PERCENT: Decimal = Decimal("2.0")

    # Maximum grid range (even in high volatility)
    MAX_GRID_RANGE_PERCENT: Decimal = Decimal("6.0")
    
    # Dynamic Grid Rebalancing: DISABLED for safety
    # Static Grid prevents position accumulation during trends
    DYNAMIC_GRID_REBALANCE: bool = False
    
    # Grid Side: "BOTH", "LONG", or "SHORT"
    # BOTH = traditional grid (BUY below price, SELL above price)
    # LONG = only BUY orders (for bullish market)
    # SHORT = only SELL orders (for bearish market)
    # Jan 2026: LONG - Extreme Fear (25) + Elliott Wave (iii) impulse starting
    GRID_SIDE: Literal["BOTH", "LONG", "SHORT"] = "LONG"
    
    # Quantity per grid level - Balanced for ~$550 balance
    # $50 per grid with 5x leverage = $250 notional per level
    # Max 5 positions = $1250 notional ($250 margin, 45% of $550 balance)
    QUANTITY_PER_GRID_USDT: Decimal = Decimal("50.0")
    
    # Maximum number of open orders allowed
    MAX_OPEN_ORDERS: int = 20
    
    # Auto Re-Grid: Automatically reposition grid when price drifts too far
    # When enabled, bot monitors price and re-grids if distance > threshold
    AUTO_REGRID_ENABLED: bool = True
    
    # Re-grid threshold: if price moves more than this % from grid center, re-grid
    # 3% threshold = balanced, re-grids when price exits grid boundary
    REGRID_THRESHOLD_PERCENT: Decimal = Decimal("3.0")
    
    # How often to check for re-grid (in minutes)
    REGRID_CHECK_INTERVAL_MINUTES: int = 30
    
    # ==========================================================================
    # Auto Switch Side: Automatically change grid direction based on trend
    # ==========================================================================
    
    # Enable automatic side switching based on trend analysis
    # Uses multi-indicator confirmation (EMA + MACD + RSI) for safety
    AUTO_SWITCH_SIDE_ENABLED: bool = True
    
    # Minimum trend score to trigger a switch (±2 = moderate, ±3 = strong)
    # Score range: -3 (strong bearish) to +3 (strong bullish)
    # Score ±1 = unclear, ±2 = moderate trend, ±3 = strong trend
    MIN_SWITCH_SCORE: int = 2
    
    # Number of consecutive confirmations needed before switching
    # Each check is 30 min apart, so 2 checks = 1 hour confirmation
    SWITCH_CONFIRMATION_CHECKS: int = 2
    
    # What to do when trend is unclear (score 0 or ±1)
    # "STAY" = keep current side, "PAUSE" = pause trading
    UNCLEAR_TREND_ACTION: Literal["STAY", "PAUSE"] = "STAY"

    # ==========================================================================
    # Point-Based Trend Confirmation (Faster than 2-check system)
    # ==========================================================================

    # Enable point-based confirmation instead of 2-check system
    # Points accumulate based on multiple signals, faster for strong signals
    USE_POINT_CONFIRMATION: bool = True

    # Check interval for point accumulation (seconds)
    # More frequent checks = faster response to strong signals
    CONFIRMATION_CHECK_INTERVAL: int = 300  # 5 minutes (vs 15 min for 2-check)

    # Points required to trigger a side switch
    SWITCH_THRESHOLD_POINTS: int = 4

    # Points awarded per signal type
    STRONG_SIGNAL_POINTS: int = 2   # For trend score >=3 or <=-3
    MODERATE_SIGNAL_POINTS: int = 1  # For trend score ±2

    # StochRSI bonus thresholds
    # K < 20 (oversold) = bonus for LONG recommendation
    # K > 80 (overbought) = bonus for SHORT recommendation
    STOCHRSI_BONUS_LOW: float = 20.0
    STOCHRSI_BONUS_HIGH: float = 80.0

    # Volume bonus threshold (volume ratio > this = +1 point)
    VOLUME_BONUS_THRESHOLD: float = 1.3

    # Point decay rate on unclear signals (-1 per unclear check)
    POINT_DECAY_RATE: int = 1
    
    # ==========================================================================
    # Dynamic Re-Grid on TP: Re-analyze and re-place after Take Profit fills
    # ==========================================================================
    
    # Enable dynamic re-grid when TP order fills
    # When enabled, bot re-analyzes market after each TP and decides:
    # - Same trend → Re-place BUY at original level
    # - Different trend → Queue for full re-grid
    REGRID_ON_TP_ENABLED: bool = True
    
    # Minimum minutes between full re-grids (rate limiting)
    # Prevents excessive re-gridding in volatile markets
    REGRID_MIN_INTERVAL_MINUTES: int = 5
    
    # Use cached analysis if it's less than this many minutes old
    # Reduces API calls and computation
    REGRID_ANALYSIS_CACHE_MINUTES: int = 5


@dataclass
class RiskConfig:
    """
    Risk management and safety parameters.

    The circuit breaker is a critical safety feature that stops the bot
    when losses exceed acceptable thresholds. This prevents catastrophic
    losses during black swan events or API issues.

    Phase 3 Risk Settings (Moderate Profile):
    - Circuit Breaker: 20% (protects 80% of capital)
    - Daily Loss Limit: 10% (max daily loss before pause)
    - Max Positions: 5 (limits exposure per symbol)
    - Trailing Stop: 8% (locks profit when price reverses)
    """
    # Maximum drawdown before circuit breaker triggers (percentage of initial balance)
    # 20% drawdown = bot stops to protect remaining 80% of capital
    MAX_DRAWDOWN_PERCENT: Decimal = Decimal("20.0")

    # Daily loss limit - pause trading if daily loss exceeds this percentage
    # Resets every 24 hours from session start
    DAILY_LOSS_LIMIT_PERCENT: Decimal = Decimal("10.0")

    # Maximum number of grid positions that can be held simultaneously
    # Prevents over-exposure to a single asset during trends
    MAX_POSITIONS: int = 5

    # Trailing stop percentage - close all positions if price drops this much
    # from the highest price seen during the session
    # Set to None to disable trailing stop
    TRAILING_STOP_PERCENT: Decimal | None = Decimal("8.0")
    
    # Stop loss per individual position (not recommended for grid, but available)
    STOP_LOSS_PERCENT: Decimal | None = None
    
    # Take profit per grid step (implicit in grid logic, but can override)
    TAKE_PROFIT_PERCENT: Decimal | None = None
    
    # Auto Take Profit - automatically place TP order after BUY fill
    AUTO_TP_ENABLED: bool = True

    # Smart TP - use indicators (RSI, MACD) to determine optimal TP%
    # If False, uses DEFAULT_TP_PERCENT
    USE_SMART_TP: bool = True

    # Default TP percentage when Smart TP is disabled or fails
    DEFAULT_TP_PERCENT: Decimal = Decimal("1.5")

    # ==========================================================================
    # Trailing TP (SuperTrend-based) - Replaces fixed % TP
    # ==========================================================================

    # Enable SuperTrend-based trailing TP instead of fixed %
    # When enabled, TP trails with SuperTrend stop level
    USE_TRAILING_TP: bool = True

    # SuperTrend parameters
    SUPERTREND_LENGTH: int = 10         # ATR period
    SUPERTREND_MULTIPLIER: float = 3.0  # ATR multiplier

    # How often to update trailing TP (seconds)
    TRAILING_TP_UPDATE_INTERVAL: int = 300  # 5 minutes

    # Fallback TP% when SuperTrend stop is not yet profitable
    FALLBACK_TP_PERCENT: Decimal = Decimal("1.5")

    # Minimum profit % before activating trailing mode
    # Until price moves this much, use fixed TP
    MIN_PROFIT_FOR_TRAILING: Decimal = Decimal("0.5")

    # SuperTrend flip alert cooldown (seconds)
    # Prevents spam when SuperTrend flips repeatedly in choppy markets
    SUPERTREND_FLIP_ALERT_COOLDOWN: int = 3600  # 1 hour
    
    # Minimum balance to maintain (bot stops if balance falls below)
    MIN_BALANCE_USDT: Decimal = Decimal("50.0")
    
    # Maximum position size as percentage of balance
    MAX_POSITION_PERCENT: Decimal = Decimal("80.0")

    # ==========================================================================
    # Intelligent Drawdown Management (Moderate Mode)
    # Protects capital through graduated responses to drawdown
    # ==========================================================================

    # Level 1: Pause new BUY orders (keep existing TP orders)
    DRAWDOWN_PAUSE_PERCENT: Decimal = Decimal("15.0")

    # Level 2: Partial cut loss (reduce position)
    DRAWDOWN_PARTIAL_CUT_PERCENT: Decimal = Decimal("20.0")

    # Level 3: Full cut loss (close all positions)
    DRAWDOWN_FULL_CUT_PERCENT: Decimal = Decimal("25.0")

    # How much to cut at Level 2 (30% of position)
    PARTIAL_CUT_RATIO: Decimal = Decimal("30.0")

    # ==========================================================================
    # Safety Net
    # ==========================================================================

    # Minimum balance guard - stop everything if balance falls below this
    MIN_BALANCE_GUARD: Decimal = Decimal("100.0")

    # Daily loss limit in USDT - pause for 24h if exceeded
    DAILY_LOSS_LIMIT_USDT: Decimal = Decimal("50.0")

    # ==========================================================================
    # Auto Re-entry after Cut Loss
    # ==========================================================================

    # Enable automatic re-entry after full cut loss
    AUTO_REENTRY_ENABLED: bool = True

    # RSI threshold for re-entry (wait for oversold bounce)
    REENTRY_RSI_THRESHOLD: Decimal = Decimal("30.0")

    # Position size ratio for re-entry (50% = start with half size)
    REENTRY_POSITION_SIZE_RATIO: Decimal = Decimal("50.0")

    # Minimum wait time after cut loss before re-entry (minutes)
    REENTRY_MIN_WAIT_MINUTES: int = 30


@dataclass
class HarvestConfig:
    """
    Airdrop optimization settings (Harvest Mode).
    
    Aster DEX rewards trading activity with airdrop points.
    Taker orders (market orders) receive 2x point multiplier.
    
    When HARVEST_MODE is True:
    - Initial entries use MARKET orders instead of LIMIT
    - Urgent rebalancing uses MARKET orders
    - This sacrifices some entry price precision for airdrop rewards
    
    Trade-off consideration:
    - Market orders have slippage (unfavorable fill price)
    - But earn 2x airdrop points
    - For airdrop farming, this trade-off may be worthwhile
    """
    # Enable harvest mode for airdrop optimization
    HARVEST_MODE: bool = False
    
    # Use market orders for initial grid placement
    USE_MARKET_FOR_INITIAL: bool = False
    
    # Threshold for switching to taker orders (price deviation percent)
    # If price moves more than this % during rebalance, use market order
    TAKER_PRIORITY_THRESHOLD: Decimal = Decimal("0.5")


@dataclass
class LogConfig:
    """Logging configuration."""
    # Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Log directory - Railway volume mount point
    LOG_DIR: str = os.getenv("LOG_DIR", "/app/logs")
    
    # Log file path (uses LOG_DIR)
    LOG_FILE: str | None = os.path.join(os.getenv("LOG_DIR", "/app/logs"), "grid_bot.log")
    
    # Trade events log (for analysis) - JSON format
    TRADE_EVENTS_LOG: str | None = os.path.join(os.getenv("LOG_DIR", "/app/logs"), "trade_events.jsonl")
    
    # Log rotation settings
    LOG_ROTATION_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
    LOG_ROTATION_BACKUP_COUNT: int = 5  # Keep 5 backup files
    
    # Enable structured JSON logging
    JSON_LOGGING: bool = False


@dataclass
class BotConfig:
    """
    Main configuration container aggregating all config sections.
    
    Usage:
        from config import config
        print(config.trading.SYMBOL)
        print(config.grid.GRID_COUNT)
    """
    api: APIConfig = field(default_factory=APIConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    harvest: HarvestConfig = field(default_factory=HarvestConfig)
    log: LogConfig = field(default_factory=LogConfig)
    
    # Dry run mode - simulate orders without executing
    DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"
    
    # Initial capital for the bot
    INITIAL_CAPITAL_USDT: Decimal = Decimal("550.0")
    
    def validate(self) -> list[str]:
        """
        Validate configuration and return list of errors.
        
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        # API credentials check
        if not self.api.API_KEY and not self.DRY_RUN:
            errors.append("ASTER_API_KEY is required (set in .env or environment)")
        if not self.api.API_SECRET and not self.DRY_RUN:
            errors.append("ASTER_API_SECRET is required (set in .env or environment)")
        
        # Grid validation
        if self.grid.GRID_COUNT < 2:
            errors.append("GRID_COUNT must be at least 2")
        if self.grid.GRID_COUNT > 50:
            errors.append("GRID_COUNT should not exceed 50 (API limits)")
        
        # Risk validation
        if self.risk.MAX_DRAWDOWN_PERCENT <= 0:
            errors.append("MAX_DRAWDOWN_PERCENT must be positive")
        if self.risk.MAX_DRAWDOWN_PERCENT > Decimal("50"):
            errors.append("MAX_DRAWDOWN_PERCENT > 50% is extremely risky - recommend 20% for safety")
        
        # Capital validation
        if self.INITIAL_CAPITAL_USDT < Decimal("100"):
            errors.append("Minimum recommended capital is 100 USDT")
        
        return errors


# Global configuration instance - import this in other modules
config = BotConfig()


if __name__ == "__main__":
    # Quick validation check when running config.py directly
    print("=== Aster DEX Grid Bot Configuration ===")
    print(f"Symbol: {config.trading.SYMBOL}")
    print(f"Leverage: {config.trading.LEVERAGE}x")
    print(f"Grid Count: {config.grid.GRID_COUNT}")
    print(f"Grid Range: ±{config.grid.GRID_RANGE_PERCENT}%")
    print(f"Quantity per Grid: {config.grid.QUANTITY_PER_GRID_USDT} USDT")
    print(f"Max Drawdown: {config.risk.MAX_DRAWDOWN_PERCENT}%")
    print(f"Harvest Mode: {config.harvest.HARVEST_MODE}")
    print(f"Dry Run: {config.DRY_RUN}")
    
    errors = config.validate()
    if errors:
        print("\n⚠️ Configuration Errors:")
        for err in errors:
            print(f"  - {err}")
    else:
        print("\n✅ Configuration valid!")
