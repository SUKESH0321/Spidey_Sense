"""Module 4: Backtesting Engine & Institutional Performance Metrics.

This script is both a deterministic verification test and a performance
tear-sheet runner:

  1. **Smoke test** - a hand-built 10-candle BTC cycle that must produce exactly
     one completed round-trip. Invariants are asserted so the run is a real
     test (PASS / FAIL), not just a report:
       * exactly one trade, closed by a ``REVERSAL`` (not stop-loss)
       * ``net_pnl == gross_pnl - fees - slippage``
       * ``final_capital == initial_capital + net_pnl``
  2. **Tear sheet** - loads a larger BTC/USDT window (live Binance data when
      available, deterministic synthetic OHLCV otherwise) and prints a formatted
      ASCII performance summary to the console.

Run from the trading_bot directory:  python test_module_4.py [--live]
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from backtesting.engine import BacktestEngine
from backtesting.metrics import (
    BacktestMetrics,
    MetricsCalculator,
    format_tear_sheet,
)
from config.btc_config import get_btc_config
from data.historical_data import fetch_ohlcv
from risk.risk_manager import RiskManager
from strategy.base_strategy import ExitReason, SignalType
from strategy.btc_strategy import BTCStrategy

VOLUME = 1000.0
TOLERANCE = 1e-6


def make_candle(
    close: float,
    open_: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: float = VOLUME,
) -> Dict[str, float]:
    """Build a deterministic OHLCV candle dict from a close price."""
    hi = max(open_, close) if high is None else high
    lo = min(open_, close) if low is None else low
    return {
        "open": float(open_),
        "high": float(hi),
        "low": float(lo),
        "close": float(close),
        "volume": float(volume),
    }


def check(condition: bool, message: str, failures: List[str]) -> None:
    """Append a message to ``failures`` when ``condition`` is False."""
# ----------------------------------------------------------------------
# Deterministic smoke test
# ----------------------------------------------------------------------
def run_smoke_test() -> bool:
    """Drive a known 10-candle cycle and assert engine/metric invariant math."""
    print("=" * 72)
    print("Smoke Test: deterministic round trip + metric invariants")
    print("-" * 72)

    candles = [
        make_candle(100.0, 100.0),            # 1 seed reference
        make_candle(96.0, 100.0),             # 2 -4% -> DOWNTREND
        make_candle(92.0, 96.0),              # 3 deeper low
        make_candle(88.0, 91.0, low=87.8),    # 4 extreme low
        make_candle(91.0, 93.0, low=90.5),    # 5 rebound (red) -> READY_TO_BUY
        make_candle(94.0, 93.0),              # 6 green confirmation -> BUY @ 94
        make_candle(100.0, 94.0),             # 7 hold
        make_candle(108.0, 100.0),            # 8 hold
        make_candle(112.0, 108.0),            # 9 new high
        make_candle(107.0, 112.0, low=106.5), # 10 red confirmation -> SELL @ 107
    ]
    index = pd.date_range("2024-06-01", periods=len(candles), freq="1h", tz="UTC")
    df = pd.DataFrame(candles, index=index)

    engine = BacktestEngine(
        BTCStrategy(), RiskManager(), initial_capital=10_000.0
    )
    result = engine.run(df)
    metrics = MetricsCalculator().from_result(result)

    failures: List[str] = []

    # --- Exactly one completed trade, closed by reversal ------------------
    check(
        len(result.trades) == 1,
        f"expected exactly 1 trade, got {len(result.trades)}",
        failures,
    )
    if result.trades:
        trade = result.trades[0]
        check(
            trade.exit_reason == ExitReason.REVERSAL,
            f"expected REVERSAL exit, got {trade.exit_reason.value}",
            failures,
        )
        check(
            trade.gross_pnl > 0.0 and trade.net_pnl > 0.0,
            "this engineered cycle must be profitable",
            failures,
        )
        # Identity: net == gross - slippage - fees
        computed = (
            trade.gross_pnl
            - (trade.entry_slippage + trade.exit_slippage)
            - (trade.entry_fee + trade.exit_fee)
        )
        check(
            abs(computed - trade.net_pnl) < TOLERANCE,
            f"net identity mismatch: {computed:.6f} vs {trade.net_pnl:.6f}",
            failures,
        )
    
# --- Aggregate invariants ---------------------------------------------
    check(
        metrics.total_trades == 1
        and metrics.winning_trades == 1
        and metrics.losing_trades == 0,
        f"win/loss split wrong: total={metrics.total_trades} "
        f"wins={metrics.winning_trades} losses={metrics.losing_trades}",
        failures,
    )
    check(
        abs(metrics.final_capital - (metrics.initial_capital + metrics.net_pnl)) < TOLERANCE,
        f"final capital accounting drift: {metrics.final_capital:.6f}",
        failures,
    )
    check(
        abs(
            metrics.total_slippage + metrics.total_fees
            - metrics.gross_pnl + metrics.net_pnl
        ) < TOLERANCE,
        "fees+slippage reconcile with gross/net",
        failures,
    )
    check(
        metrics.max_drawdown_pct <= 0.0,
        f"drawdown must be <= 0, got {metrics.max_drawdown_pct}",
        failures,
    )

    # Print the trade detail for transparency.
    if result.trades:
        t = result.trades[0]
        print(f"    entry @ {t.entry_market_price:.4f} quantity={t.quantity:.4f}")
        print(f"    exit  @ {t.exit_market_price:.4f} reason={t.exit_reason.value}")
        print(f"    gross={t.gross_pnl:.2f} fees={t.entry_fee + t.exit_fee:.2f} "
              f"slippage={t.entry_slippage + t.exit_slippage:.2f} net={t.net_pnl:.2f}")
    print(f"    final capital={metrics.final_capital:.4f} "
          f"net pnl={metrics.net_pnl:.4f}")

    ok = not failures
    print("Smoke Test:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    return ok
# ----------------------------------------------------------------------
# Synthetic OHLCV generator (deterministic, regime-switching random walk)
# ----------------------------------------------------------------------
def _freq_for(timeframe: str) -> str:
    """Map a Binance kline interval to a pandas frequency string."""
    t = timeframe.lower()
    if t.endswith("m"):
        return f"{int(t[:-1])}min"
    if t.endswith("h"):
        return f"{int(t[:-1])}h"
    if t.endswith("d"):
        return f"{int(t[:-1])}D"
    if t.endswith("w"):
        return f"{int(t[:-1])}W"
    return "5min"


def make_synthetic_ohlcv(
    *,
    symbol: str,
    timeframe: str,
    n_candles: int = 2000,
    base: float = 60_000.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Build a deterministic OHLCV walk with trend / reversal regimes.

    Uses an episodic quant process: consecutive candles share a drift and
    volatility (regime), then flip to a new regime - producing the trend and
    reversion cycles the Module 2 strategy is designed to catch.
    """
    freq = _freq_for(timeframe)
    rng = np.random.default_rng(seed)

    log_prices = [math.log(base)]
    episode_remaining = 0
    drift = 0.0
    vol = 0.001

    for _ in range(n_candles):
        if episode_remaining <= 0:
            episode_remaining = int(rng.integers(15, 90))
            drift = float(rng.normal(0.0, 0.0012))
            vol = float(rng.uniform(0.0006, 0.0022))
        episode_remaining -= 1
        log_prices.append(log_prices[-1] + float(rng.normal(drift, vol)))

    closes = [math.exp(p) for p in log_prices[1:]]
    opens = [closes[0]] + closes[:-1]

    tails = rng.uniform(0.0002, 0.0016, size=len(closes))
    highs = [max(o, c) * (1.0 + tail) for o, c, tail in zip(opens, closes, tails)]
    lows = [min(o, c) * (1.0 - tail) for o, c, tail in zip(opens, closes, tails)]

    start = pd.Timestamp("2024-01-01", tz="UTC")
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(500.0, 2_000.0, size=len(closes)),
        },
        index=index,
    )


# ----------------------------------------------------------------------
# Dataset loader + tear-sheet runner
# ----------------------------------------------------------------------
def load_dataset(
    symbol: str,
    timeframe: str,
    *,
    live: bool = False,
) -> pd.DataFrame:
    """Fetch live Binance data when requested and reachable, else synthetic."""
    if live:
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=30)
            frame = fetch_ohlcv(
                symbol=symbol, timeframe=timeframe, start=start, end=end
            )
            print(f"[live] fetched {len(frame)} candles from Binance")
            return frame
        except Exception as exc:  # noqa: BLE001 - report and fall back
            print(f"[!] live fetch unavailable ({exc}); falling back to synthetic")
    return make_synthetic_ohlcv(symbol=symbol, timeframe=timeframe)


def run_report(*, use_live: bool = False) -> BacktestMetrics:
    """Load BTC/USDT data, run the backtest, print the ASCII tear sheet."""
    cfg = get_btc_config()
    symbol = cfg["symbol"]
    timeframe = cfg["timeframe"]

    df = load_dataset(symbol, timeframe, live=use_live)

    strategy = BTCStrategy()
    risk_manager = RiskManager()
    engine = BacktestEngine(
        strategy,
        risk_manager,
        initial_capital=cfg.get("initial_capital", 10_000.0),
    )

    result = engine.run(df)
    metrics = MetricsCalculator().from_result(result)

    print()
    print(format_tear_sheet(metrics))
    print()
    print(
        f"Summary: {len(result.trades)} trade(s) over {len(df)} candles; "
        f"{len(result.rejected_orders)} BUY signal(s) rejected by RiskManager."
    )
    return metrics
# ----------------------------------------------------------------------
def main() -> int:
    """Run the smoke test, then the full data tear-sheet report."""
    use_live = "--live" in sys.argv[1:]

    smoke_ok = run_smoke_test()

    print()
    run_report(use_live=use_live)

    print("=" * 72)
    print("OVERALL RESULT:", "PASS" if smoke_ok else "FAIL")
    return 0 if smoke_ok else 1


if __name__ == "__main__":
    sys.exit(main())