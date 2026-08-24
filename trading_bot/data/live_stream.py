"""Module 6: resilient Binance public market-data watcher (paper engine feed).

Implements a lightweight polling stream over the public Binance REST kline
endpoint -- **no API keys and no WebSocket dependency**. The streamer watches
the newest candle of each configured pair and forwards only *completed*
candles to the ``on_candle`` callback as dicts with ``open/high/low/close``,
``volume``, an ISO-8601 UTC ``timestamp`` and the raw ``open_time_ms``.

The strategy layer (Modules 2/3a) evaluates exactly one completed candle per
processing step, so this streamer is deliberately "edge triggered": a candle
is emitted once, when the newest row rolls over to a new open time.

Resilience (auto-reconnect):

  * every failed poll is logged and retried with capped exponential backoff;
  * HTTP ``429`` / ``418`` rate-limit responses trigger longer sleeps;
  * a long network outage never crashes the caller -- the stream keeps
    retrying and resumes emitting the next completed candle when the
    connection recovers. If candles were missed during the outage, a gap
    warning is logged with the number of missing intervals.

The public kline endpoint requires no authentication, so this module is safe
to run with zero credentials and can never accidentally place a real order.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.binance.com"
_KLINE_ENDPOINT = "/api/v3/klines"

# Supported candle intervals: Binance interval label -> duration in ms.
_INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Cap for the exponential reconnect backoff.
_MAX_BACKOFF_SECONDS = 30.0


class LiveStreamError(Exception):
    """Raised when a kline poll fails after exhausting its retry budget."""


def timeframe_to_ms(timeframe: str) -> int:
    """Map a Binance interval label to its duration in milliseconds."""
    key = str(timeframe).lower()
    if key not in _INTERVAL_MS:
        raise LiveStreamError(f"unsupported timeframe {timeframe!r}")
    return _INTERVAL_MS[key]


class LiveCandleStream:
    """Watch Binance public klines and emit completed candles.

    Args:
        symbols: Trading pairs to watch, e.g. ``("BTC/USDT", "ETH/USDT")``.
        timeframe: Candle interval, e.g. ``"5m"`` (must be in ``_INTERVAL_MS``).
        poll_interval_seconds: How often each pair's newest candle is polled.
            Anything >= 0.5s is accepted; 1-3s keeps a ``1m`` bot responsive.
        base_url: Binance REST base URL (swap for a mirror if needed).
        timeout: Per-request HTTP timeout in seconds.
        max_retries: Request retries per poll before raising
            :class:`LiveStreamError` (the stream then waits and reconnects).
        retry_backoff_seconds: Base delay for the exponential backoff.
    """

    def __init__(
        self,
        symbols: Tuple[str, ...] = ("BTC/USDT", "ETH/USDT"),
        timeframe: str = "5m",
        poll_interval_seconds: float = 3.0,
        base_url: str = _BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if not symbols:
            raise ValueError("at least one symbol is required")
        # Validate the timeframe up-front so misconfiguration fails fast.
        timeframe_to_ms(timeframe)

        self.symbols: Tuple[str, ...] = tuple(symbols)
        self.timeframe: str = str(timeframe)
        self._poll_interval_seconds = max(0.5, float(poll_interval_seconds))
        self._base_url = str(base_url).rstrip("/")
        self._timeout = float(timeout)
        self._max_retries = max(1, int(max_retries))
        self._retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))

        # Open time (ms) of the newest, still-forming candle per symbol.
        self._last_seen_open_ms: Dict[str, Optional[int]] = {}
        # Consecutive failed full polls per symbol (for reconnect logging).
        self._consecutive_errors: Dict[str, int] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self, on_candle: Callable[[Dict[str, Any]], None]) -> None:
        """Blocking stream loop: seed, then poll until :meth:`stop`.

        Args:
            on_candle: Callback invoked for every newly completed candle with
                a dict of ``open/high/low/close/volume``.
        """
        for symbol in self.symbols:
            self._seed(symbol)
        logger.info(
            "Candle stream live for %s on %s (poll every %.1fs)",
            ", ".join(self.symbols),
            self.timeframe,
            self._poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            self._poll_once(on_candle)
            if self._stop_event.wait(self._poll_interval_seconds):
                break
        logger.info("Candle stream stopped.")

    def start(
        self, on_candle: Callable[[Dict[str, Any]], None]
    ) -> threading.Thread:
        """Run :meth:`run` on a daemon thread (non-blocking convenience)."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run,
            args=(on_candle,),
            name="binance-live-candle-stream",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Signal the loop to stop after the current poll completes."""
        self._stop_event.set()
# ------------------------------------------------------------------
    # Polling / reconnect internals
    # ------------------------------------------------------------------
    def _seed(self, symbol: str) -> None:
        """Establish the baseline candle for ``symbol`` before the live loop.

        Retries forever (with backoff) so a bot started during an outage
        comes online automatically the moment the API responds again.
        """
        while not self._stop_event.is_set():
            try:
                rows = self._fetch_klines(symbol, limit=2)
                if not rows:
                    raise LiveStreamError(
                        f"[{symbol}] klines returned an empty payload"
                    )
                # The newest row is the still-forming candle: treating it as
                # "seen" means it will be emitted exactly when it completes.
                self._last_seen_open_ms[symbol] = int(rows[-1][0])
                logger.info(
                    "[%s] stream baseline established (open_time=%d)",
                    symbol, rows[-1][0],
                )
                return
            except LiveStreamError as exc:
                logger.error(
                    "%s - retrying seed in %.1fs (auto-reconnect)",
                    exc, self._backoff(3),
                )
                if self._stop_event.wait(self._backoff(3)):
                    return

    def _poll_once(self, on_candle: Callable[[Dict[str, Any]], None]) -> None:
        """Poll every symbol once and emit any candles that just completed."""
        for symbol in self.symbols:
            if self._stop_event.is_set():
                return
            try:
                rows = self._fetch_klines(symbol, limit=2)
                self._consume(symbol, rows, on_candle)
                errors = self._consecutive_errors.pop(symbol, 0)
                if errors >= self._max_retries:
                    logger.info(
                        "[%s] stream reconnected after %d failed poll(s)",
                        symbol, errors,
                    )
            except LiveStreamError as exc:
                previous = self._consecutive_errors.get(symbol, 0)
                self._consecutive_errors[symbol] = previous + 1
                if self._consecutive_errors[symbol] == self._max_retries:
                    logger.error(
                        "%s - stream paused, still reconnecting in the background", exc,
                    )
                    self._consecutive_errors[symbol] = 0
                time.sleep(min(1.0, self._poll_interval_seconds / 2.0))

    def _consume(
        self,
        symbol: str,
        rows: List[Any],
        on_candle: Callable[[Dict[str, Any]], None],
    ) -> None:
        """Detect and emit a newly completed candle for ``symbol``.

        The newest kline row is always the in-progress candle; when its open
        time differs from the remembered one, the previous row is the candle
        that just completed and is forwarded exactly once.
        """
        if not rows:
            return
        newest_open_ms = int(rows[-1][0])
        last_seen_ms = self._last_seen_open_ms.get(symbol)

        if last_seen_ms is None:
            self._last_seen_open_ms[symbol] = newest_open_ms
            return
        if newest_open_ms == last_seen_ms:
            return  # same candle still forming -> nothing completed yet

        gap_ms = newest_open_ms - last_seen_ms
        expected_ms = timeframe_to_ms(self.timeframe)
        if gap_ms > int(expected_ms * 1.5):
            missed = int(round(gap_ms / expected_ms))
            logger.warning(
                "[%s] missed %d candle(s) during a disconnect (gap of %d ms)",
                symbol, max(1, missed), gap_ms,
            )

        completed = rows[-2] if len(rows) >= 2 else rows[-1]
        on_candle(self._row_to_candle(symbol, completed))
        self._last_seen_open_ms[symbol] = newest_open_ms

    def _fetch_klines(self, symbol: str, limit: int = 2) -> List[Any]:
        """Fetch the newest ``limit`` klines with retries and backoff.

        Raises:
            LiveStreamError: After ``max_retries`` failed attempts.
        """
        params: Dict[str, Any] = {
            "symbol": self._api_symbol(symbol),
            "interval": self.timeframe,
            "limit": limit,
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = requests.get(
                    f"{self._base_url}{_KLINE_ENDPOINT}",
                    params=params,
                    timeout=self._timeout,
                )
                if resp.status_code in (429, 418):
                    # Rate limited: back off well beyond the normal retry pace.
                    wait = 30.0 if resp.status_code == 418 else self._backoff(attempt)
                    logger.warning(
                        "[%s] rate limited (HTTP %d), sleeping %.1fs",
                        symbol, resp.status_code, wait,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "[%s] poll attempt %d/%d failed: %s",
                    symbol, attempt, self._max_retries, exc,
                )
                time.sleep(self._backoff(attempt))
            except ValueError as exc:  # malformed JSON body
                last_exc = exc
                logger.warning(
                    "[%s] malformed kline payload on attempt %d/%d: %s",
                    symbol, attempt, self._max_retries, exc,
                )
                time.sleep(self._backoff(attempt))

        raise LiveStreamError(
            f"[{symbol}] could not fetch klines after "
            f"{self._max_retries} attempts: {last_exc}"
        )

    @staticmethod
    def _row_to_candle(symbol: str, row: Any) -> Dict[str, Any]:
        """Build the canonical candle dict from a Binance kline row."""
        return {
            "symbol": symbol,
            "timestamp": datetime.fromtimestamp(
                int(row[0]) / 1000.0, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "open_time_ms": int(row[0]),
            "close_time_ms": int(row[6]),
        }

    @staticmethod
    def _api_symbol(symbol: str) -> str:
        """Convert a slashed pair to Binance API form (``BTC/USDT`` -> ``BTCUSDT``)."""
        return symbol.replace("/", "")

    def _backoff(self, attempt: int) -> float:
        """Capped exponential backoff in seconds for the given attempt."""
        return min(
            _MAX_BACKOFF_SECONDS,
            self._retry_backoff_seconds * (2 ** max(0, attempt - 1)),
        )