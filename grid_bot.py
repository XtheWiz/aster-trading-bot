"""
Aster DEX Grid Trading Bot - Main Execution Logic

This module implements the grid trading strategy:
1. Calculate grid levels based on current price and configuration
2. Place buy orders below current price, sell orders above
3. Monitor order fills via WebSocket
4. Dynamically rebalance: filled buy -> place sell, filled sell -> place buy
5. Execute safety circuit breaker if drawdown exceeds threshold

Grid Trading Strategy Explained:
================================
Grid trading profits from price oscillation within a range.
- We divide a price range into N levels (grids)
- Place BUY orders at lower levels, SELL orders at upper levels
- When price drops and hits our BUY, we accumulate
- When price rises and hits our SELL, we take profit
- The strategy works best in sideways/ranging markets

Example:
    Current price: 0.9683 USDT
    Grid range: ±5% (0.9199 - 1.0167)
    Grid count: 10
    Each grid: ~0.0097 USDT apart
    
    When price drops to 0.9586, our BUY fills
    We immediately place SELL at 0.9683 (one grid up)
    If price bounces back, we capture the grid profit
"""
import asyncio
import logging
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Callable

from config import config
from aster_client import AsterClient, AsterAPIError
from trade_logger import TradeLogger, create_trade_record, BalanceSnapshot
from telegram_notifier import TelegramNotifier
from telegram_commands import TelegramCommandHandler
from strategy_manager import StrategyManager
from indicator_analyzer import IndicatorAnalyzer, get_smart_tp
from trade_event_logger import trade_event_logger

# Configure logging with structured format
logging.basicConfig(
    level=getattr(logging, config.log.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        *(
            [logging.FileHandler(config.log.LOG_FILE)]
            if config.log.LOG_FILE
            else []
        ),
    ],
)
# Suppress noisy library logs
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("GridBot")


class OrderSide(Enum):
    """Order direction."""
    BUY = "BUY"
    SELL = "SELL"


class BotState(Enum):
    """Bot operational state."""
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class GridLevelState(Enum):
    """State machine for grid level lifecycle."""
    EMPTY = "EMPTY"                    # No order, no position
    BUY_PLACED = "BUY_PLACED"          # BUY order placed, waiting fill
    POSITION_HELD = "POSITION_HELD"    # BUY filled, holding position
    TP_PLACED = "TP_PLACED"            # TP SELL order placed, waiting fill
    SELL_PLACED = "SELL_PLACED"        # Regular SELL order placed


@dataclass
class GridLevel:
    """
    Represents a single grid level with its order and position state.

    Attributes:
        index: Grid level index (0 = lowest price)
        price: Price at this grid level
        side: BUY or SELL (determined by position relative to entry price)
        order_id: Active order ID at this level (None if no order)
        client_order_id: Client order ID for tracking
        filled: Whether order at this level has been filled (fully)
        state: Current state in the lifecycle (EMPTY -> BUY_PLACED -> POSITION_HELD -> TP_PLACED)
        entry_price: Price at which position was entered (BUY fill price)
        position_quantity: Quantity held at this level (accumulated from partial fills)
        tp_order_id: Take profit order ID (separate from regular order_id)
        partial_tp_order_ids: List of TP order IDs for partial fills
        intended_price: Original intended price for slippage calculation
    """
    index: int
    price: Decimal
    side: OrderSide | None = None
    order_id: int | None = None
    client_order_id: str | None = None
    filled: bool = False
    # Position tracking
    state: GridLevelState = GridLevelState.EMPTY
    entry_price: Decimal = Decimal("0")
    position_quantity: Decimal = Decimal("0")
    tp_order_id: int | None = None
    # Partial fill tracking
    partial_tp_order_ids: list[int] = field(default_factory=list)
    partial_fill_count: int = 0
    # Slippage tracking
    intended_price: Decimal = Decimal("0")
    actual_fill_price: Decimal = Decimal("0")
    slippage_percent: Decimal = Decimal("0")

    def __repr__(self) -> str:
        if self.state == GridLevelState.POSITION_HELD:
            partial_info = f" ({self.partial_fill_count} partial)" if self.partial_fill_count > 0 else ""
            status = f"HOLDING:{self.position_quantity:.2f}@{self.entry_price:.4f}{partial_info}"
        elif self.state == GridLevelState.TP_PLACED:
            status = f"TP:{self.tp_order_id}"
        elif self.order_id:
            status = f"ORDER:{self.order_id}"
        else:
            status = self.state.value
        return f"Grid[{self.index}] {self.price:.4f} {self.side.value if self.side else 'N/A'} ({status})"

    def reset(self) -> None:
        """Reset level to empty state after TP fill."""
        self.filled = False
        self.state = GridLevelState.EMPTY
        self.entry_price = Decimal("0")
        self.position_quantity = Decimal("0")
        self.order_id = None
        self.tp_order_id = None
        self.client_order_id = None
        self.partial_tp_order_ids = []
        self.partial_fill_count = 0
        self.intended_price = Decimal("0")
        self.actual_fill_price = Decimal("0")
        self.slippage_percent = Decimal("0")

    def add_partial_fill(self, price: Decimal, quantity: Decimal) -> None:
        """
        Add a partial fill to this level's position.

        Updates entry_price as weighted average if there are multiple partial fills.
        """
        if self.position_quantity == 0:
            # First fill
            self.entry_price = price
            self.position_quantity = quantity
        else:
            # Weighted average entry price
            total_qty = self.position_quantity + quantity
            self.entry_price = (
                (self.entry_price * self.position_quantity + price * quantity) / total_qty
            )
            self.position_quantity = total_qty

        self.partial_fill_count += 1
        self.state = GridLevelState.POSITION_HELD

    def calculate_slippage(self, fill_price: Decimal) -> Decimal:
        """Calculate slippage percentage from intended price."""
        if self.intended_price <= 0:
            return Decimal("0")

        self.actual_fill_price = fill_price
        self.slippage_percent = (
            (fill_price - self.intended_price) / self.intended_price * 100
        )
        return self.slippage_percent


@dataclass
class GridState:
    """
    Complete state of the grid trading system.
    
    This tracks all grid levels, orders, and financial metrics
    for monitoring and decision-making.
    """
    # Grid configuration
    lower_price: Decimal = Decimal("0")
    upper_price: Decimal = Decimal("0")
    grid_step: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    
    # Grid levels
    levels: list[GridLevel] = field(default_factory=list)
    
    # Financial tracking
    initial_balance: Decimal = Decimal("0")
    current_balance: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    total_trades: int = 0
    
    # Timing
    start_time: datetime | None = None
    
    @property
    def drawdown_percent(self) -> Decimal:
        """Calculate current drawdown as percentage of initial balance."""
        if self.initial_balance <= 0:
            return Decimal("0")
        
        current_equity = self.current_balance + self.unrealized_pnl
        pnl = current_equity - self.initial_balance
        
        if pnl >= 0:
            return Decimal("0")
        
        return abs(pnl) / self.initial_balance * 100
    
    @property
    def active_orders_count(self) -> int:
        """Count of grid levels with active orders."""
        return sum(1 for level in self.levels if level.order_id is not None)
    
    @property
    def step_size(self) -> Decimal:
        """Grid step size (alias for grid_step)."""
        return self.grid_step
    
    def get_level_by_order_id(self, order_id: int) -> GridLevel | None:
        """Find grid level by order ID (includes both regular and TP orders)."""
        for level in self.levels:
            if level.order_id == order_id or level.tp_order_id == order_id:
                return level
        return None

    def get_level_by_tp_order_id(self, tp_order_id: int) -> GridLevel | None:
        """Find grid level by TP order ID specifically."""
        for level in self.levels:
            if level.tp_order_id == tp_order_id:
                return level
        return None

    def get_level_by_price(self, price: Decimal, tolerance: Decimal = Decimal("0.0001")) -> GridLevel | None:
        """Find grid level closest to given price within tolerance."""
        for level in self.levels:
            if abs(level.price - price) <= tolerance:
                return level
        return None

    def get_total_position_quantity(self) -> Decimal:
        """Get total position quantity across all grid levels."""
        return sum(level.position_quantity for level in self.levels)

    def get_levels_with_position(self) -> list[GridLevel]:
        """Get all levels that are holding a position."""
        return [
            level for level in self.levels
            if level.state in (GridLevelState.POSITION_HELD, GridLevelState.TP_PLACED)
        ]


class GridBot:
    """
    The main Grid Trading Bot implementation.
    
    This class orchestrates:
    - Grid calculation based on market price
    - Order placement and management
    - WebSocket event handling for order updates
    - Risk management and circuit breaker logic
    - Dynamic rebalancing on order fills
    
    Usage:
        bot = GridBot()
        await bot.run()
    """
    
    def __init__(self):
        """Initialize the grid bot with configuration."""
        self.client = AsterClient()
        self.state = GridState()
        self.bot_state = BotState.INITIALIZING
        
        # Symbol information (fetched from exchange)
        self.tick_size: Decimal = Decimal("0.0001")
        self.lot_size: Decimal = Decimal("0.01")
        self.min_notional: Decimal = Decimal("5")
        
        # Shutdown handling
        self._shutdown_event = asyncio.Event()
        
        # Harvest mode tracking
        self._initial_orders_placed = False
        
        # Trade logging and notifications
        self.trade_logger = TradeLogger()
        self.telegram = TelegramNotifier()
        self.telegram_commands = TelegramCommandHandler(bot_reference=self)
        self.strategy_manager = StrategyManager(self.client, bot_reference=self)
        self._session_id: int = 0
        self._last_hourly_summary = datetime.now()
    
    # =========================================================================
    # GRID CALCULATION
    # =========================================================================

    async def get_dynamic_grid_range(self, current_price: Decimal) -> Decimal:
        """
        Calculate dynamic grid range based on ATR (volatility).

        Formula:
            grid_range = ATR% × ATR_GRID_MULTIPLIER
            clamped to [MIN_GRID_RANGE_PERCENT, MAX_GRID_RANGE_PERCENT]

        Returns:
            Grid range percentage (e.g., 3.0 for ±3%)
        """
        if not config.grid.DYNAMIC_GRID_SPACING_ENABLED:
            return config.grid.GRID_RANGE_PERCENT

        try:
            # Get ATR from strategy manager's last analysis or calculate fresh
            atr_percent = Decimal("0")

            if self.strategy_manager.last_analysis:
                atr_value = self.strategy_manager.last_analysis.atr_value
                if atr_value > 0 and current_price > 0:
                    atr_percent = (atr_value / current_price) * 100
            else:
                # Fetch fresh analysis
                analysis = await self.strategy_manager.analyze_market()
                if analysis.atr_value > 0 and current_price > 0:
                    atr_percent = (analysis.atr_value / current_price) * 100

            if atr_percent <= 0:
                logger.warning("ATR is zero, using default grid range")
                return config.grid.GRID_RANGE_PERCENT

            # Calculate dynamic range
            dynamic_range = atr_percent * config.grid.ATR_GRID_MULTIPLIER

            # Clamp to min/max bounds
            dynamic_range = max(dynamic_range, config.grid.MIN_GRID_RANGE_PERCENT)
            dynamic_range = min(dynamic_range, config.grid.MAX_GRID_RANGE_PERCENT)

            logger.info(
                f"Dynamic Grid: ATR={atr_percent:.2f}% × {config.grid.ATR_GRID_MULTIPLIER} = "
                f"{dynamic_range:.2f}% (bounds: {config.grid.MIN_GRID_RANGE_PERCENT}-{config.grid.MAX_GRID_RANGE_PERCENT}%)"
            )

            return dynamic_range

        except Exception as e:
            logger.error(f"Error calculating dynamic grid range: {e}")
            return config.grid.GRID_RANGE_PERCENT

    def calculate_grid_levels(self, current_price: Decimal, grid_range_percent: Decimal | None = None) -> list[GridLevel]:
        """
        Calculate grid price levels using arithmetic spacing.

        Arithmetic Grid Formula:
        =======================
        grid_step = (upper_price - lower_price) / (grid_count - 1)
        level[i] = lower_price + (i * grid_step)

        This creates evenly spaced price levels. Each level represents
        a potential order placement point.

        Args:
            current_price: Current market price for centering the grid
            grid_range_percent: Optional override for grid range (from dynamic calculation)

        Returns:
            List of GridLevel objects with calculated prices
        """
        # Determine price range
        if config.grid.LOWER_PRICE and config.grid.UPPER_PRICE:
            lower = config.grid.LOWER_PRICE
            upper = config.grid.UPPER_PRICE
        else:
            # Use provided range or fall back to config
            range_pct = (grid_range_percent or config.grid.GRID_RANGE_PERCENT) / 100
            lower = current_price * (1 - range_pct)
            upper = current_price * (1 + range_pct)
        
        # Calculate grid step
        grid_count = config.grid.GRID_COUNT
        grid_step = (upper - lower) / (grid_count - 1)
        
        # Store in state
        self.state.lower_price = lower
        self.state.upper_price = upper
        self.state.grid_step = grid_step
        self.state.entry_price = current_price
        
        # Generate levels
        levels = []
        for i in range(grid_count):
            price = lower + (Decimal(i) * grid_step)
            # Round to tick size
            price = self._round_price(price)
            
            # Determine order side based on position relative to current price
            if price < current_price:
                side = OrderSide.BUY
            elif price > current_price:
                side = OrderSide.SELL
            else:
                # At current price - skip or use as reference
                side = None
            
            # Filter by GRID_SIDE config
            # LONG mode: only BUY orders (for bullish markets)
            # SHORT mode: only SELL orders (for bearish markets)
            # BOTH mode: traditional grid with both sides
            grid_side = config.grid.GRID_SIDE
            if grid_side == "LONG" and side == OrderSide.SELL:
                side = None  # Skip SELL orders in LONG mode
            elif grid_side == "SHORT" and side == OrderSide.BUY:
                side = None  # Skip BUY orders in SHORT mode
            
            levels.append(GridLevel(
                index=i,
                price=price,
                side=side,
            ))
        
        logger.info(f"Grid calculated: {grid_count} levels from {lower:.4f} to {upper:.4f}")
        logger.info(f"Grid step: {grid_step:.4f} ({grid_step/current_price*100:.2f}%)")
        
        return levels
    
    def _round_price(self, price: Decimal) -> Decimal:
        """Round price to valid tick size."""
        return (price / self.tick_size).quantize(Decimal("1"), ROUND_DOWN) * self.tick_size
    
    def _round_quantity(self, quantity: Decimal) -> Decimal:
        """Round quantity to valid lot size."""
        return (quantity / self.lot_size).quantize(Decimal("1"), ROUND_DOWN) * self.lot_size
    
    def calculate_quantity_for_level(self, price: Decimal) -> Decimal:
        """
        Calculate order quantity for a grid level.
        
        We use fixed USDT value per grid and convert to base asset quantity.
        
        Formula:
            quantity = (usdt_per_grid * leverage) / price
        
        Args:
            price: Order price
            
        Returns:
            Quantity in base asset (rounded to lot size)
        """
        usdt_per_grid = config.grid.QUANTITY_PER_GRID_USDT
        leverage = Decimal(config.trading.LEVERAGE)
        
        # Calculate base quantity
        quantity = (usdt_per_grid * leverage) / price
        
        # Round to lot size
        quantity = self._round_quantity(quantity)
        
        # Validate minimum notional
        notional = quantity * price
        if notional < self.min_notional:
            logger.warning(f"Order notional {notional} < min {self.min_notional}")
            quantity = (self.min_notional / price).quantize(Decimal("0.01"), ROUND_UP)
            quantity = self._round_quantity(quantity)
        
        return quantity
    
    # =========================================================================
    # ORDER MANAGEMENT
    # =========================================================================
    
    async def place_grid_orders(self) -> None:
        """
        Place initial grid orders based on calculated levels.
        
        - BUY orders placed at levels below current price
        - SELL orders placed at levels above current price
        
        In HARVEST_MODE, initial orders may use MARKET type
        for airdrop point optimization.
        """
        orders_placed = 0
        
        for level in self.state.levels:
            if level.side is None:
                continue
            
            if level.order_id is not None:
                continue  # Already has an order
            
            try:
                quantity = self.calculate_quantity_for_level(level.price)
                
                # Determine order type
                # In harvest mode, use MARKET for initial orders to maximize taker fees
                if (
                    config.harvest.HARVEST_MODE 
                    and config.harvest.USE_MARKET_FOR_INITIAL
                    and not self._initial_orders_placed
                ):
                    order_type = "MARKET"
                    price = None
                else:
                    order_type = "LIMIT"
                    price = level.price
                
                # Generate client order ID for tracking
                client_order_id = f"grid_{level.index}_{int(datetime.now().timestamp())}"
                level.client_order_id = client_order_id
                
                # Place order
                response = await self.client.place_order(
                    symbol=config.trading.SYMBOL,
                    side=level.side.value,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                    client_order_id=client_order_id,
                )
                
                level.order_id = response.get("orderId")
                # Set intended price for slippage tracking
                level.intended_price = level.price
                # Set state based on side
                if level.side == OrderSide.BUY:
                    level.state = GridLevelState.BUY_PLACED
                else:
                    level.state = GridLevelState.SELL_PLACED
                orders_placed += 1

                logger.info(
                    f"Placed {level.side.value} {order_type} @ {level.price:.4f} | "
                    f"Qty: {quantity} | OrderID: {level.order_id}"
                )
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.1)
                
            except AsterAPIError as e:
                logger.error(f"Failed to place order at level {level.index}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error placing order: {e}")
        
        self._initial_orders_placed = True
        logger.info(f"Total orders placed: {orders_placed}")
    
    async def cancel_all_orders(self) -> None:
        """Cancel all open orders for the trading symbol."""
        try:
            await self.client.cancel_all_orders(config.trading.SYMBOL)

            # Clear order IDs and reset states (preserve position info for levels with positions)
            for level in self.state.levels:
                level.order_id = None
                level.tp_order_id = None
                # Only reset state if no position held
                if level.state not in (GridLevelState.POSITION_HELD,):
                    level.state = GridLevelState.EMPTY

            logger.info("All orders canceled")
        except AsterAPIError as e:
            logger.error(f"Failed to cancel all orders: {e}")
    
    async def rebalance_on_fill(
        self,
        filled_level: GridLevel,
        fill_price: Decimal | None = None,
        fill_qty: Decimal | None = None
    ) -> None:
        """
        Rebalance grid after an order fill.

        Dynamic Grid Rebalancing (when DYNAMIC_GRID_REBALANCE=True):
        ============================================================
        After each fill:
        1. Cancel all existing orders
        2. Get current market price
        3. Recalculate grid centered on current price
        4. Place new grid orders

        This makes the grid "follow" the price, ideal for trending markets.

        Static Grid Rebalancing (when DYNAMIC_GRID_REBALANCE=False):
        ============================================================
        - When BUY order fills: Place SELL at one grid step HIGHER
        - When SELL order fills: Place BUY at one grid step LOWER

        Args:
            filled_level: The grid level whose order just filled
            fill_price: Price at which order was filled (for logging)
            fill_qty: Quantity that was filled (for logging)
        """
        self.state.total_trades += 1
        filled_level.filled = True

        # Clear regular order_id (tp_order_id handled separately)
        if filled_level.state != GridLevelState.TP_PLACED:
            filled_level.order_id = None
        
        # Check if Dynamic Grid Rebalancing is enabled
        if getattr(config.grid, 'DYNAMIC_GRID_REBALANCE', False):
            await self._dynamic_rebalance(filled_level)
            return
        
        # Static Grid Rebalancing (original behavior)
        await self._static_rebalance(filled_level)
    
    async def _dynamic_rebalance(self, filled_level: GridLevel) -> None:
        """
        Dynamic Grid: Cancel all orders and recalculate grid from current price.
        """
        filled_side = filled_level.side
        logger.info(
            f"🔄 DYNAMIC REBALANCE: {filled_side.value} filled @ {filled_level.price:.4f} | "
            f"Trade #{self.state.total_trades}"
        )

        try:
            # Cancel all existing orders
            await self.cancel_all_orders()

            # Get current market price
            ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
            current_price = Decimal(ticker["price"])

            logger.info(f"🔄 DYNAMIC REBALANCE: Recalculating grid from ${current_price:.4f}")

            # Calculate dynamic grid range
            grid_range = await self.get_dynamic_grid_range(current_price)

            # Recalculate grid levels centered on current price
            self.state.levels = self.calculate_grid_levels(current_price, grid_range)
            self.state.entry_price = current_price

            # Place new grid orders
            await self.place_grid_orders()

            logger.info(
                f"🔄 DYNAMIC REBALANCE: Complete! New grid: "
                f"${self.state.lower_price:.4f} - ${self.state.upper_price:.4f} (±{grid_range:.2f}%)"
            )

        except AsterAPIError as e:
            logger.error(f"Dynamic rebalance failed: {e}")
    
    async def _static_rebalance(self, filled_level: GridLevel) -> None:
        """
        Static Grid: Place counter-order at existing grid level.
        """
        # Calculate target level for counter-order
        if filled_level.side == OrderSide.BUY:
            # BUY filled -> place SELL one level up
            target_index = filled_level.index + 1
            new_side = OrderSide.SELL
            log_action = "BUY filled -> placing SELL"
        else:
            # SELL filled -> place BUY one level down
            target_index = filled_level.index - 1
            new_side = OrderSide.BUY
            log_action = "SELL filled -> placing BUY"
        
        # Check GRID_SIDE config - handle special cases for LONG/SHORT only modes
        grid_side = config.grid.GRID_SIDE
        
        if grid_side == "LONG" and new_side == OrderSide.SELL:
            # LONG mode: Instead of regular counter-order, place Smart TP
            if config.risk.AUTO_TP_ENABLED:
                await self._place_smart_tp(filled_level)
            else:
                logger.info(f"LONG mode: AUTO_TP disabled, skipping SELL")
            return
        elif grid_side == "LONG" and filled_level.side == OrderSide.SELL:
            # LONG mode: TP SELL filled -> Dynamic Re-Grid decision
            await self._handle_tp_sell_filled(filled_level)
            return
        elif grid_side == "SHORT" and new_side == OrderSide.BUY:
            logger.info(f"SHORT mode: Skipping BUY counter-order after SELL fill")
            return
        
        # Validate target level exists
        if target_index < 0 or target_index >= len(self.state.levels):
            logger.warning(f"Target level {target_index} out of range - skipping rebalance")
            return
        
        target_level = self.state.levels[target_index]
        
        # Skip if target already has an order
        if target_level.order_id is not None:
            logger.debug(f"Target level {target_index} already has order - skipping")
            return
        
        try:
            quantity = self.calculate_quantity_for_level(target_level.price)
            
            # In harvest mode, check if we should use market order for urgency
            if config.harvest.HARVEST_MODE:
                price_deviation = abs(self.state.entry_price - target_level.price) / self.state.entry_price * 100
                if price_deviation > config.harvest.TAKER_PRIORITY_THRESHOLD:
                    order_type = "MARKET"
                    price = None
                else:
                    order_type = "LIMIT"
                    price = target_level.price
            else:
                order_type = "LIMIT"
                price = target_level.price
            
            client_order_id = f"grid_{target_level.index}_{int(datetime.now().timestamp())}"
            target_level.client_order_id = client_order_id
            target_level.side = new_side
            
            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side=new_side.value,
                order_type=order_type,
                quantity=quantity,
                price=price,
                client_order_id=client_order_id,
            )
            
            target_level.order_id = response.get("orderId")
            
            logger.info(
                f"REBALANCE: {log_action} @ {target_level.price:.4f} | "
                f"Trade #{self.state.total_trades}"
            )
            
        except AsterAPIError as e:
            logger.error(f"Failed to place rebalance order: {e}")
    
    async def _place_smart_tp(self, filled_level: GridLevel) -> None:
        """
        Place intelligent Take-Profit order based on market indicators.

        This method:
        1. Uses cached analysis from StrategyManager if available
        2. Falls back to fetching candle data if no cache
        3. Determines optimal TP% based on market conditions
        4. Places SELL order at calculated TP price

        Args:
            filled_level: The grid level that just got filled (BUY)
        """
        try:
            entry_price = filled_level.price

            # Get TP percentage
            if config.risk.USE_SMART_TP:
                # First try to use cached analysis from StrategyManager
                cached_analysis = self.strategy_manager.last_analysis

                if cached_analysis and cached_analysis.rsi > 0:
                    tp_percent = await get_smart_tp(market_analysis=cached_analysis)
                    logger.info(f"🧠 Smart TP (cached): {tp_percent}%")
                else:
                    # Fallback: Fetch candle data for indicator calculation
                    candles = await self.client.get_klines(
                        symbol=config.trading.SYMBOL,
                        interval="1h",
                        limit=50
                    )

                    if candles:
                        tp_percent = await get_smart_tp(candles=candles)
                        logger.info(f"🧠 Smart TP (calculated): {tp_percent}%")
                    else:
                        tp_percent = config.risk.DEFAULT_TP_PERCENT
                        logger.warning(f"No candle data, using default TP: {tp_percent}%")
            else:
                tp_percent = config.risk.DEFAULT_TP_PERCENT
                logger.info(f"Smart TP disabled, using default: {tp_percent}%")
            
            # Calculate TP price
            tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
            tp_price = self._round_price(tp_price)
            
            # Calculate quantity (same as filled order)
            quantity = self.calculate_quantity_for_level(entry_price)
            
            # Generate client order ID
            client_order_id = f"tp_{filled_level.index}_{int(datetime.now().timestamp())}"
            
            # Place SELL order at TP price
            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side="SELL",
                order_type="LIMIT",
                quantity=quantity,
                price=tp_price,
                client_order_id=client_order_id,
            )
            
            order_id = response.get("orderId")

            # Store TP order info separately from BUY order
            filled_level.tp_order_id = order_id
            filled_level.state = GridLevelState.TP_PLACED
            filled_level.side = OrderSide.SELL
            filled_level.client_order_id = client_order_id
            # Keep order_id pointing to TP for backward compatibility with get_level_by_order_id
            filled_level.order_id = order_id

            logger.info(
                f"🎯 SMART TP PLACED: SELL @ ${tp_price:.4f} (+{tp_percent}%) | "
                f"Entry: ${entry_price:.4f} | Qty: {filled_level.position_quantity} | OrderID: {order_id}"
            )
            
            # Send Telegram notification
            await self.telegram.send_message(
                f"🎯 Smart TP Placed!\n"
                f"Entry: ${entry_price:.4f}\n"
                f"TP: ${tp_price:.4f} (+{tp_percent}%)\n"
                f"Qty: {quantity}"
            )
            
            # Log trade event for analysis
            trade_event_logger.log_smart_tp(
                entry_price=entry_price,
                tp_price=tp_price,
                tp_percent=tp_percent,
                rsi=0.0,  # Will be enhanced with actual values
                macd_hist=0.0,
                trend="",
            )
            
        except AsterAPIError as e:
            logger.error(f"Failed to place Smart TP order: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in Smart TP: {e}")
    
    async def _handle_tp_sell_filled(self, filled_level: GridLevel) -> None:
        """
        Handle TP SELL fill with Dynamic Re-Grid logic.

        Sequence:
        1. Reset level position tracking
        2. Ask StrategyManager what to do
        3. REPLACE → re-place BUY at original level
        4. REGRID → cancel all and re-grid
        5. WAIT → do nothing, wait for more confirmation
        """
        try:
            logger.info(
                f"🎯 TP SELL filled at level {filled_level.index} | "
                f"PnL already calculated in on_order_update"
            )

            # Reset level after TP fill (position is closed)
            filled_level.reset()
            
            # Get recommendation from strategy manager
            action = await self.strategy_manager.should_regrid_on_tp()
            
            if action == "REPLACE":
                # Re-place BUY at the original level
                await self._re_place_buy(filled_level)
                
                await self.telegram.send_message(
                    f"🔄 TP Filled → BUY Re-placed\n\n"
                    f"Level: {filled_level.index}\n"
                    f"Same trend continues ✅"
                )
                
            elif action == "REGRID":
                # Full re-grid
                logger.warning("🔄 Trend changed - Full Re-Grid triggered")

                await self.telegram.send_message(
                    f"🔄 Trend Changed → Full Re-Grid\n\n"
                    f"Canceling all orders and repositioning..."
                )

                await self.cancel_all_orders()

                ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
                current_price = Decimal(ticker["price"])

                # Calculate dynamic grid range
                grid_range = await self.get_dynamic_grid_range(current_price)

                self.state.entry_price = current_price
                self.state.levels = self.calculate_grid_levels(current_price, grid_range)

                await self.place_grid_orders()

                # Record new grid placement
                self.strategy_manager.record_grid_placement()

                await self.telegram.send_message(
                    f"✅ Re-Grid Complete!\n\n"
                    f"New Center: ${current_price:.2f}\n"
                    f"Range: ±{grid_range:.2f}%\n"
                    f"Orders: {len([l for l in self.state.levels if l.order_id])}"
                )
                
            elif action == "WAIT":
                # Wait for confirmation - do nothing
                logger.info("Waiting for trend confirmation, not placing new BUY")
                
        except Exception as e:
            logger.error(f"Error handling TP SELL fill: {e}")
            # Fallback: re-place BUY to avoid idle grid
            await self._re_place_buy(filled_level)
    
    async def _re_place_buy(self, level: GridLevel) -> None:
        """Re-place a BUY order at the specified grid level."""
        try:
            quantity = self.calculate_quantity_for_level(level.price)
            client_order_id = f"grid_{level.index}_{int(datetime.now().timestamp())}"

            level.side = OrderSide.BUY
            level.client_order_id = client_order_id
            level.state = GridLevelState.BUY_PLACED
            level.filled = False

            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side="BUY",
                order_type="LIMIT",
                quantity=quantity,
                price=level.price,
                client_order_id=client_order_id,
            )

            level.order_id = response.get("orderId")

            logger.info(f"📥 BUY re-placed: ${level.price:.4f} | Level {level.index}")

        except AsterAPIError as e:
            logger.error(f"Failed to re-place BUY: {e}")
    
    async def _handle_partial_fill(
        self,
        level: GridLevel,
        side: str,
        price: Decimal,
        quantity: Decimal
    ) -> None:
        """
        Handle partial fill by placing a TP order for the filled portion.

        This ensures we don't miss profit opportunities on partial fills.
        The TP order is placed using Smart TP calculation for BUY fills.

        Args:
            level: The grid level that was partially filled
            side: BUY or SELL
            price: Fill price
            quantity: Filled quantity
        """
        try:
            if side != "BUY":
                # For now, only handle BUY partial fills (LONG mode)
                return

            # Use Smart TP if enabled, otherwise use grid step
            if config.risk.USE_SMART_TP and config.risk.AUTO_TP_ENABLED:
                # Get Smart TP percentage
                try:
                    candles = await self.client.get_klines(
                        symbol=config.trading.SYMBOL,
                        interval="1h",
                        limit=50
                    )
                    if candles:
                        from indicator_analyzer import get_smart_tp
                        tp_percent = await get_smart_tp(candles=candles)
                    else:
                        tp_percent = config.risk.DEFAULT_TP_PERCENT
                except Exception:
                    tp_percent = config.risk.DEFAULT_TP_PERCENT

                tp_price = price * (Decimal("1") + tp_percent / Decimal("100"))
            else:
                # Fallback: use grid step
                grid_step = self.state.step_size
                tp_price = price + grid_step

            # Round price to tick size
            tp_price = self._round_price(tp_price)

            # Check minimum notional
            notional = quantity * tp_price
            if notional < self.min_notional:
                logger.info(f"Partial TP notional too small ({notional:.2f}), skipping")
                return

            # Place TP order
            client_order_id = f"partial_tp_{level.index}_{level.partial_fill_count}_{int(datetime.now().timestamp())}"

            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side="SELL",
                order_type="LIMIT",
                quantity=quantity,
                price=tp_price,
                client_order_id=client_order_id,
            )

            partial_tp_order_id = response.get("orderId")

            # Track partial TP order ID
            level.partial_tp_order_ids.append(partial_tp_order_id)

            logger.info(
                f"PARTIAL TP: SELL {quantity} @ {tp_price:.4f} | "
                f"Entry: {price:.4f} | OrderID: {partial_tp_order_id} | "
                f"Partial TPs: {len(level.partial_tp_order_ids)}"
            )
            
        except AsterAPIError as e:
            logger.error(f"Failed to place partial TP order: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in partial fill handler: {e}")
    
    # =========================================================================
    # RISK MANAGEMENT
    # =========================================================================
    
    async def check_circuit_breaker(self) -> bool:
        """
        Check if circuit breaker should trigger.
        
        Circuit Breaker Conditions:
        1. Drawdown exceeds MAX_DRAWDOWN_PERCENT
        2. Balance falls below MIN_BALANCE_USDT
        
        Returns:
            True if circuit breaker triggered (bot should stop)
        """
        # Update current balance and PnL
        try:
            balances = await self.client.get_account_balance()
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            
            # Find USDT balance
            for balance in balances:
                if balance.get("asset") == config.trading.MARGIN_ASSET:
                    self.state.current_balance = Decimal(balance.get("availableBalance", "0"))
                    break
            
            # Get unrealized PnL
            for position in positions:
                if position.get("symbol") == config.trading.SYMBOL:
                    self.state.unrealized_pnl = Decimal(position.get("unRealizedProfit", "0"))
                    break
            
        except Exception as e:
            logger.error(f"Error fetching balance/position: {e}")
            return False
        
        # Check conditions
        drawdown = self.state.drawdown_percent
        
        if drawdown >= config.risk.MAX_DRAWDOWN_PERCENT:
            logger.critical(
                f"🚨 CIRCUIT BREAKER: Drawdown {drawdown:.2f}% >= "
                f"MAX {config.risk.MAX_DRAWDOWN_PERCENT}%"
            )
            return True
        
        if self.state.current_balance < config.risk.MIN_BALANCE_USDT:
            logger.critical(
                f"🚨 CIRCUIT BREAKER: Balance {self.state.current_balance} < "
                f"MIN {config.risk.MIN_BALANCE_USDT}"
            )
            return True
        
        return False
    
    async def emergency_shutdown(self) -> None:
        """
        Execute emergency shutdown procedure.
        
        1. Cancel all open orders
        2. Close all positions (optional)
        3. Stop the bot
        """
        logger.warning("🚨 EMERGENCY SHUTDOWN INITIATED")
        
        try:
            # Cancel all orders
            await self.cancel_all_orders()
            
            # Note: We don't automatically close positions here
            # as that could lock in losses. Manual intervention preferred.
            logger.warning("All orders canceled. Positions remain open for manual review.")
            
        except Exception as e:
            logger.error(f"Error during emergency shutdown: {e}")
        
        self.bot_state = BotState.STOPPED
        self._shutdown_event.set()
    
    async def pause(self) -> None:
        """
        Pause bot operations gracefully.
        
        Unlike emergency_shutdown, this:
        - Does NOT cancel existing orders (they may still fill)
        - Just stops placing new orders
        - Can be resumed with resume()
        
        Used by StrategyManager when market conditions are dangerous.
        """
        if self.bot_state == BotState.PAUSED:
            logger.info("Bot is already paused")
            return
        
        logger.warning("⏸️ BOT PAUSED - No new orders will be placed")
        self.bot_state = BotState.PAUSED
        
        await self.telegram.send_message(
            "⏸️ Bot Paused\n\n"
            "Existing orders remain active.\n"
            "Use /resume to restart operations."
        )
    
    async def resume(self) -> None:
        """
        Resume bot operations after pause.
        """
        if self.bot_state != BotState.PAUSED:
            logger.info(f"Bot is not paused (state: {self.bot_state})")
            return
        
        logger.info("▶️ BOT RESUMED - Normal operations restored")
        self.bot_state = BotState.RUNNING
        
        await self.telegram.send_message(
            "▶️ Bot Resumed\n\n"
            "Normal operations restored."
        )
    
    async def switch_grid_side(self, new_side: str) -> None:
        """
        Switch grid direction (LONG/SHORT/BOTH).
        
        Called by StrategyManager when trend score confirms new direction.
        
        Process:
        1. Cancel all existing orders
        2. Update GRID_SIDE in runtime config
        3. Get current price
        4. Recalculate grid levels
        5. Place new orders
        
        Args:
            new_side: "LONG", "SHORT", or "BOTH"
        """
        old_side = config.grid.GRID_SIDE
        
        if old_side == new_side:
            logger.info(f"Already on {new_side} side, no switch needed")
            return
        
        logger.warning(f"🔄 SWITCHING GRID SIDE: {old_side} → {new_side}")
        
        try:
            # 1. Cancel all existing orders
            await self.cancel_all_orders()
            logger.info("All orders canceled for side switch")
            
            # 2. Update runtime config (note: this doesn't persist to .env file)
            # We modify the config object directly
            config.grid.GRID_SIDE = new_side
            logger.info(f"Grid side updated to: {new_side}")
            
            # 3. Get current price
            ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
            current_price = Decimal(ticker["price"])
            logger.info(f"Current price for new grid: ${current_price:.4f}")

            # 4. Calculate dynamic grid range
            grid_range = await self.get_dynamic_grid_range(current_price)

            # 5. Recalculate grid levels
            self.state.levels = self.calculate_grid_levels(current_price, grid_range)
            self.state.entry_price = current_price

            # 6. Place new orders
            await self.place_grid_orders()

            logger.info(
                f"✅ Side switch complete: {old_side} → {new_side} | "
                f"New grid: ${self.state.lower_price:.2f} - ${self.state.upper_price:.2f} (±{grid_range:.2f}%)"
            )
            
            # Log trade event
            trade_event_logger.log_event("SIDE_SWITCH", {
                "old_side": old_side,
                "new_side": new_side,
                "price": str(current_price),
                "lower_price": str(self.state.lower_price),
                "upper_price": str(self.state.upper_price),
            })
            
        except Exception as e:
            logger.error(f"Side switch failed: {e}")
            # Attempt to restore old side on failure
            config.grid.GRID_SIDE = old_side
            await self.telegram.send_message(
                f"❌ Side Switch Failed!\n\n"
                f"Error: {e}\n"
                f"Keeping current side: {old_side}"
            )
    
    # =========================================================================
    # EVENT HANDLERS
    # =========================================================================
    
    def on_order_update(self, order_data: dict) -> None:
        """
        Handle order update from WebSocket.
        
        Order statuses:
        - NEW: Order accepted
        - PARTIALLY_FILLED: Partial fill
        - FILLED: Complete fill
        - CANCELED: Order canceled
        - EXPIRED: Order expired
        
        Args:
            order_data: Order update payload from WebSocket
        """
        order_id = order_data.get("i")  # orderId
        status = order_data.get("X")    # order status
        side = order_data.get("S")      # BUY/SELL
        price = order_data.get("p")     # price
        exec_qty = order_data.get("l")  # last executed quantity
        
        logger.debug(f"Order update: {order_id} {status} {side} @ {price}")
        
        if status in ("FILLED", "PARTIALLY_FILLED"):
            # Find the grid level for this order
            level = self.state.get_level_by_order_id(order_id)

            if level and status == "FILLED":
                fill_price = Decimal(price or "0")
                fill_qty = Decimal(exec_qty or "0")

                # Calculate slippage
                slippage = level.calculate_slippage(fill_price)
                slippage_info = f" | Slippage: {slippage:+.3f}%" if slippage != 0 else ""

                # Update position tracking based on side
                if side == "BUY":
                    # BUY filled: record entry position (handles both full and accumulated partial)
                    if level.partial_fill_count > 0:
                        # Already had partial fills, this is the final fill
                        level.add_partial_fill(fill_price, fill_qty)
                        logger.info(
                            f"Order FILLED: BUY @ {price} | Level {level.index} | "
                            f"Total Position: {level.position_quantity:.4f} @ {level.entry_price:.4f} "
                            f"({level.partial_fill_count} fills){slippage_info}"
                        )
                    else:
                        # Clean full fill
                        level.entry_price = fill_price
                        level.position_quantity = fill_qty
                        level.actual_fill_price = fill_price
                        level.state = GridLevelState.POSITION_HELD
                        logger.info(
                            f"Order FILLED: BUY @ {price} | Level {level.index} | "
                            f"Position: {fill_qty} @ {fill_price}{slippage_info}"
                        )
                    pnl = Decimal("0")

                elif side == "SELL" and level.entry_price > 0:
                    # SELL (TP) filled: calculate realized PnL
                    pnl = (fill_price - level.entry_price) * level.position_quantity
                    self.state.realized_pnl += pnl
                    logger.info(
                        f"Order FILLED: SELL @ {price} | Level {level.index} | "
                        f"Entry: {level.entry_price} | Qty: {level.position_quantity} | "
                        f"PnL: {pnl:+.4f} | Total Realized: {self.state.realized_pnl:+.4f}{slippage_info}"
                    )
                else:
                    pnl = Decimal("0")
                    logger.info(f"Order FILLED: {side} @ {price} | Level {level.index}{slippage_info}")

                # Schedule rebalancing (can't await in callback)
                asyncio.create_task(self.rebalance_on_fill(level, fill_price, fill_qty))

                # Log trade and send telegram notification
                asyncio.create_task(self._log_and_notify_fill(
                    side, price, exec_qty, level.index, pnl, slippage
                ))
            
            elif status == "PARTIALLY_FILLED":
                exec_qty_decimal = Decimal(exec_qty or "0")
                price_decimal = Decimal(price or "0")
                notional = exec_qty_decimal * price_decimal

                # Only handle partial fill if significant enough (> min_notional)
                if notional >= self.min_notional and level:
                    # Track partial fill in level (accumulate position)
                    if side == "BUY":
                        level.add_partial_fill(price_decimal, exec_qty_decimal)

                    logger.info(
                        f"Order PARTIAL: {side} @ {price} | Qty: {exec_qty} | "
                        f"Accumulated: {level.position_quantity:.4f} @ {level.entry_price:.4f} "
                        f"({level.partial_fill_count} fills)"
                    )

                    # Log trade for this partial fill
                    asyncio.create_task(self._log_and_notify_fill(
                        side, price, exec_qty, level.index, Decimal("0"), Decimal("0"),
                        is_partial=True
                    ))

                    # Schedule a partial rebalance to place TP for filled portion
                    asyncio.create_task(self._handle_partial_fill(
                        level, side, price_decimal, exec_qty_decimal
                    ))
                else:
                    logger.info(f"Partial fill too small ({notional:.2f} < {self.min_notional}), skipping")
    
    def on_position_update(self, position_data: dict) -> None:
        """Handle position update from WebSocket."""
        symbol = position_data.get("s")
        if symbol != config.trading.SYMBOL:
            return
        
        position_amt = Decimal(position_data.get("pa", "0"))
        entry_price = Decimal(position_data.get("ep", "0"))
        unrealized_pnl = Decimal(position_data.get("up", "0"))
        
        self.state.unrealized_pnl = unrealized_pnl
        
        logger.debug(
            f"Position: {position_amt} @ {entry_price:.4f} | "
            f"uPnL: {unrealized_pnl:.4f}"
        )
    
    def on_balance_update(self, balance_data: dict) -> None:
        """Handle balance update from WebSocket."""
        asset = balance_data.get("a")
        if asset != config.trading.MARGIN_ASSET:
            return
        
        wallet_balance = Decimal(balance_data.get("wb", "0"))
        cross_wallet = Decimal(balance_data.get("cw", "0"))
        
        self.state.current_balance = wallet_balance
        
        logger.debug(f"Balance: {asset} = {wallet_balance:.4f}")
    
    async def _log_and_notify_fill(
        self,
        side: str,
        price: str,
        quantity: str,
        grid_level: int,
        pnl: Decimal = Decimal("0"),
        slippage: Decimal = Decimal("0"),
        is_partial: bool = False
    ) -> None:
        """Log trade to database and send Telegram notification."""
        try:
            # Log to SQLite with PnL
            status = "PARTIALLY_FILLED" if is_partial else "FILLED"
            trade = create_trade_record(
                symbol=config.trading.SYMBOL,
                side=side,
                order_type="LIMIT",
                price=Decimal(price),
                quantity=Decimal(quantity or "0"),
                order_id=0,
                client_order_id="",
                status=status,
                grid_level=grid_level,
                pnl=pnl,
            )
            await self.trade_logger.log_trade(trade)

            # Build slippage info if significant
            slippage_str = f"\n📉 Slippage: `{slippage:+.3f}%`" if abs(slippage) > Decimal("0.01") else ""

            # Send Telegram alert with PnL info for SELL orders
            if side == "SELL" and pnl != 0:
                await self.telegram.send_message(
                    f"💰 *TP Filled!*\n\n"
                    f"📊 Side: `SELL`\n"
                    f"💵 Price: `{Decimal(price):.4f}`\n"
                    f"📦 Qty: `{Decimal(quantity or '0'):.2f}`\n"
                    f"🔢 Level: `{grid_level}`\n"
                    f"💵 PnL: `{pnl:+.4f} USDT`\n"
                    f"📈 Total Realized: `{self.state.realized_pnl:+.4f} USDT`{slippage_str}"
                )
            elif is_partial:
                # Partial fill notification (less verbose)
                await self.telegram.send_message(
                    f"📦 *Partial Fill*\n\n"
                    f"📊 Side: `{side}`\n"
                    f"💵 Price: `{Decimal(price):.4f}`\n"
                    f"📦 Qty: `{Decimal(quantity or '0'):.2f}`\n"
                    f"🔢 Level: `{grid_level}`{slippage_str}"
                )
            else:
                await self.telegram.send_order_filled(
                    side=side,
                    price=Decimal(price),
                    quantity=Decimal(quantity or "0"),
                    grid_level=grid_level,
                )
        except Exception as e:
            logger.error(f"Error logging trade: {e}")
    
    # =========================================================================
    # MAIN BOT LOOP
    # =========================================================================
    
    async def initialize(self) -> bool:
        """
        Initialize the bot before starting.
        
        Steps:
        1. Test API connection
        2. Fetch symbol information (tick size, lot size)
        3. Set leverage and margin type
        4. Get initial balance
        5. Fetch current price and calculate grid
        
        Returns:
            True if initialization successful
        """
        logger.info("=" * 60)
        logger.info("Aster DEX Grid Trading Bot - Initializing")
        logger.info("=" * 60)
        logger.info(f"Symbol: {config.trading.SYMBOL}")
        logger.info(f"Leverage: {config.trading.LEVERAGE}x")
        logger.info(f"Grid Count: {config.grid.GRID_COUNT}")
        logger.info(f"Grid Range: ±{config.grid.GRID_RANGE_PERCENT}%")
        logger.info(f"Capital: {config.INITIAL_CAPITAL_USDT} USDT")
        logger.info(f"Dry Run: {config.DRY_RUN}")
        logger.info(f"Harvest Mode: {config.harvest.HARVEST_MODE}")
        logger.info("=" * 60)
        
        try:
            # Test connection
            if not await self.client.test_connection():
                logger.error("Failed to connect to API")
                return False
            
            # Fetch exchange info for symbol constraints
            try:
                exchange_info = await self.client.get_exchange_info(config.trading.SYMBOL)
                symbols = exchange_info.get("symbols", [])
                
                for sym_info in symbols:
                    if sym_info.get("symbol") == config.trading.SYMBOL:
                        filters = sym_info.get("filters", [])
                        for f in filters:
                            if f.get("filterType") == "PRICE_FILTER":
                                self.tick_size = Decimal(f.get("tickSize", "0.0001"))
                            elif f.get("filterType") == "LOT_SIZE":
                                self.lot_size = Decimal(f.get("stepSize", "0.01"))
                            elif f.get("filterType") == "MIN_NOTIONAL":
                                self.min_notional = Decimal(f.get("notional", "5"))
                        break
                
                logger.info(f"Symbol constraints: tick={self.tick_size}, lot={self.lot_size}, minNotional={self.min_notional}")
            except Exception as e:
                logger.warning(f"Could not fetch exchange info, using defaults: {e}")
            
            # Set leverage
            try:
                await self.client.set_leverage(config.trading.SYMBOL, config.trading.LEVERAGE)
                logger.info(f"Leverage set to {config.trading.LEVERAGE}x")
            except AsterAPIError as e:
                if "No need to change" not in str(e):
                    logger.warning(f"Could not set leverage: {e}")
            
            # Set margin type
            # Note: In Multi-Asset Mode, margin type is locked to CROSSED
            # Error -4168 indicates we're in Multi-Asset mode and can't change
            try:
                await self.client.set_margin_type(config.trading.SYMBOL, config.trading.MARGIN_TYPE)
                logger.info(f"Margin type set to {config.trading.MARGIN_TYPE}")
            except AsterAPIError as e:
                if "No need to change" not in str(e) and "-4168" not in str(e):
                    logger.warning(f"Could not set margin type: {e}")
                elif "-4168" in str(e):
                    logger.info("Multi-Asset Mode detected - margin type is managed by exchange")
            
            # Get initial balance (check both USDT and USDF for Multi-Asset Mode)
            balances = await self.client.get_account_balance()
            usdt_balance = Decimal("0")
            usdf_balance = Decimal("0")
            
            for balance in balances:
                asset = balance.get("asset", "")
                if asset == "USDT":
                    usdt_balance = Decimal(balance.get("availableBalance", "0"))
                elif asset == "USDF":
                    usdf_balance = Decimal(balance.get("availableBalance", "0"))
                    
                # Set primary balance based on config
                if asset == config.trading.MARGIN_ASSET:
                    self.state.initial_balance = Decimal(balance.get("balance", "0"))
                    self.state.current_balance = Decimal(balance.get("availableBalance", "0"))
            
            logger.info(f"Initial balance: {self.state.initial_balance} {config.trading.MARGIN_ASSET}")
            
            # USDF Recommendation Warning (for Airdrop optimization)
            if usdt_balance > Decimal("10") and usdf_balance < Decimal("10"):
                logger.critical(
                    "🚨 AIRDROP ALERT: You have USDT but no USDF! "
                    "Swap USDT to USDF on AsterDEX for 20x Airdrop Multiplier!"
                )
            
            # Get current price and calculate grid
            ticker = await self.client.get_ticker_price()
            current_price = Decimal(ticker.get("price", "0"))
            
            if current_price <= 0:
                logger.error("Failed to get current price")
                return False
            
            logger.info(f"Current price: {current_price}")
            
            # Cancel all existing orders before placing new grid
            # This prevents duplicate orders on bot restart/redeploy
            logger.info("Cancelling existing orders before placing new grid...")
            await self.cancel_all_orders()
            await asyncio.sleep(1)  # Wait for orders to be cancelled

            # Calculate dynamic grid range based on ATR
            grid_range = await self.get_dynamic_grid_range(current_price)
            logger.info(f"Grid Range: ±{grid_range:.2f}% (Dynamic: {config.grid.DYNAMIC_GRID_SPACING_ENABLED})")

            # Calculate grid levels with dynamic range
            self.state.levels = self.calculate_grid_levels(current_price, grid_range)
            
            # Log grid levels
            logger.info("Grid Levels:")
            for level in self.state.levels:
                logger.info(f"  {level}")
            
            self.state.start_time = datetime.now()
            self.bot_state = BotState.RUNNING
            
            # Initialize trade logger and telegram
            await self.trade_logger.initialize()
            self._session_id = await self.trade_logger.start_session(
                config.trading.SYMBOL, str(self.state.initial_balance)
            )
            
            await self.telegram.start()
            await self.telegram_commands.start()
            await self.telegram.send_bot_started(
                symbol=config.trading.SYMBOL,
                balance=self.state.initial_balance,
                grid_count=config.grid.GRID_COUNT,
                leverage=config.trading.LEVERAGE,
            )
            
            # Start Strategy Manager
            asyncio.create_task(self.strategy_manager.start_monitoring())
            
            # Start Daily Report Scheduler
            asyncio.create_task(self._daily_report_scheduler())
            
            # Start Auto Re-Grid Monitor
            asyncio.create_task(self._auto_regrid_monitor())
            
            return True
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            return False
    
    async def run_websocket_loop(self) -> None:
        """Run WebSocket event loop for real-time updates."""
        while not self._shutdown_event.is_set():
            try:
                await self.client.subscribe_user_data(
                    on_order_update=self.on_order_update,
                    on_position_update=self.on_position_update,
                    on_balance_update=self.on_balance_update,
                )
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if not self._shutdown_event.is_set():
                    logger.info("Reconnecting WebSocket in 5 seconds...")
                    await asyncio.sleep(5)
    
    async def run_monitoring_loop(self) -> None:
        """Run periodic monitoring for circuit breaker and status."""
        while not self._shutdown_event.is_set():
            try:
                # Check circuit breaker
                if await self.check_circuit_breaker():
                    await self.emergency_shutdown()
                    return
                
                # Log status periodically
                runtime = datetime.now() - self.state.start_time if self.state.start_time else None
                logger.info(
                    f"STATUS | Balance: {self.state.current_balance:.2f} | "
                    f"uPnL: {self.state.unrealized_pnl:.4f} | "
                    f"Drawdown: {self.state.drawdown_percent:.2f}% | "
                    f"Trades: {self.state.total_trades} | "
                    f"Orders: {self.state.active_orders_count} | "
                    f"Runtime: {runtime}"
                )
                
            except Exception as e:
                logger.error(f"Monitoring error: {e}")
            
            # Send hourly summary
            if (datetime.now() - self._last_hourly_summary).total_seconds() >= 3600:
                # Build market status from strategy manager
                market_status = None
                if self.strategy_manager.last_analysis:
                    analysis = self.strategy_manager.last_analysis
                    market_status = {
                        "state": analysis.state.value,
                        "trend_score": analysis.trend_score,
                        "rsi": analysis.rsi,
                        "price": float(analysis.current_price),
                        "current_side": config.grid.GRID_SIDE,
                    }
                
                await self.telegram.send_hourly_summary(
                    trades_count=self.state.total_trades,
                    realized_pnl=self.state.realized_pnl,
                    unrealized_pnl=self.state.unrealized_pnl,
                    current_balance=self.state.current_balance,
                    active_orders=self.state.active_orders_count,
                    market_status=market_status,
                )
                self._last_hourly_summary = datetime.now()
            
            await asyncio.sleep(60)  # Check every minute
    
    async def _auto_regrid_monitor(self) -> None:
        """
        Monitor price drift and automatically reposition grid when needed.
        
        Logic:
        1. Run every REGRID_CHECK_INTERVAL_MINUTES
        2. Calculate grid center from current levels
        3. If price drift > REGRID_THRESHOLD_PERCENT, trigger re-grid
        """
        if not config.grid.AUTO_REGRID_ENABLED:
            logger.info("Auto Re-Grid is disabled")
            return
        
        interval_seconds = config.grid.REGRID_CHECK_INTERVAL_MINUTES * 60
        threshold = float(config.grid.REGRID_THRESHOLD_PERCENT)
        
        logger.info(
            f"Auto Re-Grid Monitor started: checking every {config.grid.REGRID_CHECK_INTERVAL_MINUTES} min, "
            f"threshold {threshold}%"
        )
        
        while not self._shutdown_event.is_set():
            try:
                # Wait for interval
                await asyncio.sleep(interval_seconds)
                
                if self._shutdown_event.is_set():
                    break
                
                # Calculate grid center
                if not self.state.levels:
                    continue
                
                grid_center = (self.state.lower_price + self.state.upper_price) / 2
                
                # Get current price
                ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
                current_price = Decimal(str(ticker.get("price", 0)))
                
                if current_price == 0:
                    continue
                
                # Calculate drift percentage
                drift = abs(current_price - grid_center) / grid_center * 100
                
                logger.info(
                    f"Re-Grid Check: Price ${current_price:.2f} | "
                    f"Grid Center ${grid_center:.2f} | Drift {drift:.2f}%"
                )
                
                if drift > Decimal(str(threshold)):
                    logger.warning(
                        f"🔄 RE-GRID TRIGGERED: Drift {drift:.2f}% > {threshold}%"
                    )
                    
                    # Send Telegram notification
                    await self.telegram.send_message(
                        f"🔄 Auto Re-Grid Triggered!\n"
                        f"Price: ${current_price:.2f}\n"
                        f"Grid Center: ${grid_center:.2f}\n"
                        f"Drift: {drift:.2f}%"
                    )
                    
                    # Cancel all orders
                    await self.cancel_all_orders()

                    # Calculate dynamic grid range
                    grid_range = await self.get_dynamic_grid_range(current_price)

                    # Recalculate grid centered on current price
                    self.state.entry_price = current_price
                    self.state.levels = self.calculate_grid_levels(current_price, grid_range)

                    # Place new orders
                    await self.place_grid_orders()

                    logger.info(
                        f"✅ Re-Grid complete: New grid ${self.state.lower_price:.2f} - ${self.state.upper_price:.2f} "
                        f"(Range: ±{grid_range:.2f}%)"
                    )

                    await self.telegram.send_message(
                        f"✅ Re-Grid Complete!\n"
                        f"New Grid: ${self.state.lower_price:.2f} - ${self.state.upper_price:.2f}\n"
                        f"Range: ±{grid_range:.2f}%"
                    )
                    
            except Exception as e:
                logger.error(f"Auto Re-Grid error: {e}")
    
    async def _daily_report_scheduler(self) -> None:
        """
        Send daily performance report every 24 hours.
        
        Report includes:
        - Total trades, PnL, ROI
        - Win rate, current balance
        - Runtime statistics
        """
        # Wait 24 hours before first report (or until 8:00 AM)
        while not self._shutdown_event.is_set():
            try:
                # Calculate stats
                runtime = datetime.now() - self.state.start_time if self.state.start_time else None
                runtime_hours = runtime.total_seconds() / 3600 if runtime else 0
                
                # Get win rate from trade logger
                win_rate = Decimal("0")
                try:
                    trades = await self.trade_logger.get_recent_trades(100)
                    if trades:
                        profits = [float(t.get('pnl', 0) or 0) for t in trades]
                        wins = len([p for p in profits if p > 0])
                        total = len([p for p in profits if p != 0])
                        win_rate = Decimal(str(wins / total * 100)) if total > 0 else Decimal("0")
                except Exception as e:
                    logger.debug(f"Could not calculate win rate: {e}")
                
                # Send daily report
                await self.telegram.send_daily_report(
                    symbol=config.trading.SYMBOL,
                    total_trades=self.state.total_trades,
                    realized_pnl=self.state.realized_pnl,
                    unrealized_pnl=self.state.unrealized_pnl,
                    current_balance=self.state.current_balance,
                    initial_balance=self.state.initial_balance,
                    win_rate=win_rate,
                    runtime_hours=runtime_hours,
                )
                
                logger.info("Daily report sent via Telegram")
                
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")
            
            # Wait 24 hours
            await asyncio.sleep(86400)  # 24 hours
    
    async def run(self) -> None:
        """
        Main entry point to run the bot.
        
        This method:
        1. Initializes the bot
        2. Places initial grid orders
        3. Starts WebSocket event loop
        4. Starts monitoring loop
        5. Handles graceful shutdown
        """
        # Setup signal handlers for graceful shutdown
        def signal_handler():
            logger.info("Shutdown signal received")
            self._shutdown_event.set()
        
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
        
        try:
            async with self.client:
                # Initialize
                if not await self.initialize():
                    logger.error("Initialization failed - exiting")
                    return
                
                # Place initial grid orders
                await self.place_grid_orders()
                
                # Start concurrent tasks
                ws_task = asyncio.create_task(self.run_websocket_loop())
                monitor_task = asyncio.create_task(self.run_monitoring_loop())
                
                # Wait for shutdown
                await self._shutdown_event.wait()
                
                # Cleanup
                ws_task.cancel()
                monitor_task.cancel()
                
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass
                
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            self.bot_state = BotState.ERROR
        
        finally:
            # Send shutdown notification
            await self.telegram.send_bot_stopped(
                reason="User shutdown" if self.bot_state != BotState.ERROR else "Error",
                total_trades=self.state.total_trades,
                realized_pnl=self.state.realized_pnl,
                final_balance=self.state.current_balance,
            )
            
            # End session in database
            await self.trade_logger.end_session(
                session_id=self._session_id,
                final_balance=str(self.state.current_balance),
                total_trades=self.state.total_trades,
                realized_pnl=str(self.state.realized_pnl),
                status="COMPLETED" if self.bot_state != BotState.ERROR else "ERROR",
            )
            
            # Cleanup
            await self.telegram.stop()
            await self.telegram_commands.stop()
            await self.trade_logger.close()
            
            logger.info("Bot shutdown complete")
            
            # Final status report
            logger.info("=" * 60)
            logger.info("FINAL REPORT")
            logger.info("=" * 60)
            logger.info(f"Total Trades: {self.state.total_trades}")
            logger.info(f"Final Balance: {self.state.current_balance}")
            logger.info(f"Realized PnL: {self.state.realized_pnl}")
            logger.info(f"Unrealized PnL: {self.state.unrealized_pnl}")
            if self.state.start_time:
                runtime = datetime.now() - self.state.start_time
                logger.info(f"Total Runtime: {runtime}")
            logger.info("=" * 60)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    """Entry point for the grid trading bot."""
    # Validate configuration
    errors = config.validate()
    if errors:
        print("Configuration errors:")
        for err in errors:
            print(f"  ❌ {err}")
        sys.exit(1)
    
    # Run bot
    bot = GridBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
