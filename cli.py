#!/usr/bin/env python3
"""
Aster DEX CLI Tool
==================
A command-line interface for interacting with Aster DEX API.

Usage:
    python cli.py status              - Show full status (balance, position, orders, analysis)
    python cli.py balance             - Check account balance
    python cli.py price [symbol]      - Get current price
    python cli.py orders [symbol]     - List open orders
    python cli.py positions           - List open positions
    python cli.py analyze [symbol]    - Analyze market trend & conditions
    python cli.py test                - Test API connection

Analytics (Phase 4):
    python cli.py stats [days]        - Show trading statistics
    python cli.py daily [days]        - Show daily performance
    python cli.py levels              - Show grid level performance
    python cli.py trades [limit]      - Show recent trades

Backtesting (Phase 5):
    python cli.py backtest [days]     - Run backtest with current settings
    python cli.py optimize [days]     - Find optimal grid parameters
    python cli.py spread [symbol]     - Analyze orderbook spread
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


async def cmd_status():
    """Show comprehensive status: balance, positions, orders, and market analysis."""
    from strategy_manager import StrategyManager

    print("🔄 Fetching Status...")

    client = AsterClient()
    sm = StrategyManager(client)
    symbol = config.trading.SYMBOL

    try:
        # Fetch all data in parallel
        balance_task = client.get_account_balance()
        position_task = client.get_position_risk(symbol)
        orders_task = client.get_open_orders(symbol)
        ticker_task = client.get_ticker_price(symbol)
        analysis_task = sm.analyze_market(symbol)

        balances, positions, orders, ticker, analysis = await asyncio.gather(
            balance_task, position_task, orders_task, ticker_task, analysis_task
        )

        current_price = Decimal(ticker['price'])

        print("\n" + "=" * 70)
        print("📊 ASTER DEX STATUS")
        print("=" * 70)

        # === CONFIG ===
        print(f"\n⚙️  CONFIG:")
        print(f"   Symbol:     {symbol}")
        print(f"   Grid Side:  {config.grid.GRID_SIDE}")
        print(f"   Leverage:   {config.trading.LEVERAGE}x")
        print(f"   Grid Count: {config.grid.GRID_COUNT}")
        print(f"   Grid Range: ±{config.grid.GRID_RANGE_PERCENT}%")
        print(f"   Qty/Grid:   ${config.grid.QUANTITY_PER_GRID_USDT}")

        # === BALANCE ===
        print(f"\n💰 BALANCE:")
        for b in balances:
            bal = Decimal(b.get("balance", "0"))
            avail = Decimal(b.get("availableBalance", "0"))
            if bal > 0 or avail > 0:
                print(f"   {b['asset']}: {avail:.4f} available / {bal:.4f} total")

        # === PRICE ===
        print(f"\n📈 PRICE:")
        print(f"   {symbol}: ${current_price:.4f}")

        # === POSITION ===
        print(f"\n📍 POSITION:")
        active_pos = [p for p in positions if Decimal(p.get("positionAmt", "0")) != 0]
        if not active_pos:
            print("   ✅ No open position")
        else:
            for p in active_pos:
                amt = Decimal(p['positionAmt'])
                side = "LONG 📈" if amt > 0 else "SHORT 📉"
                entry = Decimal(p['entryPrice'])
                mark = Decimal(p['markPrice'])
                upnl = Decimal(p.get('unRealizedProfit', '0'))
                liq = p.get('liquidationPrice', 'N/A')

                pnl_icon = "🟢" if upnl >= 0 else "🔴"
                pnl_pct = ((mark - entry) / entry * 100) if entry > 0 else Decimal(0)
                if amt < 0:  # SHORT position
                    pnl_pct = -pnl_pct

                print(f"   Side:      {side}")
                print(f"   Size:      {abs(amt)} ({abs(amt) * mark:.2f} USDT)")
                print(f"   Entry:     ${entry:.4f}")
                print(f"   Mark:      ${mark:.4f}")
                print(f"   uPnL:      {pnl_icon} ${upnl:.4f} ({pnl_pct:+.2f}%)")
                print(f"   Liq Price: ${liq}")

        # === ORDERS ===
        print(f"\n📋 OPEN ORDERS: {len(orders)}")
        if orders:
            buy_orders = [o for o in orders if o['side'] == 'BUY']
            sell_orders = [o for o in orders if o['side'] == 'SELL']
            print(f"   BUY:  {len(buy_orders)} orders")
            print(f"   SELL: {len(sell_orders)} orders")

            # Show price range
            if buy_orders:
                buy_prices = [Decimal(o['price']) for o in buy_orders]
                print(f"   BUY range:  ${min(buy_prices):.4f} - ${max(buy_prices):.4f}")
            if sell_orders:
                sell_prices = [Decimal(o['price']) for o in sell_orders]
                print(f"   SELL range: ${min(sell_prices):.4f} - ${max(sell_prices):.4f}")
        else:
            print("   ✅ No open orders")

        # === MARKET ANALYSIS ===
        print(f"\n🎯 MARKET ANALYSIS:")

        # Trend direction
        trend = analysis.trend_direction
        if trend == "UP":
            trend_str = "🟢 BULLISH"
        elif trend == "DOWN":
            trend_str = "🔴 BEARISH"
        else:
            trend_str = "🟡 FLAT"

        print(f"   Trend:     {trend_str}")
        print(f"   State:     {analysis.state.value}")

        # Trend Score
        trend_score = sm.current_trend_score
        if trend_score:
            score_icon = "🟢" if trend_score.total > 0 else ("🔴" if trend_score.total < 0 else "⚪")
            print(f"   Score:     {score_icon} {trend_score.total:+d} (EMA:{trend_score.ema_score:+d} MACD:{trend_score.macd_score:+d} RSI:{trend_score.rsi_score:+d} Vol:{trend_score.volume_score:+d})")
            print(f"   Recommend: {trend_score.recommended_side}")

        # Indicators
        print(f"   RSI:       {analysis.rsi:.1f}")
        print(f"   ATR:       ${float(analysis.atr_value):.4f} ({float(analysis.atr_value)/float(current_price)*100:.2f}%)")

        # === ALIGNMENT CHECK ===
        print(f"\n🔄 ALIGNMENT:")
        current_side = config.grid.GRID_SIDE
        recommended = trend_score.recommended_side if trend_score else "STAY"

        if current_side == recommended or recommended == "STAY":
            print(f"   ✅ Grid side ({current_side}) aligned with recommendation ({recommended})")
        else:
            print(f"   ⚠️  Grid side ({current_side}) differs from recommendation ({recommended})")
            if active_pos:
                print(f"   ⛔ Side switch blocked - position open")
            else:
                print(f"   💡 Consider switching to {recommended}")

        print("\n" + "=" * 70)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


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


async def cmd_analyze(symbol: str = None):
    """Analyze market trend and conditions for a symbol."""
    from strategy_manager import StrategyManager
    
    symbol = symbol or config.trading.SYMBOL
    print(f"📊 Analyzing {symbol}...")
    
    client = AsterClient()
    sm = StrategyManager(client)
    
    try:
        analysis = await sm.analyze_market(symbol)
        
        price = float(analysis.current_price)
        atr = float(analysis.atr_value)
        atr_pct = (atr / price) * 100 if price > 0 else 0
        
        # Determine direction
        trend = analysis.trend_direction
        if trend == "UP":
            direction = "🟢 BULLISH (ขาขึ้น)"
            recommendation = "พิจารณาเปิด LONG หรือรอ Buy Grid Fill"
        elif trend == "DOWN":
            direction = "🔴 BEARISH (ขาลง)"
            recommendation = "พิจารณาเปิด SHORT หรือรอ Sell Grid Fill"
        else:
            direction = "🟡 FLAT (Sideways)"
            recommendation = "เหมาะสำหรับ Grid Trading (แกว่งตัวในกรอบ)"
        
        # Market state description
        state = analysis.state.value
        if "VOLATILE" in state:
            state_desc = "⚠️ ผันผวนสูง - ระวังความเสี่ยง"
        elif "STABLE" in state:
            state_desc = "✅ เสถียร - เหมาะกับ Grid Trading"
        elif "TRENDING" in state:
            state_desc = "📈 มีเทรนด์ชัดเจน - Grid อาจ Fill ข้างเดียว"
        else:
            state_desc = state
        
        print("\n" + "=" * 60)
        print(f"📈 MARKET ANALYSIS: {symbol}")
        print("=" * 60)
        print(f"\n💰 Price:      ${price:.4f}")
        print(f"📊 ATR (14h):  ${atr:.4f} ({atr_pct:.2f}%)")
        print(f"🎯 Trend:      {direction}")
        print(f"🌡️  State:      {state_desc}")
        print(f"📐 Volatility: {analysis.volatility_score:.2f}%")

        # Display Trend Score details
        print("\n" + "-" * 60)
        print("📊 TREND SCORE BREAKDOWN:")
        trend_score = sm.current_trend_score
        if trend_score:
            ema_icon = "🟢" if trend_score.ema_score > 0 else ("🔴" if trend_score.ema_score < 0 else "⚪")
            macd_icon = "🟢" if trend_score.macd_score > 0 else ("🔴" if trend_score.macd_score < 0 else "⚪")
            rsi_icon = "🟢" if trend_score.rsi_score > 0 else ("🔴" if trend_score.rsi_score < 0 else "⚪")
            vol_icon = "🟢" if trend_score.volume_score > 0 else ("🔴" if trend_score.volume_score < 0 else "⚪")
            total_icon = "🟢" if trend_score.total > 0 else ("🔴" if trend_score.total < 0 else "⚪")

            print(f"   {ema_icon} EMA (7/25):    {trend_score.ema_score:+d}  (Fast: ${float(analysis.ema_fast):.2f}, Slow: ${float(analysis.ema_slow):.2f})")
            print(f"   {macd_icon} MACD Hist:    {trend_score.macd_score:+d}  ({analysis.macd_histogram:+.4f})")
            print(f"   {rsi_icon} RSI (14):     {trend_score.rsi_score:+d}  ({analysis.rsi:.1f})")
            vol_status = "confirms" if trend_score.volume_score != 0 else "neutral"
            print(f"   {vol_icon} Volume:       {trend_score.volume_score:+d}  (ratio: {trend_score.volume_ratio:.2f}x, {vol_status})")
            print(f"   {total_icon} Total Score:  {trend_score.total:+d}  (Range: -4 to +4)")
            print(f"\n   ➡️  Recommended Side: {trend_score.recommended_side}")

        print("\n" + "-" * 60)
        print("💡 RECOMMENDATION:")
        print(f"   {recommendation}")
        
        # Grid suitability
        if state == "RANGING_STABLE":
            print("\n✅ Grid Trading: เหมาะสมมาก (ตลาดแกว่งตัวในกรอบ)")
        elif state == "RANGING_VOLATILE":
            print("\n⚠️ Grid Trading: เหมาะสม แต่ระวัง Volatility สูง")
        elif "TRENDING" in state:
            print("\n⚠️ Grid Trading: ไม่เหมาะ (ตลาดมี Trend ชัดเจน)")
        
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Analysis failed: {e}")
    finally:
        await client.close()


async def cmd_close_position(symbol: str = None):
    """Close an open position for a symbol."""
    symbol = symbol or config.trading.SYMBOL
    
    print(f"📉 Closing position for {symbol}...")
    
    client = AsterClient()
    
    try:
        # Get current position
        positions = await client.get_position_risk(symbol)
        position = None
        
        for pos in positions:
            if pos.get("symbol") == symbol:
                size = float(pos.get("positionAmt", 0))
                if size != 0:
                    position = pos
                    break
        
        if not position:
            print(f"\n✅ No open position for {symbol}")
            return
        
        size = float(position.get("positionAmt", 0))
        entry = float(position.get("entryPrice", 0))
        mark = float(position.get("markPrice", 0))
        upnl = float(position.get("unrealizedProfit", 0))
        
        print(f"\n📊 Current Position:")
        print(f"   Side: {'LONG' if size > 0 else 'SHORT'}")
        print(f"   Size: {abs(size)}")
        print(f"   Entry: ${entry:.4f}")
        print(f"   Mark: ${mark:.4f}")
        print(f"   uPnL: ${upnl:.2f}")
        
        # Close by placing opposite order
        close_side = "SELL" if size > 0 else "BUY"
        close_qty = abs(size)
        
        print(f"\n🔄 Placing {close_side} order for {close_qty} {symbol}...")
        
        result = await client.place_order(
            symbol=symbol,
            side=close_side,
            quantity=Decimal(str(close_qty)),
            order_type="MARKET"
        )
        
        print(f"\n✅ Position closed!")
        print(f"   Order ID: {result.get('orderId')}")
        print(f"   Realized PnL: ~${upnl:.2f}")
        
    except Exception as e:
        print(f"\n❌ Failed to close position: {e}")
    finally:
        await client.close()


async def cmd_order(symbol: str, side: str, quantity: str, order_type: str = "MARKET", price: str = None):
    """Place an order."""
    symbol = symbol.upper()
    side = side.upper()
    order_type = order_type.upper()

    print(f"📝 Placing {side} {order_type} order for {quantity} {symbol}...")

    client = AsterClient()

    try:
        kwargs = {
            "symbol": symbol,
            "side": side,
            "quantity": Decimal(quantity),
            "order_type": order_type,
        }

        if price and order_type == "LIMIT":
            kwargs["price"] = Decimal(price)

        result = await client.place_order(**kwargs)

        print(f"\n✅ Order placed!")
        print(f"   Order ID: {result.get('orderId')}")
        print(f"   Symbol: {symbol}")
        print(f"   Side: {side}")
        print(f"   Type: {order_type}")
        print(f"   Quantity: {quantity}")
        if price:
            print(f"   Price: ${price}")

    except Exception as e:
        print(f"\n❌ Failed to place order: {e}")
    finally:
        await client.close()


# =============================================================================
# Phase 4: Analytics Commands
# =============================================================================

async def cmd_stats(days: int = 7):
    """Show comprehensive trading statistics."""
    from trade_logger import TradeLogger

    print(f"📊 Trading Statistics (Last {days} days)")

    logger = TradeLogger()
    await logger.initialize()

    try:
        stats = await logger.get_analytics(days)

        print("\n" + "=" * 60)
        print("📈 PERFORMANCE METRICS")
        print("=" * 60)

        # Win/Loss
        print(f"\n🎯 Win Rate:        {stats['win_rate']}%")
        print(f"   Winning Trades:  {stats['winning_trades']}")
        print(f"   Losing Trades:   {stats['losing_trades']}")
        print(f"   Total Trades:    {stats['total_trades']}")

        # PnL
        pnl_icon = "🟢" if stats['total_pnl'] >= 0 else "🔴"
        print(f"\n💰 Total PnL:       {pnl_icon} {stats['total_pnl']:+.4f} USDT")
        print(f"   Avg Win:         +{stats['avg_win']:.4f} USDT")
        print(f"   Avg Loss:        {stats['avg_loss']:.4f} USDT")
        print(f"   Avg Trade:       {stats['avg_trade']:+.4f} USDT")

        # Risk Metrics
        print(f"\n📊 Risk Metrics:")
        print(f"   Profit Factor:   {stats['profit_factor']}")
        print(f"   Sharpe Ratio:    {stats['sharpe_ratio']}")
        print(f"   Max Drawdown:    -{stats['max_drawdown']:.4f} USDT")

        # Best/Worst
        print(f"\n🏆 Best Trade:      +{stats['best_trade']:.4f} USDT")
        print(f"💀 Worst Trade:     {stats['worst_trade']:.4f} USDT")

        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await logger.close()


async def cmd_daily(days: int = 7):
    """Show daily performance breakdown."""
    from trade_logger import TradeLogger

    print(f"📅 Daily Performance (Last {days} days)")

    logger = TradeLogger()
    await logger.initialize()

    try:
        daily = await logger.get_daily_stats(days)

        if not daily:
            print("\n✅ No trades found in this period")
            return

        print("\n" + "=" * 70)
        print("DATE".ljust(12) + "TRADES".rjust(8) + "WINS".rjust(8) +
              "LOSSES".rjust(8) + "WIN%".rjust(8) + "PNL".rjust(14))
        print("=" * 70)

        total_pnl = 0
        for d in daily:
            pnl_str = f"{d['pnl']:+.4f}"
            pnl_icon = "🟢" if d['pnl'] >= 0 else "🔴"
            print(f"{d['date'].ljust(12)}{str(d['trades']).rjust(8)}"
                  f"{str(d['wins']).rjust(8)}{str(d['losses']).rjust(8)}"
                  f"{str(d['win_rate']).rjust(7)}%{pnl_icon}{pnl_str.rjust(12)}")
            total_pnl += d['pnl']

        print("=" * 70)
        total_icon = "🟢" if total_pnl >= 0 else "🔴"
        print(f"{'TOTAL'.ljust(44)}{total_icon}{total_pnl:+.4f} USDT".rjust(14))
        print("=" * 70)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await logger.close()


async def cmd_levels():
    """Show grid level performance."""
    from trade_logger import TradeLogger

    print("📊 Grid Level Performance")

    logger = TradeLogger()
    await logger.initialize()

    try:
        levels = await logger.get_grid_level_stats()

        if not levels:
            print("\n✅ No trades found")
            return

        print("\n" + "=" * 65)
        print("LEVEL".ljust(8) + "FILLS".rjust(8) + "BUYS".rjust(8) +
              "SELLS".rjust(8) + "PNL".rjust(14) + "AVG PNL".rjust(14))
        print("=" * 65)

        for l in levels:
            pnl_icon = "🟢" if l['pnl'] >= 0 else "🔴"
            print(f"{str(l['level']).ljust(8)}{str(l['total_fills']).rjust(8)}"
                  f"{str(l['buys']).rjust(8)}{str(l['sells']).rjust(8)}"
                  f"{pnl_icon}{l['pnl']:+.4f}".rjust(13) +
                  f"{l['avg_pnl']:+.4f}".rjust(14))

        print("=" * 65)

        # Summary
        total_pnl = sum(l['pnl'] for l in levels)
        best_level = max(levels, key=lambda x: x['pnl']) if levels else None
        worst_level = min(levels, key=lambda x: x['pnl']) if levels else None

        print(f"\n💰 Total PnL: {total_pnl:+.4f} USDT")
        if best_level:
            print(f"🏆 Best Level: {best_level['level']} ({best_level['pnl']:+.4f} USDT)")
        if worst_level:
            print(f"💀 Worst Level: {worst_level['level']} ({worst_level['pnl']:+.4f} USDT)")

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await logger.close()


async def cmd_trades(limit: int = 20):
    """Show recent trades."""
    from trade_logger import TradeLogger

    print(f"📋 Recent Trades (Last {limit})")

    logger = TradeLogger()
    await logger.initialize()

    try:
        trades = await logger.get_recent_trades(limit)

        if not trades:
            print("\n✅ No trades found")
            return

        print("\n" + "=" * 90)
        print("TIME".ljust(20) + "SIDE".ljust(6) + "PRICE".rjust(12) +
              "QTY".rjust(10) + "LEVEL".rjust(6) + "PNL".rjust(14) + "STATUS".rjust(12))
        print("=" * 90)

        for t in trades:
            ts = t['timestamp'][:19] if t['timestamp'] else ""
            pnl = float(t.get('pnl') or 0)
            pnl_str = f"{pnl:+.4f}" if pnl != 0 else "-"
            pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "  ")
            side_icon = "📈" if t['side'] == 'BUY' else "📉"

            print(f"{ts.ljust(20)}{side_icon}{t['side'].ljust(5)}"
                  f"{t['price'].rjust(12)}{t['quantity'].rjust(10)}"
                  f"{str(t['grid_level']).rjust(6)}"
                  f"{pnl_icon}{pnl_str.rjust(12)}{t['status'].rjust(12)}")

        print("=" * 90)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await logger.close()


# =============================================================================
# Phase 5: Orderbook Spread Analysis
# =============================================================================

async def cmd_spread(symbol: str = None):
    """Analyze orderbook spread and liquidity."""
    symbol = symbol or config.trading.SYMBOL
    print(f"📊 Orderbook Analysis: {symbol}")

    client = AsterClient()

    try:
        # Get orderbook depth
        depth = await client.get_depth(symbol, limit=20)

        # Get current price
        ticker = await client.get_ticker_price(symbol)
        current_price = Decimal(ticker['price'])

        bids = depth.get('bids', [])
        asks = depth.get('asks', [])

        if not bids or not asks:
            print("\n❌ No orderbook data available")
            return

        # Calculate spread
        best_bid = Decimal(bids[0][0])
        best_ask = Decimal(asks[0][0])
        spread = best_ask - best_bid
        spread_pct = (spread / current_price) * 100

        # Calculate depth (liquidity at each level)
        bid_depth = sum(Decimal(b[1]) for b in bids[:10])
        ask_depth = sum(Decimal(a[1]) for a in asks[:10])
        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth * 100 if total_depth > 0 else 0

        # Calculate weighted average prices
        bid_value = sum(Decimal(b[0]) * Decimal(b[1]) for b in bids[:10])
        ask_value = sum(Decimal(a[0]) * Decimal(a[1]) for a in asks[:10])
        vwap_bid = bid_value / bid_depth if bid_depth > 0 else best_bid
        vwap_ask = ask_value / ask_depth if ask_depth > 0 else best_ask

        # Slippage estimation for different order sizes
        print("\n" + "=" * 60)
        print("📈 ORDERBOOK ANALYSIS")
        print("=" * 60)

        print(f"\n💰 Current Price: ${current_price:.4f}")
        print(f"\n📊 Spread:")
        print(f"   Best Bid:      ${best_bid:.4f}")
        print(f"   Best Ask:      ${best_ask:.4f}")
        print(f"   Spread:        ${spread:.4f} ({spread_pct:.4f}%)")

        print(f"\n📦 Depth (Top 10 levels):")
        print(f"   Bid Depth:     {bid_depth:.2f} {symbol.replace('USDT', '')}")
        print(f"   Ask Depth:     {ask_depth:.2f} {symbol.replace('USDT', '')}")
        print(f"   Imbalance:     {imbalance:+.2f}%")

        # Imbalance interpretation
        if imbalance > 10:
            print(f"   → 🟢 Bullish (more buy orders)")
        elif imbalance < -10:
            print(f"   → 🔴 Bearish (more sell orders)")
        else:
            print(f"   → ⚪ Neutral")

        print(f"\n📏 VWAP (Volume Weighted):")
        print(f"   VWAP Bid:      ${vwap_bid:.4f}")
        print(f"   VWAP Ask:      ${vwap_ask:.4f}")

        # Slippage estimation
        print(f"\n💸 Estimated Slippage:")
        order_sizes = [10, 50, 100, 500]  # USDT

        for size in order_sizes:
            qty_needed = Decimal(size) / current_price
            buy_slippage = estimate_slippage(asks, qty_needed, current_price)
            sell_slippage = estimate_slippage(bids, qty_needed, current_price, is_sell=True)

            print(f"   ${size} order:   BUY {buy_slippage:+.3f}% | SELL {sell_slippage:+.3f}%")

        # Grid trading recommendation
        print(f"\n💡 Grid Trading Impact:")
        grid_qty = config.grid.QUANTITY_PER_GRID_USDT / current_price
        grid_slippage = estimate_slippage(asks, grid_qty, current_price)

        if spread_pct < Decimal("0.05"):
            spread_rating = "🟢 Excellent (< 0.05%)"
        elif spread_pct < Decimal("0.1"):
            spread_rating = "🟡 Good (< 0.1%)"
        else:
            spread_rating = "🔴 Wide (> 0.1%)"

        print(f"   Spread Rating: {spread_rating}")
        print(f"   Grid Order Slippage: ~{grid_slippage:.3f}%")

        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await client.close()


def estimate_slippage(
    orders: list,
    qty_needed: Decimal,
    current_price: Decimal,
    is_sell: bool = False
) -> Decimal:
    """Estimate slippage for a given order size."""
    remaining = qty_needed
    total_cost = Decimal("0")

    for order in orders:
        price = Decimal(order[0])
        available = Decimal(order[1])

        if remaining <= 0:
            break

        fill_qty = min(remaining, available)
        total_cost += fill_qty * price
        remaining -= fill_qty

    if qty_needed - remaining == 0:
        return Decimal("0")

    avg_price = total_cost / (qty_needed - remaining)
    slippage = (avg_price - current_price) / current_price * 100

    return slippage if not is_sell else -slippage


async def cmd_history(limit: int = 20):
    """Fetch trade history from exchange."""
    print(f"📜 Fetching Trade History from Exchange (Last {limit})")
    client = AsterClient()
    symbol = config.trading.SYMBOL

    try:
        trades = await client.get_user_trades(symbol=symbol, limit=limit)

        if not trades:
            print("\n✅ No trades found on exchange")
            return

        print("\n" + "=" * 100)
        print("TIME".ljust(20) + "SIDE".ljust(6) + "PRICE".rjust(12) +
              "QTY".rjust(10) + "REALIZED PNL".rjust(14) + "COMMISSION".rjust(12) + "ORDER ID".rjust(16))
        print("=" * 100)

        from datetime import datetime

        total_pnl = 0
        total_commission = 0

        for t in trades:
            # Convert timestamp
            ts_ms = t.get('time', 0)
            ts = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S') if ts_ms else ""

            side = t.get('side', '')
            price = float(t.get('price', 0))
            qty = float(t.get('qty', 0))
            realized_pnl = float(t.get('realizedPnl', 0))
            commission = float(t.get('commission', 0))
            order_id = t.get('orderId', '')

            total_pnl += realized_pnl
            total_commission += commission

            # Icons
            side_icon = "📈" if side == 'BUY' else "📉"
            pnl_icon = "🟢" if realized_pnl > 0 else ("🔴" if realized_pnl < 0 else "  ")

            print(f"{ts.ljust(20)}{side_icon}{side.ljust(5)}"
                  f"{price:>12.4f}{qty:>10.4f}"
                  f"{pnl_icon}{realized_pnl:>+12.4f}"
                  f"{commission:>12.6f}"
                  f"{str(order_id).rjust(16)}")

        print("=" * 100)
        pnl_color = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "")
        print(f"TOTAL: {pnl_color} PnL: {total_pnl:+.4f} | Commission: {total_commission:.6f}")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await client.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "status":
        asyncio.run(cmd_status())
    elif cmd == "balance":
        asyncio.run(cmd_balance())
    elif cmd == "price":
        asyncio.run(cmd_price(arg))
    elif cmd == "orders":
        asyncio.run(cmd_orders(arg))
    elif cmd == "positions":
        asyncio.run(cmd_positions())
    elif cmd == "test":
        asyncio.run(cmd_test())
    elif cmd == "analyze":
        asyncio.run(cmd_analyze(arg))
    elif cmd == "close":
        asyncio.run(cmd_close_position(arg))
    elif cmd == "order":
        # Usage: python cli.py order SOLUSDT BUY 1.0 [MARKET/LIMIT] [price]
        if len(sys.argv) < 5:
            print("Usage: python cli.py order SYMBOL SIDE QUANTITY [TYPE] [PRICE]")
            print("Example: python cli.py order SOLUSDT BUY 1.0 MARKET")
            print("Example: python cli.py order SOLUSDT SELL 1.0 LIMIT 130.00")
            return
        symbol = sys.argv[2]
        side = sys.argv[3]
        qty = sys.argv[4]
        order_type = sys.argv[5] if len(sys.argv) > 5 else "MARKET"
        price = sys.argv[6] if len(sys.argv) > 6 else None
        asyncio.run(cmd_order(symbol, side, qty, order_type, price))

    # Phase 4: Analytics Commands
    elif cmd == "stats":
        days = int(arg) if arg else 7
        asyncio.run(cmd_stats(days))
    elif cmd == "daily":
        days = int(arg) if arg else 7
        asyncio.run(cmd_daily(days))
    elif cmd == "levels":
        asyncio.run(cmd_levels())
    elif cmd == "trades":
        limit = int(arg) if arg else 20
        asyncio.run(cmd_trades(limit))

    # Phase 5: Backtesting Commands
    elif cmd == "backtest":
        from backtester import run_backtest
        days = int(arg) if arg else 30
        asyncio.run(run_backtest(days=days))
    elif cmd == "optimize":
        from backtester import run_optimization
        days = int(arg) if arg else 30
        asyncio.run(run_optimization(days=days))
    elif cmd == "spread":
        asyncio.run(cmd_spread(arg))

    # Trade History from Exchange
    elif cmd == "history":
        limit = int(arg) if arg else 20
        asyncio.run(cmd_history(limit))

    else:
        print(f"❌ Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()

