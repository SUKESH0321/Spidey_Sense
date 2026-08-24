from typing import Dict, Any

def get_eth_config() -> Dict[str, Any]:
    """
    Returns the isolated configuration for ETH/USDT.
    Uses a factory function to ensure no shared state.
    """
    return {
        "symbol": "ETH/USDT",
        "timeframe": "5m",
        "initial_capital": 5000.0,
        "fee_maker": 0.001,
        "fee_taker": 0.001,
        "thresholds": {
            "rsi_overbought": 70.0,
            "rsi_oversold": 30.0,
        },
        "strategy": {
            # Reversal / trend-reversal parameters used by the Module 2 state machine.
            "min_trend_move_percent": 2.0,
            "buy_reversal_threshold": 3.0,
            "sell_reversal_threshold": 3.0,
            # Module 3a risk parameters: emergency stop-loss and the post-exit
            # cooldown window (in completed candles).
            "stop_loss_pct": 0.05,     # 5% drawdown from entry -> force a SELL
            "cooldown_candles": 3,     # BUY signals are suppressed for 3 candles
            # Module 3b position sizing: fraction of the available USDT balance
            # to commit to a single BUY fill (0.50 = 50% of free cash).
            "position_size_pct": 0.50,
        }
    }
