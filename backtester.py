"""
Aster DEX Grid Trading Bot - Backtesting Engine (Phase 5.1)

Simulates grid trading strategy on historical data to:
- Test strategy parameters before live trading
- Analyze performance under different market conditions
- Optimize grid settings (range, count, TP%)

Usage:
    python backtester.py [symbol] [days] [--grid-count N] [--grid-range PCT]
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from aster_client import AsterClient
from config import config

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Single trade in backtest."""
    timestamp: datetime
    side: str  # BUY or SELL
    price: Decimal
    quantity: Decimal
    grid_level: int
    pnl: Decimal = Decimal("0")


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    # Config
    symbol: str
    start_date: datetime
    end_date: datetime
    initial_balance: Decimal
    grid_count: int
    grid_range_percent: Decimal
    tp_percent: Decimal

    # Results
    final_balance: Decimal = Decimal("0")
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")

    # Trades
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.winning_trades + self.losing_trades == 0:
            return 0.0
        return self.winning_trades / (self.winning_trades + self.losing_trades) * 100

    @property
    def roi(self) -> float:
        if self.initial_balance == 0:
            return 0.0
        return float((self.final_balance - self.initial_balance) / self.initial_balance * 100)

    def summary(self) -> str:
        """Generate summary report."""
        duration = self.end_date - self.start_date

        return f"""
{'=' * 60}
📊 BACKTEST RESULTS
{'=' * 60}

📅 Period: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')} ({duration.days} days)
💰 Symbol: {self.symbol}

⚙️  Settings:
   Grid Count:     {self.grid_count}
   Grid Range:     ±{self.grid_range_percent}%
   Take Profit:    {self.tp_percent}%

💵 Capital:
   Initial:        ${self.initial_balance:.2f}
   Final:          ${self.final_balance:.2f}
   PnL:            ${self.total_pnl:+.2f} ({self.roi:+.2f}%)

📈 Performance:
   Total Trades:   {self.total_trades}
   Winning:        {self.winning_trades}
   Losing:         {self.losing_trades}
   Win Rate:       {self.win_rate:.1f}%
   Max Drawdown:   ${self.max_drawdown:.2f}

{'=' * 60}
"""


@dataclass
class GridLevel:
    """Grid level for backtesting."""
    index: int
    price: Decimal
    has_position: bool = False
    entry_price: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")


class Backtester:
    """
    Grid trading backtester.

    Simulates the grid trading strategy on historical kline data.
    """

    def __init__(
        self,
        symbol: str = None,
        initial_balance: Decimal = Decimal("500"),
        grid_count: int = None,
        grid_range_percent: Decimal = None,
        tp_percent: Decimal = None,
        quantity_per_grid: Decimal = None,
    ):
        self.symbol = symbol or config.trading.SYMBOL
        self.initial_balance = initial_balance
        self.grid_count = grid_count or config.grid.GRID_COUNT
        self.grid_range = grid_range_percent or config.grid.GRID_RANGE_PERCENT
        self.tp_percent = tp_percent or config.risk.DEFAULT_TP_PERCENT
        self.quantity_per_grid = quantity_per_grid or config.grid.QUANTITY_PER_GRID_USDT

        self.client = AsterClient()
        self.levels: list[GridLevel] = []
        self.balance = initial_balance
        self.trades: list[BacktestTrade] = []
        self.peak_balance = initial_balance
        self.max_drawdown = Decimal("0")

    async def fetch_historical_data(self, days: int = 30) -> list[dict]:
        """Fetch historical kline data."""
        logger.info(f"📥 Fetching {days} days of historical data for {self.symbol}...")

        all_klines = []
        end_time = int(datetime.now().timestamp() * 1000)

        # Fetch in chunks (max 1500 per request)
        klines_per_day = 24  # 1h candles
        total_klines = days * klines_per_day

        while len(all_klines) < total_klines:
            try:
                klines = await self.client.get_klines(
                    symbol=self.symbol,
                    interval="1h",
                    limit=min(1500, total_klines - len(all_klines)),
                    end_time=end_time
                )

                if not klines:
                    break

                all_klines = klines + all_klines
                end_time = klines[0][0] - 1  # Move to earlier data

                await asyncio.sleep(0.1)  # Rate limit

            except Exception as e:
                logger.error(f"Error fetching klines: {e}")
                break

        logger.info(f"   Fetched {len(all_klines)} candles")
        return all_klines

    def setup_grid(self, center_price: Decimal) -> None:
        """Setup grid levels around center price."""
        range_pct = self.grid_range / 100
        lower = center_price * (1 - range_pct)
        upper = center_price * (1 + range_pct)
        step = (upper - lower) / (self.grid_count - 1)

        self.levels = []
        for i in range(self.grid_count):
            price = lower + (Decimal(i) * step)
            self.levels.append(GridLevel(index=i, price=price))

        logger.info(f"   Grid: {lower:.4f} - {upper:.4f} ({self.grid_count} levels)")

    def process_candle(self, candle: dict, timestamp: datetime) -> None:
        """Process a single candle and execute trades."""
        high = Decimal(str(candle[2]))
        low = Decimal(str(candle[3]))
        close = Decimal(str(candle[4]))

        # Check each grid level
        for level in self.levels:
            # Check for BUY fills (price went below level)
            if not level.has_position and low <= level.price:
                # Simulate BUY fill
                quantity = self.quantity_per_grid / level.price
                cost = quantity * level.price

                if cost <= self.balance:
                    level.has_position = True
                    level.entry_price = level.price
                    level.quantity = quantity
                    self.balance -= cost

                    self.trades.append(BacktestTrade(
                        timestamp=timestamp,
                        side="BUY",
                        price=level.price,
                        quantity=quantity,
                        grid_level=level.index,
                    ))

            # Check for TP SELL (price reached TP target)
            elif level.has_position:
                tp_price = level.entry_price * (1 + self.tp_percent / 100)

                if high >= tp_price:
                    # Simulate SELL fill at TP
                    revenue = level.quantity * tp_price
                    pnl = revenue - (level.quantity * level.entry_price)
                    self.balance += revenue

                    self.trades.append(BacktestTrade(
                        timestamp=timestamp,
                        side="SELL",
                        price=tp_price,
                        quantity=level.quantity,
                        grid_level=level.index,
                        pnl=pnl,
                    ))

                    # Reset level
                    level.has_position = False
                    level.entry_price = Decimal("0")
                    level.quantity = Decimal("0")

        # Track drawdown
        # Calculate current equity (balance + unrealized positions)
        unrealized = sum(
            level.quantity * close - level.quantity * level.entry_price
            for level in self.levels if level.has_position
        )
        current_equity = self.balance + unrealized

        if current_equity > self.peak_balance:
            self.peak_balance = current_equity

        drawdown = self.peak_balance - current_equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    async def run(self, days: int = 30) -> BacktestResult:
        """Run the backtest."""
        logger.info(f"\n🔬 Starting Backtest: {self.symbol}")
        logger.info(f"   Period: {days} days")
        logger.info(f"   Initial Balance: ${self.initial_balance}")

        try:
            # Fetch data
            klines = await self.fetch_historical_data(days)

            if not klines:
                raise ValueError("No historical data available")

            # Get start price for grid setup
            start_price = Decimal(str(klines[0][4]))  # First close price
            self.setup_grid(start_price)

            # Process each candle
            for kline in klines:
                timestamp = datetime.fromtimestamp(kline[0] / 1000)
                self.process_candle(kline, timestamp)

            # Calculate final results
            end_price = Decimal(str(klines[-1][4]))

            # Close any remaining positions at market price
            for level in self.levels:
                if level.has_position:
                    revenue = level.quantity * end_price
                    pnl = revenue - (level.quantity * level.entry_price)
                    self.balance += revenue

                    self.trades.append(BacktestTrade(
                        timestamp=datetime.fromtimestamp(klines[-1][0] / 1000),
                        side="SELL",
                        price=end_price,
                        quantity=level.quantity,
                        grid_level=level.index,
                        pnl=pnl,
                    ))

            # Compile results
            sell_trades = [t for t in self.trades if t.side == "SELL"]
            winning = [t for t in sell_trades if t.pnl > 0]
            losing = [t for t in sell_trades if t.pnl < 0]
            total_pnl = sum(t.pnl for t in sell_trades)

            result = BacktestResult(
                symbol=self.symbol,
                start_date=datetime.fromtimestamp(klines[0][0] / 1000),
                end_date=datetime.fromtimestamp(klines[-1][0] / 1000),
                initial_balance=self.initial_balance,
                grid_count=self.grid_count,
                grid_range_percent=self.grid_range,
                tp_percent=self.tp_percent,
                final_balance=self.balance,
                total_trades=len(self.trades),
                winning_trades=len(winning),
                losing_trades=len(losing),
                total_pnl=total_pnl,
                max_drawdown=self.max_drawdown,
                trades=self.trades,
            )

            return result

        finally:
            await self.client.close()


async def run_backtest(
    symbol: str = None,
    days: int = 30,
    grid_count: int = None,
    grid_range: float = None,
    tp_percent: float = None,
) -> BacktestResult:
    """Run a backtest with specified parameters."""
    backtester = Backtester(
        symbol=symbol,
        grid_count=grid_count,
        grid_range_percent=Decimal(str(grid_range)) if grid_range else None,
        tp_percent=Decimal(str(tp_percent)) if tp_percent else None,
    )

    result = await backtester.run(days)
    print(result.summary())

    return result


async def run_optimization(
    symbol: str = None,
    days: int = 30,
) -> None:
    """Run parameter optimization to find best settings."""
    logger.info("\n🔧 Running Parameter Optimization...")

    results = []

    # Test different combinations
    grid_counts = [8, 10, 12, 15]
    grid_ranges = [2.0, 3.0, 4.0, 5.0]
    tp_percents = [1.0, 1.5, 2.0, 2.5]

    total = len(grid_counts) * len(grid_ranges) * len(tp_percents)
    current = 0

    for gc in grid_counts:
        for gr in grid_ranges:
            for tp in tp_percents:
                current += 1
                logger.info(f"   Testing {current}/{total}: grids={gc}, range={gr}%, tp={tp}%")

                backtester = Backtester(
                    symbol=symbol,
                    grid_count=gc,
                    grid_range_percent=Decimal(str(gr)),
                    tp_percent=Decimal(str(tp)),
                )

                result = await backtester.run(days)
                results.append({
                    "grid_count": gc,
                    "grid_range": gr,
                    "tp_percent": tp,
                    "roi": result.roi,
                    "win_rate": result.win_rate,
                    "trades": result.total_trades,
                    "max_dd": float(result.max_drawdown),
                })

    # Sort by ROI
    results.sort(key=lambda x: x["roi"], reverse=True)

    print("\n" + "=" * 80)
    print("🏆 TOP 10 PARAMETER COMBINATIONS")
    print("=" * 80)
    print(f"{'RANK':<6}{'GRIDS':<8}{'RANGE':<8}{'TP%':<8}{'ROI%':<10}{'WIN%':<10}{'TRADES':<8}{'MAX DD':<10}")
    print("-" * 80)

    for i, r in enumerate(results[:10], 1):
        print(f"{i:<6}{r['grid_count']:<8}{r['grid_range']:<8}{r['tp_percent']:<8}"
              f"{r['roi']:+.2f}%".ljust(10) + f"{r['win_rate']:.1f}%".ljust(10) +
              f"{r['trades']:<8}${r['max_dd']:.2f}")

    print("=" * 80)

    best = results[0]
    print(f"\n✅ Best Settings: grids={best['grid_count']}, range={best['grid_range']}%, tp={best['tp_percent']}%")
    print(f"   Expected ROI: {best['roi']:+.2f}%")


if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else None
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    # Check for optimization flag
    if "--optimize" in sys.argv:
        asyncio.run(run_optimization(symbol, days))
    else:
        asyncio.run(run_backtest(symbol, days))
