#!/usr/bin/env python3
"""
Quick Exit Script
- Cancel all open orders
- Place new TP at 0.5% from entry
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)
os.environ["DRY_RUN"] = "false"

from decimal import Decimal, ROUND_DOWN
from aster_client import AsterClient
from config import config


async def main():
    client = AsterClient()
    symbol = config.trading.SYMBOL

    print(f"🔄 Quick Exit for {symbol}")
    print("=" * 50)

    # 1. Get current position
    positions = await client.get_position_risk(symbol)
    position = None
    for pos in positions:
        amt = Decimal(pos.get("positionAmt", "0"))
        if amt != 0:
            position = pos
            break

    if not position:
        print("❌ No position found!")
        return

    position_amt = Decimal(position.get("positionAmt", "0"))
    entry_price = Decimal(position.get("entryPrice", "0"))
    position_side = "LONG" if position_amt > 0 else "SHORT"

    print(f"📊 Position: {position_side} {abs(position_amt)} @ ${entry_price:.4f}")

    # 2. Calculate new TP (0.5%)
    tp_percent = Decimal("0.5")
    if position_amt > 0:  # LONG
        tp_price = entry_price * (Decimal("1") + tp_percent / Decimal("100"))
        tp_side = "SELL"
    else:  # SHORT
        tp_price = entry_price * (Decimal("1") - tp_percent / Decimal("100"))
        tp_side = "BUY"

    # Round to tick size (0.01 for SOL)
    tp_price = tp_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    print(f"🎯 New TP: {tp_side} @ ${tp_price:.4f} (+{tp_percent}%)")

    # 3. Get current orders
    orders = await client.get_open_orders(symbol)
    print(f"\n📋 Found {len(orders)} open orders")

    # 4. Cancel all orders
    if orders:
        print("🗑️  Cancelling all orders...")
        result = await client.cancel_all_orders(symbol)
        print(f"✅ Cancelled all orders")

    # 5. Place new TP order
    print(f"\n📤 Placing new TP order...")
    response = await client.place_order(
        symbol=symbol,
        side=tp_side,
        order_type="LIMIT",
        quantity=abs(position_amt),
        price=tp_price,
        client_order_id=f"quick_tp_{int(asyncio.get_event_loop().time())}"
    )

    order_id = response.get("orderId")
    print(f"✅ TP Order placed: {tp_side} {abs(position_amt)} @ ${tp_price:.4f}")
    print(f"   Order ID: {order_id}")

    print("\n" + "=" * 50)
    print("✅ Done! Bot will need restart to sync with new state.")
    print("   Run: railway up (or redeploy on Railway)")


if __name__ == "__main__":
    asyncio.run(main())
