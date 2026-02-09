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
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# Bangkok timezone (UTC+7)
BANGKOK_TZ = timezone(timedelta(hours=7))


def bangkok_now() -> datetime:
    """Get current time in Bangkok timezone."""
    return datetime.now(BANGKOK_TZ)

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
⏰ *Time:* `{bangkok_now().strftime("%Y-%m-%d %H:%M:%S")} (BKK)`

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
⏰ *Time:* `{bangkok_now().strftime("%Y-%m-%d %H:%M:%S")} (BKK)`
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

    async def send_orders_placed(
        self,
        orders_count: int,
        side: str,
        price_range: tuple[Decimal, Decimal],
        grid_side: str,
    ) -> None:
        """Send notification when grid orders are placed."""
        if not self.config.NOTIFY_ORDERS:
            return

        emoji = "🟢" if side == "BUY" else "🔴"

        message = f"""
{emoji} *Grid Orders Placed*

📊 *Orders:* `{orders_count}` × `{side}`
💵 *Range:* `${price_range[0]:.2f}` - `${price_range[1]:.2f}`
🎯 *Grid Side:* `{grid_side}`
⏰ *Time:* `{bangkok_now().strftime("%H:%M:%S")} (BKK)`
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
⏰ *Time:* `{bangkok_now().strftime("%Y-%m-%d %H:%M:%S")} (BKK)`

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
            volume_ratio = market_status.get("volume_ratio", 0)
            market_regime = market_status.get("market_regime", "Unknown")
            recommendation = market_status.get("recommendation", "")

            # Market regime emoji
            regime_emojis = {
                "Strong Trend": "🚀",
                "Trending": "📈",
                "Ranging": "↔️",
                "Choppy (Low Vol)": "⚠️",
                "High Volatility": "🚨",
            }
            regime_emoji = regime_emojis.get(market_regime, "📊")

            # Trend score display
            if trend_score > 0:
                score_emoji = "🟢"
            elif trend_score < 0:
                score_emoji = "🔴"
            else:
                score_emoji = "⚪"

            # Session and sizing info
            session = market_status.get("session", "")
            effective_size = market_status.get("effective_size", "")
            vol_factor = market_status.get("vol_factor", "")

            session_line = ""
            if session:
                session_line = f"\n├ 🕐 Session: `{session}`"
            sizing_line = ""
            if effective_size:
                sizing_line = f"\n├ 📐 Size: `{effective_size}/grid` (vol×{vol_factor})"

            # Enhanced regime info
            regime_confidence = market_status.get("regime_confidence", 0)
            regime_duration = market_status.get("regime_duration", 0)
            volatility_trend = market_status.get("volatility_trend", "")
            volume_trend_str = market_status.get("volume_trend", "")

            confidence_line = ""
            if regime_confidence:
                confidence_line = f" ({int(regime_confidence * 100)}%)"

            duration_line = ""
            if regime_duration > 0:
                hours = int(regime_duration // 60)
                mins = int(regime_duration % 60)
                duration_line = f"\n├ ⏱️ In regime: `{hours}h {mins}m`" if hours > 0 else f"\n├ ⏱️ In regime: `{mins}m`"

            trend_lines = ""
            if volatility_trend and volatility_trend != "stable":
                trend_lines += f"\n├ 📉 Vol trend: `{volatility_trend}`"
            if volume_trend_str and volume_trend_str != "stable":
                trend_lines += f"\n├ 📊 Vol ratio trend: `{volume_trend_str}`"

            # Multi-timeframe section
            mtf_section = ""
            mtf = market_status.get("mtf")
            if mtf and mtf.get("htf_4h") is not None:
                htf_4h = mtf.get("htf_4h", {})
                weekly = mtf.get("weekly", {})
                alignment = mtf.get("alignment", "UNKNOWN")
                summary = mtf.get("summary", "")

                # Alignment emoji
                align_emojis = {
                    "ALIGNED_BULLISH": "🟢",
                    "ALIGNED_BEARISH": "🔴",
                    "CONFLICTING": "⚠️",
                    "MIXED": "🟡",
                }
                align_emoji = align_emojis.get(alignment, "📊")

                def _tf_line(label, data):
                    if not data:
                        return f"\n├ ⏳ {label}: `loading...`"
                    s = data.get("score", 0)
                    bias = data.get("bias", "")
                    rsi_val = data.get("rsi", 0)
                    emoji = "🟢" if s > 0 else "🔴" if s < 0 else "⚪"
                    return f"\n├ {emoji} {label}: `{s:+d}` ({bias}) RSI `{rsi_val:.0f}`"

                mtf_section = f"""

🔭 *Multi-Timeframe*
├ {score_emoji} 1H: `{trend_score:+d}`{_tf_line("4H", htf_4h)}{_tf_line("Weekly", weekly)}
└ {align_emoji} {summary}
"""

            # TradingView screener section
            tv_section = ""
            tv = market_status.get("tv")
            if tv:
                def _tv_emoji(label):
                    if "STRONG BUY" in label:
                        return "🟢"
                    elif "BUY" in label:
                        return "🟢"
                    elif "STRONG SELL" in label:
                        return "🔴"
                    elif "SELL" in label:
                        return "🔴"
                    return "⚪"

                tv_1h = tv.get("rec_1h_label", "N/A")
                tv_4h = tv.get("rec_4h_label", "N/A")
                tv_w = tv.get("rec_w_label", "N/A")
                tv_rsi = tv.get("rsi")
                tv_rsi_str = f" RSI `{tv_rsi:.0f}`" if tv_rsi is not None else ""

                tv_section = f"""

📺 *TradingView*
├ {_tv_emoji(tv_1h)} 1H: `{tv_1h}`
├ {_tv_emoji(tv_4h)} 4H: `{tv_4h}`
├ {_tv_emoji(tv_w)} Weekly: `{tv_w}`{tv_rsi_str}
└ 📊 Rating: `{tv.get("rating_label", "N/A")}`
"""

            market_section = f"""
🌍 *Market Status*
├ {regime_emoji} Regime: `{market_regime}`{confidence_line}
├ {score_emoji} Trend: `{trend_score:+d}`
├ 📊 RSI: `{rsi:.1f}`
├ 📈 Volume: `{volume_ratio:.1f}x`
├ 💵 Price: `${price:.2f}`{duration_line}{trend_lines}{session_line}{sizing_line}
└ 🎯 Grid: `{current_side}`

💡 *{recommendation}*
{mtf_section}{tv_section}"""
        
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
    
    async def send_regime_transition(
        self,
        old_regime: str,
        new_regime: str,
        duration_minutes: float,
        trend_score: int,
        rsi: float,
        volume_ratio: float,
        atr_percent: float,
        recommendation: str,
    ) -> None:
        """Send regime transition alert."""
        hours = int(duration_minutes // 60)
        mins = int(duration_minutes % 60)
        duration_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

        regime_emojis = {
            "Strong Trend": "🚀",
            "Trending": "📈",
            "Ranging": "↔️",
            "Choppy (Low Vol)": "⚠️",
            "High Volatility": "🚨",
        }
        new_emoji = regime_emojis.get(new_regime, "📊")

        message = f"""
{new_emoji} *Regime Change*

📊 `{old_regime}` → `{new_regime}`
⏱️ Was in `{old_regime}` for `{duration_str}`

📈 *Indicators:*
├ Trend: `{trend_score:+d}` | RSI: `{rsi:.1f}`
├ Volume: `{volume_ratio:.1f}x` | ATR: `{atr_percent:.1f}%`
└ ⏰ `{bangkok_now().strftime("%H:%M:%S")} (BKK)`

💡 *{recommendation}*
"""
        self.queue_message(message.strip())

    async def send_error(self, error_type: str, details: str) -> None:
        """Send error notification."""
        message = f"""
⚠️ *Error Occurred*

❌ *Type:* `{error_type}`
📝 *Details:* `{details}`
⏰ *Time:* `{bangkok_now().strftime("%Y-%m-%d %H:%M:%S")} (BKK)`
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
⏰ *Time:* `{bangkok_now().strftime("%H:%M:%S")} (BKK)`

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
        best_trade: float = 0.0,
        worst_trade: float = 0.0,
        session_breakdown: dict | None = None,
        regime_distribution: dict | None = None,
        side_switches: int = 0,
    ) -> None:
        """Send daily performance report with enhanced analytics."""
        total_pnl = realized_pnl + unrealized_pnl
        roi = ((current_balance - initial_balance) / initial_balance) * 100 if initial_balance > 0 else Decimal("0")

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        roi_emoji = "📈" if roi >= 0 else "📉"

        # Session breakdown section
        session_section = ""
        if session_breakdown:
            lines = []
            for session, data in session_breakdown.items():
                if data["count"] > 0:
                    s_pnl = data["pnl"]
                    s_emoji = "🟢" if s_pnl >= 0 else "🔴"
                    lines.append(f"├ {s_emoji} {session}: `{data['count']}` trades, `{s_pnl:+.4f}`")
            if lines:
                session_section = "\n🕐 *Sessions:*\n" + "\n".join(lines)

        # Regime distribution section
        regime_section = ""
        if regime_distribution:
            lines = []
            regime_emojis = {
                "Strong Trend": "🚀", "Trending": "📈", "Ranging": "↔️",
                "Choppy (Low Vol)": "⚠️", "High Volatility": "🚨",
            }
            for regime, pct in regime_distribution.items():
                emoji = regime_emojis.get(regime, "📊")
                lines.append(f"├ {emoji} {regime}: `{pct}%`")
            if lines:
                regime_section = "\n📊 *Regime Distribution:*\n" + "\n".join(lines)

        # Best/worst trade
        trade_highlights = ""
        if best_trade != 0 or worst_trade != 0:
            trade_highlights = f"\n├ 🏆 Best: `{best_trade:+.4f}` | Worst: `{worst_trade:+.4f}`"

        message = f"""
📅 *Daily Report* - {bangkok_now().strftime("%Y-%m-%d")}

🎯 *Symbol:* `{symbol}`
⏱️ *Runtime:* `{runtime_hours:.1f} hours`

📊 *Performance:*
├ 🔄 Total Trades: `{total_trades}`
├ 🎯 Win Rate: `{win_rate:.1f}%`
├ 💵 Realized PnL: `{realized_pnl:+.4f}`
├ 💭 Unrealized PnL: `{unrealized_pnl:+.4f}`
├ {pnl_emoji} Total PnL: `{total_pnl:+.4f} USDT`{trade_highlights}
└ 🔀 Side Switches: `{side_switches}`

💰 *Balance:*
├ Initial: `${initial_balance:.2f}`
├ Current: `${current_balance:.2f}`
└ {roi_emoji} ROI: `{roi:+.2f}%`
{session_section}{regime_section}
"""
        self.queue_message(message.strip())

    async def send_weekly_report(
        self,
        symbol: str,
        total_trades: int,
        realized_pnl: Decimal,
        current_balance: Decimal,
        initial_balance: Decimal,
        win_rate: float,
        best_trade: float = 0.0,
        worst_trade: float = 0.0,
        daily_stats: list[dict] | None = None,
        regime_distribution: dict | None = None,
    ) -> None:
        """Send weekly performance report."""
        roi = ((current_balance - initial_balance) / initial_balance) * 100 if initial_balance > 0 else Decimal("0")
        pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"
        roi_emoji = "📈" if roi >= 0 else "📉"

        # Daily breakdown
        daily_section = ""
        if daily_stats:
            lines = []
            for day in daily_stats[:7]:
                d_pnl = day.get("pnl", 0)
                d_emoji = "🟢" if d_pnl >= 0 else "🔴"
                lines.append(f"├ {d_emoji} {day['date']}: `{day['trades']}` trades, `{d_pnl:+.4f}`")
            if lines:
                daily_section = "\n📅 *Daily Breakdown:*\n" + "\n".join(lines)

        # Regime distribution
        regime_section = ""
        if regime_distribution:
            lines = []
            regime_emojis = {
                "Strong Trend": "🚀", "Trending": "📈", "Ranging": "↔️",
                "Choppy (Low Vol)": "⚠️", "High Volatility": "🚨",
            }
            for regime, pct in regime_distribution.items():
                emoji = regime_emojis.get(regime, "📊")
                lines.append(f"├ {emoji} {regime}: `{pct}%`")
            if lines:
                regime_section = "\n📊 *Regime Distribution:*\n" + "\n".join(lines)

        message = f"""
📊 *Weekly Report* - Week of {bangkok_now().strftime("%Y-%m-%d")}

🎯 *Symbol:* `{symbol}`

📈 *Performance:*
├ 🔄 Total Trades: `{total_trades}`
├ 🎯 Win Rate: `{win_rate:.1f}%`
├ {pnl_emoji} Realized PnL: `{realized_pnl:+.4f} USDT`
├ 🏆 Best: `{best_trade:+.4f}` | Worst: `{worst_trade:+.4f}`
└ {roi_emoji} ROI: `{roi:+.2f}%`

💰 *Balance:*
├ Start: `${initial_balance:.2f}`
└ Current: `${current_balance:.2f}`
{daily_section}{regime_section}
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
