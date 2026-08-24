"""Global, cross-asset risk limits (Module 3b).

Per-asset configs (``btc_config``/``eth_config``) control a single trading
pair. Everything in this module applies to the whole portfolio: these are the
account-level guardrails every BUY signal must satisfy before execution.
"""

from typing import Any, Dict

# The bot must never invest more than 80% of the total portfolio value at any
# given time - regardless of how many assets are signalling BUY.
MAX_ACCOUNT_EXPOSURE_PERCENT = 0.80

# Any order whose approved USDT notional would fall below this is rejected
# outright (spot-exchange minimum-notional conventions).
MIN_ORDER_VALUE_USDT = 5.0


def get_global_config() -> Dict[str, Any]:
    """Return a fresh dict of the global risk limits.

    Kept behind a factory (like the per-asset configs) so callers cannot
    accidentally share mutable configuration state.
    """
    return {
        "max_account_exposure_percent": MAX_ACCOUNT_EXPOSURE_PERCENT,
        "min_order_value_usdt": MIN_ORDER_VALUE_USDT,
    }