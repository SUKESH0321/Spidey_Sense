"""Self-contained verification script for Module 1.

Fetches the last 14 days of 5m OHLCV data for BTC/USDT and ETH/USDT,
runs integrity validation, and prints a summary to the console.

Run from the trading_bot directory:  python test_module_1.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from config.btc_config import get_btc_config
from config.eth_config import get_eth_config
from data.historical_data import fetch_ohlcv
from data.market_data import validate_market_data

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("test_module_1")


def summarize(
    asset_label: str, config: dict, df: pd.DataFrame, result
) -> None:
    """Print a concise summary for one asset's dataset."""
    print("=" * 72)
    print(f"ASSET: {asset_label} ({config['symbol']})")
    print("-" * 72)
    print(f"  rows            : {result.row_count}")
    print(f"  date range      : {df.index.min()} -> {df.index.max()}")
    print(f"  null values     : {result.null_columns}")
    print(f"  duplicates      : {result.duplicates}")
    print(f"  out-of-order    : {result.non_monotonic}")
    print(f"  gaps            : {len(result.gaps)}")
    print()
    print("  head(5):")
    print(df.head(5).to_string())
    print()
    print("  tail(5):")
    print(df.tail(5).to_string())
    print()
    print(result.report())
    print()


def main() -> int:
    days_back = 14
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)

    configs = [
        ("BTC", get_btc_config()),
        ("ETH", get_eth_config()),
    ]

    all_valid = True
    for label, config in configs:
        symbol = config["symbol"]
        timeframe = config["timeframe"]
        logger.info("Fetching %s (%s) from %s to %s", symbol, timeframe, start, end)
        try:
            df = fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
            )
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", symbol, exc)
            all_valid = False
            continue

        result = validate_market_data(df, asset=label, timeframe=timeframe)
        summarize(label, config, df, result)
        if not result.is_valid:
            all_valid = False

    print("=" * 72)
    print("OVERALL RESULT:", "PASS" if all_valid else "FAIL")
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())