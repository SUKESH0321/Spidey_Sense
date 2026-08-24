"""Deterministic verification tests for Module 3a.

Module 3a adds two risk mechanisms to the Module 2 state machine:

  1. **Stop-Loss Override** - while HOLDING, a candle close at or below
     ``entry_price * (1 - stop_loss_pct)`` immediately flattens the position:
     it emits a SELL signal (exit reason ``STOP_LOSS``) and moves straight to
     COOLDOWN, bypassing the normal two-step drawdown confirmation.
  2. **Post-Exit Cooldown** - after any exit the machine rejects every BUY
     signal for ``cooldown_candles`` completed candles, then resets to WAITING.

This script is fully synthetic and deterministic:

  Test 1 (Stop-Loss Trigger)
     Enter at 100.0 with stop_loss_pct = 0.05 (stop price 95.0).
     Feed 100 -> 97 -> 94. At 94 the machine must move HOLDING -> COOLDOWN,
     emit SELL and record last_exit_reason == "STOP_LOSS".

  Test 2 (Cooldown Suppression)
     Trigger a STOP_LOSS exit with cooldown_candles = 3, then feed 3 strongly
     bullish candles that would otherwise set up a fresh buy. State must stay
     COOLDOWN with zero BUY signals for all 3 candles, and the 4th candle must
     reset the machine to WAITING.

Run from the trading_bot directory:  python test_module_3a.py
"""

from __future__ import annotations

import sys
from typing import Dict, List

from strategy.base_strategy import BaseStrategy, ExitReason, SignalType, TradingState

VOLUME = 1000.0

# Deliberately high sell-reversal threshold so the normal exit path can never
# fire before the stop-loss in these tests.
_SELL_REVERSAL_THRESHOLD = 15.0


def make_candle(
    close: float,
    open_: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: float = VOLUME,
) -> Dict[str, float]:
    """Build a deterministic OHLCV candle dict from a close price."""
    hi = max(open_, close) if high is None else high
    lo = min(open_, close) if low is None else low
    return {
        "open": float(open_),
        "high": float(hi),
        "low": float(lo),
        "close": float(close),
        "volume": float(volume),
    }


def check(condition: bool, message: str, failures: List[str]) -> None:
    """Append a message to ``failures`` when ``condition`` is False."""
    if not condition:
        failures.append(message)


def build_strategy(
    *,
    stop_loss_pct: float | None = 0.05,
    cooldown_candles: int = 3,
) -> BaseStrategy:
    """Construct a bare, deterministic strategy for Module 3a tests."""
    return BaseStrategy(
        asset_label="TEST",
        symbol="T/USDT",
        timeframe="1m",
        min_trend_move_percent=2.0,
        buy_reversal_threshold=3.0,
        sell_reversal_threshold=_SELL_REVERSAL_THRESHOLD,
        stop_loss_pct=stop_loss_pct,
        cooldown_candles=cooldown_candles,
    )


def drive_into_holding(strategy: BaseStrategy, failures: List[str]) -> None:
    """Run a scripted cycle so the machine opens a position at 100.0."""
    entry_candles = [
        make_candle(100.0, 100.0),             # seed reference -> WAITING
        make_candle(96.0, 100.0),              # -4% -> DOWNTREND
        make_candle(92.0, 96.0, low=91.5),     # deeper low
        make_candle(88.0, 91.0, low=87.5),     # extreme low
        make_candle(91.0, 93.0, low=90.5),     # rebound -> READY_TO_BUY
        make_candle(100.0, 91.0),              # green confirmation -> BUY @ 100
    ]
    for candle in entry_candles:
        strategy.process_candle(candle)

    check(strategy.get_state() == TradingState.HOLDING,
          f"setup: expected HOLDING, got {strategy.get_state().value}", failures)
    check(strategy.entry_price == 100.0,
          f"setup: expected entry at 100.0, got {strategy.entry_price}", failures)


def run_test_1() -> bool:
    """Stop-loss fires before the normal reversal logic and records the reason."""
    print("=" * 72)
    print("Test 1: Stop-Loss Trigger (enter @ 100, feed 100 -> 97 -> 94)")
    print("  stop_loss_pct = 0.05  ->  stop price = 95.0")
    print("-" * 72)

    strategy = build_strategy(stop_loss_pct=0.05, cooldown_candles=3)
    failures: List[str] = []
    drive_into_holding(strategy, failures)

    # Candle at 97: below entry but ABOVE the 95.0 stop, so it must hold.
    signal_97 = strategy.process_candle(make_candle(97.0, 100.0))
    print(f"    close= 97.00 -> state={strategy.get_state().value:<12} "
          f"signal={signal_97.value}")
    check(signal_97 == SignalType.HOLD,
          f"97 should be a plain HOLD (no stop hit), got {signal_97.value}", failures)
    check(strategy.get_state() == TradingState.HOLDING,
          "state must stay HOLDING above the stop price", failures)

    # Price at 94: <= 95.0 -> emergency stop-loss exit.
    signal_94 = strategy.process_candle(make_candle(94.0, 97.0))
    print(f"    close= 94.00 -> state={strategy.get_state().value:<12} "
          f"signal={signal_94.value}  exit_reason="
          f"{strategy.last_exit_reason.value if strategy.last_exit_reason else None}")

    check(signal_94 == SignalType.SELL,
          f"at 94 expected a SELL signal, got {signal_94.value}", failures)
    check(strategy.get_state() == TradingState.COOLDOWN,
          f"after stop-loss state must be COOLDOWN, "
          f"got {strategy.get_state().value}", failures)
    check(strategy.last_exit_reason == ExitReason.STOP_LOSS,
          f"exit reason must be STOP_LOSS, got {strategy.last_exit_reason}", failures)
    check(strategy.cooldown_remaining == 3,
          f"cooldown must be armed to 3, got {strategy.cooldown_remaining}", failures)
    check(not strategy.position_open and strategy.entry_price is None,
          "position must be flattened after the stop-loss exit", failures)
    check(strategy._sells == 1, f"expected 1 sell, got sells={strategy._sells}", failures)

    # Snapshot exposes the reason as a plain string as well.
    snapshot = strategy.get_snapshot()
    check(snapshot["last_exit_reason"] == ExitReason.STOP_LOSS.value,
          f"snapshot exit reason mismatch: {snapshot['last_exit_reason']}", failures)

    ok = not failures
    print("-" * 72)
    print("Test 1:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok


def run_test_2() -> bool:
    """Cooldown suppresses BUY signals for cooldown_candles, then resets to WAITING."""
    print("=" * 72)
    print("Test 2: Cooldown Suppression (cooldown_candles = 3)")
    print("=" * 72)

    strategy = build_strategy(stop_loss_pct=0.05, cooldown_candles=3)
    failures: List[str] = []
    drive_into_holding(strategy, failures)

    # Trigger an exit -> STOP_LOSS at 94 -> COOLDOWN armed with 3 candles.
    exit_signal = strategy.process_candle(make_candle(94.0, 97.0))
    check(exit_signal == SignalType.SELL
          and strategy.get_state() == TradingState.COOLDOWN,
          "setup exit must produce a SELL and enter COOLDOWN", failures)
    print(f"    exit candle close= 94.00 -> state={strategy.get_state().value} "
          f"signal={exit_signal.value}  reason={strategy.last_exit_reason.value}")

    # Three strongly bullish candles.
    #   - Without the cooldown they would (eventually) qualify a fresh BUY.
    #   - Inside COOLDOWN every candle must return NONE and keep the state.
    suppressed = [
        make_candle(98.0, 94.0),
        make_candle(103.0, 98.0),
        make_candle(107.0, 103.0),
    ]
    for step, candle in enumerate(suppressed, start=1):
        signal = strategy.process_candle(candle)
        print(f"    cooldown candle {step}: close={candle['close']:>6.2f} -> "
              f"state={strategy.get_state().value:<10} signal={signal.value}")
        check(signal == SignalType.NONE,
              f"cooldown candle {step}: must be suppressed to NONE, "
              f"got {signal.value}", failures)
        check(strategy.get_state() == TradingState.COOLDOWN,
              f"cooldown candle {step}: state must remain COOLDOWN, "
              f"got {strategy.get_state().value}", failures)

    check(strategy.cooldown_remaining == 0,
          f"cooldown counter should be exhausted, got {strategy.cooldown_remaining}",
          failures)

    # The 4th candle after the exit: cooldown elapsed -> back to WAITING.
    fourth = make_candle(110.0, 107.0)
    signal_4 = strategy.process_candle(fourth)
    print(f"        candle 4      close={fourth['close']:>6.2f} -> "
          f"state={strategy.get_state().value:<10} signal={signal_4.value}")
    check(strategy.get_state() == TradingState.WAITING,
          f"4th candle must reset to WAITING, got {strategy.get_state().value}",
          failures)
    check(signal_4 == SignalType.NONE,
          f"4th candle is a fresh WAITING tick, expected NONE, got {signal_4.value}",
          failures)
    check(strategy.cooldown_remaining == 0,
          "cooldown_remaining must stay 0 after reset", failures)
    check(strategy.reference_price == 110.0,
          "WAITING reference should absorb the rising candle", failures)

    # reset() must fully clear the module-3a fields.
    strategy.reset()
    pristine = [
        strategy.get_state() == TradingState.WAITING,
        strategy.last_exit_reason is None,
        strategy.cooldown_remaining == 0,
        not strategy.position_open,
        strategy.entry_price is None,
    ]
    check(all(pristine), "reset() must clear cooldown/exit-reason state", failures)

    ok = not failures
    print("-" * 72)
    print("Test 2:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok


def main() -> int:
    """Run both Module 3a tests and print a summary."""
    results = [
        ("1: Stop-Loss Trigger", run_test_1()),
        ("2: Cooldown Suppression", run_test_2()),
    ]

    print("=" * 72)
    all_ok = True
    for name, ok in results:
        print(f"{name:<28} {'PASS' if ok else 'FAIL'}")
        all_ok = all_ok and ok
    print("=" * 72)
    print("OVERALL RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())