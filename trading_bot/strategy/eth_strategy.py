"""ETH/USDT trading strategy (Module 2).

Subclass of ``BaseStrategy`` fully initialized with ``config/eth_config.py``.
Instances own their state; no mutable state is shared across assets.
"""

from __future__ import annotations

from config.eth_config import get_eth_config
from strategy.base_strategy import BaseStrategy


class ETHStrategy(BaseStrategy):
    """ETH/USDT trend/reversal strategy."""

    def __init__(self) -> None:
        """Load the ETH configuration and wire it into the state machine."""
        # Fresh: get() returns a new dict per call -> no shared mutable config.
        cfg = get_eth_config()
        self.config = cfg

        strategy_params = cfg["strategy"]
        super().__init__(
            asset_label="ETH",
            symbol=cfg["symbol"],
            timeframe=cfg["timeframe"],
            min_trend_move_percent=strategy_params["min_trend_move_percent"],
            buy_reversal_threshold=strategy_params["buy_reversal_threshold"],
            sell_reversal_threshold=strategy_params["sell_reversal_threshold"],
            stop_loss_pct=strategy_params["stop_loss_pct"],
            cooldown_candles=strategy_params["cooldown_candles"],
        )