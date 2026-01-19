import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from aster_client import AsterClient
from config import config

async def check_orders():
    client = AsterClient()

    # Get current price
    ticker = await client.get_ticker_price("SOLUSDT")
    current_price = float(ticker["price"])
    print(f"Current Price: ${current_price:.2f}")
    print(f"Grid Side: {config.grid.GRID_SIDE}")
    print()

    # Get open orders
    orders = await client.get_open_orders("SOLUSDT")

    if not orders:
        print("No open orders found")
    else:
        print(f"=== Open Orders ({len(orders)}) ===")
        for o in sorted(orders, key=lambda x: float(x.get("price", 0)), reverse=True):
            side = o.get("side", "N/A")
            price = float(o.get("price", 0))
            qty = float(o.get("origQty", 0))
            order_type = o.get("type", "N/A")
            diff_pct = ((price - current_price) / current_price) * 100
            print(f"{side:5} @ ${price:.4f} (qty: {qty:.3f}) | {diff_pct:+.2f}% from current | {order_type}")

    # Get position
    print()
    positions = await client.get_position_risk("SOLUSDT")
    for pos in positions:
        if pos.get("symbol") == "SOLUSDT":
            amt = float(pos.get("positionAmt", 0))
            entry = float(pos.get("entryPrice", 0))
            pnl = float(pos.get("unrealizedProfit", 0))
            if amt != 0:
                print("=== Position ===")
                print(f"Amount: {amt:.4f} SOL")
                print(f"Entry: ${entry:.4f}")
                print(f"Unrealized PnL: ${pnl:.2f}")
            else:
                print("No open position")

    await client.close()

asyncio.run(check_orders())
