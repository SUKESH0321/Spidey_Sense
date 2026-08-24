"""Trading strategy package (Modules 2 + 3a)."""

from strategy.base_strategy import (
    BaseStrategy,
    ExitReason,
    SignalType,
    TradingState,
)

__all__ = ["BaseStrategy", "ExitReason", "SignalType", "TradingState"]