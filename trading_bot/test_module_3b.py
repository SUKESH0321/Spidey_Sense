"""Standalone deterministic verification script for Module 3b (Risk Manager).

The RiskManager sits between the strategy layer and the execution layer. For
every BUY signal it decides how much capital can be committed:

    intended_usdt   = available_usdt * position_size_pct
    max_allowed_usdt = total_portfolio_value * MAX_ACCOUNT_EXPOSURE_PERCENT
                       - (total_portfolio_value - available_usdt)
    cost_usdt       = min(intended_usdt, max_allowed_usdt)
    quantity        = cost_usdt / current_price

Two required scenarios are verified with fully synthetic numbers, plus one
rejection-path demo:

  Test 1 - Standard Approval
     10,000 USDT portfolio, all of it available. BTC position_size_pct = 0.50,
     price = 100. The 80% exposure cap (8,000 USDT) does not bind, so the
     trade is approved for exactly 5,000 USDT -> 50 BTC.

  Test 2 - Exposure Cap / Scale-Down
     10,000 USDT portfolio but 7,000 USDT is already invested in BTC, leaving
     3,000 USDT available. ETH asks for 100% of the free cash
     (position_size_pct = 1.0 -> 3,000 USDT wanted). The 80% cap leaves only
     8,000 - 7,000 = 1,000 USDT headroom, so the order is scaled down and
     approved for exactly 1,000 USDT (10 ETH at price 100).

  Demo - Rejection Paths (bonus)
     Shows both rejects: an exposure cap that is fully consumed (cost <= 0)
     and a positive-but-tiny notional below MIN_ORDER_VALUE_USDT.

Run from the trading_bot directory:  python test_module_3b.py
"""

from __future__ import annotations

import sys
from typing import List

from config.btc_config import get_btc_config
from config.eth_config import get_eth_config
from config.global_config import MAX_ACCOUNT_EXPOSURE_PERCENT, MIN_ORDER_VALUE_USDT
from risk.risk_manager import RiskDecision, RiskManager


def check(condition: bool, message: str, failures: List[str]) -> None:
    """Append ``message`` to ``failures`` when ``condition`` is False."""
    if not condition:
        failures.append(message)


def show_math(
    *,
    label: str,
    current_price: float,
    available_usdt: float,
    total_portfolio_value: float,
    position_size_pct: float,
    exposure_percent: float,
    decision: RiskDecision,
) -> None:
    """Print every sizing step of one ``validate_trade`` call."""
    invested_usdt = total_portfolio_value - available_usdt
    intended_usdt = available_usdt * position_size_pct
    max_allowed_usdt = (
        total_portfolio_value * exposure_percent - invested_usdt
    )
    print(f"  [{label}]")
    print(f"    position_size_pct         = {position_size_pct:.4f}")
    print(f"    current_price             = {current_price:>10.4f} USDT")
    print(f"    available_usdt            = {available_usdt:>10.4f} USDT")
    print(f"    already invested          = {invested_usdt:>10.4f} USDT")
    print(f"    intended_usdt             = {intended_usdt:>10.4f} USDT  (available x size_pct)")
    print(f"    max_allowed_usdt          = {max_allowed_usdt:>10.4f} USDT  (exposure cap - invested)")
    print(f"    final cost_usdt           = {decision.cost_usdt:>10.4f} USDT  (min of the two, post-checks)")
    print(f"    final quantity            = {decision.quantity:>10.6f}  (cost_usdt / price)")
    print(f"    verdict                   = {'APPROVED' if decision.is_approved else 'REJECTED'}")
    print(f"    reason                    = {decision.reason}")
    print()


def run_test_1() -> bool:
    """Test 1: Standard approval - 50% of a fully available 10k portfolio."""
    print("=" * 76)
    print("TEST 1 - Standard Approval: BTC, size_pct=0.50, price=100, 10k/10k")
    print("-" * 76)

    failures: List[str] = []

    total_portfolio_value = 10_000.0
    available_usdt = 10_000.0
    current_price = 100.0

    btc_cfg = get_btc_config()
    position_size_pct = float(btc_cfg["strategy"]["position_size_pct"])

    check(
        position_size_pct == 0.50,
        f"BTC config must ship position_size_pct=0.50, got {position_size_pct}",
        failures,
    )

    manager = RiskManager()
    check(
        manager.max_account_exposure_percent == MAX_ACCOUNT_EXPOSURE_PERCENT == 0.80,
        "RiskManager must default to MAX_ACCOUNT_EXPOSURE_PERCENT=0.80",
        failures,
    )

    decision = manager.validate_trade(
        asset_config=btc_cfg,
        current_price=current_price,
        available_usdt=available_usdt,
        total_portfolio_value=total_portfolio_value,
    )

    show_math(
        label="BTC/USDT BUY request",
        current_price=current_price,
        available_usdt=available_usdt,
        total_portfolio_value=total_portfolio_value,
        position_size_pct=position_size_pct,
        exposure_percent=manager.max_account_exposure_percent,
        decision=decision,
    )

    check(
        decision.is_approved,
        f"Test 1 must approve the trade, got rejection: {decision.reason}",
        failures,
    )
    check(
        decision.cost_usdt == 5_000.0,
        f"expected cost_usdt=5000.0, got {decision.cost_usdt}",
        failures,
    )
    check(
        decision.quantity == 50.0,
        f"expected quantity=50.0 BTC, got {decision.quantity}",
        failures,
    )
    check(
        bool(decision.reason),
        "approved decision must carry a non-empty reason",
        failures,
    )

    ok = not failures
    print(f"  Result: {'PASS' if ok else 'FAIL'}")
    for message in failures:
        print(f"    ! {message}")
    return ok


def run_test_2() -> bool:
    """Test 2: Exposure-cap scale-down - ETH wants 100% of 3k, gets 1k."""
    print("=" * 76)
    print("TEST 2 - Exposure Cap Scale-Down: ETH wants 3k, capped to 1k")
    print("-" * 76)

    failures: List[str] = []

    total_portfolio_value = 10_000.0  # 7,000 already invested in BTC
    available_usdt = 3_000.0
    current_price = 100.0

    eth_cfg = get_eth_config()
    # The shipped config keeps 0.50; this scenario simulates an aggressive
    # asset requesting 100% of the remaining free cash. Mutating the fresh
    # factory dict leaves the canonical config file untouched.
    eth_cfg["strategy"]["position_size_pct"] = 1.0
    position_size_pct = float(eth_cfg["strategy"]["position_size_pct"])

    check(
        MAX_ACCOUNT_EXPOSURE_PERCENT == 0.80,
        "MAX_ACCOUNT_EXPOSURE_PERCENT must be 0.80 in global_config",
        failures,
    )

    manager = RiskManager()
    decision = manager.validate_trade(
        asset_config=eth_cfg,
        current_price=current_price,
        available_usdt=available_usdt,
        total_portfolio_value=total_portfolio_value,
    )

    show_math(
        label="ETH/USDT BUY request",
        current_price=current_price,
        available_usdt=available_usdt,
        total_portfolio_value=total_portfolio_value,
        position_size_pct=position_size_pct,
        exposure_percent=manager.max_account_exposure_percent,
        decision=decision,
    )

    check(
        decision.is_approved,
        f"Test 2 must approve the scaled trade, got rejection: {decision.reason}",
        failures,
    )
    check(
        decision.cost_usdt == 1_000.0,
        f"expected scaled cost_usdt=1000.0, got {decision.cost_usdt}",
        failures,
    )
    check(
        decision.quantity == 10.0,
        f"expected qty=10.0 ETH (1000/100), got {decision.quantity}",
        failures,
    )
    check(
        "scaled" in decision.reason.lower() or "down" in decision.reason.lower(),
        f"reason must explain the scale-down, got {decision.reason!r}",
        failures,
    )

    # Whatever the shipped ETH config is, the factory must always expose the key.
    check(
        "position_size_pct" in get_eth_config()["strategy"],
        "eth_config must define strategy.position_size_pct",
        failures,
    )

    ok = not failures
    print(f"  Result: {'PASS' if ok else 'FAIL'}")
    for message in failures:
        print(f"    ! {message}")
    return ok
def run_rejection_demo() -> bool:
    """Demo both rejection paths: cost<=0 and sub-minimum notional."""
    print("=" * 76)
    print("DEMO - Rejection Paths (bonus): cap consumed / below min order size")
    print("-" * 76)

    failures: List[str] = []
    manager = RiskManager()

    # --- Rejection A: exposure cap fully consumed -> cost_usdt <= 0 ---------
    print("  [A] Exposure cap fully consumed: 9,000 of 10,000 invested,")
    print("      1,000 USDT left, but the 8,000 cap has zero headroom left.")
    print()
    btc_cfg = get_btc_config()
    btc_cfg["strategy"]["position_size_pct"] = 1.0

    decision_a = manager.validate_trade(
        asset_config=btc_cfg,
        current_price=100.0,
        available_usdt=1_000.0,
        total_portfolio_value=10_000.0,
    )
    show_math(
        label="BTC/USDT BUY request (already 90% invested)",
        current_price=100.0,
        available_usdt=1_000.0,
        total_portfolio_value=10_000.0,
        position_size_pct=1.0,
        exposure_percent=manager.max_account_exposure_percent,
        decision=decision_a,
    )
    check(
        decision_a.is_approved is False,
        "cap-consumed case must be rejected",
        failures,
    )
    check(
        decision_a.cost_usdt == 0.0 and decision_a.quantity == 0.0,
        "rejected decisions must carry zero cost and quantity",
        failures,
    )

    # --- Rejection B: notional below the minimum order size ----------------
    print()
    print("  [B] Sub-minimum notional: RiskManager(min_order_value_usdt=100),")
    print("      a 0.5% position on 10,000 makes only 50 USDT < 100 USDT min.")
    print()
    tight_manager = RiskManager(min_order_value_usdt=100.0)
    btc_cfg["strategy"]["position_size_pct"] = 0.005

    decision_b = tight_manager.validate_trade(
        asset_config=btc_cfg,
        current_price=100.0,
        available_usdt=10_000.0,
        total_portfolio_value=10_000.0,
    )
    show_math(
        label="BTC/USDT BUY request (tiny 0.5% position)",
        current_price=100.0,
        available_usdt=10_000.0,
        total_portfolio_value=10_000.0,
        position_size_pct=0.005,
        exposure_percent=tight_manager.max_account_exposure_percent,
        decision=decision_b,
    )
    check(
        decision_b.is_approved is False,
        "sub-minimum notional case must be rejected",
        failures,
    )
    check(
        "minimum" in decision_b.reason.lower(),
        f"reason must mention the minimum order size, got {decision_b.reason!r}",
        failures,
    )
    check(
        manager.min_order_value_usdt == MIN_ORDER_VALUE_USDT == 5.0,
        "RiskManager default minimum order size must come from global_config",
        failures,
    )

    ok = not failures
    print(f"  Result: {'PASS' if ok else 'FAIL'}")
    for message in failures:
        print(f"    ! {message}")
    return ok


def main() -> int:
    """Run all Module 3b checks and print a summary."""
    results = [
        ("1: Standard Approval (5,000 USDT / 50 BTC)", run_test_1()),
        ("2: Exposure Cap Scale-Down (1,000 USDT / 10 ETH)", run_test_2()),
        ("3: Rejection Paths (cap consumed / min order)", run_rejection_demo()),
    ]

    print("=" * 76)
    all_ok = True
    for name, ok in results:
        print(f"{name:<52} {'PASS' if ok else 'FAIL'}")
        all_ok = all_ok and ok
    print("=" * 76)
    print("OVERALL RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())