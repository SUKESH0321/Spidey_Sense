"""Module 5: concurrent multi-asset portfolio backtesting engine.

Runs the trading bot across *multiple* assets at once with a **single shared
USDT capital pool**. Each asset owns an independent strategy instance (BTC,
ETH, ...), but every BUY signal is authorised and sized by one shared
:class:`RiskManager` against the same ``available_usdt`` balance and the same
``total_portfolio_value`` equity state.

Time synchronization
--------------------
The replay walks a unified, chronological list of every unique timestamp from
**all** asset dataframes (the ``union`` of the per-asset Datetime indexes). At
each timestamp the engine loops over the assets, feeding state machine and
capital pool concurrently forward in wall-clock order:

1. If an asset has a candle at the current timestamp, it is fed to
   ``strategy.process_candle()``.
2. A ``BUY`` signal asks :meth:`RiskManager.validate_trade` *with the shared
   available USDT and total portfolio value*; approved orders fill at
   ``close * (1 + slippage_pct)``, the fee is deducted and the **shared**
   ``available_usdt`` is reduced.
3. A ``SELL`` (reversal or stop-loss) sells the open position at
   ``close * (1 - slippage_pct)`` and adds proceeds (net of fee) back to the
   shared ``available_usdt``.
4. At the end of the timestamp the combined equity
   ``available_usdt + sum(holdings_asset * last_close_asset)`` is recorded.

This is the mechanism by which a BTC BUY reduces the USDT available for a
subsequent ETH BUY, so the *global* account exposure limit enforced by the
RiskManager becomes a real, binding portfolio constraint.

No live Binance execution is involved — everything is simulated spot
accounting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from risk.risk_manager import RiskManager
from strategy.base_strategy import BaseStrategy, ExitReason, SignalType

logger = logging.getLogger(__name__)


@dataclass
class ExecutionLog:
    """One recorded order/execution step of the portfolio replay.

    ``action`` is ``"BUY"``, ``"SELL"`` or ``"REJECTED_BUY"``. For a rejected
    BUY the ``quantity``/``fee``/``slippage``/``notional`` fields are zero and
    ``reason`` carries the risk-manager rejection text. ``price`` is the market
    close for a rejection and the actual fill price for an execution.
    """

    timestamp: pd.Timestamp
    asset: str
    action: str
    quantity: float
    price: float
    notional_usdt: float
    fee: float
    slippage: float
    available_usdt_after: float
    total_portfolio_after: float
    reason: str = ""


@dataclass
class PortfolioTrade:
    """One completed round-trip (buy -> sell) for a single asset.

    Fields mirror the Module 4 :class:`Trade` but carry the owning asset label.
    ``gross_pnl`` is the pure price move before costs; ``net_pnl`` subtracts
    entry+exit slippage and entry+exit fees.
    """

    asset: str
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
        if self.entry_notional <= 0.0:
            return 0.0
        return self.net_pnl / self.entry_notional * 100.0


@dataclass
class PortfolioResult:
    """Everything a single :meth:`PortfolioBacktester.run` call produces.

    Attributes:
        trades: Completed round-trips across all assets (chronological).
        equity_curve: Combined portfolio equity (cash + marked-to-market
            holdings) at the close of every unified timestamp.
        executions: Order/decision audit trail (incl. rejected BUYs).
        initial_capital: Starting shared USDT balance.
        final_capital: Terminal equity after force-closing open positions.
        assets: Asset labels in dict insertion order.
    """

    trades: List[PortfolioTrade]
    equity_curve: pd.Series
    executions: List[ExecutionLog]
    initial_capital: float
    final_capital: float
    assets: Tuple[str, ...]

    @property
    def trade_count_by_asset(self) -> Dict[str, int]:
        """How many completed trades each asset took."""
        counts: Dict[str, int] = {}
        for trade in self.trades:
            counts[trade.asset] = counts.get(trade.asset, 0) + 1
        return counts


class PortfolioBacktester:
    """Time-synchronised multi-asset portfolio backtester.

    Args:
        strategies: ``{asset_label: BaseStrategy}`` - one independent state
            machine per asset (fresh, non-shared instances).
        risk_manager: Single shared :class:`RiskManager` sizing *every* asset
            against the common cash pool and total portfolio value.
        initial_capital: Starting USDT for the shared capital pool.
        fee_pct: Bilateral trading fee as a fraction (``0.001`` = 0.1%).
        slippage_pct: Adverse fill spread as a fraction (``0.0005``).

    Raises:
        ValueError / TypeError: For an empty strategy dict, non-BaseStrategy
            entries, a missing ``.config``, non-positive capital or negative
            fee/slippage.
    """

    def __init__(
        self,
        strategies: Dict[str, BaseStrategy],
        risk_manager: RiskManager,
        *,
        initial_capital: float = 10_000.0,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
    ) -> None:
        if not strategies:
            raise ValueError("strategies must contain at least one asset")
        for label, strategy in strategies.items():
            if not isinstance(strategy, BaseStrategy):
                raise TypeError(
                    f"strategies[{label!r}] must be a BaseStrategy, got "
                    f"{type(strategy).__name__}"
                )
            if not hasattr(strategy, "config"):
                raise ValueError(
                    f"strategy {label} must expose a `.config` dict to size "
                    f"risk positions"
                )
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if fee_pct < 0 or slippage_pct < 0:
            raise ValueError("fee_pct and slippage_pct must be >= 0")

        self.strategies = dict(strategies)
        self.risk_manager = risk_manager
        self.initial_capital = float(initial_capital)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.assets: List[str] = list(strategies.keys())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, market_data: Dict[str, pd.DataFrame]) -> PortfolioResult:
        """Replay every unified timestamp across the assets.

        Args:
            market_data: ``{asset: OHLCV DataFrame}`` with a UTC DatetimeIndex
                (columns open/high/low/close, volume optional).

        Raises:
            ValueError: If an asset's data is missing, empty or lacks an OHLC
                column.
        """
        assets = list(self.assets)
        missing = [label for label in assets if label not in market_data]
        if missing:
            raise ValueError(
                f"market_data is missing candles for assets: {sorted(missing)}"
            )
        for label in assets:
            df = market_data[label]
            if df is None or len(df) == 0:
                raise ValueError(f"market_data[{label!r}] is empty")
            for col in ("open", "high", "low", "close"):
                if col not in df.columns:
                    raise ValueError(
                        f"market_data[{label!r}] is missing OHLC column {col!r}"
                    )

        # ---- per-asset candle lookup + unified chronological timeline ------
        candles: Dict[str, Dict[pd.Timestamp, Dict[str, float]]] = {}
        all_times: List[pd.Timestamp] = []
        for label in assets:
            candles[label] = {}
            for ts, row in market_data[label].iterrows():
                candle: Dict[str, float] = {
                    key: float(row[key])
                    for key in ("open", "high", "low", "close", "volume")
                    if key in row.index
                }
                candle["timestamp"] = ts
                candles[label][ts] = candle
                all_times.append(ts)
        # Unified sorted timeline; duplicates across assets collapse to one
        # wall-clock instant (time synchronization).
        timeline: List[pd.Timestamp] = sorted(set(all_times))

        # ---- portfolio account state --------------------------------------
        available_usdt = self.initial_capital
        holdings: Dict[str, float] = {label: 0.0 for label in assets}
        entries: Dict[str, Optional[Dict[str, float]]] = {
            label: None for label in assets
        }
        last_close: Dict[str, Optional[float]] = {label: None for label in assets}
        trades: List[PortfolioTrade] = []
        executions: List[ExecutionLog] = []
        equity_values: List[float] = []
        equity_times: List[pd.Timestamp] = []

        # ---- time-synchronised event loop ---------------------------------
        for ts in timeline:
            # 1) Fold in any candles now visible (latest close per asset).
            for label in assets:
                candle = candles[label].get(ts)
                if candle is not None:
                    last_close[label] = candle["close"]

            # 2) Feed each asset its own candle, sharing the one USDT pool.
            for label in assets:
                candle = candles[label].get(ts)
                if candle is None:
                    continue  # No bar for this asset at this instant.
                strategy = self.strategies[label]
                signal = strategy.process_candle(candle)
                close = candle["close"]
                pre_equity = self._total_value(
                    available_usdt, holdings, last_close
                )

                if signal is SignalType.BUY:
                    if holdings[label] > 0.0:
                        # Defensive: state machine never signals BUY while
                        # holding, but never double-deploy if it somehow does.
                        logger.warning(
                            "[%s] [%s] BUY signal while position open - ignored",
                            ts, label,
                        )
                        continue
                    decision = self.risk_manager.validate_trade(
                        asset_config=strategy.config,
                        current_price=close,
                        available_usdt=available_usdt,
                        total_portfolio_value=pre_equity,
                    )
                    if decision.is_approved and decision.cost_usdt > 0.0:
                        exec_price = close * (1.0 + self.slippage_pct)
                        quantity = decision.cost_usdt / exec_price
                        entry_fee = decision.cost_usdt * self.fee_pct
                        entry_slippage = quantity * close * self.slippage_pct

                        available_usdt -= decision.cost_usdt + entry_fee
                        holdings[label] = quantity
                        entries[label] = {
                            "timestamp": ts,
                            "market_price": close,
                            "exec_price": exec_price,
                            "notional": decision.cost_usdt,
                            "fee": entry_fee,
                            "slippage": entry_slippage,
                        }
                        total_equity = self._total_value(
                            available_usdt, holdings, last_close
                        )
                        executions.append(
                            ExecutionLog(
                                timestamp=ts,
                                asset=label,
                                action="BUY",
                                quantity=quantity,
                                price=exec_price,
                                notional_usdt=decision.cost_usdt,
                                fee=entry_fee,
                                slippage=entry_slippage,
                                available_usdt_after=available_usdt,
                                total_portfolio_after=total_equity,
                                reason=decision.reason,
                            )
                        )
                        self._log_execution(
                            ts, label, "BUY", quantity,
                            exec_price, decision.cost_usdt, available_usdt,
                        )
                    else:
                        # Risk manager rejected the BUY -> audit trail entry.
                        executions.append(
                            ExecutionLog(
                                timestamp=ts,
                                asset=label,
                                action="REJECTED_BUY",
                                quantity=0.0,
                                price=close,
                                notional_usdt=0.0,
                                fee=0.0,
                                slippage=0.0,
                                available_usdt_after=available_usdt,
                                total_portfolio_after=pre_equity,
                                reason=decision.reason,
                            )
                        )
                elif signal is SignalType.SELL:
                    if holdings[label] <= 0.0 or entries[label] is None:
                        # Defensive: SELL without an open position in the pool.
                        logger.warning(
                            "[%s] [%s] SELL ignored - no open position",
                            ts, label,
                        )
                        continue
                    exit_reason = (
                        strategy.last_exit_reason
                        if strategy.last_exit_reason is not None
                        else ExitReason.REVERSAL
                    )
                    entry = entries[label]
                    trade, proceeds, exit_fee = self._close_trade(
                        asset=label,
                        entry=entry,
                        exit_ts=ts,
                        close_price=close,
                        quantity=holdings[label],
                        exit_reason=exit_reason,
                    )
                    entries[label] = None
                    trades.append(trade)
                    available_usdt += proceeds - exit_fee
                    holdings[label] = 0.0
                    total_equity = self._total_value(
                        available_usdt, holdings, last_close
                    )
                    executions.append(
                        ExecutionLog(
                            timestamp=ts,
                            asset=label,
                            action="SELL",
                            quantity=trade.quantity,
                            price=trade.exit_exec_price,
                            notional_usdt=proceeds,
                            fee=exit_fee,
                            slippage=trade.exit_slippage,
                            available_usdt_after=available_usdt,
                            total_portfolio_after=total_equity,
                            reason=exit_reason.value,
                        )
                    )
                    self._log_execution(
                        ts, label, "SELL", trade.quantity,
                        trade.exit_exec_price, proceeds, available_usdt,
                    )

            # 3) Record the combined mark-to-market equity for this timestamp.
            equity_values.append(
                self._total_value(available_usdt, holdings, last_close)
            )
            equity_times.append(ts)

        # Force-close any position still open so the terminal equity is fully
        # realised (standard backtest convention - mirrors Module 4 engine).
        terminal_ts = equity_times[-1] if equity_times else None
        for label in assets:
            if holdings[label] > 0.0 and entries[label] is not None:
                close_at_end = last_close[label]
                if close_at_end is None:
                    raise RuntimeError(
                        f"[{label}] open position without a price at window end"
                    )
                exit_reason = (
                    self.strategies[label].last_exit_reason
                    if self.strategies[label].last_exit_reason is not None
                    else ExitReason.REVERSAL
                )
                trade, proceeds, exit_fee = self._close_trade(
                    asset=label,
                    entry=entries[label],
                    exit_ts=terminal_ts,
                    close_price=close_at_end,
                    quantity=holdings[label],
                    exit_reason=exit_reason,
                )
                entries[label] = None
                trades.append(trade)
                available_usdt += proceeds - exit_fee
                holdings[label] = 0.0

        if equity_values:
            equity_values[-1] = available_usdt

        equity_curve = pd.Series(
            equity_values, index=equity_times, name="portfolio_equity"
        )

        return PortfolioResult(
            trades=trades,
            equity_curve=equity_curve,
            executions=executions,
            initial_capital=self.initial_capital,
            final_capital=(
                float(equity_curve.iloc[-1])
                if len(equity_values)
                else float(self.initial_capital)
            ),
            assets=tuple(self.assets),
        )

    # ------------------------------------------------------------------
    # Execution / valuation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _log_execution(
        ts: pd.Timestamp,
        asset: str,
        action: str,
        quantity: float,
        price: float,
        notional_usdt: float,
        available_usdt: float,
    ) -> None:
        """Print: [TIMESTAMP] [ASSET] ACTION | Qty | Price | Cost USDT | New Available USDT."""
        print(
            f"[{ts}] [{asset}] {action} | "
            f"Qty {quantity:,.6f} | Price {price:,.7f} | "
            f"Cost USDT {notional_usdt:,.2f} | "
            f"New Available USDT {available_usdt:,.2f}"
        )

    @classmethod
    def _total_value(
        cls,
        available: float,
        holdings: Dict[str, float],
        last_close: Dict[str, Optional[float]],
    ) -> float:
        """Cash plus the marked-to-market value of every open position."""
        invested = sum(
            quantity * last_close[label]
            for label, quantity in holdings.items()
            if quantity > 0.0 and last_close[label] is not None
        )
        return float(available + invested)

    def _close_trade(
        self,
        *,
        asset: str,
        entry: Dict[str, float],
        exit_ts: Optional[pd.Timestamp],
        close_price: float,
        quantity: float,
        exit_reason: ExitReason,
    ) -> Tuple[PortfolioTrade, float, float]:
        """Build a completed :class:`PortfolioTrade` with its exit economics.

        Returns:
            ``(trade, proceeds, exit_fee)`` - the record, gross USDT from the
            sell fill, and the exit trading fee to net out of the cash pool.
        """
        if exit_ts is None:
            raise ValueError("exit timestamp cannot be None for a sell fill")
        exec_price = close_price * (1.0 - self.slippage_pct)
        exit_notional = quantity * exec_price
        exit_fee = exit_notional * self.fee_pct
        exit_slippage = quantity * close_price * self.slippage_pct

        gross_pnl = (close_price - entry["market_price"]) * quantity
        total_slippage = entry["slippage"] + exit_slippage
        total_fees = entry["fee"] + exit_fee
        net_pnl = gross_pnl - total_slippage - total_fees

        trade = PortfolioTrade(
            asset=asset,
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