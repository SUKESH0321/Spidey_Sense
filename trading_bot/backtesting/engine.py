"""Module 4a: zero-look-ahead backtesting engine (candle-by-candle spot simulation).

This engine replays historical OHLCV candles *sequentially*. At every candle ``t``
only the just-completed candle is visible to the strategy state machine
(Module 2 + 3a) and the RiskManager (Module 3b) before any execution is
simulated - future candles are never inspected, eliminating look-ahead bias.

Execution modelling (spot, deterministic):

    * ``BUY``  - fill at ``close * (1 + slippage_pct)``; the committed USDT
      notional (from :class:`RiskManager`) plus the entry trading fee is
      deducted from the cash balance, and the base-asset quantity is credited.
    * ``SELL`` - fill at ``close * (1 - slippage_pct)``; the exit proceeds
      minus the exit trading fee is added to cash. The reason for the exit
      (``"REVERSAL"`` or ``"STOP_LOSS"``) is captured from the strategy.

The final :class:`BacktestResult` carries the full trade log and a per-candle
equity curve ready for the Module 4b metrics calculator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from risk.risk_manager import RiskDecision, RiskManager
from strategy.base_strategy import BaseStrategy, ExitReason, SignalType, TradingState


@dataclass
class Trade:
    """One completed round-trip (buy -> sell) in the simulation.

    ``*_market_price`` values are the raw ``close`` used to trigger the signal;
    ``*_exec_price`` values include slippage. ``gross_pnl`` is the pure price
    move ``(exit_market - entry_market) * quantity`` *before* slippage and
    fees; ``net_pnl`` subtracts both so that:

        net_pnl = gross_pnl - (entry_slippage + exit_slippage)
                             - (entry_fee + exit_fee)
    """

    entry_timestamp: pd.Timestamp
    exit_timestamp: pd.Timestamp
    quantity: float
    entry_market_price: float
    entry_exec_price: float
    entry_notional: float
    entry_fee: float
    entry_slippage: float
    exit_market_price: float
    exit_exec_price: float
    exit_notional: float
    exit_fee: float
    exit_slippage: float
    gross_pnl: float
    net_pnl: float
    exit_reason: ExitReason

    @property
    def net_return_pct(self) -> float:
        """Net P&L as a percentage of the entry notional (cost basis)."""
        if self.entry_notional <= 0:
            return 0.0
        return self.net_pnl / self.entry_notional * 100.0


@dataclass
class BacktestResult:
    """Everything produced by a single :meth:`BacktestEngine.run` call.

    Attributes:
        trades: Completed trades in chronological order.
        equity_curve: Portfolio equity (cash + marked-to-market holdings) at
            the close of every replayed candle.
        close_prices: Close price series used to value open positions.
        initial_capital: Starting USDT balance.
        final_capital: Terminal equity (marks any open position to the last close).
        symbol / timeframe: Asset context for reporting.
        rejected_orders: BUY proposals declined by the RiskManager (audit trail).
    """

    trades: List["Trade"]
    equity_curve: pd.Series
    close_prices: pd.Series
    initial_capital: float
    final_capital: float
    symbol: str
    timeframe: str
    rejected_orders: List[Dict[str, object]] = field(default_factory=list)


class BacktestEngine:
    """Runs a deterministic, candle-by-candle spot backtest.

    Args:
        strategy: ``BaseStrategy`` instance (BTC/USDT or ETH/USDT) exposing a
            ``.config`` dict (for risk-position sizing) and ``process_candle``.
        risk_manager: ``RiskManager`` authorising/sizing every BUY signal.
        initial_capital: Starting USDT balance (e.g. 10_000.0).
        fee_pct: Bilateral trading fee as a fraction (e.g. ``0.001`` = 0.1%).
        slippage_pct: Adverse fill spread as a fraction (e.g. ``0.0005``).

    Raises:
        ValueError: For a non-positive capital, a negative fee / slippage, or a
            strategy that exposes no ``.config`` dict.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        *,
        initial_capital: float = 10_000.0,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if fee_pct < 0 or slippage_pct < 0:
            raise ValueError("fee_pct and slippage_pct must be >= 0")
        if not hasattr(strategy, "config"):
            raise ValueError(
                "strategy must expose a `.config` dict to size risk positions"
            )

        self.strategy = strategy
        self.risk_manager = risk_manager
        self.initial_capital = float(initial_capital)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.config: Dict[str, object] = strategy.config
# ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _close_trade(
        self,
        *,
        entry: Dict[str, float],
        exit_ts: pd.Timestamp,
        close_price: float,
        quantity: float,
        exit_reason: ExitReason,
    ) -> Tuple[Trade, float, float]:
        """Build a completed :class:`Trade` and return it with exit economics.

        Returns:
            ``(trade, exit_notional, exit_fee)`` - the trade record, the gross
            USDT received from the sell fill, and the exit trading fee.
        """
        exec_price = close_price * (1.0 - self.slippage_pct)
        exit_notional = quantity * exec_price
        exit_fee = exit_notional * self.fee_pct
        exit_slippage = quantity * close_price * self.slippage_pct

        gross_pnl = (close_price - entry["market_price"]) * quantity
        total_slippage = entry["slippage"] + exit_slippage
        total_fees = entry["fee"] + exit_fee
        net_pnl = gross_pnl - total_slippage - total_fees

        trade = Trade(
            entry_timestamp=entry["timestamp"],
            exit_timestamp=exit_ts,
            quantity=quantity,
            entry_market_price=entry["market_price"],
            entry_exec_price=entry["exec_price"],
            entry_notional=entry["notional"],
            entry_fee=entry["fee"],
            entry_slippage=entry["slippage"],
            exit_market_price=close_price,
            exit_exec_price=exec_price,
            exit_notional=exit_notional,
            exit_fee=exit_fee,
            exit_slippage=exit_slippage,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            exit_reason=exit_reason,
        )
        return trade, exit_notional, exit_fee

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame) -> BacktestResult:
        """Replay every candle in ``df`` and return the backtest result.

        The simulation is strictly sequential: candle ``t`` is evaluated and
        executed (if a signal fires) using only information available at candle
        ``t``. The equity curve records portfolio value at every single candle.

        Args:
            df: OHLCV DataFrame with a DatetimeIndex and at least the columns
                ``open/high/low/close`` (``volume`` optional).

        Returns:
            A populated :class:`BacktestResult`.

        Raises:
            ValueError: If ``df`` is empty or missing required OHLCV columns.
        """
        if not isinstance(df, pd.DataFrame) or df.empty:
            raise ValueError("run() requires a non-empty OHLCV DataFrame")
        missing = {"open", "high", "low", "close"} - set(df.columns)
        if missing:
            raise ValueError(
                f"DataFrame missing required OHLCV columns: {sorted(missing)}"
            )

        strategy = self.strategy
        strategy.reset()

        cash = float(self.initial_capital)
        holdings = 0.0
        holding = False
        entry: Optional[Dict[str, float]] = None

        trades: List[Trade] = []
        rejected: List[Dict[str, object]] = []

        equity_timestamps: List[pd.Timestamp] = []
        equity_values: List[float] = []
        close_timestamps: List[pd.Timestamp] = []
        close_values: List[float] = []

        has_volume = "volume" in df.columns

        for ts, row in df.iterrows():
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"]) if has_volume else 0.0

            # 1. Strategy evaluates ONLY this just-completed candle.
            signal = strategy.process_candle(
                {
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )

            # Portfolio value before any execution on this candle.
            pre_equity = cash + holdings * close
# --- 2. Execution modelling ----------------------------------
            if signal is SignalType.BUY and not holding:
                decision: RiskDecision = self.risk_manager.validate_trade(
                    asset_config=self.config,
                    current_price=close,
                    available_usdt=cash,
                    total_portfolio_value=pre_equity,
                )
                if decision.is_approved and decision.cost_usdt > 0.0:
                    exec_price = close * (1.0 + self.slippage_pct)
                    quantity = decision.cost_usdt / exec_price
                    entry_fee = decision.cost_usdt * self.fee_pct
                    entry_slippage = quantity * close * self.slippage_pct
                    cash -= decision.cost_usdt + entry_fee

                    holdings = quantity
                    holding = True
                    entry = {
                        "timestamp": ts,
                        "market_price": close,
                        "exec_price": exec_price,
                        "notional": decision.cost_usdt,
                        "fee": entry_fee,
                        "slippage": entry_slippage,
                    }
                else:
                    rejected.append({"timestamp": ts, "reason": decision.reason})

            elif signal is SignalType.SELL and holding and (entry is not None):
                trade, exit_notional, exit_notional_fee = self._close_trade(
                    entry=entry,
                    exit_ts=ts,
                    close_price=close,
                    quantity=holdings,
                    exit_reason=(
                        strategy.last_exit_reason
                        if strategy.last_exit_reason is not None
                        else ExitReason.REVERSAL
                    ),
                )
                trades.append(trade)
                cash += exit_notional - exit_notional_fee
                holdings = 0.0
                holding = False
                entry = None

            equity = cash + holdings * close
            equity_values.append(equity)
            equity_timestamps.append(ts)
            close_values.append(close)
            close_timestamps.append(ts)

        # Force-close any position still open at the end of the window so the
        # terminal equity is fully realized (standard backtest convention).
        if holding and (entry is not None):
            last_close = float(df["close"].iloc[-1])
            last_ts = ts
            trade, exit_notional, exit_notional_fee = self._close_trade(
                entry=entry,
                exit_ts=last_ts,
                close_price=last_close,
                quantity=holdings,
                exit_reason=(
                    strategy.last_exit_reason
                    if strategy.last_exit_reason is not None
                    else ExitReason.REVERSAL
                ),
            )
            trades.append(trade)
            cash += exit_notional - exit_notional_fee
            holdings = 0.0
            holding = False
            entry = None
            equity_values[-1] = cash

        equity_curve = pd.Series(
            equity_values, index=equity_timestamps, name="equity"
        )
        close_prices = pd.Series(close_values, index=close_timestamps, name="close")

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            close_prices=close_prices,
            initial_capital=self.initial_capital,
            final_capital=float(equity_curve.iloc[-1]),
            symbol=getattr(self.strategy, "symbol", "UNKNOWN"),
            timeframe=getattr(self.strategy, "timeframe", ""),
            rejected_orders=rejected,
        )