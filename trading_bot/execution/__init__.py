"""Paper execution package (Module 6).

Provides :class:`PaperExecutor` - the virtual spot execution engine that
maintains simulated USDT / BTC / ETH balances and persists every fill to the
SQLite ledger. No real orders are ever placed.
"""

from execution.paper_executor import (
    OpenPosition,
    PaperExecutionError,
    PaperExecutor,
    PaperFill,
)

__all__ = ["OpenPosition", "PaperExecutionError", "PaperExecutor", "PaperFill"]