"""SQLite persistence package (Module 6).

Provides :class:`Database` - the authoritative local ledger for the live
paper-trading engine. Every virtual fill lands in the ``trades`` table and
every per-asset strategy snapshot lands in ``bot_state``.
"""

from database.database import Database, utc_now_iso

__all__ = ["Database", "utc_now_iso"]