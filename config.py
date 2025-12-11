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
    # Trading symbol - ASTERUSDT is Aster DEX's native token pair
    SYMBOL: str = "BTCUSDT"
    
    # Leverage multiplier - LOW to avoid liquidation during extended runs
    # 2x is conservative, 3x is moderate - avoid 5x+ for grid trading
    LEVERAGE: int = 2
    
    # Margin type: ISOLATED or CROSSED
    # For Multi-Asset Mode (using USDF as collateral), CROSSED is required
    # CROSSED also provides 20x Airdrop Multiplier benefit when using USDF
    MARGIN_TYPE: Literal["ISOLATED", "CROSSED"] = "CROSSED"
    
    # Primary margin asset
    MARGIN_ASSET: Literal["USDT", "USDF"] = "USDT"


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
    # Number of grid levels (total buy + sell orders)
    GRID_COUNT: int = 10
    
    # Price boundaries - will be calculated dynamically based on current price
    # if not specified (using GRID_RANGE_PERCENT)
    LOWER_PRICE: Decimal | None = None
    UPPER_PRICE: Decimal | None = None
    
    # If LOWER/UPPER not set, use this percentage range around current price
    # ±15% means grid spans from -15% to +15% of entry price
    # Widened from 5% to 15% to handle high volatility conditions
    GRID_RANGE_PERCENT: Decimal = Decimal("15.0")
    
    # Quantity per grid level (in quote currency value, e.g., USDT)
    # With 300 USDT capital, 2x leverage = 600 USDT effective
    # Divided across ~5 active grids = ~120 USDT per grid is aggressive
    # Using ~35 USDT per grid is balanced for this capital
    QUANTITY_PER_GRID_USDT: Decimal = Decimal("35.0")
    
    # Maximum number of open orders allowed
    MAX_OPEN_ORDERS: int = 20


@dataclass
class RiskConfig:
    """
    Risk management and safety parameters.
    
    The circuit breaker is a critical safety feature that stops the bot
    when losses exceed acceptable thresholds. This prevents catastrophic
    losses during black swan events or API issues.
    """
    # Maximum drawdown before circuit breaker triggers (percentage of initial balance)
    # 10% drawdown on 500 USDT = 50 USDT max loss before emergency stop
    MAX_DRAWDOWN_PERCENT: Decimal = Decimal("10.0")
    
    # Stop loss per individual position (not recommended for grid, but available)
    STOP_LOSS_PERCENT: Decimal | None = None
    
    # Take profit per grid step (implicit in grid logic, but can override)
    TAKE_PROFIT_PERCENT: Decimal | None = None
    
    # Minimum balance to maintain (bot stops if balance falls below)
    MIN_BALANCE_USDT: Decimal = Decimal("50.0")
    
    # Maximum position size as percentage of balance
    MAX_POSITION_PERCENT: Decimal = Decimal("80.0")


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
    
    # Log file path (None for stdout only)
    LOG_FILE: str | None = "grid_bot.log"
    
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
    INITIAL_CAPITAL_USDT: Decimal = Decimal("300.0")
    
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
            errors.append("MAX_DRAWDOWN_PERCENT > 50% is extremely risky")
        
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
