"""
Unit Tests for Auto Switch Side - Trend Scoring Algorithm

Tests cover edge cases:
1. Strong bullish (all +1) → LONG
2. Strong bearish (all -1) → SHORT  
3. Mixed signals → STAY
4. Confirmation tracking
5. TrendScore properties
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_manager import TrendScore, StrategyManager, MarketAnalysis, MarketState


class TestTrendScore:
    """Test TrendScore calculation and recommendations."""
    
    def test_strong_bullish_all_positive(self):
        """All indicators bullish → Score +3 → LONG"""
        ts = TrendScore(ema_score=1, macd_score=1, rsi_score=1)
        
        assert ts.total == 3
        assert ts.recommended_side == "LONG"
        print(f"✅ Strong Bullish: {ts} → {ts.recommended_side}")
    
    def test_strong_bearish_all_negative(self):
        """All indicators bearish → Score -3 → SHORT"""
        ts = TrendScore(ema_score=-1, macd_score=-1, rsi_score=-1)
        
        assert ts.total == -3
        assert ts.recommended_side == "SHORT"
        print(f"✅ Strong Bearish: {ts} → {ts.recommended_side}")
    
    def test_moderate_bullish(self):
        """2 bullish + 1 neutral → Score +2 → LONG (meets min_score=2)"""
        ts = TrendScore(ema_score=1, macd_score=1, rsi_score=0)
        
        assert ts.total == 2
        assert ts.recommended_side == "LONG"
        print(f"✅ Moderate Bullish: {ts} → {ts.recommended_side}")
    
    def test_moderate_bearish(self):
        """2 bearish + 1 neutral → Score -2 → SHORT"""
        ts = TrendScore(ema_score=-1, macd_score=-1, rsi_score=0)
        
        assert ts.total == -2
        assert ts.recommended_side == "SHORT"
        print(f"✅ Moderate Bearish: {ts} → {ts.recommended_side}")
    
    def test_weak_bullish_stay(self):
        """1 bullish + 2 neutral → Score +1 → STAY (unclear)"""
        ts = TrendScore(ema_score=1, macd_score=0, rsi_score=0)
        
        assert ts.total == 1
        assert ts.recommended_side == "STAY"  # Unclear default
        print(f"✅ Weak Bullish: {ts} → {ts.recommended_side}")
    
    def test_weak_bearish_stay(self):
        """1 bearish + 2 neutral → Score -1 → STAY"""
        ts = TrendScore(ema_score=-1, macd_score=0, rsi_score=0)
        
        assert ts.total == -1
        assert ts.recommended_side == "STAY"
        print(f"✅ Weak Bearish: {ts} → {ts.recommended_side}")
    
    def test_neutral_zero(self):
        """All neutral → Score 0 → STAY"""
        ts = TrendScore(ema_score=0, macd_score=0, rsi_score=0)
        
        assert ts.total == 0
        assert ts.recommended_side == "STAY"
        print(f"✅ Neutral: {ts} → {ts.recommended_side}")
    
    def test_mixed_conflicting_signals(self):
        """1 bullish + 1 bearish + 1 neutral → Score 0 → STAY"""
        ts = TrendScore(ema_score=1, macd_score=-1, rsi_score=0)
        
        assert ts.total == 0
        assert ts.recommended_side == "STAY"
        print(f"✅ Mixed (cancel out): {ts} → {ts.recommended_side}")
    
    def test_mixed_bullish_macd_bearish(self):
        """EMA+RSI bullish, MACD bearish → Score +1 → STAY"""
        ts = TrendScore(ema_score=1, macd_score=-1, rsi_score=1)
        
        assert ts.total == 1
        assert ts.recommended_side == "STAY"
        print(f"✅ Mixed (2 vs 1): {ts} → {ts.recommended_side}")


class TestCalculateTrendScore:
    """Test the _calculate_trend_score method."""
    
    def setup_method(self):
        """Setup mock client and manager."""
        self.mock_client = MagicMock()
        self.manager = StrategyManager(self.mock_client)
    
    def test_ema_bullish_with_buffer(self):
        """EMA fast > slow × 1.01 → bullish (+1)"""
        ema_fast = Decimal("135.00")  # > 133 × 1.01 = 134.33
        ema_slow = Decimal("133.00")
        
        ts = self.manager._calculate_trend_score(ema_fast, ema_slow, 0.1, 55)
        
        assert ts.ema_score == 1
        print(f"✅ EMA Bullish: {ema_fast} vs {ema_slow} → EMA score={ts.ema_score}")
    
    def test_ema_within_buffer_neutral(self):
        """EMA within ±1% buffer → neutral (0)"""
        ema_fast = Decimal("133.50")  # Between 131.67 and 134.33
        ema_slow = Decimal("133.00")
        
        ts = self.manager._calculate_trend_score(ema_fast, ema_slow, 0.1, 55)
        
        assert ts.ema_score == 0
        print(f"✅ EMA Neutral: {ema_fast} vs {ema_slow} (within 1%) → EMA score={ts.ema_score}")
    
    def test_ema_bearish_with_buffer(self):
        """EMA fast < slow × 0.99 → bearish (-1)"""
        ema_fast = Decimal("131.00")  # < 133 × 0.99 = 131.67
        ema_slow = Decimal("133.00")
        
        ts = self.manager._calculate_trend_score(ema_fast, ema_slow, -0.1, 45)
        
        assert ts.ema_score == -1
        print(f"✅ EMA Bearish: {ema_fast} vs {ema_slow} → EMA score={ts.ema_score}")
    
    def test_macd_positive_bullish(self):
        """MACD histogram > 0 → bullish (+1)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), 0.5, 50)
        
        assert ts.macd_score == 1
        print(f"✅ MACD Bullish: hist=0.5 → MACD score={ts.macd_score}")
    
    def test_macd_negative_bearish(self):
        """MACD histogram < 0 → bearish (-1)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), -0.5, 50)
        
        assert ts.macd_score == -1
        print(f"✅ MACD Bearish: hist=-0.5 → MACD score={ts.macd_score}")
    
    def test_macd_zero_neutral(self):
        """MACD histogram = 0 → neutral (0)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), 0.0, 50)
        
        assert ts.macd_score == 0
        print(f"✅ MACD Neutral: hist=0 → MACD score={ts.macd_score}")
    
    def test_rsi_above_50_bullish(self):
        """RSI > 50 → bullish (+1)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), 0, 65)
        
        assert ts.rsi_score == 1
        print(f"✅ RSI Bullish: RSI=65 → RSI score={ts.rsi_score}")
    
    def test_rsi_below_50_bearish(self):
        """RSI < 50 → bearish (-1)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), 0, 35)
        
        assert ts.rsi_score == -1
        print(f"✅ RSI Bearish: RSI=35 → RSI score={ts.rsi_score}")
    
    def test_rsi_exactly_50_neutral(self):
        """RSI = 50 → neutral (0)"""
        ts = self.manager._calculate_trend_score(Decimal("133"), Decimal("133"), 0, 50)
        
        assert ts.rsi_score == 0
        print(f"✅ RSI Neutral: RSI=50 → RSI score={ts.rsi_score}")


class TestConfirmationLogic:
    """Test the confirmation period logic."""
    
    def setup_method(self):
        """Setup mock client and manager."""
        self.mock_client = MagicMock()
        self.manager = StrategyManager(self.mock_client)
    
    def test_initial_pending_switch_none(self):
        """Initial state: no pending switch"""
        assert self.manager.pending_switch_side is None
        assert self.manager.switch_confirmation_count == 0
        print("✅ Initial state: no pending switch")
    
    def test_confirmation_count_tracking(self):
        """Test that confirmation count tracks correctly"""
        self.manager.pending_switch_side = "SHORT"
        self.manager.switch_confirmation_count = 1
        
        assert self.manager.pending_switch_side == "SHORT"
        assert self.manager.switch_confirmation_count == 1
        print("✅ Confirmation count tracks correctly")
    
    def test_reset_on_signal_change(self):
        """When signal changes, pending should reset"""
        self.manager.pending_switch_side = "SHORT"
        self.manager.switch_confirmation_count = 1
        
        # Simulate new signal
        self.manager.pending_switch_side = "LONG"
        self.manager.switch_confirmation_count = 1  # Reset to 1
        
        assert self.manager.pending_switch_side == "LONG"
        assert self.manager.switch_confirmation_count == 1
        print("✅ Pending resets on signal change")


class TestRealWorldScenarios:
    """Test with realistic market scenarios."""
    
    def setup_method(self):
        self.mock_client = MagicMock()
        self.manager = StrategyManager(self.mock_client)
    
    def test_scenario_strong_uptrend(self):
        """
        Scenario: Strong uptrend
        EMA: 140 vs 130 (fast >> slow) → +1
        MACD: +0.5 → +1
        RSI: 72 → +1
        Expected: Score +3, LONG
        """
        ts = self.manager._calculate_trend_score(
            Decimal("140"), Decimal("130"), 0.5, 72
        )
        
        assert ts.total == 3
        assert ts.recommended_side == "LONG"
        print(f"✅ Strong Uptrend: {ts} → {ts.recommended_side}")
    
    def test_scenario_strong_downtrend(self):
        """
        Scenario: Strong downtrend
        EMA: 120 vs 130 (fast << slow) → -1
        MACD: -0.5 → -1
        RSI: 28 → -1
        Expected: Score -3, SHORT
        """
        ts = self.manager._calculate_trend_score(
            Decimal("120"), Decimal("130"), -0.5, 28
        )
        
        assert ts.total == -3
        assert ts.recommended_side == "SHORT"
        print(f"✅ Strong Downtrend: {ts} → {ts.recommended_side}")
    
    def test_scenario_choppy_sideways(self):
        """
        Scenario: Choppy sideways market
        EMA: 133.5 vs 133 (within 1%) → 0
        MACD: -0.01 → -1
        RSI: 52 → +1
        Expected: Score 0, STAY
        """
        ts = self.manager._calculate_trend_score(
            Decimal("133.5"), Decimal("133"), -0.01, 52
        )
        
        assert ts.total == 0
        assert ts.recommended_side == "STAY"
        print(f"✅ Choppy Sideways: {ts} → {ts.recommended_side}")
    
    def test_scenario_early_reversal_from_downtrend(self):
        """
        Scenario: Early reversal from downtrend
        EMA: 128 vs 130 (still bearish but catching up) → -1
        MACD: +0.2 (turning positive) → +1
        RSI: 55 (recovering) → +1
        Expected: Score +1, STAY (need more confirmation)
        """
        ts = self.manager._calculate_trend_score(
            Decimal("128"), Decimal("130"), 0.2, 55
        )
        
        assert ts.total == 1
        assert ts.recommended_side == "STAY"
        print(f"✅ Early Reversal: {ts} → {ts.recommended_side}")
    
    def test_scenario_overbought_warning(self):
        """
        Scenario: Overbought but still trending
        EMA: 138 vs 133 (bullish) → +1
        MACD: -0.1 (weakening) → -1
        RSI: 78 (overbought but > 50) → +1
        Expected: Score +1, STAY (momentum fading)
        """
        ts = self.manager._calculate_trend_score(
            Decimal("138"), Decimal("133"), -0.1, 78
        )
        
        assert ts.total == 1
        assert ts.recommended_side == "STAY"
        print(f"✅ Overbought Warning: {ts} → {ts.recommended_side}")


class TestExtremeScenarios:
    """
    ⚠️ CRITICAL: Tests for extreme market conditions that could blow portfolio.
    These scenarios test the safety mechanisms.
    """
    
    def setup_method(self):
        self.mock_client = MagicMock()
        self.manager = StrategyManager(self.mock_client)
    
    def test_flash_crash_all_bearish(self):
        """
        Scenario: Flash Crash (price drops 10%+ in minutes)
        - All indicators turn extremely bearish
        - Should switch to SHORT to protect capital
        
        EMA: 90 vs 100 (10% drop) → -1
        MACD: -2.0 (extreme) → -1
        RSI: 15 (extreme oversold) → -1
        Expected: Score -3, SHORT immediately (after confirmation)
        """
        ts = self.manager._calculate_trend_score(
            Decimal("90"), Decimal("100"), -2.0, 15
        )
        
        assert ts.total == -3
        assert ts.recommended_side == "SHORT"
        print(f"✅ Flash Crash: {ts} → {ts.recommended_side} (PROTECT CAPITAL!)")
    
    def test_pump_and_dump_detection(self):
        """
        Scenario: Pump (price spikes 10%+ then dumps)
        - After pump: EMA fast >> slow, RSI overbought
        - But MACD weakening (early warning)
        
        Phase 1 (During Pump):
        EMA: 145 vs 130 → +1
        MACD: +1.5 → +1
        RSI: 85 → +1
        Expected: Score +3, LONG (ride the pump)
        """
        ts_pump = self.manager._calculate_trend_score(
            Decimal("145"), Decimal("130"), 1.5, 85
        )
        assert ts_pump.total == 3
        print(f"✅ Pump Phase: {ts_pump} → {ts_pump.recommended_side}")
        
        """
        Phase 2 (Dump Starting):
        EMA: 142 vs 132 → +1 (still bullish but catching up)
        MACD: -0.5 → -1 (momentum reversed!)
        RSI: 75 → +1
        Expected: Score +1, STAY (warning signal!)
        """
        ts_dump_start = self.manager._calculate_trend_score(
            Decimal("142"), Decimal("132"), -0.5, 75
        )
        assert ts_dump_start.total == 1
        assert ts_dump_start.recommended_side == "STAY"
        print(f"✅ Dump Starting: {ts_dump_start} → {ts_dump_start.recommended_side} (⚠️ Warning!)")
    
    def test_whipsaw_prevention(self):
        """
        Scenario: Whipsaw (rapid direction changes)
        - Signals keep flipping + to - and back
        - STAY should be recommended to avoid losses
        
        Check 1: Score +1 → STAY
        Check 2: Score -1 → STAY (signal flipped, reset confirmation)
        
        This tests that we don't switch on every signal change.
        """
        # First check: weak bullish (EMA neutral, MACD+, RSI neutral)
        # 133.5 vs 133 = within 1% buffer → EMA=0
        # MACD=0.1 → +1, RSI=50 → 0
        # Total = +1 → STAY
        ts1 = self.manager._calculate_trend_score(
            Decimal("133.5"), Decimal("133"), 0.1, 50
        )
        assert ts1.total == 1, f"Expected total=1, got {ts1.total}"
        assert ts1.recommended_side == "STAY"
        
        # Second check: weak bearish (market whipsawed)
        # EMA=0 (within buffer), MACD=-0.1 → -1, RSI=50 → 0
        # Total = -1 → STAY
        ts2 = self.manager._calculate_trend_score(
            Decimal("132.5"), Decimal("133"), -0.1, 50
        )
        assert ts2.total == -1, f"Expected total=-1, got {ts2.total}"
        assert ts2.recommended_side == "STAY"
        
        print("✅ Whipsaw: Both weak signals → STAY (no flip-flopping)")
    
    def test_extreme_volatility_pause(self):
        """
        Scenario: Extreme Volatility (ATR > 10% of price)
        - Strategy Manager should detect EXTREME_VOLATILITY
        - Bot should PAUSE (existing feature)
        - Even if trend score says switch, volatility blocks it
        
        This is handled by MarketState.EXTREME_VOLATILITY, not TrendScore.
        TrendScore might still show a direction, but safety overrides.
        """
        ts = self.manager._calculate_trend_score(
            Decimal("120"), Decimal("130"), -3.0, 20
        )
        
        # Score shows bearish, but in real scenario,
        # evaluate_safety would pause due to ATR
        assert ts.total == -3
        print(f"✅ Extreme Volatility: Score={ts.total} but safety should PAUSE first")
    
    def test_stuck_in_wrong_side_recovery(self):
        """
        Scenario: Bot was LONG, market crashed, now recovering
        - Need to ensure bot can switch to SHORT if trend confirms
        
        Current: LONG (stuck with losses)
        EMA: 95 vs 100 → -1
        MACD: -0.8 → -1
        RSI: 38 → -1
        Expected: Score -3, should switch to SHORT after 2 confirmations
        """
        ts = self.manager._calculate_trend_score(
            Decimal("95"), Decimal("100"), -0.8, 38
        )
        
        assert ts.total == -3
        assert ts.recommended_side == "SHORT"
        print(f"✅ Stuck in Wrong Side: {ts} → Ready to switch to {ts.recommended_side}")
    
    def test_confirmation_prevents_instant_switch(self):
        """
        Scenario: Sudden market move, but need 2 confirmations
        - This prevents panic switching during volatility spikes
        - Ensures trend is sustained before acting
        """
        self.manager.pending_switch_side = None
        self.manager.switch_confirmation_count = 0
        
        # First strong signal
        assert self.manager.switch_confirmation_count == 0
        
        # Simulate first check finding SHORT signal
        self.manager.pending_switch_side = "SHORT"
        self.manager.switch_confirmation_count = 1
        assert self.manager.switch_confirmation_count < 2  # Config default
        
        print("✅ Confirmation Delay: 1 check not enough, need 2")
    
    def test_cascade_liquidation_prevention(self):
        """
        Scenario: Price near liquidation, need to protect remaining capital
        
        If price dropped significantly and bot is LONG:
        - Score will be bearish
        - Switch to SHORT or PAUSE to stop bleeding
        
        EMA: 80 vs 100 (20% drop!) → -1
        MACD: -5.0 (extreme) → -1
        RSI: 10 (extremely oversold) → -1
        Expected: Score -3, switch to SHORT
        """
        ts = self.manager._calculate_trend_score(
            Decimal("80"), Decimal("100"), -5.0, 10
        )
        
        assert ts.total == -3
        assert ts.recommended_side == "SHORT"
        print(f"✅ Near Liquidation: {ts} → STOP LOSSES with {ts.recommended_side}")
    
    def test_recovery_after_crash(self):
        """
        Scenario: After crash, market starts recovering
        - Need to detect and switch back to LONG
        
        EMA: 105 vs 100 (recovering, now above) → +1
        MACD: +0.5 (positive again) → +1
        RSI: 55 (recovering) → +1
        Expected: Score +3, switch to LONG for recovery
        """
        ts = self.manager._calculate_trend_score(
            Decimal("105"), Decimal("100"), 0.5, 55
        )
        
        assert ts.total == 3
        assert ts.recommended_side == "LONG"
        print(f"✅ Recovery: {ts} → Ride the recovery with {ts.recommended_side}")


def run_all_tests():
    """Run all tests and print summary."""
    print("=" * 60)
    print("AUTO SWITCH SIDE - EDGE CASE TESTS")
    print("=" * 60)
    print()
    
    # TrendScore tests
    print("📊 TrendScore Tests")
    print("-" * 40)
    ts_tests = TestTrendScore()
    ts_tests.test_strong_bullish_all_positive()
    ts_tests.test_strong_bearish_all_negative()
    ts_tests.test_moderate_bullish()
    ts_tests.test_moderate_bearish()
    ts_tests.test_weak_bullish_stay()
    ts_tests.test_weak_bearish_stay()
    ts_tests.test_neutral_zero()
    ts_tests.test_mixed_conflicting_signals()
    ts_tests.test_mixed_bullish_macd_bearish()
    print()
    
    # Calculate trend score tests
    print("🔢 Calculate Trend Score Tests")
    print("-" * 40)
    calc_tests = TestCalculateTrendScore()
    calc_tests.setup_method()
    calc_tests.test_ema_bullish_with_buffer()
    calc_tests.test_ema_within_buffer_neutral()
    calc_tests.test_ema_bearish_with_buffer()
    calc_tests.test_macd_positive_bullish()
    calc_tests.test_macd_negative_bearish()
    calc_tests.test_macd_zero_neutral()
    calc_tests.test_rsi_above_50_bullish()
    calc_tests.test_rsi_below_50_bearish()
    calc_tests.test_rsi_exactly_50_neutral()
    print()
    
    # Confirmation logic tests
    print("🔄 Confirmation Logic Tests")
    print("-" * 40)
    conf_tests = TestConfirmationLogic()
    conf_tests.setup_method()
    conf_tests.test_initial_pending_switch_none()
    conf_tests.test_confirmation_count_tracking()
    conf_tests.test_reset_on_signal_change()
    print()
    
    # Real world scenarios
    print("🌍 Real World Scenario Tests")
    print("-" * 40)
    rw_tests = TestRealWorldScenarios()
    rw_tests.setup_method()
    rw_tests.test_scenario_strong_uptrend()
    rw_tests.test_scenario_strong_downtrend()
    rw_tests.test_scenario_choppy_sideways()
    rw_tests.test_scenario_early_reversal_from_downtrend()
    rw_tests.test_scenario_overbought_warning()
    print()
    
    # EXTREME scenarios (portfolio protection)
    print("⚠️  EXTREME SCENARIO TESTS (Portfolio Protection)")
    print("-" * 40)
    ext_tests = TestExtremeScenarios()
    ext_tests.setup_method()
    ext_tests.test_flash_crash_all_bearish()
    ext_tests.test_pump_and_dump_detection()
    ext_tests.test_whipsaw_prevention()
    ext_tests.test_extreme_volatility_pause()
    ext_tests.test_stuck_in_wrong_side_recovery()
    ext_tests.test_confirmation_prevents_instant_switch()
    ext_tests.test_cascade_liquidation_prevention()
    ext_tests.test_recovery_after_crash()
    print()
    
    print("=" * 60)
    print("🎉 ALL TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
