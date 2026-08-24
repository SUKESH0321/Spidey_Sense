"""Module 6: SQLite persistence layer for the live paper-trading engine.

The local SQLite database is the authoritative ledger of the simulated paper
account:

  * ``trades``    - every virtual BUY / SELL fill with price, quantity,
                    notional (``cost_usdt``), fee, slippage impact, reason and
                    -- for exits -- the realized ``net_pnl``.
  * ``bot_state`` - one row per asset with a snapshot of the strategy state
                    machine (``current_state``, ``entry_price``,
                    ``highest_price``, ``lowest_price``) after every processed
                    candle. Rows are upserted keyed on ``symbol``.

The engine writes in autocommit mode: every ``INSERT`` / ``UPDATE`` is
committed immediately, so an interruption (Ctrl+C, crash, power loss) can
never leave the paper ledger in an inconsistent or half-written state.

Only Python's built-in ``sqlite3`` module is used -- there is no external
database driver dependency.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Canonical trades columns (id is the auto-increment primary key).
_TRADE_COLUMNS: tuple = (
    "timestamp",
    "symbol",
    "side",
    "price",
    "quantity",
    "cost_usdt",
    "fee",
    "slippage",
    "reason",
    "net_pnl",
)

_VALID_SIDES = ("BUY", "SELL")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite-backed persistence for paper trades and bot state.

    Args:
        db_path: File path of the SQLite database. Defaults to
            ``paper_trading.db`` in the working directory. ``":memory:"`` is
            supported for ephemeral tests, though the data will not survive a
            connection close.
        check_same_thread: Allow the connection to be used from multiple
            threads. Enabled by default here because the live stream may run
            the orchestrator loop on the main thread while a dashboard thread
            reads the ledger concurrently. All writes are serialised through
            an internal lock.

    Raises:
        sqlite3.Error: If the database cannot be opened or the schema cannot
            be created.
    """

    def __init__(
        self,
        db_path: str = "paper_trading.db",
        *,
        check_same_thread: bool = False,
    ) -> None:
        self.db_path = str(db_path)
        self._write_lock = threading.Lock()
        # Autocommit isolation level: every statement is committed right away.
        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=check_same_thread,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("SQLite ledger initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _create_tables(self) -> None:
        """Create the ``trades`` and ``bot_state`` tables if missing."""
        # WAL allows a reader to stay open while the writer commits.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # In-memory databases do not support WAL; ignore silently.
            pass

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                symbol    TEXT    NOT NULL,
                side      TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
                price     REAL    NOT NULL,
                quantity  REAL    NOT NULL,
                cost_usdt REAL    NOT NULL,
                fee       REAL    NOT NULL DEFAULT 0,
                slippage  REAL    NOT NULL DEFAULT 0,
                reason    TEXT    NOT NULL DEFAULT '',
                net_pnl   REAL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                symbol        TEXT PRIMARY KEY,
                current_state TEXT NOT NULL,
                entry_price   REAL,
                highest_price REAL,
                lowest_price  REAL,
                last_updated  TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        """Flush and close the SQLite connection (safe to call twice)."""
        with self._write_lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except sqlite3.Error as exc:  # pragma: no cover - defensive
                    logger.warning("Error closing SQLite connection: %s", exc)
                finally:
                    self.conn = None  # type: ignore[assignment]
            logger.info("SQLite ledger closed for %s", self.db_path)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _cursor(self) -> sqlite3.Cursor:
        """Return a cursor, raising a clear error once the ledger is closed."""
        if self.conn is None:
            raise RuntimeError(f"SQLite ledger at {self.db_path!r} is closed")
        return self.conn.cursor()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def save_trade(self, trade: Dict[str, Any]) -> int:
        """Persist one virtual fill and return the new ``trades.id``.

        Required keys: ``timestamp``, ``symbol``, ``side``, ``price``,
        ``quantity``, ``cost_usdt``. Optional keys (``fee``, ``slippage``,
        ``reason``, ``net_pnl``) default to ``0`` / ``""`` / ``None``.

        Raises:
            ValueError: For a missing column, an invalid ``side`` or a
                non-positive price/quantity.
        """
        side = str(trade.get("side", "")).upper()
        if side not in _VALID_SIDES:
            raise ValueError(
                f"save_trade requires side in {_VALID_SIDES}, got {side!r}"
            )

        row: Dict[str, Any] = {
            "timestamp": str(trade["timestamp"]),
            "symbol": str(trade["symbol"]),
            "side": side,
            "price": float(trade["price"]),
            "quantity": float(trade["quantity"]),
            "cost_usdt": float(trade["cost_usdt"]),
            "fee": float(trade.get("fee", 0.0)),
            "slippage": float(trade.get("slippage", 0.0)),
            "reason": str(trade.get("reason", "")),
            "net_pnl": (
                float(trade["net_pnl"])
                if trade.get("net_pnl") is not None
                else None
            ),
        }
        if row["price"] <= 0 or row["quantity"] <= 0:
            raise ValueError("save_trade requires positive price and quantity")
        if row["fee"] < 0 or row["slippage"] < 0:
            raise ValueError("save_trade fee/slippage must be >= 0")

        placeholders = ", ".join(["?"] * len(_TRADE_COLUMNS))
        columns = ", ".join(_TRADE_COLUMNS)
        with self._write_lock:
            cursor = self._cursor().execute(
                f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
                tuple(row[col] for col in _TRADE_COLUMNS),
            )
            new_id = int(cursor.lastrowid)
        logger.debug(
            "Ledger insert #%d: %s %s qty=%.6f price=%.6f",
            new_id,
            row["side"],
            row["symbol"],
            row["quantity"],
            row["price"],
        )
        return new_id

    def get_recent_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent ``limit`` trades, newest first.

        Rows are plain dicts keyed by the ``trades`` column names.
        """
        limit = max(1, int(limit))
        with self._write_lock:
            rows = self._cursor().execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Bot state
    # ------------------------------------------------------------------
    def save_bot_state(
        self,
        symbol: str,
        *,
        current_state: str,
        entry_price: Optional[float] = None,
        highest_price: Optional[float] = None,
        lowest_price: Optional[float] = None,
        last_updated: Optional[str] = None,
    ) -> None:
        """Upsert the strategy snapshot for one asset symbol.

        ``symbol`` is the primary key; a second write for the same symbol
        replaces the previous row atomically.
        """
        row: Dict[str, Any] = {
            "symbol": str(symbol),
            "current_state": str(current_state),
            "entry_price": (
                float(entry_price) if entry_price is not None else None
            ),
            "highest_price": (
                float(highest_price) if highest_price is not None else None
            ),
            "lowest_price": (
                float(lowest_price) if lowest_price is not None else None
            ),
            "last_updated": str(last_updated or utc_now_iso()),
        }
        with self._write_lock:
            self._cursor().execute(
                """
                INSERT INTO bot_state
                    (symbol, current_state, entry_price,
                     highest_price, lowest_price, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    current_state = excluded.current_state,
                    entry_price   = excluded.entry_price,
                    highest_price = excluded.highest_price,
                    lowest_price  = excluded.lowest_price,
                    last_updated  = excluded.last_updated
                """,
                (
                    row["symbol"],
                    row["current_state"],
                    row["entry_price"],
                    row["highest_price"],
                    row["lowest_price"],
                    row["last_updated"],
                ),
            )
        logger.debug(
            "bot_state upserted for %s -> state=%s",
            row["symbol"],
            row["current_state"],
        )

    def load_bot_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the persisted snapshot for ``symbol``, or ``None``."""
        with self._write_lock:
            row = self._cursor().execute(
                "SELECT * FROM bot_state WHERE symbol = ?",
                (str(symbol),),
            ).fetchone()
        return dict(row) if row is not None else None