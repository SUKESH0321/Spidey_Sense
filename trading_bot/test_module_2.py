"""Synthetic, deterministic unit & state tests for Module 2.

Covers three scenarios:

  A. Standard trend & reversal cycle
      100 -> 96 -> 92 -> 88 (low) -> 91 (rebound) -> 94 (confirmed buy)
      -> 100 -> 108 -> 112 (high) -> 107 (confirmed sell)
     Asserts the exact state order
     WAITING -> DOWNTREND -> READY_TO_BUY -> HOLDING -> READY_TO_SELL -> WAITING.

  B. Sideways-market noise (no micro-fluctuation trading)
     100.0 -> 100.2 -> 99.8 -> 100.1 -> 99.9 -> 100.05
     Asserts the machine stays in WAITING with zero BUY/SELL signals.

  C. Asset state isolation
     BTC crashes into DOWNTREND while ETH stays flat in WAITING.

Run from the trading_bot directory:  python test_module_2.py
"""

from __future__ import annotations

import sys
from typing import Dict, List

from config.btc_config import get_btc_config
from config.eth_config import get_eth_config
from strategy.base_strategy import (
    BaseStrategy,
    ExitReason,
    SignalType,
    TradingState,
)
from strategy.btc_strategy import BTCStrategy
from strategy.eth_strategy import ETHStrategy

VOLUME = 1000.0


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


def run_scenario_a() -> bool:
    """Standard trend & reversal cycle (buy on confirmed rebound, sell on confirmed drop)."""
    print("=" * 72)
    print("Scenario A: Standard Trend & Reversal Cycle")
    print(" 100 -> 96 -> 92 -> 88 (low) -> 91 (rebound) -> 94 (buy)")
    print(" 100 -> 108 -> 112 (high) -> 107 (sell)")
    print("-" * 72)

    candles = [
        make_candle(100.0, 100.0),            # 1 seed reference
        make_candle(96.0, 100.0),             # 2 drop 4% -> DOWNTREND
        make_candle(92.0, 96.0),              # 3 deeper low
        make_candle(88.0, 91.0, low=87.8),    # 4 extreme low
        make_candle(91.0, 93.0, low=90.5),    # 5 rebound, red candle -> READY_TO_BUY
        make_candle(94.0, 93.0),              # 6 green confirmation -> BUY
        make_candle(100.0, 94.0),             # 7 hold
        make_candle(108.0, 100.0),            # 8 hold
        make_candle(112.0, 108.0),            # 9 new high
        make_candle(107.0, 112.0, low=106.5), # 10 red confirmation -> SELL
    ]

    strategy: BaseStrategy = BTCStrategy()
    failures: List[str] = []
    signals: List[SignalType] = []

    for step, candle in enumerate(candles, start=1):
        signal = strategy.process_candle(candle)
        signals.append(signal)
        print(f"    step {step:>2} close={candle['close']:>7.2f} -> "
              f"state={strategy.get_state().value:<12} signal={signal.value}")

    # --- Signals per candle ------------------------------------------------
    expected_signals = [
        SignalType.NONE, SignalType.NONE, SignalType.NONE, SignalType.NONE,
        SignalType.NONE, SignalType.BUY,  SignalType.HOLD, SignalType.HOLD,
        SignalType.HOLD, SignalType.SELL,
    ]
    check(signals == expected_signals,
          f"signal sequence mismatch:\n  expected={expected_signals}\n  actual  ={signals}",
          failures)

    # --- State sequence in the exact expected order ------------------------
    expected_states = [
        TradingState.WAITING,
        TradingState.DOWNTREND,
        TradingState.READY_TO_BUY,
        TradingState.HOLDING,
        TradingState.READY_TO_SELL,
        TradingState.COOLDOWN,  # Module 3a: every exit lands in COOLDOWN first
    ]
    check(strategy.state_history == expected_states,
          f"state sequence mismatch:\n  expected={expected_states}\n  actual  ={strategy.state_history}",
          failures)

    # --- Explicit transition list ------------------------------------------
    expected_transitions = [
        (TradingState.WAITING, TradingState.DOWNTREND),
        (TradingState.DOWNTREND, TradingState.READY_TO_BUY),
        (TradingState.READY_TO_BUY, TradingState.HOLDING),
        (TradingState.HOLDING, TradingState.READY_TO_SELL),
        (TradingState.READY_TO_SELL, TradingState.COOLDOWN),
    ]
    check(strategy.transition_history == expected_transitions,
          f"transition history mismatch:\n  expected={expected_transitions}\n  actual  ={strategy.transition_history}",
          failures)

    # --- Key book keeping after the full cycle -----------------------------
    check(strategy.position_open is False, "position should be closed after SELL", failures)
    check(strategy.entry_price is None, "entry_price should be cleared after SELL", failures)
    check(strategy.highest_price is None, "highest_price should be cleared after SELL", failures)
    check(strategy.lowest_price is None, "lowest_price should be cleared after SELL", failures)
    check(strategy.reference_price == 107.0, "reference should reset to the sell price", failures)
    check(strategy.last_exit_reason == ExitReason.REVERSAL,
          f"exit reason should be REVERSAL, got {strategy.last_exit_reason}", failures)
    check(strategy.cooldown_remaining == strategy.cooldown_candles,
          "cooldown should be armed after an exit", failures)
    check(strategy._buys == 1 and strategy._sells == 1,
          f"expected exactly 1 buy and 1 sell, got buys={strategy._buys} sells={strategy._sells}",
          failures)

    # --- Snapshot / reset utilities ----------------------------------------
    snapshot = strategy.get_snapshot()
    check(snapshot["current_state"] == TradingState.COOLDOWN.value,
          "snapshot state mismatch", failures)
    check(snapshot["last_exit_reason"] == ExitReason.REVERSAL.value,
          "snapshot exit reason mismatch", failures)
    strategy.reset()
    pristine = [
        strategy.current_state == TradingState.WAITING,
        not strategy.position_open,
        strategy.entry_price is None,
        strategy.lowest_price is None,
        strategy.highest_price is None,
        strategy.reference_price is None,
        strategy.last_signal == SignalType.NONE,
        strategy.last_exit_reason is None,
        strategy.cooldown_remaining == 0,
        strategy.state_history == [TradingState.WAITING],
        not strategy.transition_history,
    ]
    check(all(pristine), "reset() did not fully restore the initial state", failures)

    ok = not failures
    print("Scenario A:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok
def run_scenario_b() -> bool:
    """Scenario B: noisy, low-volatility chop must not trigger any trading."""
    print("=" * 72)
    print("Scenario B: Sideways Market Noise (no micro-fluctuation trading)")
    print(" 100.0 -> 100.2 -> 99.8 -> 100.1 -> 99.9 -> 100.05")
    print("-" * 72)

    candles = [
        make_candle(100.0, 100.0),
        make_candle(100.2, 100.0),
        make_candle(99.8, 100.2),
        make_candle(100.1, 99.8),
        make_candle(99.9, 100.1),
        make_candle(100.05, 99.9),
    ]

    strategy: BaseStrategy = BTCStrategy()
    failures: List[str] = []

    for step, candle in enumerate(candles, start=1):
        signal = strategy.process_candle(candle)
        check(strategy.get_state() == TradingState.WAITING,
              f"step {step}: expected WAITING, got {strategy.get_state().value}", failures)
        check(signal not in (SignalType.BUY, SignalType.SELL),
              f"step {step}: unexpected {signal.value} signal in a noisy market", failures)

    check(not strategy.transition_history,
          "chop produced transitions even though nothing should fire", failures)
    check(strategy.reference_price == 100.2, "reference should be the chop top (100.2)", failures)
    check(strategy.position_open is False and strategy.entry_price is None,
          "no position may open in Scenario B", failures)

    ok = not failures
    print("Scenario B:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok


def run_scenario_c() -> bool:
    """Scenario C: BTC crashes while ETH stays flat -> per-asset state isolation."""
    print("=" * 72)
    print("Scenario C: Asset State Isolation (BTC crashes, ETH stays flat)")
    print("-" * 72)

    btc: BTCStrategy = BTCStrategy()
    eth: ETHStrategy = ETHStrategy()
    failures: List[str] = []

    # Configs must be independent objects produced by the factory functions.
    btc_cfg = get_btc_config()
    eth_cfg = get_eth_config()
    check(btc_cfg is not eth_cfg, "config factories must return fresh dicts", failures)
    check(btc.config is not eth.config, "strategy configs must not be shared", failures)

    # BTC gets a crashing series; ETH gets flat (sideways) candles.
    btc_crash = [
        make_candle(100.0, 100.0),            # reference seed
        make_candle(95.0, 100.0),             # -5% drop -> DOWNTREND
        make_candle(91.0, 95.0, low=90.6),
        make_candle(88.0, 91.0, low=87.4),
    ]
    eth_flat = [
        make_candle(100.0, 100.0),
        make_candle(100.1, 100.0),
        make_candle(99.9, 100.1),
        make_candle(100.0, 99.9),
    ]

    for btc_candle, eth_candle in zip(btc_crash, eth_flat):
        btc.process_candle(btc_candle)
        eth.process_candle(eth_candle)

    # --- BTC moved into DOWNTREND and tracked the crash extreme -------------
    check(btc.get_state() == TradingState.DOWNTREND,
          f"BTC should be in DOWNTREND, got {btc.get_state().value}", failures)
    check(btc.lowest_price == 87.4,
          f"BTC lowest_price should be 87.4, got {btc.lowest_price}", failures)
    check(btc.position_open is False, "BTC must not hold a position", failures)

    # --- ETH was never touched by the crash ---------------------------------
    check(eth.get_state() == TradingState.WAITING,
          f"ETH must stay WAITING, got {eth.get_state().value}", failures)
    check(eth.lowest_price is None, "ETH extreme lows must not be polluted by BTC", failures)
    check(eth.highest_price is None, "ETH extreme highs must not be polluted by BTC", failures)
    check(eth.entry_price is None and not eth.position_open,
          "ETH position fields must be intact", failures)

    # Feeding ETH a genuine crash of its own must not mutate BTC's state.
    eth.process_candle(make_candle(93.0, 96.0))
    check(eth.get_state() == TradingState.DOWNTREND,
          "ETH should react to its own candles", failures)
    check(btc.get_state() == TradingState.DOWNTREND,
          "BTC state must not be mutated by ETH processing", failures)

    ok = not failures
    print("Scenario C:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok


def main() -> int:
    """Run all three scenarios and print a summary."""
    results = [
        ("A: Standard Trend & Reversal Cycle", run_scenario_a()),
        ("B: Sideways Market Noise", run_scenario_b()),
        ("C: Asset State Isolation", run_scenario_c()),
    ]

    print("=" * 72)
    all_ok = True
    for name, ok in results:
        print(f"{name:<40} {'PASS' if ok else 'FAIL'}")
        all_ok = all_ok and ok
    print("=" * 72)
    print("OVERALL RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())