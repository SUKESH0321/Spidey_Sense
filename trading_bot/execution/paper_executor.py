"""Module 6: virtual paper execution engine + SQLite ledger.

:class:`PaperExecutor` maintains an in-memory simulated spot account (USDT,
BTC, ETH balances) and persists every fill to the :class:`Database` ledger.
Execution is strictly virtual:

  * a BUY  fills at ``price * (1 + slippage_pct)`` and debits
    ``quantity * fill_price + fee`` from the virtual USDT;
  * a SELL fills at ``price * (1 - slippage_pct)`` and credits the proceeds
    minus the exit fee back to virtual USDT, realising gross / net P&L.

P&L accounting is the same identity used by the Module 4/5 backtest engines::

    gross_pnl = (sell_fill - buy_fill) * quantity
    net_pnl   = gross_pnl - entry_fee - exit_fee

where ``buy_fill`` is the actual (slippage-adjusted) entry price. Because the
executor remembers each open position's fill price and entry fee, ``net_pnl``
exactly equals the realized change in the virtual USDT balance.

Nothing in this module can place a real order on any exchange; an API-error
like attempt to *inject* live trading would have to be added upstream and is
deliberately absent here (``TRADING_MODE = "PAPER"``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from database.database import Database, utc_now_iso

logger = logging.getLogger(__name__)

# Base-asset labels the executor can hold balances for.
_SUPPORTED_LABELS = ("BTC", "ETH")


class PaperExecutionError(Exception):
    """Raised when a paper order cannot be filled (bad params / no funds)."""


@dataclass
class OpenPosition:
    """An open virtual spot position for one asset.

    Attributes:
        symbol: Full trading pair, e.g. ``"BTC/USDT"``.
        entry_price: Actual BUY fill price (includes slippage).
        quantity: Units of the base asset currently held.
        entry_fee: USDT trading fee paid on entry.
        opened_at: ISO-8601 timestamp of the entry fill.
    """

    symbol: str
    entry_price: float
    quantity: float
    entry_fee: float
    opened_at: str


@dataclass
class PaperFill:
    """The result of one virtual execution.

    Attributes:
        symbol: Trading pair (``"BTC/USDT"``).
        side: ``"BUY"`` or ``"SELL"``.
        market_price: Raw market price the signal used.
        execution_price: Actual fill price after slippage.
        quantity: Executed base-asset quantity.
        notional_usdt: BUY -> cost (``qty * fill``); SELL -> gross proceeds.
        fee: Trading fee charged on the fill notional.
        slippage: Slippage cost in USDT (``qty * market_price * slippage_pct``).
        reason: Strategy / risk reason attached to the order.
        net_pnl: Realized net P&L for SELL fills, ``None`` for BUY fills.
        usdt_balance_after: Virtual USDT balance after the fill.
    """

    timestamp: str
    symbol: str
    side: str
    market_price: float
    execution_price: float
    quantity: float
    notional_usdt: float
    fee: float
    slippage: float
    reason: str
    net_pnl: Optional[float]
    usdt_balance_after: float


class PaperExecutor:
    """Virtual spot execution engine over a :class:`Database` ledger.

    Args:
        database: Open :class:`Database` instance where every fill is logged.
        initial_usdt: Starting virtual USDT capital (e.g. ``10_000.0``).

    Raises:
        ValueError: If ``initial_usdt`` is not positive.
    """

    def __init__(self, database: Database, initial_usdt: float = 10_000.0) -> None:
        if initial_usdt <= 0:
            raise ValueError("initial_usdt must be positive")
        self._database = database
        self.initial_usdt = float(initial_usdt)

        # Public balances as required by the module contract.
        self.usdt_balance: float = self.initial_usdt
        self.btc_balance: float = 0.0
        self.eth_balance: float = 0.0

        # Open fill records keyed by base-asset label ("BTC", "ETH").
        self._positions: Dict[str, OpenPosition] = {}

    # ------------------------------------------------------------------
    # Balance / position introspection
    # ------------------------------------------------------------------
    @property
    def database(self) -> Database:
        """The ledger this executor writes to."""
        return self._database

    @property
    def balances(self) -> Dict[str, float]:
        """Current virtual balances as ``{label: amount}`` (USDT included)."""
        return {
            "USDT": self.usdt_balance,
            "BTC": self.btc_balance,
            "ETH": self.eth_balance,
        }

    def get_asset_balance(self, symbol: str) -> float:
        """Return the base-asset balance held for a pair (e.g. ``BTC/USDT``)."""
        label = self._base_label(symbol)
        if label == "USDT":
            return self.usdt_balance
        return getattr(self, f"{label.lower()}_balance")

    def get_position(self, symbol: str) -> Optional[OpenPosition]:
        """Return the open-position record for ``symbol`` or ``None``."""
        return self._positions.get(self._base_label(symbol))

    def positions_open(self) -> int:
        """Number of assets currently carrying an open paper position."""
        return sum(1 for pos in self._positions.values() if pos.quantity > 0.0)

    def portfolio_value(self, prices: Mapping[str, float]) -> float:
        """Mark-to-market value: USDT cash + open holdings at ``prices``.

        ``prices`` should be keyed by base-asset label (``{"BTC": 64_250.0}``).
        """
        total = self.usdt_balance
        for label in _SUPPORTED_LABELS:
            price = prices.get(label)
            quantity = getattr(self, f"{label.lower()}_balance")
            if price is not None and quantity > 0.0:
                total += quantity * float(price)
        return float(total)
# ------------------------------------------------------------------
    # Execution API
    # ------------------------------------------------------------------
    def execute_buy(
        self,
        symbol: str,
        price: float,
        quantity: float,
        fee_pct: float,
        slippage_pct: float,
        reason: str,
        *,
        timestamp: Optional[str] = None,
    ) -> PaperFill:
        """Fill a virtual market BUY for ``symbol``.

        The fill price is ``price * (1 + slippage_pct)``; the committed
        notional ``quantity * fill_price`` plus the entry fee is deducted from
        the virtual USDT balance and the base-asset quantity is credited.
        The fill is persisted to the SQLite ``trades`` table.

        Args:
            symbol: Trading pair (``"BTC/USDT"``, ``"ETH/USDT"``).
            price: Market price (candle close) used to trigger the signal.
            quantity: Base-asset units to purchase.
            fee_pct: Entry trading fee as a fraction (e.g. ``0.001``).
            slippage_pct: Adverse fill spread as a fraction (e.g. ``0.0005``).
            reason: Why the order was placed (risk manager text).
            timestamp: Fill timestamp stored in the ledger. Defaults to now.

        Returns:
            A fully-populated :class:`PaperFill`.

        Raises:
            PaperExecutionError: For invalid parameters or insufficient
                virtual USDT.
        """
        label = self._base_label(symbol)
        self._validate_params(price, quantity, fee_pct, slippage_pct)

        fill_price = price * (1.0 + slippage_pct)
        notional = quantity * fill_price
        fee = notional * fee_pct
        total_cost = notional + fee

        if total_cost > self.usdt_balance + 1e-9:
            raise PaperExecutionError(
                f"[{label}] paper BUY rejected: need {total_cost:,.2f} USDT "
                f"but only {self.usdt_balance:,.2f} available"
            )

        self.usdt_balance -= total_cost
        new_balance = self.get_asset_balance(symbol) + quantity
        self._set_asset_balance(label, new_balance)
        self._positions[label] = OpenPosition(
            symbol=symbol,
            entry_price=fill_price,
            quantity=quantity,
            entry_fee=fee,
            opened_at=timestamp or utc_now_iso(),
        )

        slippage_usdt = quantity * price * slippage_pct
        fill = PaperFill(
            timestamp=timestamp or utc_now_iso(),
            symbol=symbol,
            side="BUY",
            market_price=price,
            execution_price=fill_price,
            quantity=quantity,
            notional_usdt=notional,
            fee=fee,
            slippage=slippage_usdt,
            reason=reason,
            net_pnl=None,
            usdt_balance_after=self.usdt_balance,
        )
        self._log_fill(symbol, fill_price, quantity, notional, "BUY")
        self._persist_fill(fill)
        return fill

    def execute_sell(
        self,
        symbol: str,
        price: float,
        quantity: float,
        fee_pct: float,
        slippage_pct: float,
        reason: str,
        entry_price: Optional[float] = None,
        *,
        timestamp: Optional[str] = None,
    ) -> PaperFill:
        """Fill a virtual market SELL, realising P&L for an open position.

        The fill price is ``price * (1 - slippage_pct)``; the gross proceeds
        minus the exit fee are credited back to virtual USDT. The asset
        balance is cleared and the fill is persisted with the realized
        ``net_pnl``.

        ``entry_price`` should be the actual BUY fill price; when omitted the
        executor falls back to the remembered open-position fill::

            gross_pnl = (sell_fill - entry_price) * quantity
            net_pnl   = gross_pnl - entry_fee - exit_fee

        Args:
            symbol: Trading pair (``"BTC/USDT"``, ``"ETH/USDT"``).
            price: Market price (candle close) used to trigger the signal.
            quantity: Base-asset units to sell. Must not exceed the balance.
            fee_pct: Exit trading fee as a fraction (e.g. ``0.001``).
            slippage_pct: Adverse fill spread as a fraction (e.g. ``0.0005``).
            reason: Exit reason (``"REVERSAL"`` / ``"STOP_LOSS"`` / ...).
            entry_price: Actual entry fill price for P&L maths. Defaults to
                the remembered open position's fill price.
            timestamp: Fill timestamp stored in the ledger. Defaults to now (UTC).

        Returns:
            A fully-populated :class:`PaperFill` (``net_pnl`` set).

        Raises:
            PaperExecutionError: On a missing balance or missing entry price
                with no open position record.
        """
        label = self._base_label(symbol)
        self._validate_params(price, quantity, fee_pct, slippage_pct)

        held = self.get_asset_balance(symbol)
        if held <= 1e-12:
            raise PaperExecutionError(
                f"[{label}] paper SELL rejected: no asset balance to sell"
            )
        if quantity > held + 1e-12:
            raise PaperExecutionError(
                f"[{label}] paper SELL rejected: asked to sell "
                f"{quantity:.8f} but only {held:.8f} held"
            )

        position = self._positions.get(label)
        entry_used = entry_price if entry_price is not None and entry_price > 0.0 else None
        if entry_used is None:
            if position is None:
                raise PaperExecutionError(
                    f"[{label}] paper SELL needs a positive entry_price for "
                    "P&L when no position record exists"
                )
            entry_used = position.entry_price
        buy_fee = position.entry_fee if position is not None else 0.0

        fill_price = price * (1.0 - slippage_pct)
        proceeds = quantity * fill_price
        fee = proceeds * fee_pct
        net_proceeds = proceeds - fee

        gross_pnl = (fill_price - entry_used) * quantity
        net_pnl = gross_pnl - buy_fee - fee

        self.usdt_balance += net_proceeds
        self._set_asset_balance(label, held - quantity)
        if quantity >= held - 1e-12:
            self._positions.pop(label, None)

        slippage_usdt = quantity * price * slippage_pct
        fill = PaperFill(
            timestamp=timestamp or utc_now_iso(),
            symbol=symbol,
            side="SELL",
            market_price=price,
            execution_price=fill_price,
            quantity=quantity,
            notional_usdt=proceeds,
            fee=fee,
            slippage=slippage_usdt,
            reason=reason,
            net_pnl=net_pnl,
            usdt_balance_after=self.usdt_balance,
        )
        self._log_fill(symbol, fill_price, quantity, proceeds, "SELL", net_pnl)
        self._persist_fill(fill)
        return fill

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _base_label(symbol: str) -> str:
        """Extract the base-asset label (``"BTC/USDT"`` -> ``"BTC"``)."""
        label = symbol.split("/", 1)[0] if "/" in symbol else symbol
        upper = label.upper()
        if upper not in _SUPPORTED_LABELS:
            raise PaperExecutionError(
                f"unsupported asset label {label!r}; "
                f"supported: {_SUPPORTED_LABELS}"
            )
        return upper

    def _set_asset_balance(self, label: str, value: float) -> None:
        """Write the public balance attribute for a base asset."""
        amount = float(value)
        if amount < 0 and abs(amount) > 1e-9:
            raise PaperExecutionError(
                f"[{label}] balance cannot go negative (tried {amount:.8f})"
            )
        setattr(self, f"{label.lower()}_balance", amount)

    def _validate_params(
        self,
        price: float,
        quantity: float,
        fee_pct: float,
        slippage_pct: float,
    ) -> None:
        """Guard against nonsensical execution parameters."""
        try:
            price_f = float(price)
            qty_f = float(quantity)
            fee_f = float(fee_pct)
            slip_f = float(slippage_pct)
        except (TypeError, ValueError) as exc:
            raise PaperExecutionError(
                "price/quantity/fee_pct/slippage_pct must be numeric"
            ) from exc
        if price_f <= 0.0:
            raise PaperExecutionError("price must be positive")
        if qty_f <= 0.0:
            raise PaperExecutionError("quantity must be positive")
        if not 0.0 <= fee_f < 1.0:
            raise PaperExecutionError("fee_pct must be within [0, 1)")
        if not 0.0 <= slip_f < 1.0:
            raise PaperExecutionError("slippage_pct must be within [0, 1)")

    def _persist_fill(self, fill: PaperFill) -> None:
        """Append the fill to the SQLite ledger."""
        self._database.save_trade(
            {
                "timestamp": fill.timestamp,
                "symbol": fill.symbol,
                "side": fill.side,
                "price": fill.execution_price,
                "quantity": fill.quantity,
                "cost_usdt": fill.notional_usdt,
                "fee": fill.fee,
                "slippage": fill.slippage,
                "reason": fill.reason,
                "net_pnl": fill.net_pnl,
            }
        )

    @staticmethod
    def _log_fill(
        symbol: str,
        fill_price: float,
        quantity: float,
        notional: float,
        side: str,
        net_pnl: Optional[float] = None,
    ) -> None:
        """Print a concise paper-fill line to the console."""
        pnl_txt = f" | Net P&L: {net_pnl:+,.2f}" if net_pnl is not None else ""
        print(
            f"[PAPER] {symbol} {side} @ {fill_price:,.4f} | "
            f"Qty {quantity:,.6f} | Notional {notional:,.2f} USDT{pnl_txt}"
        )