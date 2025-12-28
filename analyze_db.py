
import sqlite3
import pandas as pd

try:
    conn = sqlite3.connect('grid_bot_trades.db')
    cursor = conn.cursor()

    # Get overall stats
    print("=== Trade Statistics ===")
    cursor.execute("SELECT COUNT(*) FROM trades")
    total = cursor.fetchone()[0]
    print(f"Total Trades: {total}")

    if total > 0:
        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM trades")
        start, end = cursor.fetchone()
        print(f"Time Range: {start} to {end}")

        cursor.execute("""
            SELECT side, COUNT(*), AVG(CAST(price AS REAL)), SUM(CAST(quantity AS REAL))
            FROM trades
            GROUP BY side
        """)
        for col in cursor.fetchall():
            print(f"{col[0]}: {col[1]} trades, Avg Price: {col[2]:.4f}, Vol: {col[3]:.2f}")

        print("\n=== Last 5 Trades ===")
        cursor.execute("SELECT timestamp, side, price, quantity, grid_level FROM trades ORDER BY id DESC LIMIT 5")
        for row in cursor.fetchall():
            print(f"{row[0]} | {row[1]} @ {row[2]} (Qty: {row[3]}) [Grid {row[4]}]")
    else:
        print("No trades found in database.")

except Exception as e:
    print(f"Error accessing DB: {e}")
finally:
    if 'conn' in locals():
        conn.close()
