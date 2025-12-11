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
from strategy_manager import StrategyManager

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


@dataclass
class GridLevel:
    """
    Represents a single grid level with its order state.
    
    Attributes:
        index: Grid level index (0 = lowest price)
        price: Price at this grid level
        side: BUY or SELL (determined by position relative to entry price)
        order_id: Active order ID at this level (None if no order)
        filled: Whether order at this level has been filled
    """
    index: int
    price: Decimal
    side: OrderSide | None = None
    order_id: int | None = None
    client_order_id: str | None = None
    filled: bool = False
    
    def __repr__(self) -> str:
        status = "FILLED" if self.filled else f"ORDER:{self.order_id}" if self.order_id else "EMPTY"
        return f"Grid[{self.index}] {self.price:.4f} {self.side.value if self.side else 'N/A'} ({status})"


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
    
    def get_level_by_order_id(self, order_id: int) -> GridLevel | None:
        """Find grid level by order ID."""
        for level in self.levels:
            if level.order_id == order_id:
                return level
        return None
    
    def get_level_by_price(self, price: Decimal, tolerance: Decimal = Decimal("0.0001")) -> GridLevel | None:
        """Find grid level closest to given price within tolerance."""
        for level in self.levels:
            if abs(level.price - price) <= tolerance:
                return level
        return None


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
        self.strategy_manager = StrategyManager(self.client)
        self._session_id: int = 0
        self._last_hourly_summary = datetime.now()
    
    # =========================================================================
    # GRID CALCULATION
    # =========================================================================
    
    def calculate_grid_levels(self, current_price: Decimal) -> list[GridLevel]:
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
            
        Returns:
            List of GridLevel objects with calculated prices
        """
        # Determine price range
        if config.grid.LOWER_PRICE and config.grid.UPPER_PRICE:
            lower = config.grid.LOWER_PRICE
            upper = config.grid.UPPER_PRICE
        else:
            # Calculate range from current price
            range_pct = config.grid.GRID_RANGE_PERCENT / 100
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
            
            # Clear order IDs from levels
            for level in self.state.levels:
                level.order_id = None
            
            logger.info("All orders canceled")
        except AsterAPIError as e:
            logger.error(f"Failed to cancel all orders: {e}")
    
    async def rebalance_on_fill(self, filled_level: GridLevel) -> None:
        """
        Rebalance grid after an order fill.
        
        Grid Trading Rebalancing Logic:
        ===============================
        - When BUY order fills: Place SELL at one grid step HIGHER
        - When SELL order fills: Place BUY at one grid step LOWER
        
        This creates the "grid trap" that captures profit from oscillation.
        
        Example:
            BUY fills at 0.9500 -> Place SELL at 0.9600
            Price rises to 0.9600, SELL fills -> Profit captured
            Place BUY at 0.9500 again -> Ready for next cycle
        
        Args:
            filled_level: The grid level whose order just filled
        """
        self.state.total_trades += 1
        filled_level.filled = True
        filled_level.order_id = None
        
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
                # Schedule rebalancing (can't await in callback)
                asyncio.create_task(self.rebalance_on_fill(level))
                logger.info(f"Order FILLED: {side} @ {price} | Level {level.index}")
                
                # Log trade and send telegram notification
                asyncio.create_task(self._log_and_notify_fill(
                    side, price, exec_qty, level.index
                ))
            
            elif status == "PARTIALLY_FILLED":
                logger.info(f"Order PARTIAL: {side} @ {price} | Qty: {exec_qty}")
    
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
        self, side: str, price: str, quantity: str, grid_level: int
    ) -> None:
        """Log trade to database and send Telegram notification."""
        try:
            # Log to SQLite
            trade = create_trade_record(
                symbol=config.trading.SYMBOL,
                side=side,
                order_type="LIMIT",
                price=Decimal(price),
                quantity=Decimal(quantity or "0"),
                order_id=0,
                client_order_id="",
                status="FILLED",
                grid_level=grid_level,
            )
            await self.trade_logger.log_trade(trade)
            
            # Send Telegram alert
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
            
            # Calculate grid levels
            self.state.levels = self.calculate_grid_levels(current_price)
            
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
            await self.telegram.send_bot_started(
                symbol=config.trading.SYMBOL,
                balance=self.state.initial_balance,
                grid_count=config.grid.GRID_COUNT,
                leverage=config.trading.LEVERAGE,
            )
            
            # Start Strategy Manager
            asyncio.create_task(self.strategy_manager.start_monitoring())
            
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
                await self.telegram.send_hourly_summary(
                    trades_count=self.state.total_trades,
                    realized_pnl=self.state.realized_pnl,
                    unrealized_pnl=self.state.unrealized_pnl,
                    current_balance=self.state.current_balance,
                    active_orders=self.state.active_orders_count,
                )
                self._last_hourly_summary = datetime.now()
            
            await asyncio.sleep(60)  # Check every minute
    
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
