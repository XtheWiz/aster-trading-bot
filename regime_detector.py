"""
Regime Detector - Scoring-based market regime classification with hysteresis.

Replaces the simple 5-rule threshold detection with a confidence-scored system
that tracks regime persistence, volatility/volume trends, and prevents flapping.
"""
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("RegimeDetector")

REGIMES = [
    "Strong Trend",
    "Trending",
    "Ranging",
    "Choppy (Low Vol)",
    "High Volatility",
]

RECOMMENDATIONS = {
    "Strong Trend": "Follow trend",
    "Trending": "Grid optimal",
    "Ranging": "Grid optimal",
    "Choppy (Low Vol)": "Reduce exposure",
    "High Volatility": "Widen grid or pause",
}


@dataclass
class RegimeResult:
    """Result of regime detection."""
    regime: str
    confidence: float  # 0.0 to 1.0
    recommendation: str
    duration_minutes: float = 0.0
    volatility_trend: str = "stable"  # "increasing", "decreasing", "stable"
    volume_trend: str = "stable"      # "increasing", "decreasing", "stable"


class RegimeDetector:
    """
    Scoring-based market regime detector with hysteresis.

    Each regime is scored 0-1.0 based on weighted factors. The highest-scoring
    regime wins, but a 15% confidence margin is required to switch away from
    the current regime (hysteresis prevents flapping).
    """

    HYSTERESIS_MARGIN = 0.15  # 15% margin required to switch regime
    HISTORY_SIZE = 50         # Number of readings to track for trends

    def __init__(self):
        self._current_regime: str | None = None
        self._regime_entered_at: datetime | None = None
        self._atr_history: deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._volume_history: deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._regime_history: deque[str] = deque(maxlen=6)

    def detect(self, analysis) -> RegimeResult:
        """
        Detect current market regime from analysis data.

        Args:
            analysis: MarketAnalysis with trend_score, atr_value, current_price, volume_ratio

        Returns:
            RegimeResult with regime, confidence, recommendation, and trends
        """
        trend_score = analysis.trend_score
        volume_ratio = getattr(analysis, 'volume_ratio', 1.0)
        atr_percent = float(analysis.atr_value / analysis.current_price * 100) if analysis.current_price > 0 else 0.0

        # Track history for trend detection
        self._atr_history.append(atr_percent)
        self._volume_history.append(volume_ratio)

        # Score each regime
        scores = {
            "High Volatility": self._score_high_volatility(atr_percent),
            "Strong Trend": self._score_strong_trend(trend_score, volume_ratio),
            "Trending": self._score_trending(trend_score, volume_ratio),
            "Choppy (Low Vol)": self._score_choppy(trend_score, volume_ratio, atr_percent),
            "Ranging": self._score_ranging(trend_score, volume_ratio, atr_percent),
        }

        # Find best regime
        best_regime = max(scores, key=scores.get)
        best_score = scores[best_regime]

        # Apply hysteresis: keep current regime unless new one is significantly better
        if self._current_regime and self._current_regime != best_regime:
            current_score = scores.get(self._current_regime, 0)
            if best_score - current_score < self.HYSTERESIS_MARGIN:
                best_regime = self._current_regime
                best_score = current_score

        # Detect oscillation (choppy override)
        self._regime_history.append(best_regime)
        if self._is_oscillating():
            if scores["Choppy (Low Vol)"] > 0.2:
                best_regime = "Choppy (Low Vol)"
                best_score = max(best_score, 0.5)

        # Track regime duration
        now = datetime.now()
        if best_regime != self._current_regime:
            self._current_regime = best_regime
            self._regime_entered_at = now

        duration = 0.0
        if self._regime_entered_at:
            duration = (now - self._regime_entered_at).total_seconds() / 60

        return RegimeResult(
            regime=best_regime,
            confidence=round(min(best_score, 1.0), 2),
            recommendation=RECOMMENDATIONS.get(best_regime, "Monitor"),
            duration_minutes=round(duration, 1),
            volatility_trend=self._compute_trend(self._atr_history),
            volume_trend=self._compute_trend(self._volume_history),
        )

    def _score_high_volatility(self, atr_percent: float) -> float:
        """Score for High Volatility regime."""
        if atr_percent >= 10:
            return 1.0
        elif atr_percent >= 5:
            return 0.5 + (atr_percent - 5) / 10  # 0.5 to 1.0
        elif atr_percent >= 3:
            return 0.1 + (atr_percent - 3) / 10  # 0.1 to 0.3
        return 0.0

    def _score_strong_trend(self, trend_score: int, volume_ratio: float) -> float:
        """Score for Strong Trend regime."""
        score = 0.0
        abs_trend = abs(trend_score)
        if abs_trend >= 3:
            score = 0.7 + min(abs_trend - 3, 1) * 0.2  # 0.7-0.9
        elif abs_trend == 2:
            score = 0.3
        # Volume confirmation bonus
        if volume_ratio > 1.3:
            score += 0.1
        return min(score, 1.0)

    def _score_trending(self, trend_score: int, volume_ratio: float) -> float:
        """Score for Trending regime (moderate trend)."""
        abs_trend = abs(trend_score)
        if abs_trend == 2:
            score = 0.7
        elif abs_trend == 1:
            score = 0.4
        elif abs_trend >= 3:
            score = 0.3  # Strong trend scores higher in its own category
        else:
            score = 0.1
        if volume_ratio > 0.8:
            score += 0.1
        return min(score, 1.0)

    def _score_choppy(self, trend_score: int, volume_ratio: float, atr_percent: float) -> float:
        """Score for Choppy (Low Vol) regime."""
        score = 0.0
        if volume_ratio < 0.3:
            score = 0.7
        elif volume_ratio < 0.5:
            score = 0.5
        elif volume_ratio < 0.7:
            score = 0.2
        # Low trend adds to choppiness
        if abs(trend_score) <= 1:
            score += 0.2
        # Low ATR with low volume = textbook choppy
        if atr_percent < 2 and volume_ratio < 0.5:
            score += 0.1
        return min(score, 1.0)

    def _score_ranging(self, trend_score: int, volume_ratio: float, atr_percent: float) -> float:
        """Score for Ranging regime (stable sideways)."""
        score = 0.0
        abs_trend = abs(trend_score)
        # Low trend = more ranging
        if abs_trend <= 1:
            score = 0.5
        elif abs_trend == 0:
            score = 0.7
        # Normal volume confirms ranging
        if 0.5 <= volume_ratio <= 1.5:
            score += 0.2
        # Normal volatility
        if 1.0 <= atr_percent <= 4.0:
            score += 0.1
        return min(score, 1.0)

    def _is_oscillating(self) -> bool:
        """Detect if regime is oscillating (3+ different regimes in last 6 readings)."""
        if len(self._regime_history) < 6:
            return False
        recent = list(self._regime_history)[-6:]
        return len(set(recent)) >= 3

    def _compute_trend(self, history: deque[float]) -> str:
        """Compute trend direction from a history of values."""
        if len(history) < 10:
            return "stable"

        recent = list(history)
        first_half = sum(recent[:len(recent)//2]) / (len(recent)//2)
        second_half = sum(recent[len(recent)//2:]) / (len(recent) - len(recent)//2)

        if second_half == 0 and first_half == 0:
            return "stable"

        pct_change = (second_half - first_half) / max(first_half, 0.001) * 100

        if pct_change > 15:
            return "increasing"
        elif pct_change < -15:
            return "decreasing"
        return "stable"

    @property
    def current_regime(self) -> str | None:
        return self._current_regime

    @property
    def regime_entered_at(self) -> datetime | None:
        return self._regime_entered_at
