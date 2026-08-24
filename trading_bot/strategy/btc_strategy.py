"""BTC/USDT trading strategy (Module 2).

Subclass of ``BaseStrategy`` fully initialized from ``config/btc_config.py``.
Instances keep their own state; no mutable state is shared across assets.
"""

from __future__ import annotations

from config.btc_config import get_btc_config
from strategy.base_strategy import BaseStrategy


class BTCStrategy(BaseStrategy):
    """BTC/USDT trend/reversal strategy."""

    def __init__(self) -> None:
        """Load the BTC configuration and wire it into the state machine."""
        # Fresh dict per call -> no shared mutable configuration.
        cfg = get_btc_config()
        self.config = cfg

        strategy_params = cfg["strategy"]
        super().__init__(
            asset_label="BTC",
            symbol=cfg["symbol"],
            timeframe=cfg["timeframe"],
            min_trend_move_percent=strategy_params["min_trend_move_percent"],
            buy_reversal_threshold=strategy_params["buy_reversal_threshold"],
            sell_reversal_threshold=strategy_params["sell_reversal_threshold"],
            stop_loss_pct=strategy_params["stop_loss_pct"],
            cooldown_candles=strategy_params["cooldown_candles"],
        )