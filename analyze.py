#!/usr/bin/env python3
"""
Aster DEX Grid Trading Bot - Analytics CLI Tool

Local analysis tool for trade data and performance metrics.

Usage:
    python analyze.py summary        - Overall trading summary
    python analyze.py trades [n]     - Last n trades (default: 20)
    python analyze.py grid           - Grid performance analysis
    python analyze.py export [file]  - Export trades to CSV
"""
import sys
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


DB_PATH = "grid_bot_trades.db"


def get_connection():
    """Get database connection."""
    if not Path(DB_PATH).exists():
        print(f"❌ Database not found: {DB_PATH}")
        print("   The database is created by the bot when running on Railway.")
        print("   To analyze data, download the DB from Railway Volume.")
        sys.exit(1)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_summary():
    """Show overall trading summary."""
    conn = get_connection()
    cursor = conn.cursor()
    
    print("\n" + "=" * 60)
    print("📊 TRADING SUMMARY")
    print("=" * 60)
    
    # Total trades
    cursor.execute("SELECT COUNT(*) as count FROM trades")
    total_trades = cursor.fetchone()["count"]
    
    # Trades by side
    cursor.execute("""
        SELECT side, COUNT(*) as count, SUM(CAST(pnl AS REAL)) as pnl
        FROM trades GROUP BY side
    """)
    sides = {row["side"]: {"count": row["count"], "pnl": row["pnl"] or 0} for row in cursor.fetchall()}
    
    # Overall PnL
    cursor.execute("SELECT SUM(CAST(pnl AS REAL)) as total_pnl FROM trades")
    total_pnl = cursor.fetchone()["total_pnl"] or 0
    
    # Win rate
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN CAST(pnl AS REAL) < 0 THEN 1 ELSE 0 END) as losses
        FROM trades
    """)
    wr = cursor.fetchone()
    wins = wr["wins"] or 0
    losses = wr["losses"] or 0
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    # Date range
    cursor.execute("SELECT MIN(timestamp) as first, MAX(timestamp) as last FROM trades")
    dates = cursor.fetchone()
    
    print(f"\n📈 Total Trades: {total_trades}")
    print(f"   🟢 BUY:  {sides.get('BUY', {}).get('count', 0)}")
    print(f"   🔴 SELL: {sides.get('SELL', {}).get('count', 0)}")
    print(f"\n💰 Total PnL: ${total_pnl:+.4f}")
    print(f"   ✅ Wins:   {wins}")
    print(f"   ❌ Losses: {losses}")
    print(f"   🎯 Win Rate: {win_rate:.1f}%")
    
    if dates["first"]:
        print(f"\n📅 Period: {dates['first'][:10]} → {dates['last'][:10]}")
    
    # Last 24h stats
    cursor.execute("""
        SELECT COUNT(*) as count, SUM(CAST(pnl AS REAL)) as pnl
        FROM trades WHERE timestamp > datetime('now', '-24 hours')
    """)
    day = cursor.fetchone()
    print(f"\n⏰ Last 24h:")
    print(f"   Trades: {day['count']}")
    print(f"   PnL: ${day['pnl'] or 0:+.4f}")
    
    # Sessions
    cursor.execute("SELECT COUNT(*) as count FROM sessions")
    sessions = cursor.fetchone()["count"]
    print(f"\n📋 Total Sessions: {sessions}")
    
    print("=" * 60 + "\n")
    conn.close()


def cmd_trades(limit: int = 20):
    """Show recent trades."""
    conn = get_connection()
    cursor = conn.cursor()
    
    print("\n" + "=" * 80)
    print(f"📜 RECENT TRADES (Last {limit})")
    print("=" * 80)
    
    cursor.execute("""
        SELECT timestamp, side, price, quantity, pnl, grid_level, status
        FROM trades ORDER BY id DESC LIMIT ?
    """, (limit,))
    
    trades = cursor.fetchall()
    
    if not trades:
        print("No trades found.")
        conn.close()
        return
    
    print(f"{'TIME':<16} {'SIDE':<6} {'PRICE':<12} {'QTY':<10} {'PnL':<12} {'GRID':<6}")
    print("-" * 80)
    
    for t in trades:
        timestamp = t["timestamp"][:16] if t["timestamp"] else "N/A"
        side_emoji = "🟢" if t["side"] == "BUY" else "🔴"
        price = f"${float(t['price']):.4f}" if t["price"] else "N/A"
        qty = f"{float(t['quantity']):.4f}" if t["quantity"] else "N/A"
        pnl = float(t["pnl"]) if t["pnl"] else 0
        pnl_str = f"${pnl:+.4f}"
        grid = str(t["grid_level"]) if t["grid_level"] else "-"
        
        print(f"{timestamp} {side_emoji}{t['side']:<5} {price:<12} {qty:<10} {pnl_str:<12} {grid:<6}")
    
    print("=" * 80 + "\n")
    conn.close()


def cmd_grid():
    """Analyze grid level performance."""
    conn = get_connection()
    cursor = conn.cursor()
    
    print("\n" + "=" * 60)
    print("📊 GRID PERFORMANCE ANALYSIS")
    print("=" * 60)
    
    cursor.execute("""
        SELECT 
            grid_level,
            COUNT(*) as trades,
            SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) as buys,
            SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as sells,
            SUM(CAST(pnl AS REAL)) as total_pnl,
            AVG(CAST(price AS REAL)) as avg_price
        FROM trades
        GROUP BY grid_level
        ORDER BY grid_level
    """)
    
    levels = cursor.fetchall()
    
    if not levels:
        print("No grid data found.")
        conn.close()
        return
    
    print(f"\n{'LEVEL':<8} {'TRADES':<10} {'BUYS':<8} {'SELLS':<8} {'PnL':<15} {'AVG PRICE':<12}")
    print("-" * 60)
    
    for level in levels:
        grid = level["grid_level"] if level["grid_level"] else "-"
        trades = level["trades"]
        buys = level["buys"] or 0
        sells = level["sells"] or 0
        pnl = level["total_pnl"] or 0
        avg_price = level["avg_price"] or 0
        
        pnl_str = f"${pnl:+.4f}"
        price_str = f"${avg_price:.4f}" if avg_price else "N/A"
        
        print(f"{grid:<8} {trades:<10} {buys:<8} {sells:<8} {pnl_str:<15} {price_str:<12}")
    
    print("=" * 60 + "\n")
    conn.close()


def cmd_export(filename: str = "trades_export.csv"):
    """Export trades to CSV."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT timestamp, symbol, side, order_type, price, quantity, 
               order_id, status, pnl, grid_level
        FROM trades ORDER BY id
    """)
    
    trades = cursor.fetchall()
    
    if not trades:
        print("No trades to export.")
        conn.close()
        return
    
    with open(filename, "w") as f:
        # Header
        f.write("timestamp,symbol,side,order_type,price,quantity,order_id,status,pnl,grid_level\n")
        
        # Data
        for t in trades:
            row = [
                t["timestamp"] or "",
                t["symbol"] or "",
                t["side"] or "",
                t["order_type"] or "",
                t["price"] or "",
                t["quantity"] or "", 
                str(t["order_id"]) if t["order_id"] else "",
                t["status"] or "",
                t["pnl"] or "0",
                str(t["grid_level"]) if t["grid_level"] else "0",
            ]
            f.write(",".join(row) + "\n")
    
    print(f"✅ Exported {len(trades)} trades to {filename}")
    conn.close()


def show_help():
    """Show help message."""
    print("""
📊 Aster Trading Bot Analytics

Usage:
    python analyze.py summary        - Overall trading summary
    python analyze.py trades [n]     - Last n trades (default: 20)
    python analyze.py grid           - Grid performance analysis
    python analyze.py export [file]  - Export trades to CSV

Examples:
    python analyze.py summary
    python analyze.py trades 50
    python analyze.py export my_trades.csv
""")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        show_help()
        return
    
    command = sys.argv[1].lower()
    
    if command == "summary":
        cmd_summary()
    elif command == "trades":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        cmd_trades(limit)
    elif command == "grid":
        cmd_grid()
    elif command == "export":
        filename = sys.argv[2] if len(sys.argv) > 2 else "trades_export.csv"
        cmd_export(filename)
    elif command in ("help", "-h", "--help"):
        show_help()
    else:
        print(f"❌ Unknown command: {command}")
        show_help()


if __name__ == "__main__":
    main()
