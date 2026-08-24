"""Module 5: Multi-Asset Concurrent Portfolio Backtesting verification.

Deterministic 20-candle scenario over two **concurrent** assets sharing one
USDT pool (10,000) and one RiskManager:

  * BTC gets a BUY signal at timestamp #5  (position_size_pct = 0.50)
  * ETH gets a BUY signal at timestamp #6  (position_size_pct = 1.00)

The ETH config is intentionally set to "full-margin" so the *global* 80%
account-exposure cap enforced by the RiskManager becomes the binding
constraint - exactly what Module 5 must prove:

    when BTC buys, the combined ``total_portfolio_value`` is preserved (cash
    moves into a BTC holding; only the trading fee leaks), but the shared
    ``available_usdt`` drops - which mechanically caps how much USDT ETH can
    deploy on the very next timestamp.

Verification steps:
  1. Run :class:`PortfolioBacktester` over both dataframes.
  2. Assert BUY timing (BTC @ candle 5, ETH @ candle 6) from the execution
     audit trail.
  3. Assert the exposure-cap maths reproduce the ETH order notional exactly.
  4. Assert combined metrics (win rate, net return vs portfolio capital,
     final capital == initial + net P&L).
  5. Print the combined ASCII tear sheet + per-asset trade breakdown.

Run from the trading_bot directory:  python test_module_5.py
"""

from __future__ import annotations

import math
import sys
from typing import Dict, List, Tuple

import pandas as pd

from backtesting.metrics import (
    PortfolioMetrics,
    PortfolioMetricsCalculator,
    format_portfolio_tear_sheet,
)
from backtesting.portfolio_engine import PortfolioBacktester, PortfolioResult
from risk.risk_manager import RiskManager
from strategy.btc_strategy import BTCStrategy
from strategy.eth_strategy import ETHStrategy

VOLUME = 1000.0
INITIAL_CAPITAL = 10_000.0
FEE_PCT = 0.001
SLIPPAGE_PCT = 0.0005
N_CANDLES = 20

# BTC/USDT last close at the candles that carry the BUY/SELL signals.
_BTC_BUY_CLOSE = 96.0
_ETH_BUY_CLOSE = 38.5
_BTC_CLOSE_AT_T6 = 96.8  # close used to mark BTC's holding when ETH validates.

# ----------------------------------------------------------------------
# Deterministic OHLCV candle data - tuples of (open, high, low, close)
# ----------------------------------------------------------------------
BTC_CANDLES: List[Tuple[float, float, float, float]] = [
    (100.00, 100.50, 99.50, 100.00),   # 0  seed reference
    (100.00, 100.00, 95.60, 97.00),    # 1  -3% -> DOWNTREND
    (97.00, 97.00, 91.80, 92.30),      # 2  deeper low 91.80
    (96.00, 96.00, 91.80, 94.80),      # 3  rebound >=3% -> READY_TO_BUY (red)
    (95.50, 95.50, 94.60, 94.80),      # 4  still READY_TO_BUY (red)
    (94.00, 96.80, 93.80, 96.00),      # 5  green initiation -> BUY @96.00
    (96.00, 97.00, 95.60, 96.80),      # 6  HOLD
    (96.80, 98.20, 96.60, 98.00),      # 7  HOLD
    (98.00, 100.20, 97.80, 99.80),     # 8  HOLD
    (99.80, 101.00, 99.40, 100.60),    # 9  HOLD
    (100.60, 102.60, 100.40, 101.60),  # 10 HOLD (new high 102.60)
    (101.60, 102.00, 98.00, 98.50),    # 11 drawdown >=3% red -> SELL @98.50
    (98.50, 99.50, 98.00, 99.40),      # 12 COOLDOWN
    (99.40, 100.00, 99.00, 99.80),     # 13 COOLDOWN
    (99.80, 100.50, 99.40, 100.20),    # 14 COOLDOWN -> WAITING
    (100.20, 100.90, 99.80, 100.60),   # 15 WAITING
    (100.60, 101.40, 100.20, 101.20),  # 16 WAITING
    (101.20, 101.80, 100.80, 101.50),  # 17 WAITING
    (101.50, 102.10, 101.00, 101.70),  # 18 WAITING
    (101.70, 102.50, 101.40, 102.00),  # 19 WAITING
]

ETH_CANDLES: List[Tuple[float, float, float, float]] = [
    (40.00, 40.20, 39.80, 40.00),      # 0  seed reference
    (40.00, 40.00, 38.50, 38.80),      # 1  -3% -> DOWNTREND
    (38.60, 38.70, 36.00, 36.40),      # 2  deeper low 36.00
    (37.60, 37.60, 36.00, 37.20),      # 3  rebound >=3% -> READY_TO_BUY (red)
    (37.40, 37.50, 36.80, 37.20),      # 4  still READY_TO_BUY (red)
    (37.60, 37.60, 37.00, 37.30),      # 5  still READY_TO_BUY (red)
    (37.20, 38.60, 36.90, 38.50),      # 6  green initiation -> BUY @38.50
    (38.50, 39.20, 38.40, 39.00),      # 7  HOLD
    (39.00, 40.20, 38.90, 39.60),      # 8  HOLD
    (39.60, 40.40, 39.40, 40.00),      # 9  HOLD (new peak 40.40)
    (40.00, 40.20, 38.50, 39.00),      # 10 drawdown >=3% red -> SELL @39.00
    (39.00, 39.60, 38.80, 39.20),      # 11 COOLDOWN
    (39.50, 39.90, 39.10, 39.60),      # 12 COOLDOWN
    (39.60, 40.00, 39.20, 39.70),      # 13 COOLDOWN -> WAITING
    (39.70, 40.10, 39.40, 39.80),      # 14 WAITING
    (39.80, 40.30, 39.60, 40.00),      # 15 WAITING
    (40.00, 40.60, 39.80, 40.20),      # 16 WAITING
    (40.20, 41.00, 40.00, 40.60),      # 17 WAITING
    (40.60, 41.20, 40.40, 40.80),      # 18 WAITING
    (40.80, 41.50, 40.60, 41.00),      # 19 WAITING
]


def build_timestamps(n: int = N_CANDLES) -> pd.DatetimeIndex:
    """UTC hourly timestamps for the synthetic window."""
    return pd.date_range("2024-01-01 00:00", periods=n, freq="1h", tz="UTC")


def build_dataframe(
    candles: List[Tuple[float, float, float, float]],
    timestamps: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Build an OHLCV DataFrame indexed by ``timestamps``."""
    rows = [
        {"open": o, "high": h, "low": l, "close": c, "volume": VOLUME}
        for (o, h, l, c) in candles
    ]
    return pd.DataFrame(rows, index=timestamps)


def check(condition: bool, message: str, failures: List[str]) -> None:
    """Record ``message`` in ``failures`` when ``condition`` is False."""
    if not condition:
        failures.append(message)
# ----------------------------------------------------------------------
# Scenario + assertions
# ----------------------------------------------------------------------
def _expected_eth_notional(
    btc_avail_after: float,
    btc_qty: float,
    btc_close_when_eth_validates: float,
) -> float:
    """Re-derive what the shared RiskManager must approve for the ETH BUY.

    At candle 6 ETH asks for 100% of the remaining shared USDT. The global
    80% account-exposure cap constrains it to:

        min(available, 0.8 * total_portfolio_value - already_invested)

    where ``total_portfolio_value`` marks BTC's open holding at candle #6.
    """
    pre_equity = btc_avail_after + btc_qty * btc_close_when_eth_validates
    invested_btc = pre_equity - btc_avail_after
    headroom = 0.80 * pre_equity - invested_btc
    return min(btc_avail_after, headroom)


def run_suite() -> bool:
    """Run the full Module 5 scenario and assert every invariant."""
    print("=" * 78)
    print("Module 5 test: concurrent BTC+ETH portfolio backtest")
    print("=" * 78)

    failures: List[str] = []

    # ---- build the concurrent dataset -------------------------------------
    timestamps = build_timestamps(N_CANDLES)
    market_data = {
        "BTC": build_dataframe(BTC_CANDLES, timestamps),
        "ETH": build_dataframe(ETH_CANDLES, timestamps),
    }

    # ---- strategies + a single shared risk manager ------------------------
    btc_strategy = BTCStrategy()
    eth_strategy = ETHStrategy()
    # Force ETH to request 100% of the remaining free cash so the GLOBAL
    # 80% exposure cap (not per-asset sizing) binds at candle #6.
    eth_strategy.config["strategy"]["position_size_pct"] = 1.0

    risk = RiskManager()
    engine = PortfolioBacktester(
        {"BTC": btc_strategy, "ETH": eth_strategy},
        risk,
        initial_capital=INITIAL_CAPITAL,
        fee_pct=FEE_PCT,
        slippage_pct=SLIPPAGE_PCT,
    )

    # ---- run the concurrent replay ----------------------------------------
    result: PortfolioResult = engine.run(market_data)
    print(f"\nCombined equity over {len(result.equity_curve)} unified timestamps:")
    tail = result.equity_curve
    print("  first:", f"{tail.iloc[0]:,.2f}", "| last:", f"{tail.iloc[-1]:,.2f}")

    # ---- 1) scenario timing ----------------------------------------------
    buys = [e for e in result.executions if e.action == "BUY"]
    check(
        len(buys) == 2,
        f"expected exactly 2 BUY executions, got {len(buys)}",
        failures,
    )
    btc_buy = next(e for e in buys if e.asset == "BTC")
    eth_buy = next(e for e in buys if e.asset == "ETH")
    check(btc_buy.timestamp == timestamps[5], "BTC must BUY at candle 5", failures)
    check(eth_buy.timestamp == timestamps[6], "ETH must BUY at candle 6", failures)
    check(btc_buy.timestamp < eth_buy.timestamp, "BTC buys before ETH", failures)

    # ---- 2) crucial: total value preserved, shared cash drained -----------
    equity_after_btc = float(result.equity_curve.loc[btc_buy.timestamp])
    check(
        abs(equity_after_btc - INITIAL_CAPITAL) / INITIAL_CAPITAL < 0.01,
        f"BTC BUY must preserve total portfolio value "
        f"({equity_after_btc:,.2f} vs {INITIAL_CAPITAL:,.2f})",
        failures,
    )
    check(
        btc_buy.available_usdt_after < 0.50 * INITIAL_CAPITAL,
        f"BTC BUY must drain the shared USDT pool "
        f"(available after = {btc_buy.available_usdt_after:,.2f})",
        failures,
    )
# ---- 3) global exposure cap constrains the ETH notional ----------------
    expected_eth = _expected_eth_notional(
        btc_buy.available_usdt_after,
        btc_buy.quantity,
        _BTC_CLOSE_AT_T6,
    )
    check(
        math.isclose(eth_buy.notional_usdt, expected_eth, abs_tol=0.5),
        f"ETH notional must equal the exposure cap {expected_eth:,.2f}, "
        f"got {eth_buy.notional_usdt:,.2f}",
        failures,
    )
    check(
        eth_buy.notional_usdt < btc_buy.available_usdt_after,
        "ETH must be scaled down below 100% of the remaining cash",
        failures,
    )
    check(
        "scaled down" in eth_buy.reason.lower(),
        f"ETH BUY reason must mention the scale-down, got {eth_buy.reason!r}",
        failures,
    )

    # ---- 4) trade-level invariants -----------------------------------------
    check(
        len(result.trades) == 2,
        f"expected 2 completed round-trips, got {len(result.trades)}",
        failures,
    )
    counts = result.trade_count_by_asset
    check(counts == {"BTC": 1, "ETH": 1}, f"per-asset trades wrong: {counts}", failures)
    for trade in result.trades:
        identity = trade.gross_pnl - (
            trade.entry_slippage + trade.exit_slippage
            + trade.entry_fee + trade.exit_fee
        )
        check(
            math.isclose(identity, trade.net_pnl, abs_tol=1e-6),
            f"{trade.asset} net identity mismatch",
            failures,
        )

    # ---- 5) combined portfolio metrics -----------------------------------
    metrics: PortfolioMetrics = PortfolioMetricsCalculator().from_result(result)
    check(metrics.total_trades == 2, "combined metrics must count 2 trades", failures)
    check(
        metrics.winning_trades == 2 and metrics.losing_trades == 0,
        "both engineered trades must be winners",
        failures,
    )
    check(
        math.isclose(metrics.win_rate_pct, 100.0, abs_tol=1e-9),
        "combined win rate must be 100%",
        failures,
    )
    net_pnl = sum(t.net_pnl for t in result.trades)
    check(
        math.isclose(metrics.final_capital, INITIAL_CAPITAL + net_pnl, abs_tol=1e-2),
        "final capital must equal initial + net P&L",
        failures,
    )
    expected_ret = net_pnl / INITIAL_CAPITAL * 100.0
    check(
        math.isclose(metrics.net_return_pct, expected_ret, abs_tol=1e-6),
        "net return vs portfolio capital mismatch",
        failures,
    )
    check(
        metrics.max_drawdown_pct <= 0.0,
        f"max drawdown must be <= 0, got {metrics.max_drawdown_pct}",
        failures,
    )

    # ---- 6) reporting ----------------------------------------------------
    print()
    print(format_portfolio_tear_sheet(metrics))
    print("Per-asset trade breakdown (from result):")
    for asset in sorted(counts):
        print(f"  {asset:<8}{counts[asset]} trade(s)")
    print()

    ok = not failures
    print("=" * 78)
    print("OVERALL RESULT:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    print("=" * 78)
    return ok
def main() -> int:
    """Run the Module 5 suite and exit with PASS/FAIL."""
    suite_ok = run_suite()
    return 0 if suite_ok else 1


if __name__ == "__main__":
    sys.exit(main())