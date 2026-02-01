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
from indicator_analyzer import IndicatorAnalyzer, get_smart_tp, TrailingTPResult
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
    # TP tracking for ML analysis
    tp_placed_at: datetime | None = None
    tp_target_price: Decimal = Decimal("0")
    # Trailing TP (SuperTrend-based) fields
    trailing_tp_active: bool = False       # Whether trailing mode is active
    supertrend_stop: Decimal = Decimal("0")  # Current SuperTrend stop level
    highest_price_seen: Decimal = Decimal("0")   # For LONG: track highest price
    lowest_price_seen: Decimal = Decimal("999999")  # For SHORT: track lowest price
    last_tp_update: datetime | None = None  # Last time TP was updated

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
        # TP tracking for ML analysis
        self.tp_placed_at = None
        self.tp_target_price = Decimal("0")
        # Trailing TP reset
        self.trailing_tp_active = False
        self.supertrend_stop = Decimal("0")
        self.highest_price_seen = Decimal("0")
        self.lowest_price_seen = Decimal("999999")
        self.last_tp_update = None

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

    # Phase 3: Risk Management Tracking
    daily_realized_pnl: Decimal = Decimal("0")  # Resets daily
    daily_start_time: datetime | None = None
    session_high_price: Decimal = Decimal("0")  # For trailing stop

    # Timing
    start_time: datetime | None = None

    # SuperTrend flip alert cooldown tracking
    last_supertrend_flip_alert: datetime | None = None

    # Manual position close detection
    last_known_position_amt: Decimal = Decimal("0")
    
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
    def positions_count(self) -> int:
        """Count of grid levels currently holding positions."""
        return sum(
            1 for level in self.levels
            if level.state in (GridLevelState.POSITION_HELD, GridLevelState.TP_PLACED)
        )

    @property
    def daily_loss_percent(self) -> Decimal:
        """Calculate daily loss as percentage of initial balance."""
        if self.initial_balance <= 0:
            return Decimal("0")
        if self.daily_realized_pnl >= 0:
            return Decimal("0")
        return abs(self.daily_realized_pnl) / self.initial_balance * 100
    
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

        # Wait for clear signal before placing orders
        self._waiting_for_clear_signal = False
        
        # Trade logging and notifications
        self.trade_logger = TradeLogger()
        self.telegram = TelegramNotifier()
        self.telegram_commands = TelegramCommandHandler(bot_reference=self)
        self.strategy_manager = StrategyManager(self.client, bot_reference=self)
        self.indicator_analyzer = IndicatorAnalyzer()  # For trailing TP calculations
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
        - Respects MAX_POSITIONS limit (Phase 3 risk management)

        In HARVEST_MODE, initial orders may use MARKET type
        for airdrop point optimization.
        """
        # Check if waiting for clear signal
        if self._waiting_for_clear_signal:
            logger.info("⏸️ Waiting for clear signal - skipping order placement")
            return

        orders_placed = 0
        max_positions = config.risk.MAX_POSITIONS

        # Check current position count before placing new orders
        current_positions = self.state.positions_count
        if current_positions >= max_positions:
            logger.warning(
                f"Max positions reached ({current_positions}/{max_positions}), "
                f"skipping new order placement"
            )
            return

        for level in self.state.levels:
            if level.side is None:
                continue

            if level.order_id is not None:
                continue  # Already has an order

            # Check max positions limit for BUY orders (Phase 3)
            if level.side == OrderSide.BUY:
                potential_positions = self.state.positions_count + orders_placed
                if potential_positions >= max_positions:
                    logger.info(
                        f"Max positions limit ({max_positions}) reached, "
                        f"skipping remaining BUY orders"
                    )
                    break
            
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

        # Send Telegram notification for placed orders
        if orders_placed > 0:
            # Find price range of placed orders
            placed_levels = [
                level for level in self.state.levels
                if level.order_id is not None and level.state in (GridLevelState.BUY_PLACED, GridLevelState.SELL_PLACED)
            ]
            if placed_levels:
                prices = [level.price for level in placed_levels]
                price_range = (min(prices), max(prices))
                side = "BUY" if config.grid.GRID_SIDE == "LONG" else "SELL"
                await self.telegram.send_orders_placed(
                    orders_count=orders_placed,
                    side=side,
                    price_range=price_range,
                    grid_side=config.grid.GRID_SIDE,
                )
    
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

    async def close_all_positions(self) -> dict:
        """
        Close all open positions with market orders.
        Called by Telegram /close command.

        Returns:
            dict with keys:
            - success: bool
            - closed_count: int
            - total_quantity: Decimal
            - realized_pnl: Decimal
            - error: str (if failed)
        """
        result = {
            "success": False,
            "closed_count": 0,
            "total_quantity": Decimal("0"),
            "realized_pnl": Decimal("0"),
            "error": None
        }

        try:
            # First cancel all pending orders
            await self.cancel_all_orders()

            # Get actual positions from exchange
            positions = await self.client.get_position_risk(config.trading.SYMBOL)

            closed_count = 0
            total_qty = Decimal("0")
            total_pnl = Decimal("0")

            for pos in positions:
                position_amt = Decimal(pos.get("positionAmt", "0"))
                if position_amt == 0:
                    continue

                entry_price = Decimal(pos.get("entryPrice", "0"))

                # Determine close order side (opposite of position)
                close_side = "SELL" if position_amt > 0 else "BUY"
                close_qty = abs(position_amt)

                logger.info(
                    f"Closing position: {close_side} {close_qty} @ MARKET | "
                    f"Entry: ${entry_price:.4f}"
                )

                # Place market order to close
                order_result = await self.client.place_order(
                    symbol=config.trading.SYMBOL,
                    side=close_side,
                    order_type="MARKET",
                    quantity=close_qty,
                    reduce_only=True
                )

                if order_result:
                    # Get fill price from order result
                    fill_price = Decimal(order_result.get("avgPrice", "0"))
                    if fill_price == 0:
                        # Estimate from current market price
                        ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
                        fill_price = Decimal(ticker["price"])

                    # Calculate PnL
                    if position_amt > 0:  # LONG position
                        pnl = (fill_price - entry_price) * close_qty
                    else:  # SHORT position
                        pnl = (entry_price - fill_price) * close_qty

                    total_pnl += pnl
                    total_qty += close_qty
                    closed_count += 1

                    logger.info(
                        f"Position closed: {close_qty} @ ${fill_price:.4f} | "
                        f"PnL: {pnl:+.4f}"
                    )

            # Reset all grid levels that were holding positions
            for level in self.state.levels:
                if level.state in (GridLevelState.POSITION_HELD, GridLevelState.TP_PLACED):
                    level.reset()

            # Update state tracking
            self.state.realized_pnl += total_pnl
            self.state.daily_realized_pnl += total_pnl
            self.state.last_known_position_amt = Decimal("0")

            result["success"] = True
            result["closed_count"] = closed_count
            result["total_quantity"] = total_qty
            result["realized_pnl"] = total_pnl

            logger.info(
                f"All positions closed: {closed_count} positions | "
                f"Qty: {total_qty:.4f} | PnL: {total_pnl:+.4f}"
            )

        except Exception as e:
            logger.error(f"Error closing positions: {e}")
            result["error"] = str(e)

        return result

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

        This method supports two modes:
        1. Traditional Smart TP: RSI/MACD-based fixed percentage (1.0%-2.5%)
        2. Trailing TP (NEW): SuperTrend-based dynamic trailing stop

        When USE_TRAILING_TP is enabled:
        - Uses SuperTrend indicator to calculate trailing stop
        - If SuperTrend stop is above entry (LONG), use it as TP
        - If not yet profitable, use fallback fixed TP%
        - TP order will be updated periodically as price moves

        Args:
            filled_level: The grid level that just got filled (BUY)
        """
        try:
            # Get TOTAL position entry price from exchange
            # This ensures TP is always above avg entry to avoid realized loss
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            total_entry_price = Decimal("0")

            actual_position_side = None
            for pos in positions:
                if pos.get("symbol") == config.trading.SYMBOL:
                    pos_amt = Decimal(pos.get("positionAmt", "0"))
                    if pos_amt > 0:  # LONG position
                        total_entry_price = Decimal(pos.get("entryPrice", "0"))
                        actual_position_side = "LONG"
                        break
                    elif pos_amt < 0:  # SHORT position
                        total_entry_price = Decimal(pos.get("entryPrice", "0"))
                        actual_position_side = "SHORT"
                        break

            # Use total position entry if available, otherwise use level entry
            level_entry = filled_level.entry_price if filled_level.entry_price > 0 else filled_level.price

            if total_entry_price > 0:
                entry_price = total_entry_price
                logger.info(f"📊 Using TOTAL position avg entry: ${total_entry_price:.4f} (level entry: ${level_entry:.4f})")
            else:
                entry_price = level_entry
                logger.info(f"📊 No total position, using level entry: ${entry_price:.4f}")

            # Initialize tracking variables for ML logging
            rsi = 0.0
            macd_hist = 0.0
            trend = ""
            atr_percent = 0.0
            tp_mode = "fixed"  # "fixed" or "trailing"

            # Determine position side for trailing TP - use ACTUAL position side, not config
            position_side = actual_position_side if actual_position_side else ("LONG" if config.grid.GRID_SIDE == "LONG" else "SHORT")

            # Check if trailing TP is enabled
            if config.risk.USE_TRAILING_TP:
                # Fetch candles for SuperTrend calculation
                candles = await self.client.get_klines(
                    symbol=config.trading.SYMBOL,
                    interval="1h",
                    limit=100  # Need more candles for SuperTrend
                )

                if candles:
                    # Use IndicatorAnalyzer to get trailing TP recommendation
                    trailing_result = self.indicator_analyzer.get_trailing_tp(
                        candles=candles,
                        entry_price=entry_price,
                        position_side=position_side,
                        fallback_tp_percent=config.risk.FALLBACK_TP_PERCENT
                    )

                    if trailing_result.use_trailing:
                        # SuperTrend stop is profitable - use trailing mode
                        tp_price = self._round_price(trailing_result.trailing_stop)
                        tp_mode = "trailing"
                        filled_level.trailing_tp_active = True
                        filled_level.supertrend_stop = trailing_result.trailing_stop
                        logger.info(f"📈 Trailing TP (SuperTrend): ${tp_price:.4f} | {trailing_result.reason}")
                    else:
                        # SuperTrend not yet profitable - use fixed TP
                        tp_price = self._round_price(trailing_result.fixed_tp)
                        filled_level.trailing_tp_active = False
                        filled_level.supertrend_stop = trailing_result.trailing_stop
                        logger.info(f"🎯 Fixed TP (waiting for trailing): ${tp_price:.4f} | {trailing_result.reason}")

                    # Capture ATR for logging if available
                    if trailing_result.supertrend:
                        atr_percent = float(trailing_result.supertrend.atr_value / float(entry_price) * 100)
                else:
                    # No candles - use fallback
                    tp_percent = config.risk.FALLBACK_TP_PERCENT
                    if position_side == "LONG":
                        tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
                    else:  # SHORT
                        tp_price = entry_price * (Decimal("1") - tp_percent / Decimal("100"))
                    tp_price = self._round_price(tp_price)
                    logger.warning(f"No candle data for trailing TP, using fallback: {tp_percent}%")

            elif config.risk.USE_SMART_TP:
                # Original Smart TP logic (RSI/MACD-based)
                cached_analysis = self.strategy_manager.last_analysis

                if cached_analysis and cached_analysis.rsi > 0:
                    tp_percent = await get_smart_tp(market_analysis=cached_analysis)
                    logger.info(f"🧠 Smart TP (cached): {tp_percent}%")
                    rsi = cached_analysis.rsi
                    macd_hist = cached_analysis.macd_histogram
                    trend = cached_analysis.trend_direction
                    atr_percent = float(cached_analysis.atr_value / cached_analysis.current_price * 100) if cached_analysis.current_price > 0 else 0.0
                else:
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

                if position_side == "LONG":
                    tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
                else:  # SHORT
                    tp_price = entry_price * (Decimal("1") - tp_percent / Decimal("100"))
                tp_price = self._round_price(tp_price)
            else:
                tp_percent = config.risk.DEFAULT_TP_PERCENT
                if position_side == "LONG":
                    tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
                else:  # SHORT
                    tp_price = entry_price * (Decimal("1") - tp_percent / Decimal("100"))
                tp_price = self._round_price(tp_price)
                logger.info(f"Smart TP disabled, using default: {tp_percent}%")

            # Use actual position quantity, not recalculated
            quantity = filled_level.position_quantity if filled_level.position_quantity > 0 else self.calculate_quantity_for_level(entry_price)
            
            # Generate client order ID
            client_order_id = f"tp_{filled_level.index}_{int(datetime.now().timestamp())}"

            # Determine TP order side: LONG position closes with SELL, SHORT position closes with BUY
            tp_order_side = "SELL" if position_side == "LONG" else "BUY"

            # Place TP order
            response = await self.client.place_order(
                symbol=config.trading.SYMBOL,
                side=tp_order_side,
                order_type="LIMIT",
                quantity=quantity,
                price=tp_price,
                client_order_id=client_order_id,
            )

            order_id = response.get("orderId")

            # Store TP order info separately from entry order
            filled_level.tp_order_id = order_id
            filled_level.state = GridLevelState.TP_PLACED
            filled_level.side = OrderSide.SELL if position_side == "LONG" else OrderSide.BUY
            filled_level.client_order_id = client_order_id
            # Keep order_id pointing to TP for backward compatibility with get_level_by_order_id
            filled_level.order_id = order_id
            # Track TP placement time and target for ML outcome analysis
            filled_level.tp_placed_at = datetime.now()
            filled_level.tp_target_price = tp_price
            filled_level.last_tp_update = datetime.now()

            # Calculate TP percentage for logging (always show as positive profit %)
            if 'tp_percent' not in dir() or tp_percent is None:
                if position_side == "LONG":
                    tp_percent = ((tp_price - entry_price) / entry_price) * 100
                else:  # SHORT - profit when price goes down
                    tp_percent = ((entry_price - tp_price) / entry_price) * 100

            # Different log messages based on TP mode
            if tp_mode == "trailing":
                logger.info(
                    f"📈 TRAILING TP PLACED: {tp_order_side} @ ${tp_price:.4f} (+{tp_percent:.2f}%) | "
                    f"Avg Entry: ${entry_price:.4f} | Qty: {quantity} | OrderID: {order_id}"
                )
                await self.telegram.send_message(
                    f"📈 Trailing TP Placed (SuperTrend)!\n"
                    f"Position: {position_side}\n"
                    f"Avg Entry: ${entry_price:.4f}\n"
                    f"TP ({tp_order_side}): ${tp_price:.4f} (+{tp_percent:.2f}%)\n"
                    f"Qty: {quantity}\n"
                    f"Mode: Trailing (will update as price moves)"
                )
            else:
                logger.info(
                    f"🎯 SMART TP PLACED: {tp_order_side} @ ${tp_price:.4f} (+{tp_percent:.2f}%) | "
                    f"Avg Entry: ${entry_price:.4f} | Qty: {quantity} | OrderID: {order_id}"
                )
                await self.telegram.send_message(
                    f"🎯 Smart TP Placed!\n"
                    f"Position: {position_side}\n"
                    f"Avg Entry: ${entry_price:.4f}\n"
                    f"TP ({tp_order_side}): ${tp_price:.4f} (+{tp_percent:.2f}%)\n"
                    f"Qty: {quantity}\n"
                    f"(TP based on total position avg)"
                )

            # Get context data for ML logging
            btc_trend_score = 0
            if self.strategy_manager.btc_trend_score:
                btc_trend_score = self.strategy_manager.btc_trend_score.total
            funding_rate = float(self.strategy_manager.last_funding_rate * 100)
            drawdown_percent = self.state.get_drawdown_percent()

            # Log trade event with full indicator values for ML analysis
            trade_event_logger.log_smart_tp(
                entry_price=entry_price,
                tp_price=tp_price,
                tp_percent=tp_percent,
                rsi=rsi,
                macd_hist=macd_hist,
                trend=trend,
                grid_level=filled_level.index,
                position_quantity=quantity,
                atr_percent=atr_percent,
                btc_trend_score=btc_trend_score,
                funding_rate=funding_rate,
                drawdown_percent=drawdown_percent,
                order_id=str(order_id),
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
        Handle partial fill - track position but don't place TP yet.

        Approach C (Single TP): We wait until the order is fully FILLED,
        then place a single TP for the entire position. This is simpler
        and avoids over-hedging issues.

        Args:
            level: The grid level that was partially filled
            side: BUY or SELL
            price: Fill price
            quantity: Filled quantity
        """
        # Approach C: Don't place partial TPs
        # Position is already tracked via add_partial_fill() in on_order_update
        # Single TP will be placed when order is fully FILLED
        logger.info(
            f"PARTIAL FILL tracked: {side} {quantity} @ {price:.4f} | "
            f"Level {level.index} | Accumulated: {level.position_quantity:.4f} @ {level.entry_price:.4f} | "
            f"Waiting for full fill to place TP"
        )
    
    # =========================================================================
    # POSITION SYNC (for bot restart)
    # =========================================================================

    async def sync_existing_positions(self) -> int:
        """
        Sync existing positions from exchange and place TP orders.

        Called on bot startup to handle positions that were opened before
        the bot restarted. This ensures no position is left without a TP.

        Returns:
            Number of positions synced
        """
        try:
            positions = await self.client.get_position_risk(config.trading.SYMBOL)

            synced = 0
            for pos in positions:
                if pos.get("symbol") != config.trading.SYMBOL:
                    continue

                position_amt = Decimal(pos.get("positionAmt", "0"))
                entry_price = Decimal(pos.get("entryPrice", "0"))

                # Skip if no position
                if position_amt == 0 or entry_price == 0:
                    continue

                # Log when syncing position that doesn't match grid mode
                # (Still place TP to protect the position)
                if config.grid.GRID_SIDE == "LONG" and position_amt < 0:
                    logger.info(f"📋 Syncing SHORT position in LONG mode (placing protective TP)")
                elif config.grid.GRID_SIDE == "SHORT" and position_amt > 0:
                    logger.info(f"📋 Syncing LONG position in SHORT mode (placing protective TP)")

                position_qty = abs(position_amt)

                logger.info(
                    f"🔄 SYNC: Found existing position | "
                    f"Qty: {position_qty} | Entry: ${entry_price:.4f}"
                )

                # Check if TP already exists for this position
                # LONG position: TP is SELL order, SHORT position: TP is BUY order
                tp_side_to_check = "SELL" if position_amt > 0 else "BUY"
                open_orders = await self.client.get_open_orders(config.trading.SYMBOL)
                has_tp = False
                for order in open_orders:
                    # Check if there's a TP order for roughly this quantity
                    if order.get("side") == tp_side_to_check:
                        order_qty = Decimal(order.get("origQty", "0"))
                        if abs(order_qty - position_qty) < Decimal("0.01"):
                            has_tp = True
                            logger.info(f"🔄 SYNC: TP already exists for position, skipping")
                            break

                if has_tp:
                    continue

                # Calculate TP price - priority: Trailing TP > Smart TP > Default
                tp_price = None
                tp_percent = None
                tp_mode = "default"

                # Determine position side for Trailing TP
                position_side = "LONG" if position_amt > 0 else "SHORT"

                # 1. Try Trailing TP (SuperTrend) first
                if config.risk.USE_TRAILING_TP:
                    candles = await self.client.get_klines(
                        symbol=config.trading.SYMBOL,
                        interval="1h",
                        limit=100
                    )
                    if candles:
                        trailing_result = self.indicator_analyzer.get_trailing_tp(
                            candles=candles,
                            entry_price=entry_price,
                            position_side=position_side,
                            fallback_tp_percent=config.risk.FALLBACK_TP_PERCENT
                        )
                        if trailing_result.use_trailing:
                            tp_price = self._round_price(trailing_result.trailing_stop)
                            tp_mode = "trailing"
                            logger.info(f"📈 SYNC Trailing TP (SuperTrend): ${tp_price:.4f} | {trailing_result.reason}")
                        else:
                            tp_price = self._round_price(trailing_result.fixed_tp)
                            tp_mode = "fixed"
                            logger.info(f"🎯 SYNC Fixed TP (waiting for trailing): ${tp_price:.4f} | {trailing_result.reason}")

                # 2. Fallback to Smart TP if Trailing TP not available
                if tp_price is None and config.risk.USE_SMART_TP and config.risk.AUTO_TP_ENABLED:
                    cached_analysis = self.strategy_manager.last_analysis
                    if cached_analysis and cached_analysis.rsi > 0:
                        tp_percent = await get_smart_tp(market_analysis=cached_analysis)
                    else:
                        tp_percent = config.risk.DEFAULT_TP_PERCENT
                    tp_mode = "smart"

                # 3. Final fallback to default
                if tp_price is None and tp_percent is None:
                    tp_percent = config.risk.DEFAULT_TP_PERCENT
                    tp_mode = "default"

                # Calculate TP price from percent if not set by Trailing TP
                if tp_price is None:
                    # LONG: TP above entry (SELL higher), SHORT: TP below entry (BUY lower)
                    if position_amt > 0:
                        tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
                    else:
                        tp_price = entry_price * (Decimal("1") - tp_percent / Decimal("100"))
                    tp_price = self._round_price(tp_price)

                # Place TP order
                client_order_id = f"sync_tp_{int(datetime.now().timestamp())}"

                response = await self.client.place_order(
                    symbol=config.trading.SYMBOL,
                    side="SELL" if position_amt > 0 else "BUY",
                    order_type="LIMIT",
                    quantity=position_qty,
                    price=tp_price,
                    client_order_id=client_order_id,
                )

                order_id = response.get("orderId")
                tp_side = "SELL" if position_amt > 0 else "BUY"
                tp_sign = "+" if position_amt > 0 else "-"

                # Calculate TP distance for logging
                if tp_percent is not None:
                    tp_info = f"{tp_sign}{tp_percent}%"
                else:
                    tp_distance = abs((tp_price - entry_price) / entry_price * 100)
                    tp_info = f"{tp_sign}{tp_distance:.2f}% ({tp_mode})"

                logger.info(
                    f"🎯 SYNC TP PLACED: {tp_side} @ ${tp_price:.4f} ({tp_info}) | "
                    f"Avg Entry: ${entry_price:.4f} | Qty: {position_qty} | OrderID: {order_id}"
                )

                # Send Telegram notification
                await self.telegram.send_message(
                    f"🔄 Synced Existing Position ({position_side})\n\n"
                    f"Avg Entry: ${entry_price:.4f}\n"
                    f"TP ({tp_side}): ${tp_price:.4f} ({tp_info})\n"
                    f"Qty: {position_qty}\n"
                    f"Mode: {tp_mode.capitalize()}\n\n"
                    f"TP based on total position avg entry"
                )

                synced += 1

            if synced > 0:
                logger.info(f"🔄 SYNC: Placed TP for {synced} existing position(s)")
            else:
                logger.info("🔄 SYNC: No existing positions to sync")

            return synced

        except AsterAPIError as e:
            logger.error(f"Failed to sync existing positions: {e}")
            return 0
        except Exception as e:
            logger.error(f"Unexpected error syncing positions: {e}")
            return 0

    # =========================================================================
    # RISK MANAGEMENT
    # =========================================================================

    async def check_circuit_breaker(self) -> bool:
        """
        Check if circuit breaker should trigger.

        Circuit Breaker Conditions (Phase 3):
        1. Drawdown exceeds MAX_DRAWDOWN_PERCENT (20%)
        2. Daily loss exceeds DAILY_LOSS_LIMIT_PERCENT (10%)
        3. Trailing stop triggered (price dropped 8% from session high)
        4. Balance falls below MIN_BALANCE_USDT

        Returns:
            True if circuit breaker triggered (bot should stop)
        """
        # Update current balance and PnL
        try:
            balances = await self.client.get_account_balance()
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            current_price = Decimal("0")

            # Find balance - use 'balance' (wallet balance) not 'availableBalance'
            # availableBalance is reduced by margin locked for pending orders
            for balance in balances:
                if balance.get("asset") == config.trading.MARGIN_ASSET:
                    self.state.current_balance = Decimal(balance.get("balance", "0"))
                    break

            # Get unrealized PnL and current price
            for position in positions:
                if position.get("symbol") == config.trading.SYMBOL:
                    self.state.unrealized_pnl = Decimal(position.get("unRealizedProfit", "0"))
                    mark_price = position.get("markPrice", "0")
                    if mark_price:
                        current_price = Decimal(mark_price)
                    break

            # Update session high price for trailing stop
            if current_price > self.state.session_high_price:
                self.state.session_high_price = current_price

        except Exception as e:
            logger.error(f"Error fetching balance/position: {e}")
            return False

        # Check 1: Max Drawdown
        drawdown = self.state.drawdown_percent
        if drawdown >= config.risk.MAX_DRAWDOWN_PERCENT:
            logger.critical(
                f"🚨 CIRCUIT BREAKER: Drawdown {drawdown:.2f}% >= "
                f"MAX {config.risk.MAX_DRAWDOWN_PERCENT}%"
            )
            await self.telegram.send_message(
                f"🚨 *CIRCUIT BREAKER TRIGGERED*\n\n"
                f"Reason: Max Drawdown\n"
                f"Drawdown: {drawdown:.2f}%\n"
                f"Limit: {config.risk.MAX_DRAWDOWN_PERCENT}%\n\n"
                f"Bot is stopping to protect capital."
            )
            return True

        # Check 2: Daily Loss Limit
        daily_loss = self.state.daily_loss_percent
        if daily_loss >= config.risk.DAILY_LOSS_LIMIT_PERCENT:
            logger.critical(
                f"🚨 DAILY LOSS LIMIT: Loss {daily_loss:.2f}% >= "
                f"LIMIT {config.risk.DAILY_LOSS_LIMIT_PERCENT}%"
            )
            await self.telegram.send_message(
                f"🚨 *DAILY LOSS LIMIT REACHED*\n\n"
                f"Daily Loss: {daily_loss:.2f}%\n"
                f"Limit: {config.risk.DAILY_LOSS_LIMIT_PERCENT}%\n\n"
                f"Bot pausing until tomorrow."
            )
            # Pause instead of full stop for daily limit
            await self.pause()
            return False  # Don't trigger full shutdown

        # Check 3: Trailing Stop
        if (
            config.risk.TRAILING_STOP_PERCENT
            and self.state.session_high_price > 0
            and current_price > 0
            and self.state.positions_count > 0  # Only if we have positions
        ):
            drop_from_high = (
                (self.state.session_high_price - current_price)
                / self.state.session_high_price * 100
            )
            if drop_from_high >= config.risk.TRAILING_STOP_PERCENT:
                logger.critical(
                    f"🚨 TRAILING STOP: Price dropped {drop_from_high:.2f}% from high "
                    f"${self.state.session_high_price:.2f}"
                )
                await self.telegram.send_message(
                    f"🚨 *TRAILING STOP TRIGGERED*\n\n"
                    f"Session High: ${self.state.session_high_price:.2f}\n"
                    f"Current: ${current_price:.2f}\n"
                    f"Drop: {drop_from_high:.2f}%\n"
                    f"Limit: {config.risk.TRAILING_STOP_PERCENT}%\n\n"
                    f"Closing all positions to protect profits."
                )
                # For trailing stop, we might want to close positions
                # For now, just trigger emergency shutdown
                return True

        # Check 4: Minimum Balance
        if self.state.current_balance < config.risk.MIN_BALANCE_USDT:
            logger.critical(
                f"🚨 CIRCUIT BREAKER: Balance {self.state.current_balance} < "
                f"MIN {config.risk.MIN_BALANCE_USDT}"
            )
            await self.telegram.send_message(
                f"🚨 *MINIMUM BALANCE REACHED*\n\n"
                f"Balance: {self.state.current_balance:.2f}\n"
                f"Minimum: {config.risk.MIN_BALANCE_USDT}\n\n"
                f"Bot is stopping."
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

    @property
    def is_paused(self) -> bool:
        """Check if bot is currently paused."""
        return self.bot_state == BotState.PAUSED

    async def pause_buying(self) -> None:
        """
        Pause only BUY orders while keeping existing TP (SELL) orders active.

        Used by Drawdown Management to prevent accumulating more positions
        while still allowing profitable exits via TP orders.
        """
        logger.warning("⏸️ PAUSE BUYING - Cancelling BUY orders only, keeping TP orders")

        try:
            # Get all open orders
            open_orders = await self.client.get_open_orders(config.trading.SYMBOL)

            cancelled_count = 0
            kept_count = 0

            for order in open_orders:
                order_side = order.get("side", "")
                order_id = order.get("orderId")

                # Only cancel BUY orders
                if order_side == "BUY" and order_id:
                    try:
                        await self.client.cancel_order(
                            symbol=config.trading.SYMBOL,
                            order_id=order_id
                        )
                        cancelled_count += 1

                        # Update grid level state
                        level = self.state.get_level_by_order_id(order_id)
                        if level:
                            level.order_id = None
                            level.state = GridLevelState.EMPTY

                    except Exception as e:
                        logger.error(f"Failed to cancel BUY order {order_id}: {e}")
                else:
                    kept_count += 1

            logger.info(f"Pause buying complete: cancelled {cancelled_count} BUY orders, kept {kept_count} TP orders")

            # Set bot to paused state to prevent new orders
            self.bot_state = BotState.PAUSED

        except Exception as e:
            logger.error(f"Error during pause_buying: {e}")

    async def _smart_startup_side_check(self) -> None:
        """
        Smart startup: Determine optimal grid side from market analysis.

        Called during initialization BEFORE placing grid orders.
        Sets grid side dynamically based on:
        1. Real-time market analysis (trend score)
        2. Existing position constraints

        Config GRID_SIDE is used only as fallback when analysis is unavailable.
        """
        logger.info("🔍 Smart Startup: Determining optimal grid side from analysis...")

        try:
            # Run market analysis
            analysis = await self.strategy_manager.analyze_market(config.trading.SYMBOL)
            trend_score = self.strategy_manager.current_trend_score

            if not trend_score:
                logger.warning("Smart Startup: No trend score available, using config fallback")
                logger.info(f"Smart Startup: Grid side = {config.grid.GRID_SIDE} (from config)")
                return

            # Determine optimal side from analysis
            config_side = config.grid.GRID_SIDE  # Fallback only
            analysis_side = trend_score.recommended_side

            logger.info(
                f"Smart Startup: Analysis Score={trend_score.total:+d} "
                f"(EMA:{trend_score.ema_score:+d} MACD:{trend_score.macd_score:+d} "
                f"RSI:{trend_score.rsi_score:+d} Vol:{trend_score.volume_score:+d})"
            )

            # If analysis is unclear (STAY), wait for clear signal
            if analysis_side == "STAY":
                self._waiting_for_clear_signal = True
                logger.info(f"Smart Startup: Analysis unclear (score={trend_score.total}), waiting for clear signal...")
                logger.info("Smart Startup: ⏸️ No orders will be placed until trend is clear (score ≥+2 or ≤-2)")
                await self.telegram.send_message(
                    f"⏸️ Waiting for Clear Signal\n\n"
                    f"Trend Score: {trend_score.total:+d} (unclear)\n"
                    f"EMA:{trend_score.ema_score:+d} MACD:{trend_score.macd_score:+d} "
                    f"RSI:{trend_score.rsi_score:+d} Vol:{trend_score.volume_score:+d}\n\n"
                    f"No orders placed. Analyzing every 5 min..."
                )
                return  # Exit early, don't set grid side yet
            else:
                optimal_side = analysis_side
                logger.info(f"Smart Startup: Analysis recommends {optimal_side}")

            # Check for existing positions that might force a different side
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            position_amt = Decimal("0")

            for pos in positions:
                if pos.get("symbol") == config.trading.SYMBOL:
                    position_amt = Decimal(pos.get("positionAmt", "0"))
                    break

            # If we have an existing position, we must match its side
            if position_amt > 0:
                # Existing LONG position - must stay LONG
                if optimal_side == "SHORT":
                    logger.warning(
                        f"Smart Startup: ⛔ Existing LONG position ({position_amt}) "
                        f"forces LONG instead of recommended SHORT"
                    )
                optimal_side = "LONG"
            elif position_amt < 0:
                # Existing SHORT position - must stay SHORT
                if optimal_side == "LONG":
                    logger.warning(
                        f"Smart Startup: ⛔ Existing SHORT position ({position_amt}) "
                        f"forces SHORT instead of recommended LONG"
                    )
                optimal_side = "SHORT"

            # Set the grid side
            config.grid.GRID_SIDE = optimal_side

            await self.telegram.send_message(
                f"🚀 Dynamic Grid Initialization\n\n"
                f"Grid Side: {optimal_side}\n"
                f"Trend Score: {trend_score.total:+d}\n"
                f"Position: {position_amt if position_amt != 0 else 'None'}\n\n"
                f"Side determined by real-time analysis, not config."
            )

            logger.info(f"Smart Startup: ✅ Grid side set to {optimal_side}")

        except Exception as e:
            logger.error(f"Smart Startup check failed: {e}")
            logger.info(f"Falling back to config grid side: {config.grid.GRID_SIDE}")

    async def _send_switch_blocked_alert(
        self,
        pos_side: str,
        new_side: str,
        position_amt: Decimal,
        entry_price: Decimal,
        pos: dict
    ) -> None:
        """
        Send detailed alert when side switch is blocked by existing position.

        Provides actionable information for manual decision:
        - Current position details
        - TP order status if exists
        - Unrealized PnL
        - Options for user
        """
        symbol = config.trading.SYMBOL

        # Get unrealized PnL from position data
        unrealized_pnl = Decimal(str(pos.get("unrealizedProfit", 0)))

        # Calculate PnL percentage
        position_value = entry_price * position_amt
        pnl_percent = (unrealized_pnl / position_value * 100) if position_value > 0 else Decimal(0)

        # Get trend score if available
        trend_score_str = "N/A"
        if hasattr(self, 'strategy_manager') and self.strategy_manager:
            if hasattr(self.strategy_manager, 'current_trend_score') and self.strategy_manager.current_trend_score:
                trend_score_str = f"{self.strategy_manager.current_trend_score.total:+d}"

        # Get TP order info
        tp_info = "No TP order found ⚠️"
        try:
            open_orders = await self.client.get_open_orders(symbol)
            tp_side = "SELL" if pos_side == "LONG" else "BUY"

            for order in open_orders:
                if order.get("side") == tp_side:
                    tp_price = Decimal(str(order.get("price", 0)))
                    if entry_price > 0:
                        tp_percent = ((tp_price - entry_price) / entry_price * 100)
                        tp_info = f"TP @ ${tp_price:.2f} ({tp_percent:+.2f}%)"
                    else:
                        tp_info = f"TP @ ${tp_price:.2f}"
                    break
        except Exception as e:
            logger.warning(f"Failed to get TP order info: {e}")

        # Log the blocked switch
        logger.warning(
            f"🚫 BLOCKED SIDE SWITCH: Have {pos_side} position {position_amt} @ ${entry_price:.4f}. "
            f"Cannot switch to {new_side} - would cause realized loss! Wait for TP to fill first."
        )

        # Send improved alert with actionable info
        alert_msg = (
            f"⚠️ **Side Switch Blocked**\n\n"
            f"Current: {pos_side} position ({position_amt} {symbol[:3]} @ ${entry_price:.2f})\n"
            f"Recommended: Switch to {new_side} (score: {trend_score_str})\n\n"
            f"TP Status: {tp_info}\n"
            f"Position PnL: ${unrealized_pnl:.2f} ({pnl_percent:+.2f}%)\n\n"
            f"**Options:**\n"
            f"• Wait for TP to fill, then switch happens automatically\n"
            f"• Manually close position on Aster DEX if urgent"
        )

        await self.telegram.send_message(alert_msg)

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

        # CRITICAL: Check for existing positions before switching
        # Switching while holding position causes realized losses
        try:
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            for pos in positions:
                if pos.get("symbol") == config.trading.SYMBOL:
                    position_amt = Decimal(pos.get("positionAmt", "0"))
                    entry_price = Decimal(pos.get("entryPrice", "0"))

                    # Block switch if we have LONG position and switching to SHORT (or vice versa)
                    if old_side == "LONG" and new_side == "SHORT" and position_amt > 0:
                        # Get detailed info for improved alert
                        await self._send_switch_blocked_alert(
                            pos_side="LONG",
                            new_side=new_side,
                            position_amt=position_amt,
                            entry_price=entry_price,
                            pos=pos
                        )
                        return

                    if old_side == "SHORT" and new_side == "LONG" and position_amt < 0:
                        # Get detailed info for improved alert
                        await self._send_switch_blocked_alert(
                            pos_side="SHORT",
                            new_side=new_side,
                            position_amt=abs(position_amt),
                            entry_price=entry_price,
                            pos=pos
                        )
                        return
                    break
        except Exception as e:
            logger.error(f"Failed to check positions before switch: {e}")
            # Continue with switch if we can't check (fail-open for flexibility)

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
                    self.state.daily_realized_pnl += pnl  # Track daily PnL
                    logger.info(
                        f"Order FILLED: SELL @ {price} | Level {level.index} | "
                        f"Entry: {level.entry_price} | Qty: {level.position_quantity} | "
                        f"PnL: {pnl:+.4f} | Total Realized: {self.state.realized_pnl:+.4f} | "
                        f"Daily: {self.state.daily_realized_pnl:+.4f}{slippage_info}"
                    )

                    # Log TP outcome for ML analysis
                    time_to_fill = 0.0
                    if level.tp_placed_at:
                        time_to_fill = (datetime.now() - level.tp_placed_at).total_seconds()
                    trade_event_logger.log_tp_filled(
                        entry_price=level.entry_price,
                        tp_target_price=level.tp_target_price if level.tp_target_price > 0 else fill_price,
                        actual_fill_price=fill_price,
                        quantity=level.position_quantity,
                        realized_pnl=pnl,
                        time_to_fill_seconds=time_to_fill,
                        grid_level=level.index,
                        slippage_percent=float(slippage),
                        order_id=str(order_id),
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
        """
        Handle position update from WebSocket.

        Detects manual position closes (when position goes from non-zero to zero)
        and resets grid levels accordingly.
        """
        symbol = position_data.get("s")
        if symbol != config.trading.SYMBOL:
            return

        position_amt = Decimal(position_data.get("pa", "0"))
        entry_price = Decimal(position_data.get("ep", "0"))
        unrealized_pnl = Decimal(position_data.get("up", "0"))

        self.state.unrealized_pnl = unrealized_pnl

        # Detect manual position close (position went from non-zero to zero)
        last_known = self.state.last_known_position_amt
        if last_known != 0 and position_amt == 0:
            logger.warning(
                f"⚠️ Position closed externally | "
                f"Previous: {last_known} → Current: 0"
            )
            # Schedule grid reset (can't await in callback)
            asyncio.create_task(self._handle_external_position_close())

        # Update last known position amount
        self.state.last_known_position_amt = position_amt

        logger.debug(
            f"Position: {position_amt} @ {entry_price:.4f} | "
            f"uPnL: {unrealized_pnl:.4f}"
        )

    async def _handle_external_position_close(self) -> None:
        """
        Handle external position close (manual close on exchange).

        Resets grid levels that were holding positions and optionally
        re-places buy orders to reach max orders limit.
        """
        try:
            # Count levels that need to be reset
            levels_to_reset = [
                level for level in self.state.levels
                if level.state in (GridLevelState.POSITION_HELD, GridLevelState.TP_PLACED)
            ]

            if not levels_to_reset:
                logger.info("No grid levels to reset after external close")
                return

            logger.info(f"Resetting {len(levels_to_reset)} grid levels after external close")

            # Reset grid levels
            for level in levels_to_reset:
                level.reset()

            # Notify via Telegram
            await self.telegram.send_message(
                f"🔄 *External Position Close Detected*\n\n"
                f"Positions closed: `{len(levels_to_reset)}`\n"
                f"Grid levels reset to EMPTY\n\n"
                f"Bot will place new orders on next cycle."
            )

            # Trigger a re-check to place new orders
            # This ensures bot reaches max order limit
            await asyncio.sleep(2)  # Brief delay for exchange to update
            await self._ensure_max_orders()

        except Exception as e:
            logger.error(f"Error handling external position close: {e}")

    async def _ensure_max_orders(self) -> None:
        """
        Ensure bot has placed orders up to max limit.

        Called after external position close to re-fill empty grid levels.
        """
        try:
            # Count current active orders
            active_orders = sum(1 for level in self.state.levels if level.order_id is not None)
            max_orders = config.grid.MAX_OPEN_ORDERS

            if active_orders >= max_orders:
                logger.debug(f"Already at max orders: {active_orders}/{max_orders}")
                return

            # Get current price
            ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
            current_price = Decimal(ticker["price"])

            # Find empty levels that should have orders
            empty_levels = [
                level for level in self.state.levels
                if level.state == GridLevelState.EMPTY and level.order_id is None
            ]

            # Sort by distance to current price (closest first)
            empty_levels.sort(key=lambda l: abs(l.price - current_price))

            orders_to_place = min(len(empty_levels), max_orders - active_orders)

            if orders_to_place <= 0:
                return

            logger.info(f"Placing {orders_to_place} new orders after external close")

            placed = 0
            for level in empty_levels[:orders_to_place]:
                try:
                    # Determine side based on grid configuration
                    if config.grid.GRID_SIDE == "LONG":
                        # For LONG grid, buy below current price
                        if level.price < current_price:
                            await self._place_grid_order(level, "BUY")
                            placed += 1
                    else:
                        # For SHORT grid, sell above current price
                        if level.price > current_price:
                            await self._place_grid_order(level, "SELL")
                            placed += 1
                except Exception as e:
                    logger.warning(f"Failed to place order at level {level.index}: {e}")

            if placed > 0:
                logger.info(f"Placed {placed} new orders")
                # Find price range of placed orders
                placed_levels = [
                    level for level in self.state.levels
                    if level.order_id is not None and level.state in (GridLevelState.BUY_PLACED, GridLevelState.SELL_PLACED)
                ]
                if placed_levels:
                    prices = [level.price for level in placed_levels]
                    price_range = (min(prices), max(prices))
                    side = "BUY" if config.grid.GRID_SIDE == "LONG" else "SELL"
                    await self.telegram.send_orders_placed(
                        orders_count=placed,
                        side=side,
                        price_range=price_range,
                        grid_side=config.grid.GRID_SIDE,
                    )

        except Exception as e:
            logger.error(f"Error ensuring max orders: {e}")
    
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
        logger.info(f"Dry Run: {config.DRY_RUN}")
        logger.info(f"Harvest Mode: {config.harvest.HARVEST_MODE}")
        logger.info("--- Phase 3: Risk Management ---")
        logger.info(f"Max Drawdown: {config.risk.MAX_DRAWDOWN_PERCENT}%")
        logger.info(f"Daily Loss Limit: {config.risk.DAILY_LOSS_LIMIT_PERCENT}%")
        logger.info(f"Max Positions: {config.risk.MAX_POSITIONS}")
        logger.info(f"Trailing Stop: {config.risk.TRAILING_STOP_PERCENT}%")
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
            
            # Get current price first
            ticker = await self.client.get_ticker_price()
            current_price = Decimal(ticker.get("price", "0"))

            if current_price <= 0:
                logger.error("Failed to get current price")
                return False

            logger.info(f"Current price: {current_price}")

            # Cancel all existing orders FIRST before querying balance
            # This releases any margin locked up in pending orders
            # Gives accurate picture of available funds for new grid
            logger.info("Cancelling existing orders before placing new grid...")
            await self.cancel_all_orders()
            await asyncio.sleep(2)  # Wait for orders to be cancelled and margin released

            # Get initial balance AFTER cancelling orders (check both USDT and USDF for Multi-Asset Mode)
            # This ensures we see the actual available balance after margin is released
            logger.info("Querying actual balance after order cancellation...")
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

            # Initialize session tracking for Phase 3 risk management
            self.state.session_high_price = current_price
            self.state.daily_start_time = datetime.now()
            self.state.daily_realized_pnl = Decimal("0")

            # Initialize position tracking for external close detection
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            for pos in positions:
                position_amt = Decimal(pos.get("positionAmt", "0"))
                if position_amt != 0:
                    self.state.last_known_position_amt = position_amt
                    logger.info(f"Existing position detected: {position_amt}")
                    break

            # Smart Startup: Check if we should switch grid side immediately
            # This runs analysis and switches if no position is blocking
            await self._smart_startup_side_check()

            # Calculate dynamic grid range based on ATR
            grid_range = await self.get_dynamic_grid_range(current_price)
            logger.info(f"Grid Range: ±{grid_range:.2f}% (Dynamic: {config.grid.DYNAMIC_GRID_SPACING_ENABLED})")

            # Calculate grid levels with dynamic range
            self.state.levels = self.calculate_grid_levels(current_price, grid_range)
            
            # Log grid levels
            logger.info("Grid Levels:")
            for level in self.state.levels:
                logger.info(f"  {level}")

            # Sync existing positions from exchange (place TP for positions from before restart)
            logger.info("Syncing existing positions from exchange...")
            await self.sync_existing_positions()

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

            # Start Clear Signal Monitor (if waiting)
            if self._waiting_for_clear_signal:
                asyncio.create_task(self._wait_for_clear_signal_monitor())
            
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
                # Phase 3: Check if daily reset is needed (24 hours passed)
                if self.state.daily_start_time:
                    hours_elapsed = (datetime.now() - self.state.daily_start_time).total_seconds() / 3600
                    if hours_elapsed >= 24:
                        old_daily_pnl = self.state.daily_realized_pnl
                        self.state.daily_realized_pnl = Decimal("0")
                        self.state.daily_start_time = datetime.now()
                        logger.info(
                            f"📅 Daily PnL Reset: {old_daily_pnl:+.4f} USDT → 0 | "
                            f"New day started"
                        )
                        # Resume if was paused due to daily loss limit
                        if self.bot_state == BotState.PAUSED:
                            await self.resume()

                # Check circuit breaker
                if await self.check_circuit_breaker():
                    await self.emergency_shutdown()
                    return

                # Update trailing TP orders (SuperTrend-based)
                await self._update_trailing_tp_orders()

                # Log status periodically with Phase 3 risk metrics
                runtime = datetime.now() - self.state.start_time if self.state.start_time else None
                logger.info(
                    f"STATUS | Balance: {self.state.current_balance:.2f} | "
                    f"uPnL: {self.state.unrealized_pnl:.4f} | "
                    f"Drawdown: {self.state.drawdown_percent:.2f}% | "
                    f"Daily: {self.state.daily_realized_pnl:+.4f} | "
                    f"Positions: {self.state.positions_count}/{config.risk.MAX_POSITIONS} | "
                    f"High: ${self.state.session_high_price:.2f} | "
                    f"Trades: {self.state.total_trades} | "
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

                    # Determine market regime and recommendation
                    volume_ratio = getattr(analysis, 'volume_ratio', 0.0)
                    trend_score = analysis.trend_score
                    atr_percent = getattr(analysis, 'atr_percent', 0.0)

                    # Market regime detection
                    if atr_percent > 5:
                        market_regime = "High Volatility"
                        recommendation = "Widen grid or pause"
                    elif abs(trend_score) >= 3:
                        market_regime = "Strong Trend"
                        recommendation = "Follow trend"
                    elif abs(trend_score) >= 2:
                        market_regime = "Trending"
                        recommendation = "Grid optimal"
                    elif volume_ratio < 0.5:
                        market_regime = "Choppy (Low Vol)"
                        recommendation = "Reduce exposure"
                    else:
                        market_regime = "Ranging"
                        recommendation = "Grid optimal"

                    market_status = {
                        "state": analysis.state.value,
                        "trend_score": analysis.trend_score,
                        "rsi": analysis.rsi,
                        "price": float(analysis.current_price),
                        "current_side": config.grid.GRID_SIDE,
                        "volume_ratio": volume_ratio,
                        "atr_percent": atr_percent,
                        "market_regime": market_regime,
                        "recommendation": recommendation,
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
    
    async def _wait_for_clear_signal_monitor(self) -> None:
        """
        Monitor market until trend becomes clear, then start placing orders.

        Logic:
        1. Run every 5 minutes (CONFIRMATION_CHECK_INTERVAL)
        2. Re-analyze market
        3. If signal becomes clear (score ≥+2 or ≤-2), set grid side and place orders
        """
        interval_seconds = config.grid.CONFIRMATION_CHECK_INTERVAL  # 5 minutes

        logger.info(f"Clear Signal Monitor started: checking every {interval_seconds // 60} min")

        while self._waiting_for_clear_signal and not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval_seconds)

                if self._shutdown_event.is_set():
                    break

                # Re-analyze market
                analysis = await self.strategy_manager.analyze_market(config.trading.SYMBOL)
                trend_score = self.strategy_manager.current_trend_score

                if not trend_score:
                    logger.warning("Clear Signal Monitor: No trend score, continuing to wait...")
                    continue

                logger.info(
                    f"Clear Signal Monitor: Score={trend_score.total:+d} "
                    f"(EMA:{trend_score.ema_score:+d} MACD:{trend_score.macd_score:+d} "
                    f"RSI:{trend_score.rsi_score:+d} Vol:{trend_score.volume_score:+d})"
                )

                # Check if signal is now clear
                if trend_score.total >= 2:
                    optimal_side = "LONG"
                elif trend_score.total <= -2:
                    optimal_side = "SHORT"
                else:
                    logger.info(f"Clear Signal Monitor: Still unclear, waiting...")
                    continue

                # Signal is clear! Start placing orders
                logger.info(f"✅ Clear Signal Monitor: Signal clear! Setting grid to {optimal_side}")
                self._waiting_for_clear_signal = False
                config.grid.GRID_SIDE = optimal_side

                await self.telegram.send_message(
                    f"✅ Clear Signal Detected!\n\n"
                    f"Trend Score: {trend_score.total:+d}\n"
                    f"Grid Side: {optimal_side}\n\n"
                    f"Placing orders now..."
                )

                # Recalculate grid with new side and place orders
                ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
                current_price = Decimal(str(ticker.get("price", 0)))

                # Calculate dynamic grid range
                grid_range = await self.get_dynamic_grid_range(current_price)

                # Recalculate grid levels
                self.state.entry_price = current_price
                self.state.levels = self.calculate_grid_levels(current_price, grid_range)

                # Place orders
                await self.place_grid_orders()

                logger.info(f"Clear Signal Monitor: Orders placed, monitor stopping")
                break

            except Exception as e:
                logger.error(f"Clear Signal Monitor error: {e}")
                await asyncio.sleep(60)

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

    async def _update_trailing_tp_orders(self) -> None:
        """
        Update trailing TP orders based on current SuperTrend values.

        This method runs periodically (every TRAILING_TP_UPDATE_INTERVAL) and:
        1. Finds all levels with active trailing TP
        2. Recalculates SuperTrend
        3. If new stop is better (higher for LONG), updates the TP order
        4. If SuperTrend direction flips, triggers immediate exit

        Called from run_monitoring_loop.
        """
        if not config.risk.USE_TRAILING_TP:
            return

        update_interval = config.risk.TRAILING_TP_UPDATE_INTERVAL

        # Find levels with TP orders that need updating
        levels_to_update = [
            level for level in self.state.levels
            if level.state == GridLevelState.TP_PLACED
            and level.tp_order_id is not None
        ]

        if not levels_to_update:
            return

        # Check if enough time has passed since last update
        now = datetime.now()
        levels_due_for_update = []

        for level in levels_to_update:
            if level.last_tp_update is None:
                levels_due_for_update.append(level)
            else:
                seconds_since_update = (now - level.last_tp_update).total_seconds()
                if seconds_since_update >= update_interval:
                    levels_due_for_update.append(level)

        if not levels_due_for_update:
            return

        try:
            # Fetch candles for SuperTrend calculation
            candles = await self.client.get_klines(
                symbol=config.trading.SYMBOL,
                interval="1h",
                limit=100
            )

            if not candles:
                logger.warning("No candle data for trailing TP update")
                return

            # Calculate current SuperTrend
            supertrend = self.indicator_analyzer.calculate_supertrend(
                candles=candles,
                length=config.risk.SUPERTREND_LENGTH,
                multiplier=config.risk.SUPERTREND_MULTIPLIER
            )

            if not supertrend:
                logger.warning("SuperTrend calculation failed for trailing TP update")
                return

            # Get current price for direction flip check
            ticker = await self.client.get_ticker_price(config.trading.SYMBOL)
            current_price = Decimal(ticker["price"])

            # Determine actual position side from exchange (not config)
            positions = await self.client.get_position_risk(config.trading.SYMBOL)
            position_side = "LONG" if config.grid.GRID_SIDE == "LONG" else "SHORT"  # fallback
            for pos in positions:
                if pos.get("symbol") == config.trading.SYMBOL:
                    pos_amt = Decimal(pos.get("positionAmt", "0"))
                    if pos_amt > 0:
                        position_side = "LONG"
                        break
                    elif pos_amt < 0:
                        position_side = "SHORT"
                        break

            # Check cooldown for SuperTrend flip alerts (prevent spam)
            cooldown_seconds = config.risk.SUPERTREND_FLIP_ALERT_COOLDOWN
            can_send_flip_alert = (
                self.state.last_supertrend_flip_alert is None or
                (now - self.state.last_supertrend_flip_alert).total_seconds() >= cooldown_seconds
            )

            # Pre-fetch market context for smart alerts (only if alert will be sent)
            trend_score = 0  # Default to 0 (neutral) instead of None
            volume_ratio = 0.0
            market_regime = "Unknown"
            suggestion = ""

            if can_send_flip_alert:
                try:
                    # Get trend analysis for context
                    analysis = await self.indicator_analyzer.analyze(config.trading.SYMBOL)
                    if analysis and analysis.trend_score is not None:
                        trend_score = analysis.trend_score
                        volume_ratio = analysis.volume_ratio

                        # Determine market regime
                        if abs(trend_score) >= 2:
                            market_regime = "Trending"
                        elif volume_ratio < 0.5:
                            market_regime = "Choppy (Low Vol)"
                        else:
                            market_regime = "Ranging"

                        # Smart suggestion based on conditions
                        if self.state.unrealized_pnl > 0:
                            suggestion = "💡 Position profitable - consider closing"
                        elif volume_ratio < 0.5 and abs(trend_score) < 2:
                            suggestion = "💡 Choppy market - wait for clarity"
                        elif abs(trend_score) >= 2:
                            suggestion = "💡 Strong trend - monitor closely"
                        else:
                            suggestion = "💡 Mixed signals - use caution"
                except Exception as e:
                    logger.debug(f"Could not fetch market context: {e}")

            for level in levels_due_for_update:
                try:
                    # Check for SuperTrend direction flip (immediate exit signal)
                    if position_side == "LONG" and supertrend.is_bearish:
                        logger.warning(
                            f"⚠️ SuperTrend flipped BEARISH - Level {level.index} | "
                            f"Consider closing position"
                        )
                        # Only send Telegram alert if cooldown has passed
                        if can_send_flip_alert:
                            msg = (
                                f"⚠️ *SuperTrend Flip (Bearish)*\n\n"
                                f"📊 *Market Context*\n"
                                f"├ Price: `${current_price:.2f}`\n"
                                f"├ Trend Score: `{trend_score:+d}` ({market_regime})\n"
                                f"├ Volume: `{volume_ratio:.1f}x`\n"
                                f"└ uPnL: `{self.state.unrealized_pnl:+.2f}`\n\n"
                                f"📈 *Position*\n"
                                f"├ Side: `LONG`\n"
                                f"└ Count: `{self.state.positions_count}`\n\n"
                                f"{suggestion}\n\n"
                                f"Reply `/close` to close all positions\n"
                                f"_(Next alert in {cooldown_seconds // 60} min)_"
                            )
                            await self.telegram.send_message(msg)
                            self.state.last_supertrend_flip_alert = now
                            can_send_flip_alert = False  # Only one alert per batch
                        level.last_tp_update = now
                        continue

                    elif position_side == "SHORT" and supertrend.is_bullish:
                        logger.warning(
                            f"⚠️ SuperTrend flipped BULLISH - Level {level.index} | "
                            f"Consider closing position"
                        )
                        # Only send Telegram alert if cooldown has passed
                        if can_send_flip_alert:
                            msg = (
                                f"⚠️ *SuperTrend Flip (Bullish)*\n\n"
                                f"📊 *Market Context*\n"
                                f"├ Price: `${current_price:.2f}`\n"
                                f"├ Trend Score: `{trend_score:+d}` ({market_regime})\n"
                                f"├ Volume: `{volume_ratio:.1f}x`\n"
                                f"└ uPnL: `{self.state.unrealized_pnl:+.2f}`\n\n"
                                f"📈 *Position*\n"
                                f"├ Side: `SHORT`\n"
                                f"└ Count: `{self.state.positions_count}`\n\n"
                                f"{suggestion}\n\n"
                                f"Reply `/close` to close all positions\n"
                                f"_(Next alert in {cooldown_seconds // 60} min)_"
                            )
                            await self.telegram.send_message(msg)
                            self.state.last_supertrend_flip_alert = now
                            can_send_flip_alert = False  # Only one alert per batch
                        level.last_tp_update = now
                        continue

                    # Get new SuperTrend stop
                    if position_side == "LONG":
                        new_stop = Decimal(str(supertrend.long_stop))
                        # Only update if new stop is higher (better)
                        should_update = new_stop > level.supertrend_stop and new_stop > level.entry_price
                    else:  # SHORT
                        new_stop = Decimal(str(supertrend.short_stop))
                        # Only update if new stop is lower (better)
                        should_update = new_stop < level.supertrend_stop and new_stop < level.entry_price

                    if should_update:
                        # Cancel old TP order and place new one
                        old_tp_price = level.tp_target_price
                        new_tp_price = self._round_price(new_stop)

                        # Calculate profit percentage (always positive for display)
                        if position_side == "LONG":
                            tp_profit_pct = ((new_tp_price - level.entry_price) / level.entry_price * 100)
                        else:  # SHORT - profit when price drops
                            tp_profit_pct = ((level.entry_price - new_tp_price) / level.entry_price * 100)

                        logger.info(
                            f"📈 Trailing TP Update: Level {level.index} | "
                            f"${old_tp_price:.4f} → ${new_tp_price:.4f} | "
                            f"+{tp_profit_pct:.2f}%"
                        )

                        # Cancel old order
                        if level.tp_order_id:
                            await self.client.cancel_order(
                                symbol=config.trading.SYMBOL,
                                order_id=level.tp_order_id
                            )

                        # Place new order at updated price
                        quantity = level.position_quantity
                        client_order_id = f"tp_{level.index}_{int(now.timestamp())}"

                        response = await self.client.place_order(
                            symbol=config.trading.SYMBOL,
                            side="SELL" if position_side == "LONG" else "BUY",
                            order_type="LIMIT",
                            quantity=quantity,
                            price=new_tp_price,
                            client_order_id=client_order_id,
                        )

                        # Update level state
                        level.tp_order_id = response.get("orderId")
                        level.order_id = level.tp_order_id
                        level.tp_target_price = new_tp_price
                        level.supertrend_stop = new_stop
                        level.trailing_tp_active = True
                        level.client_order_id = client_order_id

                        tp_side = "SELL" if position_side == "LONG" else "BUY"
                        await self.telegram.send_message(
                            f"📈 Trailing TP Updated!\n"
                            f"Position: {position_side}\n"
                            f"Level: {level.index}\n"
                            f"New TP ({tp_side}): ${new_tp_price:.4f}\n"
                            f"Entry: ${level.entry_price:.4f}\n"
                            f"Profit: +{tp_profit_pct:.2f}%"
                        )

                    # Update tracking
                    level.last_tp_update = now
                    level.supertrend_stop = new_stop

                    # Track highest/lowest prices
                    if position_side == "LONG":
                        level.highest_price_seen = max(level.highest_price_seen, current_price)
                    else:
                        level.lowest_price_seen = min(level.lowest_price_seen, current_price)

                except Exception as e:
                    logger.error(f"Error updating trailing TP for level {level.index}: {e}")

        except Exception as e:
            logger.error(f"Error in trailing TP update: {e}")

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
