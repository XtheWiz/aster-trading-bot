"""
Aster DEX Grid Trading Bot - Telegram Notifier

Send real-time notifications to Telegram for:
- Order fills
- Grid rebalancing
- Circuit breaker alerts
- Daily summaries

Setup:
1. Create bot via @BotFather on Telegram
2. Get your chat ID via @userinfobot
3. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env
"""
import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    """Telegram notification settings."""
    BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # Notification toggles
    NOTIFY_ORDERS: bool = True
    NOTIFY_CIRCUIT_BREAKER: bool = True
    NOTIFY_HOURLY_SUMMARY: bool = True
    NOTIFY_START_STOP: bool = True
    
    @property
    def is_configured(self) -> bool:
        """Check if Telegram is properly configured."""
        return bool(self.BOT_TOKEN and self.CHAT_ID)


class TelegramNotifier:
    """
    Async Telegram notification sender.
    
    Uses Telegram Bot API (free, no rate limits for reasonable usage).
    
    Message formatting uses Markdown for better readability.
    
    Usage:
        notifier = TelegramNotifier()
        await notifier.send_order_fill("BUY", "0.9683", "100")
    """
    
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(self, config: TelegramConfig | None = None):
        """
        Initialize the notifier.
        
        Args:
            config: Telegram config (uses env vars if None)
        """
        self.config = config or TelegramConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
    
    async def start(self) -> bool:
        """
        Start the notifier.
        
        Returns:
            True if configured and started successfully
        """
        if not self.config.is_configured:
            logger.warning("Telegram not configured - notifications disabled")
            return False
        
        self._session = aiohttp.ClientSession()
        
        # Start background worker for queued messages
        self._worker_task = asyncio.create_task(self._message_worker())
        
        logger.info("Telegram notifier started")
        return True
    
    async def stop(self) -> None:
        """Stop the notifier and close connections."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        if self._session:
            await self._session.close()
            self._session = None
        
        logger.info("Telegram notifier stopped")
    
    async def _message_worker(self) -> None:
        """Background worker to send queued messages."""
        while True:
            try:
                message = await self._message_queue.get()
                await self._send_message(message)
                
                # Rate limiting - max 30 messages per second (Telegram limit)
                await asyncio.sleep(0.05)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in message worker: {e}")
    
    async def _send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Send message to Telegram.
        
        Args:
            text: Message text (Markdown supported)
            parse_mode: Telegram parse mode (Markdown or HTML)
            
        Returns:
            True if sent successfully
        """
        if not self._session or not self.config.is_configured:
            return False
        
        url = self.API_URL.format(token=self.config.BOT_TOKEN)
        
        payload = {
            "chat_id": self.config.CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        
        try:
            async with self._session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Telegram API error: {resp.status} - {error}")
                    return False
                    
        except asyncio.TimeoutError:
            logger.error("Telegram API timeout")
            return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    def queue_message(self, text: str) -> None:
        """Queue a message for sending (non-blocking)."""
        if self.config.is_configured:
            self._message_queue.put_nowait(text)

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Public method to send a message directly.

        Args:
            text: Message text (Markdown supported)
            parse_mode: Telegram parse mode (Markdown or HTML)

        Returns:
            True if sent successfully
        """
        return await self._send_message(text, parse_mode)
    
    # =========================================================================
    # NOTIFICATION METHODS
    # =========================================================================
    
    async def send_bot_started(
        self, 
        symbol: str, 
        balance: Decimal,
        grid_count: int,
        leverage: int
    ) -> None:
        """Send bot started notification."""
        if not self.config.NOTIFY_START_STOP:
            return
        
        message = f"""
🚀 *Grid Bot Started*

📊 *Symbol:* `{symbol}`
💰 *Balance:* `{balance:.2f} USDT`
📈 *Leverage:* `{leverage}x`
🔢 *Grids:* `{grid_count}`
⏰ *Time:* `{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`

_Bot is now running..._
"""
        self.queue_message(message.strip())
    
    async def send_bot_stopped(
        self,
        reason: str,
        total_trades: int,
        realized_pnl: Decimal,
        final_balance: Decimal,
    ) -> None:
        """Send bot stopped notification."""
        if not self.config.NOTIFY_START_STOP:
            return
        
        pnl_emoji = "📈" if realized_pnl >= 0 else "📉"
        
        message = f"""
🛑 *Grid Bot Stopped*

❓ *Reason:* `{reason}`
🔄 *Total Trades:* `{total_trades}`
{pnl_emoji} *Realized PnL:* `{realized_pnl:+.4f} USDT`
💰 *Final Balance:* `{final_balance:.2f} USDT`
⏰ *Time:* `{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`
"""
        self.queue_message(message.strip())
    
    async def send_order_filled(
        self,
        side: str,
        price: Decimal,
        quantity: Decimal,
        grid_level: int,
    ) -> None:
        """Send order fill notification."""
        if not self.config.NOTIFY_ORDERS:
            return
        
        emoji = "🟢" if side == "BUY" else "🔴"
        
        message = f"""
{emoji} *Order Filled*

📊 *Side:* `{side}`
💵 *Price:* `{price:.4f}`
📦 *Quantity:* `{quantity:.2f}`
🔢 *Grid Level:* `{grid_level}`
"""
        self.queue_message(message.strip())
    
    async def send_circuit_breaker(
        self,
        reason: str,
        drawdown_pct: Decimal,
        current_balance: Decimal,
    ) -> None:
        """Send circuit breaker alert (high priority)."""
        if not self.config.NOTIFY_CIRCUIT_BREAKER:
            return
        
        message = f"""
🚨🚨🚨 *CIRCUIT BREAKER TRIGGERED* 🚨🚨🚨

⚠️ *Reason:* `{reason}`
📉 *Drawdown:* `{drawdown_pct:.2f}%`
💰 *Balance:* `{current_balance:.2f} USDT`
⏰ *Time:* `{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`

_All orders canceled. Bot stopped._
_Manual intervention required!_
"""
        # Send immediately (don't queue)
        await self._send_message(message.strip())
    
    async def send_hourly_summary(
        self,
        trades_count: int,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        current_balance: Decimal,
        active_orders: int,
        market_status: dict | None = None,
    ) -> None:
        """Send hourly summary with market conditions."""
        if not self.config.NOTIFY_HOURLY_SUMMARY:
            return
        
        total_pnl = realized_pnl + unrealized_pnl
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        
        # Build market status section
        market_section = ""
        if market_status:
            state = market_status.get("state", "UNKNOWN")
            trend_score = market_status.get("trend_score", 0)
            current_side = market_status.get("current_side", "LONG")
            rsi = market_status.get("rsi", 0)
            price = market_status.get("price", 0)
            
            # State emoji
            state_emojis = {
                "RANGING_STABLE": "✅",
                "RANGING_VOLATILE": "⚠️",
                "TRENDING_UP": "📈",
                "TRENDING_DOWN": "📉",
                "EXTREME_VOLATILITY": "🚨",
                "UNKNOWN": "❓",
            }
            state_emoji = state_emojis.get(state, "📊")
            
            # Trend score display
            if trend_score > 0:
                score_emoji = "🟢"
            elif trend_score < 0:
                score_emoji = "🔴"
            else:
                score_emoji = "⚪"
            
            market_section = f"""
🌍 *Market Status:*
├ {state_emoji} State: `{state}`
├ {score_emoji} Trend Score: `{trend_score:+d}`
├ 📊 RSI: `{rsi:.1f}`
├ 💵 Price: `${price:.2f}`
└ 🎯 Grid Side: `{current_side}`
"""
        
        message = f"""
📊 *Hourly Summary*

🔄 *Trades (1h):* `{trades_count}`
💵 *Realized PnL:* `{realized_pnl:+.4f} USDT`
💭 *Unrealized PnL:* `{unrealized_pnl:+.4f} USDT`
{pnl_emoji} *Total PnL:* `{total_pnl:+.4f} USDT`
💰 *Balance:* `{current_balance:.2f} USDT`
📋 *Active Orders:* `{active_orders}`
{market_section}"""
        self.queue_message(message.strip())
    
    async def send_error(self, error_type: str, details: str) -> None:
        """Send error notification."""
        message = f"""
⚠️ *Error Occurred*

❌ *Type:* `{error_type}`
📝 *Details:* `{details}`
⏰ *Time:* `{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`
"""
        self.queue_message(message.strip())
    
    # =========================================================================
    # ADVANCED MONITORING (5x Leverage)
    # =========================================================================
    
    async def send_position_alert(
        self,
        symbol: str,
        side: str,
        size: Decimal,
        entry_price: Decimal,
        mark_price: Decimal,
        liq_price: Decimal,
        unrealized_pnl: Decimal,
    ) -> None:
        """Send position status alert with liquidation distance."""
        pnl_emoji = "📈" if unrealized_pnl >= 0 else "📉"
        
        # Calculate liquidation distance
        if side == "LONG":
            liq_distance = ((mark_price - liq_price) / mark_price) * 100
        else:
            liq_distance = ((liq_price - mark_price) / mark_price) * 100
        
        # Warning level
        if liq_distance < 10:
            status = "🚨 DANGER"
        elif liq_distance < 20:
            status = "⚠️ WARNING"
        else:
            status = "✅ SAFE"
        
        message = f"""
📊 *Position Update* {status}

🎯 *Symbol:* `{symbol}`
📍 *Side:* `{side}`
📦 *Size:* `{size:.4f}`
💵 *Entry:* `${entry_price:.4f}`
📈 *Mark:* `${mark_price:.4f}`
💀 *Liq Price:* `${liq_price:.4f}`
📏 *Liq Distance:* `{liq_distance:.1f}%`
{pnl_emoji} *uPnL:* `{unrealized_pnl:+.4f} USDT`
"""
        self.queue_message(message.strip())
    
    async def send_drawdown_warning(
        self,
        current_drawdown: Decimal,
        max_drawdown: Decimal,
        current_balance: Decimal,
        initial_balance: Decimal,
    ) -> None:
        """Send drawdown warning when approaching threshold."""
        pct_of_max = (current_drawdown / max_drawdown) * 100
        
        if pct_of_max >= 90:
            status = "🚨 CRITICAL"
        elif pct_of_max >= 75:
            status = "⚠️ HIGH"
        else:
            status = "📊 MODERATE"
        
        message = f"""
{status} *Drawdown Alert*

📉 *Current Drawdown:* `{current_drawdown:.2f}%`
🎯 *Max Threshold:* `{max_drawdown:.2f}%`
📊 *% of Max:* `{pct_of_max:.1f}%`
💰 *Current Balance:* `${current_balance:.2f}`
💵 *Initial Balance:* `${initial_balance:.2f}`
⏰ *Time:* `{datetime.now().strftime("%H:%M:%S")}`

_Monitor closely! Bot will stop at {max_drawdown}%_
"""
        self.queue_message(message.strip())
    
    async def send_daily_report(
        self,
        symbol: str,
        total_trades: int,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        current_balance: Decimal,
        initial_balance: Decimal,
        win_rate: Decimal,
        runtime_hours: float,
    ) -> None:
        """Send daily performance report."""
        total_pnl = realized_pnl + unrealized_pnl
        roi = ((current_balance - initial_balance) / initial_balance) * 100
        
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        roi_emoji = "📈" if roi >= 0 else "📉"
        
        message = f"""
📅 *Daily Report* - {datetime.now().strftime("%Y-%m-%d")}

🎯 *Symbol:* `{symbol}`
⏱️ *Runtime:* `{runtime_hours:.1f} hours`

📊 *Performance:*
├ 🔄 Total Trades: `{total_trades}`
├ 🎯 Win Rate: `{win_rate:.1f}%`
├ 💵 Realized PnL: `{realized_pnl:+.4f}`
├ 💭 Unrealized PnL: `{unrealized_pnl:+.4f}`
└ {pnl_emoji} Total PnL: `{total_pnl:+.4f} USDT`

💰 *Balance:*
├ Initial: `${initial_balance:.2f}`
├ Current: `${current_balance:.2f}`
└ {roi_emoji} ROI: `{roi:+.2f}%`

_Keep grinding! 💪_
"""
        self.queue_message(message.strip())


# Convenience function for quick send
async def send_telegram_alert(message: str) -> bool:
    """
    Quick one-off Telegram message.
    
    Usage:
        await send_telegram_alert("Test message")
    """
    config = TelegramConfig()
    if not config.is_configured:
        return False
    
    async with aiohttp.ClientSession() as session:
        url = TelegramNotifier.API_URL.format(token=config.BOT_TOKEN)
        payload = {
            "chat_id": config.CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }
        
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
            return False


if __name__ == "__main__":
    # Test the notifier
    async def test():
        print("Testing Telegram Notifier...")
        
        config = TelegramConfig()
        if not config.is_configured:
            print("❌ Telegram not configured!")
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            return
        
        notifier = TelegramNotifier(config)
        await notifier.start()
        
        # Send test message
        await notifier.send_bot_started(
            symbol="ASTERUSDT",
            balance=Decimal("500.00"),
            grid_count=10,
            leverage=2,
        )
        
        # Wait for queue to flush
        await asyncio.sleep(2)
        
        await notifier.stop()
        print("✅ Test complete!")
    
    asyncio.run(test())
