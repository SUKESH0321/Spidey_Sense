"""Market data integrity and validation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .historical_data import timeframe_to_timedelta


@dataclass
class ValidationResult:
    """Outcome of validating a DataFrame against integrity rules."""

    asset: str
    is_valid: bool
    issues: List[str]
    row_count: int
    null_columns: Dict[str, int]
    gaps: List[Tuple[pd.Timestamp, int]]
    duplicates: int
    non_monotonic: int

    def report(self) -> str:
        """Return a printable report of all findings."""
        lines = [
            f"[{self.asset}] validation {'PASSED' if self.is_valid else 'FAILED'}"
        ]
        lines.append(f"  rows={self.row_count}")

        if self.non_monotonic:
            lines.append(f"  out-of-order rows: {self.non_monotonic}")
        if self.duplicates:
            lines.append(f"  duplicate timestamps: {self.duplicates}")
        if self.gaps:
            lines.append(f"  gaps detected: {len(self.gaps)}")
            for start, count in self.gaps[:10]:
                lines.append(
                    f"    gap at {start} UTC: {count} missing candle(s)"
                )
        bad = {c: n for c, n in self.null_columns.items() if n}
        if bad:
            lines.append(f"  null/zero/non-finite values: {bad}")

        for issue in self.issues:
            lines.append(f"  ! {issue}")
        return "\n".join(lines)


def build_gap_list(
    frame: pd.DataFrame,
    timeframe_delta: pd.Timedelta,
    tolerance: pd.Timedelta,
) -> List[Tuple[pd.Timestamp, int]]:
    """Identify missing-candle gaps in a sorted DatetimeIndex.

    A gap is recorded whenever the forward spacing between consecutive candles
    exceeds the expected interval by more than ``tolerance``.
    """
    gaps: List[Tuple[pd.Timestamp, int]] = []
    if len(frame) < 2:
        return gaps

    ts = frame.index
    expected = timeframe_delta + tolerance
    for prev, cur in zip(ts[:-1], ts[1:]):
        delta = cur - prev
        if delta > expected:
            missing = int((delta - timeframe_delta) / timeframe_delta)
            gaps.append((prev, missing))
    return gaps


def validate_market_data(
    df: pd.DataFrame,
    asset: str,
    timeframe: str = "5m",
    tolerance: Optional[pd.Timedelta] = None,
) -> ValidationResult:
    """Run integrity checks on an OHLCV DataFrame.

    Checks:
      * chronological order (no out-of-order candles)
      * no duplicate timestamps
      * no NaN/inf/zero values in price/volume fields
      * missing-candle gap detection for the given timeframe

    ``df`` must have a UTC DatetimeIndex and at least the columns
    open/high/low/close/volume.
    """
    issues: List[str] = []

    frame = df[["open", "high", "low", "close", "volume"]].copy()

    # --- Chronological order ---
    non_monotonic = int((frame.index.to_series().diff() < pd.Timedelta(0)).sum())
    if non_monotonic:
        issues.append("detected out-of-order candles")

    # --- Duplicates ---
    duplicates = int(frame.index.duplicated().sum())
    if duplicates:
        issues.append("detected duplicate timestamps")

    # --- Null / zero / non-finite checks on OHLCV ---
    price_cols = ["open", "high", "low", "close"]
    null_columns: Dict[str, int] = {
        col: int(frame[col].isna().sum()) for col in price_cols + ["volume"]
    }
    for col in price_cols:
        zero_count = int((frame[col] == 0).sum())
        if zero_count:
            null_columns[col] += zero_count
        non_finite = int(
            frame[col].isin([float("inf"), float("-inf")]).sum()
        )
        if non_finite:
            null_columns[col] += non_finite
    if any(v for v in null_columns.values()):
        issues.append("found null/zero/non-finite price or volume values")

    # --- Gap detection ---
    timeframe_delta = timeframe_to_timedelta(timeframe)
    if tolerance is None:
        # Default tolerance: allow a small fuzz of one full interval.
        tolerance = timeframe_delta

    gaps: List[Tuple[pd.Timestamp, int]] = []
    if len(frame) > 1:
        gaps = build_gap_list(frame, timeframe_delta, tolerance)
        if gaps:
            issues.append(f"detected {len(gaps)} gap(s) in the data")

    is_valid = not issues
    return ValidationResult(
        asset=asset,
        is_valid=is_valid,
        issues=issues,
        row_count=len(frame),
        null_columns=null_columns,
        gaps=gaps,
        duplicates=duplicates,
        non_monotonic=non_monotonic,
    )