"""Historical spot OHLCV data fetcher for Binance.

Uses the public Binance REST API directly with pagination to pull arbitrary
historical windows while respecting API rate limits.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Binance public spot REST API base (no auth required).
_BASE_URL = "https://api.binance.com"

# Conservative pacing: Binance allows 1200 request weight / min.
# Pulling 1000 candles gets weight ~2 per call, but we stay well within limits.
_SLEEP_SECONDS = 0.15

# CNV to millis for API calls.
_MS = 1000


class HistoricalDataError(Exception):
    """Raised when historical OHLCV data cannot be fetched."""


def _to_ms(dt: datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * _MS)


def _api_symbol(symbol: str) -> str:
    """Convert a slashed symbol (BTC/USDT) to Binance API form (BTCUSDT)."""
    return symbol.replace("/", "")


def _fetch_page(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    timeout: float = 30.0,
    retries: int = 3,
) -> list:
    """Fetch a single page of klines between start_ms and end_ms (inclusive).

    Robust to transient network/timeout errors via bounded retries.
    """
    params = {
        "symbol": _api_symbol(symbol),
        "interval": timeframe,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{_BASE_URL}/api/v3/klines",
                params=params,
                timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code == 418:
                # Rate limited: back off and retry, longer for 418.
                wait = 30.0 if resp.status_code == 418 else 2.0
                logger.warning(
                    "Rate limit hit (HTTP %s) fetching %s, sleeping %.1fs",
                    resp.status_code, symbol, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Request error on attempt %d/%d for %s: %s",
                attempt, retries, symbol, exc,
            )
            time.sleep(attempt)  # linear backoff

    raise HistoricalDataError(f"Failed to fetch klines for {symbol}: {last_exc}")


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: Optional[datetime] = None,
    max_rows: int = 1000,
) -> pd.DataFrame:
    """Fetch spot OHLCV data for the given window.

    Args:
        symbol: Trading pair, e.g. "BTC/USDT".
        timeframe: Binance kline interval, e.g. "5m".
        start: Start of the window (timezone-aware preferred).
        end: End of the window. Defaults to now.
        max_rows: Max candles per page request.

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume.
        `timestamp` is a UTC datetime index.
    """
    if end is None:
        end = datetime.now(timezone.utc)

    start_ms = _to_ms(start)
    end_ms = _to_ms(end)

    all_rows: list = []
    cursor = start_ms

    # Sanity guard to prevent unbounded loops.
    safety_max_loops = 100_000

    while cursor <= end_ms:
        page = _fetch_page(symbol, timeframe, cursor, end_ms, max_rows)

        if not page:
            break

        all_rows.extend(page)

        # Advance cursor just past the last returned candle's close time.
        last_close_time = page[-1][6]
        cursor = last_close_time + 1

        if len(page) < max_rows:
            # Short page means we reached the end of the window.
            break

        safety_max_loops -= 1
        if safety_max_loops <= 0:
            raise HistoricalDataError(
                f"Pagination for {symbol} exceeded safety loop limit."
            )

        time.sleep(_SLEEP_SECONDS)

    if not all_rows:
        raise HistoricalDataError(f"No data returned for {symbol} in the window.")

    df = pd.DataFrame(all_rows)
    df = df.iloc[:, :6]  # keep open_time, OHLC, volume only
    df.columns = ["timestamp", "open", "high", "low", "close", "volume"]

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df.sort_index()
    # Drop anything outside the requested window (e.g. the final open candle).
    df = df[df.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    return df


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """Map a Binance kline interval string to a pandas Timedelta."""
    match = timeframe.lower()
    if match.endswith("m"):
        minutes = int(match[:-1])
    elif match.endswith("h"):
        minutes = int(match[:-1]) * 60
    elif match.endswith("d"):
        minutes = int(match[:-1]) * 1440
    elif match.endswith("w"):
        minutes = int(match[:-1]) * 10080
    else:
        raise HistoricalDataError(
            f"Unsupported timeframe: {timeframe!r}"
        )
    return pd.Timedelta(minutes=minutes)