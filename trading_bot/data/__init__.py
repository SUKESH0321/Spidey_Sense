"""Market data package (Modules 1 + 6).

Provides historical OHLCV fetching, integrity validation, and -- for the live
paper engine -- the resilient public candle stream:

  * :func:`fetch_ohlcv`           - paginated historical klines (Binance REST).
  * :func:`validate_market_data`  - integrity checks on an OHLCV DataFrame.
  * :class:`LiveCandleStream`     - live completed-candle feed with auto-
                                    reconnect (Module 6, no API keys).
"""

from data.historical_data import fetch_ohlcv, timeframe_to_timedelta
from data.live_stream import LiveCandleStream, LiveStreamError, timeframe_to_ms
from data.market_data import validate_market_data

__all__ = [
    "fetch_ohlcv",
    "timeframe_to_timedelta",
    "validate_market_data",
    "LiveCandleStream",
    "LiveStreamError",
    "timeframe_to_ms",
]