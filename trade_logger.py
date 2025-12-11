"""
Aster DEX Grid Trading Bot - Trade Logger

SQLite-based trade logging for:
- Persisting trade history
- Performance analysis
- Backtesting data collection

The logger is designed to be lightweight and not block the main trading loop.
"""
import sqlite3
import logging
import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Single trade record for logging."""
    timestamp: str
    symbol: str
    side: str           # BUY or SELL
    order_type: str     # LIMIT or MARKET
    price: str
    quantity: str
    order_id: int
    client_order_id: str
    status: str         # NEW, FILLED, CANCELED
    pnl: str = "0"      # Realized PnL for this trade
    grid_level: int = 0
    

@dataclass
class BalanceSnapshot:
    """Balance snapshot for tracking equity curve."""
    timestamp: str
    asset: str
    balance: str
    available_balance: str
    unrealized_pnl: str
    realized_pnl: str


class TradeLogger:
    """
    SQLite-based trade and balance logger.
    
    Features:
    - Async-friendly (uses thread executor for DB operations)
    - Automatic table creation
    - Trade history with grid level tracking
    - Balance snapshots for equity curve
    - Summary statistics
    
    Usage:
        logger = TradeLogger("trades.db")
        await logger.initialize()
        await logger.log_trade(trade_record)
        await logger.log_balance(balance_snapshot)
    """
    
    def __init__(self, db_path: str = "grid_bot_trades.db"):
        """
        Initialize the trade logger.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
    
    async def initialize(self) -> None:
        """Initialize database and create tables if needed."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._create_tables)
        logger.info(f"Trade logger initialized: {self.db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._connection is None:
            self._connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
            self._connection.row_factory = sqlite3.Row
        return self._connection
    
    def _create_tables(self) -> None:
        """Create database tables."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                order_id INTEGER,
                client_order_id TEXT,
                status TEXT NOT NULL,
                pnl TEXT DEFAULT '0',
                grid_level INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Balance snapshots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                asset TEXT NOT NULL,
                balance TEXT NOT NULL,
                available_balance TEXT NOT NULL,
                unrealized_pnl TEXT DEFAULT '0',
                realized_pnl TEXT DEFAULT '0',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Bot sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                symbol TEXT NOT NULL,
                initial_balance TEXT NOT NULL,
                final_balance TEXT,
                total_trades INTEGER DEFAULT 0,
                realized_pnl TEXT DEFAULT '0',
                status TEXT DEFAULT 'RUNNING'
            )
        """)
        
        # Create indexes for faster queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_balance_timestamp ON balance_snapshots(timestamp)")
        
        conn.commit()
    
    async def log_trade(self, trade: TradeRecord) -> int:
        """
        Log a trade to the database.
        
        Args:
            trade: Trade record to log
            
        Returns:
            Row ID of inserted record
        """
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._insert_trade, trade)
    
    def _insert_trade(self, trade: TradeRecord) -> int:
        """Insert trade record (sync)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO trades 
            (timestamp, symbol, side, order_type, price, quantity, 
             order_id, client_order_id, status, pnl, grid_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.timestamp, trade.symbol, trade.side, trade.order_type,
            trade.price, trade.quantity, trade.order_id, trade.client_order_id,
            trade.status, trade.pnl, trade.grid_level
        ))
        
        conn.commit()
        return cursor.lastrowid
    
    async def log_balance(self, snapshot: BalanceSnapshot) -> int:
        """
        Log a balance snapshot.
        
        Args:
            snapshot: Balance snapshot to log
            
        Returns:
            Row ID of inserted record
        """
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._insert_balance, snapshot)
    
    def _insert_balance(self, snapshot: BalanceSnapshot) -> int:
        """Insert balance snapshot (sync)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO balance_snapshots 
            (timestamp, asset, balance, available_balance, unrealized_pnl, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            snapshot.timestamp, snapshot.asset, snapshot.balance,
            snapshot.available_balance, snapshot.unrealized_pnl, snapshot.realized_pnl
        ))
        
        conn.commit()
        return cursor.lastrowid
    
    async def start_session(self, symbol: str, initial_balance: str) -> int:
        """
        Start a new trading session.
        
        Args:
            symbol: Trading symbol
            initial_balance: Starting balance
            
        Returns:
            Session ID
        """
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._start_session, symbol, initial_balance
            )
    
    def _start_session(self, symbol: str, initial_balance: str) -> int:
        """Start session (sync)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO sessions (start_time, symbol, initial_balance)
            VALUES (?, ?, ?)
        """, (datetime.now().isoformat(), symbol, initial_balance))
        
        conn.commit()
        return cursor.lastrowid
    
    async def end_session(
        self, 
        session_id: int, 
        final_balance: str,
        total_trades: int,
        realized_pnl: str,
        status: str = "COMPLETED"
    ) -> None:
        """End a trading session with final statistics."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._end_session,
                session_id, final_balance, total_trades, realized_pnl, status
            )
    
    def _end_session(
        self, 
        session_id: int,
        final_balance: str,
        total_trades: int,
        realized_pnl: str,
        status: str
    ) -> None:
        """End session (sync)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE sessions 
            SET end_time = ?, final_balance = ?, total_trades = ?, 
                realized_pnl = ?, status = ?
            WHERE id = ?
        """, (
            datetime.now().isoformat(), final_balance, total_trades,
            realized_pnl, status, session_id
        ))
        
        conn.commit()
    
    async def get_trade_summary(self, hours: int = 24) -> dict:
        """
        Get trading summary for the last N hours.
        
        Args:
            hours: Number of hours to look back
            
        Returns:
            Summary statistics dict
        """
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._get_summary, hours)
    
    def _get_summary(self, hours: int) -> dict:
        """Get summary (sync)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Get trade count and PnL
        cursor.execute("""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) as buy_count,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as sell_count,
                SUM(CAST(pnl AS REAL)) as total_pnl
            FROM trades
            WHERE timestamp > datetime('now', ?)
        """, (f'-{hours} hours',))
        
        row = cursor.fetchone()
        
        return {
            "total_trades": row["total_trades"] or 0,
            "buy_count": row["buy_count"] or 0,
            "sell_count": row["sell_count"] or 0,
            "total_pnl": row["total_pnl"] or 0,
        }
    
    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None
            logger.info("Trade logger closed")


# Convenience function
def create_trade_record(
    symbol: str,
    side: str,
    order_type: str,
    price: Decimal,
    quantity: Decimal,
    order_id: int,
    client_order_id: str,
    status: str,
    grid_level: int = 0,
    pnl: Decimal = Decimal("0"),
) -> TradeRecord:
    """Create a TradeRecord with proper formatting."""
    return TradeRecord(
        timestamp=datetime.now().isoformat(),
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=str(price),
        quantity=str(quantity),
        order_id=order_id,
        client_order_id=client_order_id,
        status=status,
        pnl=str(pnl),
        grid_level=grid_level,
    )


if __name__ == "__main__":
    # Quick test
    async def test():
        logger = TradeLogger("test_trades.db")
        await logger.initialize()
        
        # Log a test trade
        trade = create_trade_record(
            symbol="ASTERUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("0.9683"),
            quantity=Decimal("100"),
            order_id=12345,
            client_order_id="test_001",
            status="FILLED",
            grid_level=3,
        )
        trade_id = await logger.log_trade(trade)
        print(f"Logged trade: {trade_id}")
        
        # Get summary
        summary = await logger.get_trade_summary(24)
        print(f"Summary: {summary}")
        
        await logger.close()
    
    asyncio.run(test())
