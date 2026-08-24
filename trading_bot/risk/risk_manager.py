"""Module 3b: RiskManager - capital allocation and position sizing.

The strategy layer decides *what* to buy; the risk manager decides *how much*
and *whether at all*. Every BUY signal flows through
:meth:`RiskManager.validate_trade`, which:

1. sizes the requested order from the asset's ``position_size_pct``
   (a fraction of the currently available USDT balance),
2. caps it against the account exposure rule - the bot may never invest more
   than ``MAX_ACCOUNT_EXPOSURE_PERCENT`` of the total portfolio value,
3. returns a fully quantified :class:`RiskDecision` (approval + exact
   ``cost_usdt`` and ``quantity``) ready for an execution engine.

This module is deliberately pure capital math: no exchange APIs are touched and
no order is ever placed from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config.global_config import get_global_config


@dataclass(frozen=True)
class RiskDecision:
    """Verdict of the risk manager for a single BUY signal.

    Attributes:
        is_approved: True when the order may be executed.
        reason:      Short explanation (approval, scale-down or rejection).
        quantity:    Units of the base asset to buy (``cost_usdt / price``).
        cost_usdt:   Final USDT notional committed by the order.
    """

    is_approved: bool
    reason: str
    quantity: float
    cost_usdt: float


class RiskManager:
    """Portfolio-level capital allocation and order sizing.

    Args:
        max_account_exposure_percent: Maximum fraction of
            ``total_portfolio_value`` that may stay invested at any time.
            Defaults to ``config/global_config.MAX_ACCOUNT_EXPOSURE_PERCENT``.
        min_order_value_usdt: Minimum notional USDT an approved BUY may carry.
            Defaults to ``config/global_config.MIN_ORDER_VALUE_USDT``.
    """

    def __init__(
        self,
        max_account_exposure_percent: Optional[float] = None,
        min_order_value_usdt: Optional[float] = None,
    ) -> None:
        cfg = get_global_config()
        self.max_account_exposure_percent = (
            float(cfg["max_account_exposure_percent"])
            if max_account_exposure_percent is None
            else float(max_account_exposure_percent)
        )
        self.min_order_value_usdt = (
            float(cfg["min_order_value_usdt"])
            if min_order_value_usdt is None
            else float(min_order_value_usdt)
        )

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def validate_trade(
        self,
        asset_config: Dict[str, Any],
        current_price: float,
        available_usdt: float,
        total_portfolio_value: float,
    ) -> RiskDecision:
        """Size and approve/reject a BUY signal for one asset.

        Step-by-step sizing:

            intended_usdt = available_usdt * position_size_pct
            invested_usdt = total_portfolio_value - available_usdt
            max_allowed_usdt = total_portfolio_value * max_account_exposure_percent
                               - invested_usdt
            cost_usdt = min(intended_usdt, max_allowed_usdt)
            quantity  = cost_usdt / current_price

        The trade is rejected when ``cost_usdt <= 0`` (exposure cap consumed)
        or when the notional falls below the minimum order size.

        Args:
            asset_config: Per-asset config dict (e.g. from
                ``get_btc_config()``). ``position_size_pct`` lives in its
                ``"strategy"`` section; a top-level ``position_size_pct`` is
                honoured as a fallback.
            current_price: Current price of the base asset in USDT.
            available_usdt: Free cash balance (not yet invested).
            total_portfolio_value: Mark-to-market value of the whole portfolio
                (invested assets + free cash).

        Returns:
            A :class:`RiskDecision`. Approved orders carry the exact
            ``cost_usdt`` and ``quantity``; rejected ones carry zero for both.
        """
        # ---- Input sanity -------------------------------------------------
        if current_price <= 0:
            return self._reject(
                f"current_price must be positive, got {current_price:g}"
            )
        if available_usdt <= 0:
            return self._reject(
                f"no free capital to allocate (available_usdt={available_usdt:g})"
            )
        if total_portfolio_value <= 0:
            return self._reject(
                f"total_portfolio_value must be positive, got {total_portfolio_value:g}"
            )

        position_size_pct = self._get_position_size_pct(asset_config)
        if position_size_pct is None or not 0.0 < position_size_pct <= 1.0:
            return self._reject(
                "asset position_size_pct must be configured within (0, 1] "
                "(spot trading, no leverage)"
            )

        # ---- size math ----------------------------------------------------
        intended_usdt = available_usdt * position_size_pct

        invested_usdt = total_portfolio_value - available_usdt
        max_allowed_usdt = (
            total_portfolio_value * self.max_account_exposure_percent
            - invested_usdt
        )

        cost_usdt = min(intended_usdt, max_allowed_usdt)

        # ---- rejection rules ---------------------------------------------
        if cost_usdt <= 0:
            return self._reject(
                "account exposure cap already fully consumed "
                f"(max_allowed_usdt={max_allowed_usdt:.2f} <= 0)"
            )
        if cost_usdt < self.min_order_value_usdt:
            return self._reject(
                f"approved notional {cost_usdt:.2f} USDT is below the minimum "
                f"order size of {self.min_order_value_usdt:.2f} USDT"
            )

        quantity = cost_usdt / current_price

        if cost_usdt < intended_usdt:
            reason = (
                f"approved but scaled down: wanted {intended_usdt:.2f} USDT, "
                f"capped to {cost_usdt:.2f} USDT by the "
                f"{self.max_account_exposure_percent:.0%} global exposure limit"
            )
        else:
            reason = (
                f"approved: {cost_usdt:.2f} USDT committed (intended "
                f"{intended_usdt:.2f} USDT, max allowed "
                f"{max_allowed_usdt:.2f} USDT)"
            )

        return RiskDecision(
            is_approved=True,
            reason=reason,
            quantity=quantity,
            cost_usdt=cost_usdt,
        )

    # ----------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------

    def _get_position_size_pct(
        self, asset_config: Dict[str, Any]
    ) -> Optional[float]:
        """Read ``position_size_pct`` from the asset config.

        Looked up inside ``asset_config["strategy"]`` first, then at the top
        level of ``asset_config`` as a fallback. Returns ``None`` when the
        value is missing, non-numeric or not positive.
        """
        strategy_section = asset_config.get("strategy")
        if isinstance(strategy_section, dict):
            raw = strategy_section.get("position_size_pct")
            if raw is not None:
                return self._as_positive_float(raw)

        raw = asset_config.get("position_size_pct")
        return self._as_positive_float(raw) if raw is not None else None

    @staticmethod
    def _as_positive_float(value: Any) -> Optional[float]:
        """Coerce a config value to a positive float, else ``None``."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0.0 else None

    @staticmethod
    def _reject(reason: str) -> RiskDecision:
        """Build a rejected decision with zero quantity and zero cost."""
        return RiskDecision(
            is_approved=False,
            reason=reason,
            quantity=0.0,
            cost_usdt=0.0,
        )