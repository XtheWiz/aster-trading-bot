#!/usr/bin/env python3
"""
Aster DEX CLI Tool
==================
A command-line interface for interacting with Aster DEX API.

Usage:
    python cli.py balance          - Check account balance
    python cli.py price [symbol]   - Get current price
    python cli.py orders [symbol]  - List open orders
    python cli.py positions        - List open positions
    python cli.py test             - Test API connection
"""
import asyncio
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

# Force DRY_RUN=false for CLI
os.environ["DRY_RUN"] = "false"

from decimal import Decimal
from aster_client import AsterClient
from config import config


async def cmd_balance():
    """Show account balance for all assets."""
    print("📊 Fetching Account Balance...")
    client = AsterClient()
    try:
        balances = await client.get_account_balance()
        print("\n" + "=" * 50)
        print("ASSET".ljust(10) + "BALANCE".rjust(20) + "AVAILABLE".rjust(20))
        print("=" * 50)
        
        for b in balances:
            bal = Decimal(b.get("balance", "0"))
            avail = Decimal(b.get("availableBalance", "0"))
            if bal > 0 or avail > 0:
                print(f"{b['asset'].ljust(10)}{str(bal).rjust(20)}{str(avail).rjust(20)}")
        
        print("=" * 50)
    finally:
        await client.close()


async def cmd_price(symbol: str = None):
    """Get current price for a symbol."""
    symbol = symbol or config.trading.SYMBOL
    print(f"💰 Fetching Price for {symbol}...")
    client = AsterClient()
    try:
        ticker = await client.get_ticker_price(symbol)
        print(f"\n{symbol}: ${ticker['price']}")
    finally:
        await client.close()


async def cmd_orders(symbol: str = None):
    """List all open orders."""
    symbol = symbol or config.trading.SYMBOL
    print(f"📋 Fetching Open Orders for {symbol}...")
    client = AsterClient()
    try:
        orders = await client.get_open_orders(symbol)
        if not orders:
            print("\n✅ No open orders")
            return
        
        print("\n" + "=" * 80)
        print("ID".ljust(15) + "SIDE".ljust(8) + "TYPE".ljust(10) + 
              "PRICE".rjust(15) + "QTY".rjust(15) + "STATUS".rjust(12))
        print("=" * 80)
        
        for o in orders:
            print(f"{str(o['orderId']).ljust(15)}{o['side'].ljust(8)}{o['type'].ljust(10)}"
                  f"{o['price'].rjust(15)}{o['origQty'].rjust(15)}{o['status'].rjust(12)}")
        
        print("=" * 80)
        print(f"Total: {len(orders)} orders")
    finally:
        await client.close()


async def cmd_positions():
    """List all open positions."""
    print("📈 Fetching Positions...")
    client = AsterClient()
    try:
        positions = await client.get_position_risk()
        active = [p for p in positions if Decimal(p.get("positionAmt", "0")) != 0]
        
        if not active:
            print("\n✅ No open positions")
            return
        
        print("\n" + "=" * 120)
        print("SYMBOL".ljust(12) + "SIDE".ljust(12) + "SIZE".rjust(12) + 
              "ENTRY".rjust(20) + "MARK".rjust(20) + "PNL".rjust(20) + "LIQ".rjust(20))
        print("=" * 120)
        
        for p in active:
            amt = Decimal(p['positionAmt'])
            side = "LONG" if amt > 0 else "SHORT"
            pnl = p.get('unRealizedProfit', '0')
            print(f"{p['symbol'].ljust(12)}{side.ljust(12)}{str(amt).rjust(12)}"
                  f"{p['entryPrice'].rjust(20)}{p['markPrice'].rjust(20)}"
                  f"{pnl.rjust(20)}{p.get('liquidationPrice', 'N/A').rjust(20)}")
        
        print("=" * 120)
    finally:
        await client.close()


async def cmd_test():
    """Test API connection and signature."""
    print("🔧 Testing API Connection...")
    client = AsterClient()
    try:
        # Test 1: Public endpoint
        print("\n▶️ Test 1: Public API (ticker price)")
        ticker = await client.get_ticker_price(config.trading.SYMBOL)
        print(f"   ✅ {config.trading.SYMBOL} = ${ticker['price']}")
        
        # Test 2: Signed endpoint
        print("\n▶️ Test 2: Signed API (balance)")
        balances = await client.get_account_balance()
        usdt = next((b for b in balances if b["asset"] == "USDT"), None)
        usdf = next((b for b in balances if b["asset"] == "USDF"), None)
        
        if usdt:
            print(f"   ✅ USDT: {usdt['availableBalance']}")
        if usdf:
            print(f"   ✅ USDF: {usdf['availableBalance']}")
        
        print("\n🎉 All tests passed! API is working correctly.")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
    finally:
        await client.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    
    if cmd == "balance":
        asyncio.run(cmd_balance())
    elif cmd == "price":
        asyncio.run(cmd_price(arg))
    elif cmd == "orders":
        asyncio.run(cmd_orders(arg))
    elif cmd == "positions":
        asyncio.run(cmd_positions())
    elif cmd == "test":
        asyncio.run(cmd_test())
    else:
        print(f"❌ Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
