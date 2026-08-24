"""Module 4b: institutional performance metrics from a backtest result.

Given the trade log and equity curve produced by :class:`BacktestEngine.run`,
this module computes a full set of performance analytics:

  * Initial vs Final capital, Gross vs Net P&L ($ and %)
  * Total fees and total slippage cost
  * Trade count, win/loss split and win rate
  * Average win and average loss ($ and %)
  * Profit factor (gross profits / gross losses)
  * Maximum drawdown (peak-to-trough, % of equity peak)
  * Buy & Hold benchmark return over the exact same window

It also provides an ASCII tear-sheet renderer for console reporting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from backtesting.engine import BacktestResult, Trade
from backtesting.portfolio_engine import PortfolioResult, PortfolioTrade


@dataclass
class BacktestMetrics:
    """Aggregate performance results for one backtest run.

    Values are dollars and percentages (percentages stored without the ``%``
    sign, e.g. ``net_return_pct=+12.34``). ``max_drawdown_pct`` is the most
    negative equity drawdown (e.g. ``-5.0``). ``profit_factor`` may be
    ``math.inf`` when there were no losing (gross) trades.
    """

    symbol: str
    timeframe: str
    start: Optional[pd.Timestamp]
    end: Optional[pd.Timestamp]

    initial_capital: float
    final_capital: float

    gross_pnl: float
    gross_return_pct: float
    net_pnl: float
    net_return_pct: float

    total_fees: float
    total_slippage: float

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float

    avg_win_usdt: float
    avg_win_pct: float
    avg_loss_usdt: float
    avg_loss_pct: float

    profit_factor: float
    max_drawdown_pct: float
    buy_hold_return_pct: float
class MetricsCalculator:
    """Computes :class:`BacktestMetrics` from a trade log and equity curve.

    Stateless helper: call :meth:`calculate` with a trade list / equity curve,
    or :meth:`from_result` with a full :class:`BacktestResult`.
    """

    # ------------------------------------------------------------------
    def from_result(self, result: BacktestResult) -> BacktestMetrics:
        """Convenience wrapper around :meth:`calculate` for a run result."""
        return self.calculate(
            trades=result.trades,
            equity_curve=result.equity_curve,
            close_prices=result.close_prices,
            initial_capital=result.initial_capital,
            symbol=result.symbol,
            timeframe=result.timeframe,
        )

    def calculate(
        self,
        *,
        trades: List[Trade],
        equity_curve: pd.Series,
        close_prices: pd.Series,
        initial_capital: float,
        symbol: str = "UNKNOWN",
        timeframe: str = "",
    ) -> BacktestMetrics:
        """Compute every performance metric.

        Args:
            trades: The log of completed trades (may be empty).
            equity_curve: Per-candle equity Series (DatetimeIndex).
            close_prices: Per-candle close Series (DatetimeIndex).
            initial_capital: Starting USDT capital.
            symbol / timeframe: Reporting context.

        Returns:
            A fully-populated :class:`BacktestMetrics`.
        """
        total = len(trades)
        winners = [t for t in trades if t.net_pnl > 0.0]
        losers = [t for t in trades if t.net_pnl < 0.0]

        gross_win = sum(t.gross_pnl for t in trades if t.gross_pnl > 0.0)
        gross_loss = -sum(t.gross_pnl for t in trades if t.gross_pnl < 0.0)

        gross_pnl = sum(t.gross_pnl for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)
        total_fees = sum(t.entry_fee + t.exit_fee for t in trades)
        total_slippage = sum(t.entry_slippage + t.exit_slippage for t in trades)

        win_rate_pct = (len(winners) / total * 100.0) if total else 0.0
        avg_win_usdt = sum(t.net_pnl for t in winners) / len(winners) if winners else 0.0
        avg_win_pct = sum(t.net_return_pct for t in winners) / len(winners) if winners else 0.0
        avg_loss_usdt = sum(t.net_pnl for t in losers) / len(losers) if losers else 0.0
        avg_loss_pct = sum(t.net_return_pct for t in losers) / len(losers) if losers else 0.0

        if gross_loss > 0.0:
            profit_factor = gross_win / gross_loss
        elif gross_win > 0.0:
            profit_factor = math.inf
        else:
            profit_factor = 0.0

        max_drawdown_pct = self._max_drawdown(equity_curve)

        if len(close_prices) >= 2:
            first = float(close_prices.iloc[0])
            last = float(close_prices.iloc[-1])
            buy_hold_return_pct = (last - first) / first * 100.0
        else:
            buy_hold_return_pct = 0.0

        start = equity_curve.index[0] if len(equity_curve) else None
        end = equity_curve.index[-1] if len(equity_curve) else None
        final_capital = float(equity_curve.iloc[-1]) if len(equity_curve) else float(initial_capital)

        return BacktestMetrics(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            initial_capital=float(initial_capital),
            final_capital=final_capital,
            gross_pnl=gross_pnl,
            gross_return_pct=(gross_pnl / initial_capital) * 100.0 if initial_capital else 0.0,
            net_pnl=net_pnl,
            net_return_pct=(net_pnl / initial_capital) * 100.0 if initial_capital else 0.0,
            total_fees=total_fees,
            total_slippage=total_slippage,
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate_pct=win_rate_pct,
            avg_win_usdt=avg_win_usdt,
            avg_win_pct=avg_win_pct,
            avg_loss_usdt=avg_loss_usdt,
            avg_loss_pct=avg_loss_pct,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown_pct,
            buy_hold_return_pct=buy_hold_return_pct,
        )

    @staticmethod
    def _max_drawdown(equity_curve: pd.Series) -> float:
        """Return the most negative peak-to-trough equity drawdown in percent."""
        if not len(equity_curve):
            return 0.0
        peak = equity_curve.cummax()
        drawdown = (equity_curve - peak) / peak * 100.0
        worst = float(drawdown.min())
        return min(worst, 0.0)
# ----------------------------------------------------------------------
# Tear-sheet rendering
# ----------------------------------------------------------------------
_WIDTH = 60


def format_tear_sheet(metrics: BacktestMetrics) -> str:
    """Render a printable ASCII performance summary for ``metrics``."""
    line = "=" * _WIDTH
    mid = "-" * _WIDTH

    start_txt = metrics.start.strftime("%Y-%m-%d") if metrics.start is not None else "N/A"
    end_txt = metrics.end.strftime("%Y-%m-%d") if metrics.end is not None else "N/A"

    title = f"BACKTEST PERFORMANCE REPORT: {metrics.symbol}"
    header = line + "\n" + title.center(_WIDTH) + "\n" + line

    def usd(value: float) -> str:
        return f"${value:,.2f}"

    def pct(value: float) -> str:
        if math.isinf(value):
            return "inf"
        sign = "+" if value >= 0.0 else ""
        return f"{sign}{value:,.2f}%"

    def row(label: str, value: str) -> str:
        return f"{label:<22}{value:>20}"

    lines = [
        header,
        f"Period:                {start_txt} to {end_txt}",
        row("Initial Capital:", usd(metrics.initial_capital)),
        row("Final Net Capital:", usd(metrics.final_capital)),
        row("Net Return:", pct(metrics.net_return_pct)),
        row("Gross Return:", pct(metrics.gross_return_pct)),
        row("Buy & Hold Return:", pct(metrics.buy_hold_return_pct)),
        mid,
        row("Total Trades:", f"{metrics.total_trades:,} (Win Rate: {metrics.win_rate_pct:.1f}%)"),
        row("Net P&L:", usd(metrics.net_pnl)),
        row("Gross P&L:", usd(metrics.gross_pnl)),
        row("Avg Win:", usd(metrics.avg_win_usdt)),
        row("Avg Loss:", usd(metrics.avg_loss_usdt)),
        row("Profit Factor:", f"{metrics.profit_factor:.2f}" if not math.isinf(metrics.profit_factor) else "inf"),
        row("Max Drawdown:", pct(min(metrics.max_drawdown_pct, 0.0))),
        row("Total Fees:", usd(metrics.total_fees)),
        row("Total Slippage:", usd(metrics.total_slippage)),
        row("Total Fees & Slippage:", usd(metrics.total_fees + metrics.total_slippage)),
        line,
    ]
    return "\n".join(lines) + "\n"
# ======================================================================
# Module 5: Portfolio-level aggregation
# ======================================================================
@dataclass
class PortfolioMetrics:
    """Aggregate performance of the *whole* multi-asset portfolio.

    Computed from the combined equity curve and the merged trade log of every
    asset. All percentages omit the ``%`` sign. ``max_drawdown_pct`` is the
    most negative peak-to-trough combined-equity drawdown (<= 0).
    """

    portfolio_name: str
    start: Optional[pd.Timestamp]
    end: Optional[pd.Timestamp]

    initial_capital: float
    final_capital: float

    gross_pnl: float
    gross_return_pct: float
    net_pnl: float
    net_return_pct: float

    total_fees: float
    total_slippage: float

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float

    avg_win_usdt: float
    avg_win_pct: float
    avg_loss_usdt: float
    avg_loss_pct: float

    profit_factor: float
    max_drawdown_pct: float

    per_asset_trades: Dict[str, int]


class PortfolioMetricsCalculator:
    """Computes :class:`PortfolioMetrics` from a portfolio backtest result.

    Stateless helper: :meth:`from_result` is the normal entry point.
    """

    def from_result(self, result: PortfolioResult) -> PortfolioMetrics:
        """Convenience wrapper around :meth:`calculate` for a run result."""
        return self.calculate(
            trades=result.trades,
            equity_curve=result.equity_curve,
            initial_capital=result.initial_capital,
            portfolio_name=" + ".join(result.assets),
        )

    def calculate(
        self,
        *,
        trades: List[PortfolioTrade],
        equity_curve: pd.Series,
        initial_capital: float,
        portfolio_name: str = "PORTFOLIO",
    ) -> PortfolioMetrics:
        """Aggregate every trade and the combined equity curve.

        Args:
            trades: Completed round-trips of *all* assets (may be empty).
            equity_curve: Combined portfolio equity over the unified timeline.
            initial_capital: Starting shared USDT capital.
            portfolio_name: Display label for reporting.

        Returns:
            A fully-populated :class:`PortfolioMetrics`.
        """
        total = len(trades)
        winners = [t for t in trades if t.net_pnl > 0.0]
        losers = [t for t in trades if t.net_pnl < 0.0]

        gross_win = sum(t.gross_pnl for t in trades if t.gross_pnl > 0.0)
        gross_loss = -sum(t.gross_pnl for t in trades if t.gross_pnl < 0.0)

        gross_pnl = sum(t.gross_pnl for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)
        total_fees = sum(t.entry_fee + t.exit_fee for t in trades)
        total_slippage = sum(t.entry_slippage + t.exit_slippage for t in trades)

        win_rate_pct = (len(winners) / total * 100.0) if total else 0.0
        avg_win_usdt = (
            sum(t.net_pnl for t in winners) / len(winners) if winners else 0.0
        )
        avg_win_pct = (
            sum(t.net_return_pct for t in winners) / len(winners)
            if winners
            else 0.0
        )
        avg_loss_usdt = (
            sum(t.net_pnl for t in losers) / len(losers) if losers else 0.0
        )
        avg_loss_pct = (
            sum(t.net_return_pct for t in losers) / len(losers) if losers else 0.0
        )

        if gross_loss > 0.0:
            profit_factor = gross_win / gross_loss
        elif gross_win > 0.0:
            profit_factor = math.inf
        else:
            profit_factor = 0.0

        max_drawdown_pct = self._max_drawdown(equity_curve)

        per_asset: Dict[str, int] = {}
        for trade in trades:
            per_asset[trade.asset] = per_asset.get(trade.asset, 0) + 1

        start = equity_curve.index[0] if len(equity_curve) else None
        end = equity_curve.index[-1] if len(equity_curve) else None
        final_capital = (
            float(equity_curve.iloc[-1])
            if len(equity_curve)
            else float(initial_capital)
        )

        return PortfolioMetrics(
            portfolio_name=portfolio_name,
            start=start,
            end=end,
            initial_capital=float(initial_capital),
            final_capital=final_capital,
            gross_pnl=gross_pnl,
            gross_return_pct=(
                (gross_pnl / initial_capital) * 100.0 if initial_capital else 0.0
            ),
            net_pnl=net_pnl,
            net_return_pct=(
                (net_pnl / initial_capital) * 100.0 if initial_capital else 0.0
            ),
            total_fees=total_fees,
            total_slippage=total_slippage,
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate_pct=win_rate_pct,
            avg_win_usdt=avg_win_usdt,
            avg_win_pct=avg_win_pct,
            avg_loss_usdt=avg_loss_usdt,
            avg_loss_pct=avg_loss_pct,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown_pct,
            per_asset_trades=per_asset,
        )

    @staticmethod
    def _max_drawdown(equity_curve: pd.Series) -> float:
        """Most negative peak-to-trough equity drawdown in percent (<= 0)."""
        if not len(equity_curve):
            return 0.0
        peak = equity_curve.cummax()
        drawdown = (equity_curve - peak) / peak * 100.0
        worst = float(drawdown.min())
        return min(worst, 0.0)


def format_portfolio_tear_sheet(metrics: PortfolioMetrics) -> str:
    """Render the combined multi-asset ASCII tear sheet for ``metrics``."""
    line = "=" * _WIDTH
    mid = "-" * _WIDTH

    start_txt = (
        metrics.start.strftime("%Y-%m-%d") if metrics.start is not None else "N/A"
    )
    end_txt = metrics.end.strftime("%Y-%m-%d") if metrics.end is not None else "N/A"

    title = f"PORTFOLIO BACKTEST PERFORMANCE REPORT: {metrics.portfolio_name}"
    header = line + "\n" + title.center(_WIDTH) + "\n" + line

    def usd(value: float) -> str:
        return f"${value:,.2f}"

    def pct(value: float) -> str:
        if math.isinf(value):
            return "inf"
        sign = "+" if value >= 0.0 else ""
        return f"{sign}{value:,.2f}%"

    def row(label: str, value: str) -> str:
        return f"{label:<22}{value:>20}"

    per_asset = sorted(metrics.per_asset_trades)
    if per_asset:
        breakdown_rows = [
            f"{asset:<22}{metrics.per_asset_trades[asset]:>20}"
            for asset in per_asset
        ]
    else:
        breakdown_rows = [f"{'<no completed trades>':<22}{0:>20}"]

    lines = [
        header,
        f"Period:                {start_txt} to {end_txt}",
        row("Initial Capital:", usd(metrics.initial_capital)),
        row("Final Net Capital:", usd(metrics.final_capital)),
        row("Net Return:", pct(metrics.net_return_pct)),
        row("Gross Return:", pct(metrics.gross_return_pct)),
        mid,
        row(
            "Total Trades:",
            f"{metrics.total_trades:,} (Win Rate: {metrics.win_rate_pct:.1f}%)",
        ),
        row("Net P&L:", usd(metrics.net_pnl)),
        row("Gross P&L:", usd(metrics.gross_pnl)),
        row("Avg Win:", usd(metrics.avg_win_usdt)),
        row("Avg Loss:", usd(metrics.avg_loss_usdt)),
        row(
            "Profit Factor:",
            f"{metrics.profit_factor:.2f}"
            if not math.isinf(metrics.profit_factor)
            else "inf",
        ),
        row("Max Drawdown:", pct(min(metrics.max_drawdown_pct, 0.0))),
        row("Total Fees:", usd(metrics.total_fees)),
        row("Total Slippage:", usd(metrics.total_slippage)),
        row(
            "Total Fees & Slippage:",
            usd(metrics.total_fees + metrics.total_slippage),
        ),
        line,
        (" Per-asset trade breakdown ").center(_WIDTH, "-"),
    ]
    lines.extend(breakdown_rows)
    lines.append(line)
    return "\n".join(lines) + "\n"