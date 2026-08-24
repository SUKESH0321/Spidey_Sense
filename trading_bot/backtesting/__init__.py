"""Backtesting engine and performance analytics package (Modules 4 + 5).

This package provides:

  * :class:`BacktestEngine` - zero-look-ahead candle-by-candle spot simulation.
  * :class:`MetricsCalculator` - institutional performance metrics.
  * :func:`format_tear_sheet` - ASCII console tear-sheet renderer.

Module 5 adds the multi-asset portfolio backtester:

  * :class:`PortfolioBacktester` - time-synchronised concurrent replay of
    multiple assets sharing one USDT pool and one :class:`RiskManager`.
  * :class:`PortfolioMetricsCalculator` - portfolio-level aggregate metrics.
  * :func:`format_portfolio_tear_sheet` - combined ASCII portfolio tear-sheet.
"""

from __future__ import annotations

from backtesting.engine import BacktestEngine, BacktestResult, Trade
from backtesting.metrics import (
    BacktestMetrics,
    MetricsCalculator,
    PortfolioMetrics,
    PortfolioMetricsCalculator,
    format_portfolio_tear_sheet,
    format_tear_sheet,
)
from backtesting.portfolio_engine import (
    ExecutionLog,
    PortfolioBacktester,
    PortfolioResult,
    PortfolioTrade,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "Trade",
    "BacktestMetrics",
    "MetricsCalculator",
    "format_tear_sheet",
    "PortfolioBacktester",
    "PortfolioResult",
    "PortfolioTrade",
    "ExecutionLog",
    "PortfolioMetrics",
    "PortfolioMetricsCalculator",
    "format_portfolio_tear_sheet",
]