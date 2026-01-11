"""
Trade Event Logger
==================
Logs trading events in structured JSON format for analysis and future improvements.

Each log entry contains:
- Timestamp
- Event type (ORDER_PLACED, ORDER_FILLED, REGRID, SMART_TP, etc.)
- Relevant data (price, quantity, indicators, etc.)

Log format: JSONL (one JSON object per line)
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from config import config

logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder for Decimal types."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


class TradeEventLogger:
    """
    Logs trading events in structured JSON format for analysis.
    
    Events are written to a JSONL file (one JSON object per line).
    This makes it easy to process with tools like jq, pandas, etc.
    """
    
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file or config.log.TRADE_EVENTS_LOG
        self._ensure_log_dir()
    
    def _ensure_log_dir(self):
        """Ensure log directory exists."""
        if self.log_file:
            Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
    
    def log_event(
        self,
        event_type: str,
        data: dict[str, Any],
        symbol: Optional[str] = None,
    ) -> None:
        """
        Log a trading event.
        
        Args:
            event_type: Type of event (ORDER_PLACED, ORDER_FILLED, REGRID, etc.)
            data: Event-specific data
            symbol: Trading symbol (defaults to config)
        """
        if not self.log_file:
            return
        
        event = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "symbol": symbol or config.trading.SYMBOL,
            **data
        }
        
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(event, cls=DecimalEncoder) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trade event: {e}")
    
    # Convenience methods for common events
    
    def log_order_placed(
        self,
        order_id: str,
        side: str,
        order_type: str,
        price: Decimal,
        quantity: Decimal,
        client_order_id: Optional[str] = None,
    ):
        """Log ORDER_PLACED event."""
        self.log_event("ORDER_PLACED", {
            "order_id": order_id,
            "side": side,
            "order_type": order_type,
            "price": price,
            "quantity": quantity,
            "client_order_id": client_order_id,
        })
    
    def log_order_filled(
        self,
        order_id: str,
        side: str,
        price: Decimal,
        quantity: Decimal,
        pnl: Optional[Decimal] = None,
    ):
        """Log ORDER_FILLED event."""
        self.log_event("ORDER_FILLED", {
            "order_id": order_id,
            "side": side,
            "price": price,
            "quantity": quantity,
            "pnl": pnl,
        })
    
    def log_smart_tp(
        self,
        entry_price: Decimal,
        tp_price: Decimal,
        tp_percent: Decimal,
        rsi: float,
        macd_hist: float,
        trend: str,
        grid_level: int = 0,
        position_quantity: Decimal = Decimal("0"),
        atr_percent: float = 0.0,
        btc_trend_score: int = 0,
        funding_rate: float = 0.0,
        drawdown_percent: float = 0.0,
        order_id: Optional[str] = None,
    ):
        """Log SMART_TP event with full indicator values and context for ML analysis."""
        tp_id = f"tp_{int(datetime.utcnow().timestamp() * 1000)}"

        self.log_event("SMART_TP", {
            "tp_id": tp_id,
            "order_id": order_id,
            "grid_level": grid_level,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "tp_percent": tp_percent,
            "position_quantity": position_quantity,
            "expected_pnl": float((tp_price - entry_price) * position_quantity),
            "indicators": {
                "rsi": rsi,
                "macd_hist": macd_hist,
                "trend": trend,
            },
            "context": {
                "atr_percent": atr_percent,
                "btc_trend_score": btc_trend_score,
                "funding_rate": funding_rate,
                "drawdown_percent": drawdown_percent,
            }
        })

        return tp_id

    def log_tp_filled(
        self,
        entry_price: Decimal,
        tp_target_price: Decimal,
        actual_fill_price: Decimal,
        quantity: Decimal,
        realized_pnl: Decimal,
        time_to_fill_seconds: float,
        grid_level: int = 0,
        slippage_percent: float = 0.0,
        order_id: Optional[str] = None,
    ):
        """Log TP_FILLED event for outcome tracking and ML analysis."""
        self.log_event("TP_FILLED", {
            "order_id": order_id,
            "grid_level": grid_level,
            "entry_price": entry_price,
            "tp_target_price": tp_target_price,
            "actual_fill_price": actual_fill_price,
            "quantity": quantity,
            "realized_pnl": realized_pnl,
            "time_to_fill_seconds": time_to_fill_seconds,
            "slippage_percent": slippage_percent,
            "hit_target": float(actual_fill_price) >= float(tp_target_price),
        })

    def log_buy_filled(
        self,
        entry_price: Decimal,
        quantity: Decimal,
        grid_level: int,
        rsi: float = 0.0,
        macd_hist: float = 0.0,
        trend: str = "",
        atr_percent: float = 0.0,
        btc_trend_score: int = 0,
        order_id: Optional[str] = None,
    ):
        """Log BUY_FILLED event with market context for ML analysis."""
        self.log_event("BUY_FILLED", {
            "order_id": order_id,
            "grid_level": grid_level,
            "entry_price": entry_price,
            "quantity": quantity,
            "indicators": {
                "rsi": rsi,
                "macd_hist": macd_hist,
                "trend": trend,
            },
            "context": {
                "atr_percent": atr_percent,
                "btc_trend_score": btc_trend_score,
            }
        })
    
    def log_regrid(
        self,
        old_center: Decimal,
        new_center: Decimal,
        drift_percent: float,
        new_lower: Decimal,
        new_upper: Decimal,
    ):
        """Log REGRID event."""
        self.log_event("REGRID", {
            "old_center": old_center,
            "new_center": new_center,
            "drift_percent": drift_percent,
            "new_lower": new_lower,
            "new_upper": new_upper,
        })
    
    def log_circuit_breaker(
        self,
        drawdown_percent: float,
        threshold_percent: float,
        current_balance: Decimal,
        initial_balance: Decimal,
    ):
        """Log CIRCUIT_BREAKER event."""
        self.log_event("CIRCUIT_BREAKER", {
            "drawdown_percent": drawdown_percent,
            "threshold_percent": threshold_percent,
            "current_balance": current_balance,
            "initial_balance": initial_balance,
        })
    
    def log_market_analysis(
        self,
        price: Decimal,
        rsi: float,
        macd: float,
        macd_hist: float,
        trend: str,
        sma_20: float,
        sma_50: float,
    ):
        """Log MARKET_ANALYSIS event with all indicators."""
        self.log_event("MARKET_ANALYSIS", {
            "price": price,
            "indicators": {
                "rsi": rsi,
                "macd": macd,
                "macd_hist": macd_hist,
                "trend": trend,
                "sma_20": sma_20,
                "sma_50": sma_50,
            }
        })
    
    def log_bot_start(
        self,
        leverage: int,
        grid_count: int,
        grid_range_percent: float,
        grid_side: str,
        initial_balance: Decimal,
    ):
        """Log BOT_START event with configuration."""
        self.log_event("BOT_START", {
            "config": {
                "leverage": leverage,
                "grid_count": grid_count,
                "grid_range_percent": grid_range_percent,
                "grid_side": grid_side,
            },
            "initial_balance": initial_balance,
        })
    
    def log_bot_stop(
        self,
        reason: str,
        total_trades: int,
        realized_pnl: Decimal,
        final_balance: Decimal,
        runtime_seconds: float,
    ):
        """Log BOT_STOP event with performance summary."""
        self.log_event("BOT_STOP", {
            "reason": reason,
            "performance": {
                "total_trades": total_trades,
                "realized_pnl": realized_pnl,
                "final_balance": final_balance,
                "runtime_seconds": runtime_seconds,
            }
        })

    def log_snapshot(
        self,
        current_price: Decimal,
        balance: Decimal,
        unrealized_pnl: Decimal,
        realized_pnl: Decimal,
        position_count: int,
        open_orders: int,
        drawdown_percent: float,
        grid_side: str,
        rsi: float = 0.0,
        macd_hist: float = 0.0,
        atr_percent: float = 0.0,
        btc_trend_score: int = 0,
        funding_rate: float = 0.0,
    ):
        """Log periodic SNAPSHOT for time-series analysis."""
        self.log_event("SNAPSHOT", {
            "price": current_price,
            "balance": balance,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "position_count": position_count,
            "open_orders": open_orders,
            "drawdown_percent": drawdown_percent,
            "grid_side": grid_side,
            "indicators": {
                "rsi": rsi,
                "macd_hist": macd_hist,
                "atr_percent": atr_percent,
                "btc_trend_score": btc_trend_score,
                "funding_rate": funding_rate,
            }
        })


# Singleton instance
trade_event_logger = TradeEventLogger()
