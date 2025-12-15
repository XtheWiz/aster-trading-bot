"""
Aster DEX Grid Trading Bot - Telegram Command Handler

Interactive commands for monitoring and controlling the bot via Telegram.

Commands:
    /status   - Bot status (running, trades, runtime)
    /balance  - Account balance and margin usage
    /position - Current position with liq distance
    /orders   - Open orders list
    /pnl      - Profit and loss summary
    /grid     - Current grid levels
    /help     - Show available commands
"""
import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    """
    Handle incoming Telegram commands via long polling.
    
    This class polls Telegram for new messages and executes
    corresponding command handlers.
    
    Usage:
        handler = TelegramCommandHandler(bot_instance)
        await handler.start()
        # ... bot runs ...
        await handler.stop()
    """
    
    API_URL = "https://api.telegram.org/bot{token}"
    POLL_INTERVAL = 2  # seconds
    
    def __init__(self, bot_reference: Any = None):
        """
        Initialize command handler.
        
        Args:
            bot_reference: Reference to GridBot instance for accessing state
        """
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.bot = bot_reference
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id = 0
        self._running = False
        
        # Command handlers
        self._commands: dict[str, Callable[[], Coroutine]] = {
            "status": self._cmd_status,
            "balance": self._cmd_balance,
            "position": self._cmd_position,
            "orders": self._cmd_orders,
            "pnl": self._cmd_pnl,
            "grid": self._cmd_grid,
            "help": self._cmd_help,
        }
    
    @property
    def is_configured(self) -> bool:
        """Check if Telegram is configured."""
        return bool(self.bot_token and self.chat_id)
    
    def set_bot_reference(self, bot: Any) -> None:
        """Set reference to GridBot instance."""
        self.bot = bot
    
    async def start(self) -> bool:
        """Start command polling."""
        if not self.is_configured:
            logger.warning("Telegram commands not configured - disabled")
            return False
        
        self._session = aiohttp.ClientSession()
        self._running = True
        self._polling_task = asyncio.create_task(self._poll_updates())
        
        logger.info("Telegram command handler started")
        return True
    
    async def stop(self) -> None:
        """Stop command polling."""
        self._running = False
        
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        
        if self._session:
            await self._session.close()
            self._session = None
        
        logger.info("Telegram command handler stopped")
    
    async def _poll_updates(self) -> None:
        """Poll Telegram for new messages."""
        url = f"{self.API_URL.format(token=self.bot_token)}/getUpdates"
        
        while self._running:
            try:
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                }
                
                async with self._session.get(url, params=params, timeout=35) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok") and data.get("result"):
                            for update in data["result"]:
                                await self._process_update(update)
                                self._last_update_id = update["update_id"]
                    else:
                        logger.error(f"Telegram API error: {resp.status}")
                        await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error polling Telegram: {e}")
                await asyncio.sleep(5)
    
    async def _process_update(self, update: dict) -> None:
        """Process a single update from Telegram."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        
        # Only respond to configured chat
        if chat_id != self.chat_id:
            return
        
        # Check if it's a command
        if text.startswith("/"):
            command = text[1:].split()[0].lower()
            
            if command in self._commands:
                try:
                    await self._commands[command]()
                except Exception as e:
                    logger.error(f"Error executing command /{command}: {e}")
                    await self._send_message(f"❌ Error: {str(e)}")
            else:
                await self._send_message(
                    f"❓ Unknown command: `/{command}`\n"
                    f"Use /help to see available commands."
                )
    
    async def _send_message(self, text: str) -> bool:
        """Send message to Telegram."""
        if not self._session:
            return False
        
        url = f"{self.API_URL.format(token=self.bot_token)}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        
        try:
            async with self._session.post(url, json=payload, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False
    
    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================
    
    async def _cmd_help(self) -> None:
        """Show available commands."""
        message = """
📚 *Available Commands*

🔹 /status - Bot status & runtime
🔹 /balance - Account balance
🔹 /position - Current position
🔹 /orders - Open orders
🔹 /pnl - Profit & Loss
🔹 /grid - Grid levels
🔹 /help - This help message

_Tip: Commands only work in the configured chat._
"""
        await self._send_message(message.strip())
    
    async def _cmd_status(self) -> None:
        """Show bot status."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        state = self.bot.state
        runtime = datetime.now() - state.start_time if state.start_time else None
        runtime_str = str(runtime).split('.')[0] if runtime else "N/A"
        
        message = f"""
🤖 *Bot Status*

📊 *Symbol:* `{self.bot.symbol}`
🔄 *State:* `{self.bot._state.value if hasattr(self.bot, '_state') else 'RUNNING'}`
⏱️ *Runtime:* `{runtime_str}`
🔢 *Total Trades:* `{state.total_trades}`
📋 *Active Orders:* `{state.active_orders_count}`
💰 *Balance:* `${state.current_balance:.2f}`
📉 *Drawdown:* `{state.drawdown_percent:.2f}%`
"""
        await self._send_message(message.strip())
    
    async def _cmd_balance(self) -> None:
        """Show account balance."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        try:
            balances = await self.bot.client.get_account_balance()
            
            usdf = next((b for b in balances if b["asset"] == "USDF"), None)
            usdt = next((b for b in balances if b["asset"] == "USDT"), None)
            
            usdf_balance = Decimal(usdf["availableBalance"]) if usdf else Decimal(0)
            usdt_balance = Decimal(usdt["availableBalance"]) if usdt else Decimal(0)
            
            message = f"""
💰 *Account Balance*

💵 *USDF:* `${usdf_balance:.2f}`
💲 *USDT:* `${usdt_balance:.2f}`
📊 *Total:* `${usdf_balance + usdt_balance:.2f}`

🔒 *Initial:* `${self.bot.state.initial_balance:.2f}`
📉 *Drawdown:* `{self.bot.state.drawdown_percent:.2f}%`
"""
            await self._send_message(message.strip())
            
        except Exception as e:
            await self._send_message(f"❌ Error fetching balance: {e}")
    
    async def _cmd_position(self) -> None:
        """Show current position."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        try:
            positions = await self.bot.client.get_positions(self.bot.symbol)
            
            # Filter for non-zero positions
            active_positions = [
                p for p in positions 
                if Decimal(p.get("positionAmt", "0")) != 0
            ]
            
            if not active_positions:
                await self._send_message("✅ No open positions")
                return
            
            for pos in active_positions:
                size = Decimal(pos["positionAmt"])
                entry = Decimal(pos["entryPrice"])
                mark = Decimal(pos["markPrice"])
                liq = Decimal(pos.get("liquidationPrice", "0"))
                pnl = Decimal(pos["unrealizedProfit"])
                
                side = "LONG" if size > 0 else "SHORT"
                
                # Calculate liq distance
                if liq > 0:
                    if side == "LONG":
                        liq_dist = ((mark - liq) / mark) * 100
                    else:
                        liq_dist = ((liq - mark) / mark) * 100
                else:
                    liq_dist = Decimal(0)
                
                # Status emoji
                if liq_dist < 10:
                    status = "🚨 DANGER"
                elif liq_dist < 20:
                    status = "⚠️ WARNING"
                else:
                    status = "✅ SAFE"
                
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                
                message = f"""
📊 *Position* {status}

📍 *Side:* `{side}`
📦 *Size:* `{abs(size):.4f}`
💵 *Entry:* `${entry:.4f}`
📈 *Mark:* `${mark:.4f}`
💀 *Liq:* `${liq:.4f}`
📏 *Liq Distance:* `{liq_dist:.1f}%`
{pnl_emoji} *uPnL:* `{pnl:+.4f} USDT`
"""
                await self._send_message(message.strip())
                
        except Exception as e:
            await self._send_message(f"❌ Error fetching position: {e}")
    
    async def _cmd_orders(self) -> None:
        """Show open orders."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        try:
            orders = await self.bot.client.get_open_orders(self.bot.symbol)
            
            if not orders:
                await self._send_message("✅ No open orders")
                return
            
            buy_orders = [o for o in orders if o["side"] == "BUY"]
            sell_orders = [o for o in orders if o["side"] == "SELL"]
            
            message = f"""
📋 *Open Orders* ({len(orders)} total)

🟢 *BUY Orders:* {len(buy_orders)}
"""
            for o in sorted(buy_orders, key=lambda x: Decimal(x["price"]), reverse=True)[:3]:
                message += f"  └ `${Decimal(o['price']):.4f}` × `{Decimal(o['origQty']):.2f}`\n"
            
            if len(buy_orders) > 3:
                message += f"  └ _...and {len(buy_orders) - 3} more_\n"
            
            message += f"\n🔴 *SELL Orders:* {len(sell_orders)}\n"
            for o in sorted(sell_orders, key=lambda x: Decimal(x["price"]))[:3]:
                message += f"  └ `${Decimal(o['price']):.4f}` × `{Decimal(o['origQty']):.2f}`\n"
            
            if len(sell_orders) > 3:
                message += f"  └ _...and {len(sell_orders) - 3} more_\n"
            
            await self._send_message(message.strip())
            
        except Exception as e:
            await self._send_message(f"❌ Error fetching orders: {e}")
    
    async def _cmd_pnl(self) -> None:
        """Show PnL summary."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        state = self.bot.state
        total = state.realized_pnl + state.unrealized_pnl
        
        pnl_emoji = "🟢" if total >= 0 else "🔴"
        
        message = f"""
💹 *Profit & Loss*

💵 *Realized:* `{state.realized_pnl:+.4f} USDT`
💭 *Unrealized:* `{state.unrealized_pnl:+.4f} USDT`
{pnl_emoji} *Total:* `{total:+.4f} USDT`

📊 *Initial:* `${state.initial_balance:.2f}`
💰 *Current:* `${state.current_balance:.2f}`
📈 *ROI:* `{((state.current_balance - state.initial_balance) / state.initial_balance * 100):+.2f}%`
"""
        await self._send_message(message.strip())
    
    async def _cmd_grid(self) -> None:
        """Show grid levels."""
        if not self.bot:
            await self._send_message("❌ Bot reference not available")
            return
        
        state = self.bot.state
        levels = state.levels
        
        if not levels:
            await self._send_message("❌ No grid levels calculated")
            return
        
        message = f"""
📊 *Grid Levels*

📈 *Upper:* `${state.upper_price:.4f}`
📉 *Lower:* `${state.lower_price:.4f}`
📏 *Step:* `${state.grid_step:.4f}` ({state.grid_step / state.entry_price * 100:.2f}%)
🎯 *Entry:* `${state.entry_price:.4f}`

*Levels:*
"""
        for level in levels:
            emoji = "🟢" if level.side and level.side.value == "BUY" else "🔴" if level.side else "⚪"
            status = "📌" if level.order_id else "⏸️"
            message += f"{emoji} `${level.price:.4f}` {status}\n"
        
        await self._send_message(message.strip())


# Test the command handler
if __name__ == "__main__":
    async def test():
        print("Testing Telegram Command Handler...")
        
        handler = TelegramCommandHandler()
        if not handler.is_configured:
            print("❌ Telegram not configured!")
            return
        
        await handler.start()
        print("✅ Handler started. Send /help to test.")
        print("Press Ctrl+C to stop...")
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        
        await handler.stop()
        print("✅ Handler stopped.")
    
    asyncio.run(test())
