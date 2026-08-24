"""Core trading state machine, stop-loss override and exit cooldown (Module 3a).

The Module 2 deterministic, extreme-price-tracking state machine (operating on
**completed** OHLCV candles only) is extended with two risk mechanisms:

  * **Emergency stop-loss override** - while HOLDING, a close at or below
    ``entry_price * (1 - stop_loss_pct)`` immediately flattens the position:
    it emits a ``SELL`` signal (exit reason ``STOP_LOSS``) and moves straight
    into ``COOLDOWN``, bypassing the normal drawdown -> confirmation path.
  * **Post-exit cooldown** - every exit (``"REVERSAL"`` or ``"STOP_LOSS"``)
    moves the machine to ``COOLDOWN``. Prices keep updating and candles keep
    feeding, but all BUY signals are rejected for ``cooldown_candles``
    completed candles, after which the machine resets to ``WAITING``.

The state machine (Module 2 + 3a):

    WAITING      --(price drop >= MIN_TREND_MOVE_PERCENT)-->  DOWNTREND
    DOWNTREND    --(rebound >= BUY_REVERSAL_THRESHOLD)-->     READY_TO_BUY
    READY_TO_BUY --(green/upward-close confirmation)-->       HOLDING  (BUY signal)
    HOLDING      --(close <= entry * (1 - stop_loss_pct))-->  COOLDOWN (SELL, STOP_LOSS)
    HOLDING      --(drawdown >= SELL_REVERSAL_THRESHOLD)-->   READY_TO_SELL
    READY_TO_SELL --(red/downward-close confirmation)-->      COOLDOWN (SELL, REVERSAL)
    COOLDOWN     --(cooldown_candles completed candles)-->    WAITING
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# OHLCV keys expected on a candle (dict or pandas Series).
_OHLC_KEYS = ("open", "high", "low", "close")


class TradingState(Enum):
    """States of the trading state machine."""

    WAITING = "WAITING"
    DOWNTREND = "DOWNTREND"
    READY_TO_BUY = "READY_TO_BUY"
    HOLDING = "HOLDING"
    READY_TO_SELL = "READY_TO_SELL"
    COOLDOWN = "COOLDOWN"  # post-exit lock-out window (Module 3a)


class SignalType(Enum):
    """Trade signals emitted by the strategy after a completed candle."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NONE = "NONE"


class ExitReason(Enum):
    """Why the machine left its last open position.

    A bare ``SignalType.SELL`` cannot carry the reason, so the strategy keeps
    the reason in the ``last_exit_reason`` state attribute (also surfaced in
    ``get_snapshot()`` and the console transition line).
    """

    REVERSAL = "REVERSAL"
    STOP_LOSS = "STOP_LOSS"


class StrategyError(Exception):
    """Raised when the strategy receives invalid candle data."""


class BaseStrategy:
    """Deterministic trend/reversal trading state machine.

    The strategy evaluates exactly one **completed** OHLCV candle per
    ``process_candle`` call. It tracks:

      * ``reference_price``  - local peak used to detect the start of a drop
      * ``lowest_price``     - lowest price observed since DOWNTREND was entered
      * ``highest_price``    - highest price observed since the position opened
      * ``entry_price``      - fill price of the open position

    Reversal math (percentages):

    * rebound   = (close - lowest_price) / lowest_price * 100
    * drawdown  = (highest_price - close) / highest_price * 100

    A BUY / SELL signal additionally requires confirmation from the latest
    completed candle: an upward close (green, ``close > open``) for BUY and a
    downward close (red, ``close < open``) for SELL.

    Module 3a adds two guard rails:

      * ``stop_loss_pct``  - while HOLDING, every candle first checks whether
        the close pierced ``entry_price * (1 - stop_loss_pct)``; if so the
        position is flattened immediately with exit reason ``STOP_LOSS``
        (this runs *before* the normal reversal logic).
      * ``cooldown_candles`` - after any exit the machine enters COOLDOWN and
        rejects all BUY signals for this many completed candles, then resets
        to WAITING.
    """

    def __init__(
        self,
        asset_label: str,
        symbol: str,
        timeframe: str,
        min_trend_move_percent: float,
        buy_reversal_threshold: float,
        sell_reversal_threshold: float,
        stop_loss_pct: Optional[float] = None,
        cooldown_candles: int = 0,
    ) -> None:
        """Initialize the strategy with per-asset parameters.

        Args:
            asset_label: Short display name used in logs (e.g. ``"BTC"``).
            symbol: Trading pair (e.g. ``"BTC/USDT"``).
            timeframe: Candle interval (e.g. ``"5m"``).
            min_trend_move_percent: Min drop from the reference point that
                recognizes a ``DOWNTREND`` (noise filter).
            buy_reversal_threshold: Rebound % required to qualify a potential BUY.
            sell_reversal_threshold: Drawdown % from the peak that qualifies a
                potential SELL.
            stop_loss_pct: Emergency stop-loss fraction of the entry price
                (e.g. ``0.05`` = 5%). ``None`` disables the override.
            cooldown_candles: Number of completed candles after an exit
                during which all BUY signals are rejected.
        """
        if min_trend_move_percent <= 0 or min_trend_move_percent >= 100:
            raise ValueError("min_trend_move_percent must be in (0, 100)")
        if buy_reversal_threshold <= 0 or buy_reversal_threshold >= 100:
            raise ValueError("buy_reversal_threshold must be in (0, 100)")
        if sell_reversal_threshold <= 0 or sell_reversal_threshold >= 100:
            raise ValueError("sell_reversal_threshold must be in (0, 100)")
        if stop_loss_pct is not None and not 0 < stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be in (0, 1) or None (disabled)")
        if cooldown_candles < 0:
            raise ValueError("cooldown_candles must be >= 0")

        self.asset_label = asset_label
        self.symbol = symbol
        self.timeframe = timeframe
        self.min_trend_move_percent = min_trend_move_percent
        self.buy_reversal_threshold = buy_reversal_threshold
        self.sell_reversal_threshold = sell_reversal_threshold
        self.stop_loss_pct = stop_loss_pct
        self.cooldown_candles = cooldown_candles

        self.reset()

    # ------------------------------------------------------------------
    # State management / introspection
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Restore the strategy to its pristine WAITING state.

        Clears every state variable, signal and recorded history while
        keeping the asset parameters (label, symbol, thresholds).
        """
        self.current_state: TradingState = TradingState.WAITING
        self.entry_price: Optional[float] = None
        self.lowest_price: Optional[float] = None
        self.highest_price: Optional[float] = None
        self.position_open: bool = False
        self.last_signal: SignalType = SignalType.NONE
        self.reference_price: Optional[float] = None

        # Module 3a: post-exit lock-out bookkeeping.
        self.last_exit_reason: Optional[ExitReason] = None
        self.cooldown_remaining: int = 0

        # Introspection helpers for deterministic tests / debugging.
        self.state_history: List[TradingState] = [TradingState.WAITING]
        self.transition_history: List[Tuple[TradingState, TradingState]] = []
        self._step_count = 0
        self._label = "STEP 0"
        self._buys = 0
        self._sells = 0

    def get_state(self) -> TradingState:
        """Return the current machine state."""
        return self.current_state

    def get_snapshot(self) -> Dict[str, object]:
        """Return a plain dictionary describing the full internal state.

        Useful for tests, dashboards or module-to-module hand-off.
        """
        return {
            "asset_label": self.asset_label,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "current_state": self.current_state.value,
            "position_open": self.position_open,
            "entry_price": self.entry_price,
            "lowest_price": self.lowest_price,
            "highest_price": self.highest_price,
            "reference_price": self.reference_price,
            "last_signal": self.last_signal.value,
            "stop_loss_pct": self.stop_loss_pct,
            "cooldown_candles": self.cooldown_candles,
            "cooldown_remaining": self.cooldown_remaining,
            "last_exit_reason": (
                self.last_exit_reason.value if self.last_exit_reason is not None else None
            ),
            "step_count": self._step_count,
            "buys": self._buys,
            "sells": self._sells,
        }

    def reset_history(self) -> None:
        """Clear the inspection history but keep the machine state intact."""
        self.state_history = [self.current_state]
        self.transition_history = []

    # ------------------------------------------------------------------
    # Candle processing entry point
    # ------------------------------------------------------------------
    def process_candle(self, candle: dict | pd.Series) -> SignalType:
        """Evaluate one completed OHLCV candle and return a trade signal.

        This is the only public entry point of the state machine. The method:

        1. extracts/validates ``open/high/low/close``.
        2. updates extreme prices (``lowest_price`` / ``highest_price``).
        3. evaluates the current state, applies at most one logical transition
           per stage and returns a ``SignalType``.

        Args:
            candle: Completed OHLCV candle as a dict or a pandas Series with
                the keys ``open/high/low/close`` (``volume`` optional).

        Returns:
            SignalType.BUY / SELL when a confirmation-complete reversal is
            detected, SignalType.HOLD while a position is running, otherwise
            SignalType.NONE.

        Note:
            A returned ``SignalType.SELL`` alone does not reveal *why* the
            exit happened; the reason is exposed on the ``last_exit_reason``
            attribute (``"REVERSAL"`` or ``"STOP_LOSS"``) and in the snapshot.

        Raises:
            StrategyError: if the candle is missing fields or prices invalid.
        """
        open_, high, low, close = self._extract_candle(candle)
        self._step_count += 1
        self._label_candle(candle)

        # Dispatch to the handler for the current state. Some handlers chain
        # into the next state's handler so a single candle can move the
        # machine through several deterministic stages at once.
        state = self.current_state
        if state is TradingState.WAITING:
            signal = self._handle_waiting(open_, high, low, close)
        elif state is TradingState.DOWNTREND:
            signal = self._handle_downtrend(open_, high, low, close)
        elif state is TradingState.READY_TO_BUY:
            signal = self._handle_ready_to_buy(open_, high, low, close)
        elif state is TradingState.HOLDING:
            signal = self._handle_holding(open_, high, low, close)
        elif state is TradingState.READY_TO_SELL:
            signal = self._handle_ready_to_sell(open_, high, low, close)
        else:  # TradingState.COOLDOWN
            signal = self._handle_cooldown(open_, high, low, close)

        self.last_signal = signal
        return signal

    # ------------------------------------------------------------------
    # WAITING
    # ------------------------------------------------------------------
    def _handle_waiting(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Watch for a drop of at least ``min_trend_move_percent``.

        The reference point is the highest close seen while waiting, which
        filters out micro-fluctuation noise.
        """
        if self.reference_price is None or self.reference_price <= 0:
            self.reference_price = close
            return SignalType.NONE

        if close > self.reference_price:
            self.reference_price = close

        drop_pct = (self.reference_price - close) / self.reference_price * 100.0
        if drop_pct < self.min_trend_move_percent:
            return SignalType.NONE

        # A trend is recognized: start tracking the extreme low.
        self.lowest_price = low
        self._transition(
            TradingState.DOWNTREND,
            extreme=self.lowest_price,
            signal=SignalType.NONE,
        )
        # The triggering candle may already contain an internal rebound;
        # evaluate it right away to keep the decision deterministic.
        return self._handle_downtrend(open_, high, low, close)

    # ------------------------------------------------------------------
    # DOWNTREND
    # ------------------------------------------------------------------
    def _handle_downtrend(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Track the extreme low and qualify a potential BUY on rebound."""
        assert self.lowest_price is not None
        self._update_lowest(low)

        rebound_pct = (close - self.lowest_price) / self.lowest_price * 100.0
        if rebound_pct < self.buy_reversal_threshold:
            return SignalType.NONE

        self._transition(
            TradingState.READY_TO_BUY,
            extreme=self.lowest_price,
            signal=SignalType.NONE,
        )
        return self._handle_ready_to_buy(open_, high, low, close)

    # ------------------------------------------------------------------
    # READY_TO_BUY
    # ------------------------------------------------------------------
    def _handle_ready_to_buy(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Require candle confirmation (upward close) before emitting BUY."""
        assert self.lowest_price is not None
        self._update_lowest(low)

        rebound_pct = (close - self.lowest_price) / self.lowest_price * 100.0
        if rebound_pct < self.buy_reversal_threshold:
            # The extreme-reversal conditions no longer hold: back to trend.
            self._transition(
                TradingState.DOWNTREND,
                extreme=self.lowest_price,
                signal=SignalType.NONE,
            )
            return SignalType.NONE

        # Confirmation: the latest completed candle must close green (upward).
        if close <= open_:
            return SignalType.NONE

        # Confirmed reversal -> open a position.
        self.entry_price = close
        self.highest_price = max(high, close)
        self.position_open = True
        self._buys += 1
        self._transition(
            TradingState.HOLDING,
            extreme=self.highest_price,
            signal=SignalType.BUY,
        )
        return SignalType.BUY

    # ------------------------------------------------------------------
    # HOLDING
    # ------------------------------------------------------------------
    def _handle_holding(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Track the peak price, watching the stop-loss *first*.

        While a position is open this handler performs, in order:

        1. an emergency stop-loss check against ``entry_price`` (Module 3a);
        2. the normal drawdown -> READY_TO_SELL -> confirmation path.

        The stop-loss runs before the reversal logic so a catastrophic candle
        can never be intercepted by the two-step drawdown confirmation.
        """
        assert self.highest_price is not None
        assert self.entry_price is not None
        self._update_highest(high)

        # 1. Emergency stop-loss override (Module 3a) - checked first.
        if self._stop_loss_hit(close):
            return self._exit_to_cooldown(
                price=close,
                exit_reason=ExitReason.STOP_LOSS,
            )

        # 2. Normal reversal logic.
        drawdown_pct = (self.highest_price - close) / self.highest_price * 100.0
        if drawdown_pct < self.sell_reversal_threshold:
            return SignalType.HOLD

        self._transition(
            TradingState.READY_TO_SELL,
            extreme=self.highest_price,
            signal=SignalType.HOLD,
        )
        return self._handle_ready_to_sell(open_, high, low, close)

    # ------------------------------------------------------------------
    # READY_TO_SELL
    # ------------------------------------------------------------------
    def _handle_ready_to_sell(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Require candle confirmation (red close) before emitting SELL."""
        assert self.highest_price is not None
        self._update_highest(high)

        drawdown_pct = (self.highest_price - close) / self.highest_price * 100.0
        if drawdown_pct < self.sell_reversal_threshold:
            # Price recovered: we are holding again.
            self._transition(
                TradingState.HOLDING,
                extreme=self.highest_price,
                signal=SignalType.HOLD,
            )
            return SignalType.HOLD

        # Confirmation: the latest completed candle must close red (downward).
        if close >= open_:
            return SignalType.HOLD

        # Confirmed reversal -> flatten and enter the post-exit COOLDOWN.
        return self._exit_to_cooldown(
            price=close,
            exit_reason=ExitReason.REVERSAL,
        )

    # ------------------------------------------------------------------
    # COOLDOWN
    # ------------------------------------------------------------------
    def _handle_cooldown(self, open_: float, high: float, low: float, close: float) -> SignalType:
        """Post-exit lock-out: reject every BUY and count down the candles.

        Prices keep flowing through the process but no trade signal is ever
        emitted while cooldown is active. After ``cooldown_candles`` completed
        candles the machine resets to WAITING and the current candle is
        evaluated by the WAITING handler right away.
        """
        if self.cooldown_remaining is None or self.cooldown_remaining < 0:
            self.cooldown_remaining = 0

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return SignalType.NONE

        # Cooldown elapsed: hand control back to WAITING.
        self._transition(
            TradingState.WAITING,
            extreme=self.reference_price,
            signal=SignalType.NONE,
        )
        return self._handle_waiting(open_, high, low, close)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _stop_loss_hit(self, close: float) -> bool:
        """Return True when ``close`` breached the emergency stop-loss.

        The stop price is ``entry_price * (1 - stop_loss_pct)``; a close at or
        below it qualifies. The stop is disabled when ``stop_loss_pct`` is None.
        """
        if self.stop_loss_pct is None or self.entry_price is None:
            return False
        return close <= self.entry_price * (1.0 - self.stop_loss_pct)

    def _exit_to_cooldown(self, price: float, exit_reason: ExitReason) -> SignalType:
        """Flatten an open position and enter the post-exit COOLDOWN window.

        Used by both exit paths - a confirmation-complete ``"REVERSAL"`` SELL
        and the emergency ``"STOP_LOSS"`` override. It clears every position
        field, records the exit reason, seeds the cooldown countdown and emits
        a single ``SELL`` signal.
        """
        self.position_open = False
        self.entry_price = None
        self.lowest_price = None
        self.highest_price = None
        self.reference_price = price
        self.last_exit_reason = exit_reason
        self.cooldown_remaining = self.cooldown_candles
        self._sells += 1

        self._transition(
            TradingState.COOLDOWN,
            extreme=price,
            signal=SignalType.SELL,
            exit_reason=exit_reason,
        )
        return SignalType.SELL

    def _update_lowest(self, low: float) -> None:
        """Fold a candle low into the tracked extreme-low price."""
        self.lowest_price = low if self.lowest_price is None else min(self.lowest_price, low)
        if self.lowest_price <= 0:
            raise StrategyError(
                f"[{self.asset_label}] non-positive extreme low {self.lowest_price}"
            )

    def _update_highest(self, high: float) -> None:
        """Fold a candle-high into the tracked extreme-high price."""
        self.highest_price = max(self.highest_price, high)
        if self.highest_price <= 0:
            raise StrategyError(
                f"[{self.asset_label}] non-positive extreme high {self.highest_price}"
            )

    def _extract_candle(self, candle: dict | pd.Series) -> Tuple[float, float, float, float]:
        """Validate and normalize the OHLC fields of one completed candle."""
        missing = [k for k in _OHLC_KEYS if k not in candle]
        if missing:
            raise StrategyError(f"[{self.asset_label}] candle missing keys: {missing}")

        values: List[float] = []
        for key in _OHLC_KEYS:
            try:
                value = float(candle[key])
            except (TypeError, ValueError) as exc:
                raise StrategyError(
                    f"[{self.asset_label}] non-numeric candle[{key!r}]: {candle[key]!r}"
                ) from exc
            if value <= 0:
                raise StrategyError(
                    f"[{self.asset_label}] candle[{key!r}] must be positive, got {value}"
                )
            values.append(value)

        open_, high, low, close = values
        if low > high or low > close or low > open_ or high < close or high < open_:
            raise StrategyError(
                f"[{self.asset_label}] invalid OHLC ordering: "
                f"O={open_} H={high} L={low} C={close}"
            )
        return open_, high, low, close

    def _label_candle(self, candle: dict | pd.Series) -> None:
        """Build the log label from the candle's timestamp, if present."""
        timestamp = candle.get("timestamp") if isinstance(candle, dict) else None
        self._label = str(timestamp) if timestamp is not None else f"STEP {self._step_count}"

    def _transition(
        self,
        new_state: TradingState,
        extreme: Optional[float],
        signal: SignalType,
        *,
        exit_reason: Optional[ExitReason] = None,
    ) -> None:
        """Apply a state transition, record it and print the console line.

        Args:
            new_state: Target state of the transition.
            extreme: Reference/peak/exit price shown on the console line.
            signal: Signal emitted by the transition (``SignalType``).
            exit_reason: Why a position was flattened, if the transition is an
                exit. Recorded on ``last_exit_reason`` and printed.
        """
        old_state = self.current_state
        self.current_state = new_state
        self.state_history.append(new_state)
        self.transition_history.append((old_state, new_state))
        self.last_signal = signal
        self._print_transition(
            old_state,
            new_state,
            extreme,
            signal,
            exit_reason=exit_reason,
        )

    def _print_transition(
        self,
        old_state: TradingState,
        new_state: TradingState,
        extreme: Optional[float],
        signal: SignalType,
        *,
        exit_reason: Optional[ExitReason] = None,
    ) -> None:
        """Render: [TIMESTAMP/STEP] [ASSET] State: X -> Y | Extreme Price: Z | Signal: ACTION"""
        extreme_txt = "N/A" if extreme is None else f"{extreme:.4f}"
        reason_txt = f" | Exit Reason: {exit_reason.value}" if exit_reason is not None else ""
        print(
            f"[{self._label}] [{self.asset_label}] "
            f"State: {old_state.value} -> {new_state.value} | "
            f"Extreme Price: {extreme_txt} | Signal: {signal.value}{reason_txt}"
        )

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(asset={self.asset_label!r}, "
            f"state={self.current_state.value}, position_open={self.position_open})"
        )