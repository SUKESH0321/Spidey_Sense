"""Module 6: Live Paper Trading Engine verification suite.

Drives the full Module 6 pipeline -- strategy -> RiskManager -> PaperExecutor
-> SQLite ledger -- without touching the network:

  1. opens a temporary SQLite ledger (``test_paper.db`` in a temp dir);
  2. simulates 5 mock live candle intervals for BTC/USDT and ETH/USDT in rapid
     succession (both first need a one-candle warm-up to seed the reference
     price, exactly like a freshly started live session);
  3. both strategies issue a BUY on interval 5 and a stop-loss SELL on the
     following candle;
  4. reads the ledger back and asserts trades + bot_state were written and
     persisted across a close -> reconnect cycle.

Also verifies the P&L accounting identity end-to-end:

    net_pnl == (sell_fill - buy_fill) * qty - entry_fee - exit_fee

Run from the trading_bot directory:

    python test_module_6.py
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from database.database import Database
from execution.paper_executor import PaperExecutionError, PaperExecutor
from main import (
    INITIAL_USDT,
    PaperTradingOrchestrator,
    TRADING_MODE,
    FEE_PCT,
    SLIPPAGE_PCT,
)

# ----------------------------------------------------------------------
# Deterministic candle data - tuple order: (open, high, low, close)
# ----------------------------------------------------------------------
BTC_WARM = (100.00, 100.50, 99.50, 100.00)
BTC_INTERVALS: List[Tuple[float, float, float, float]] = [
    (100.00, 100.00, 95.60, 97.00),    # 1: -3% -> DOWNTREND
    (97.00, 97.00, 91.80, 92.30),      # 2: deeper low 91.80
    (96.00, 96.00, 91.80, 94.80),      # 3: rebound >=3% -> READY_TO_BUY (red)
    (95.50, 95.50, 94.60, 94.80),      # 4: READY_TO_BUY (red, no confirm)
    (94.00, 96.80, 93.80, 96.00),      # 5: green confirmation -> BUY @ 96.00
]
BTC_EXIT = (92.00, 92.00, 90.00, 91.00)   # close 91 <= 96*0.95 -> STOP_LOSS
BTC_COOLDOWN: List[Tuple[float, float, float, float]] = [
    (91.50, 92.00, 91.00, 91.80),
    (92.00, 92.50, 91.50, 92.20),
    (92.20, 92.80, 92.00, 92.60),
    (92.60, 93.00, 92.30, 92.80),          # cooldown elapsed -> WAITING
]

ETH_WARM = (20.00, 20.10, 19.90, 20.00)
ETH_INTERVALS: List[Tuple[float, float, float, float]] = [
    (20.00, 20.00, 19.20, 19.40),      # 1: -3% -> DOWNTREND
    (19.40, 19.40, 18.60, 18.70),      # 2: deeper low 18.60
    (19.30, 19.30, 18.60, 19.20),      # 3: rebound >=3% -> READY_TO_BUY (red)
    (19.30, 19.35, 19.15, 19.25),      # 4: READY_TO_BUY (red, rebound held >=3%)
    (19.00, 19.40, 18.90, 19.30),      # 5: green confirmation -> BUY @ 19.30
]
ETH_EXIT = (18.40, 18.40, 18.00, 18.10)   # close 18.10 <= 19.2*0.95 -> STOP_LOSS
ETH_COOLDOWN: List[Tuple[float, float, float, float]] = [
    (18.30, 18.60, 18.00, 18.40),
    (18.50, 18.80, 18.30, 18.60),
    (18.70, 19.00, 18.50, 18.90),
    (18.90, 19.10, 18.80, 19.00),          # cooldown elapsed -> WAITING
]
def candle_for(
    symbol: str,
    ohlc: Tuple[float, float, float, float],
    minute_offset: int,
    volume: float = 1000.0,
) -> Dict[str, Any]:
    """Build a completed-candle dict for the orchestrator to consume."""
    ts = (
        f"2026-08-23T{10 + minute_offset // 60:02d}:"
        f"{minute_offset % 60:02d}:00+00:00"
    )
    opening, high, low, close = ohlc
    return {
        "symbol": symbol,
        "timestamp": ts,
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def check(condition: bool, message: str, failures: List[str]) -> None:
    """Record a failed assertion with its explanation."""
    if not condition:
        failures.append(message)


# ----------------------------------------------------------------------
# PaperExecutor unit sanity checks
# ----------------------------------------------------------------------
def run_executor_sanity() -> List[str]:
    """Direct unit checks on PaperExecutor P&L maths and funds guards."""
    failures: List[str] = []
    ledger = Database(":memory:")
    try:
        ex = PaperExecutor(ledger, initial_usdt=1000.0)

        buy = ex.execute_buy(
            "BTC/USDT", 100.0, 1.0, FEE_PCT, SLIPPAGE_PCT, "unit"
        )
        check(
            math.isclose(buy.execution_price, 100.05, abs_tol=1e-12),
            "BUY execution price must include positive slippage",
            failures,
        )
        check(
            math.isclose(buy.notional_usdt, 100.05, abs_tol=1e-9)
            and math.isclose(buy.fee, 0.10005, abs_tol=1e-9),
            "BUY notional/fee maths mismatch",
            failures,
        )
        check(
            math.isclose(ex.usdt_balance, 899.84995, abs_tol=1e-9),
            "BUY must deduct cost + fee from virtual USDT",
            failures,
        )
        check(
            math.isclose(ex.btc_balance, 1.0, abs_tol=1e-12),
            "BUY must credit the BTC balance",
            failures,
        )

        sell = ex.execute_sell(
            "BTC/USDT", 110.0, 1.0, FEE_PCT, SLIPPAGE_PCT, "unit",
            entry_price=buy.execution_price,
        )
        check(
            math.isclose(sell.execution_price, 109.945, abs_tol=1e-12),
            "SELL execution price must include adverse slippage",
            failures,
        )
        gross = (sell.execution_price - buy.execution_price) * 1.0
        net_expected = gross - buy.fee - sell.fee
        check(
            sell.net_pnl is not None
            and math.isclose(sell.net_pnl, net_expected, abs_tol=1e-9),
            f"SELL net_pnl identity mismatch: {sell.net_pnl} vs {net_expected}",
            failures,
        )
        check(
            math.isclose(ex.btc_balance, 0.0, abs_tol=1e-12),
            "SELL must clear the asset balance",
            failures,
        )
        check(
            math.isclose(ex.usdt_balance, 1000.0 + net_expected, abs_tol=1e-9),
            "USDT balance must drift exactly by realized net P&L",
            failures,
        )

        # An over-budget BUY must raise instead of corrupting the account.
        try:
            ex.execute_buy("ETH/USDT", 3000.0, 1.0, FEE_PCT, SLIPPAGE_PCT, "unit")
            failures.append("over-budget BUY must raise PaperExecutionError")
        except PaperExecutionError:
            pass
    finally:
        ledger.close()
    return failures
# ----------------------------------------------------------------------
# Full Module 6 integration suite
# ----------------------------------------------------------------------
def run_suite() -> bool:
    """Execute every Module 6 check and return True on full pass."""
    failures: List[str] = []

    print("=" * 78)
    print("MODULE 6: LIVE PAPER TRADING ENGINE - VERIFICATION")
    print("=" * 78)

    # ---- 0) static mode guard -------------------------------------------
    check(
        TRADING_MODE == "PAPER",
        "TRADING_MODE must be hard-coded to 'PAPER'",
        failures,
    )

    # ---- 1) temporary SQLite ledger --------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="spidey_module6_")
    db_path = os.path.join(tmp_dir, "test_paper.db")
    database = Database(db_path)
    print(f"\n[1] Temporary SQLite ledger opened: {db_path}")

    try:
        # ---- 2) PaperExecutor unit sanity --------------------------------
        print("[2] PaperExecutor unit sanity (P&L identity + funds guards)")
        failures.extend(run_executor_sanity())

        # ---- 3) orchestrator wiring --------------------------------------
        print("[3] Orchestrator wired: BTCStrategy + ETHStrategy + RiskManager")
        orch = PaperTradingOrchestrator(
            database=database,
            clear_screen=False,
            auto_render=False,
        )
        check(
            math.isclose(orch.executor.usdt_balance, INITIAL_USDT, abs_tol=1e-9),
            f"executor must start with {INITIAL_USDT:,.2f} virtual USDT",
            failures,
        )

        def feed_round(offset: int, btc_ohlc: tuple, eth_ohlc: tuple) -> None:
            """One live interval: BTC candle first, then ETH (rapid succession)."""
            orch.process_candle(candle_for("BTC/USDT", btc_ohlc, offset))
            orch.process_candle(candle_for("ETH/USDT", eth_ohlc, offset))

        # ---- 4) warm-up + 5 mock live candle intervals --------------------
        print("[4] Feeding warm-up candle + 5 mock live intervals per asset")
        feed_round(0, BTC_WARM, ETH_WARM)          # seeds reference prices
        for index in range(5):
            feed_round(index + 1, BTC_INTERVALS[index], ETH_INTERVALS[index])

        # ---- 5) BUY assertions --------------------------------------------
        rows = database.get_recent_trades(10)
        buy_rows = [row for row in rows if row["side"] == "BUY"]
        check(
            len(rows) == 2 and len(buy_rows) == 2,
            f"interval 5 must produce exactly 2 BUY fills, got {len(buy_rows)}",
            failures,
        )

        btc_buy = next(
            (r for r in rows if r["symbol"] == "BTC/USDT"), None
        )
        if btc_buy is not None:
            expected_qty = 5000.0 / 96.0   # 50% of 10k at the market close
            check(
                math.isclose(btc_buy["quantity"], expected_qty, abs_tol=1e-6),
                f"BTC BUY quantity must be risk-sized to {expected_qty:.6f}",
                failures,
            )
            check(
                math.isclose(btc_buy["price"], 96.048, abs_tol=1e-9),
                "BTC BUY fill price must include slippage (96 * 1.0005)",
                failures,
            )
            check(
                math.isclose(btc_buy["cost_usdt"], 5002.50, abs_tol=1e-6)
                and math.isclose(btc_buy["fee"], 5.0025, abs_tol=1e-9),
                "BTC BUY cost/fee maths mismatch",
                failures,
            )
            check(
                btc_buy["net_pnl"] is None,
                "BUY rows must not carry a net_pnl",
                failures,
            )
        else:
            failures.append("missing BTC BUY fill after interval 5")

        eth_buy = next((r for r in rows if r["symbol"] == "ETH/USDT"), None)
        check(
            eth_buy is not None,
            "missing ETH BUY fill after interval 5",
            failures,
        )
        check(
            orch.executor.usdt_balance < INITIAL_USDT,
            "virtual USDT must decrease after both BUYs",
            failures,
        )

        # ---- 6) exit candle -> mock SELL (stop-loss) ----------------------
        print("[6] Exit candle: stop-loss SELL for both open positions")
        feed_round(6, BTC_EXIT, ETH_EXIT)

        rows = database.get_recent_trades(10)
        check(
            len(rows) == 4,
            f"ledger must hold 4 fills after the exit round, got {len(rows)}",
            failures,
        )
        sell_rows = [row for row in rows if row["side"] == "SELL"]
        check(
            len(sell_rows) == 2,
            f"exit round must produce exactly 2 SELL fills, got {len(sell_rows)}",
            failures,
        )

        net_total = 0.0
        for sell_row in sell_rows:
            symbol = sell_row["symbol"]
            buy_row = next(
                (r for r in rows if r["symbol"] == symbol and r["side"] == "BUY"),
                None,
            )
            if buy_row is None:
                failures.append(f"{symbol} has a SELL without a matching BUY")
                continue
            expected_net = (
                (sell_row["price"] - buy_row["price"]) * sell_row["quantity"]
                - buy_row["fee"]
                - sell_row["fee"]
            )
            check(
                sell_row["net_pnl"] is not None
                and math.isclose(sell_row["net_pnl"], expected_net, abs_tol=1e-6),
                f"{symbol} net_pnl identity mismatch: "
                f"{sell_row['net_pnl']} vs {expected_net}",
                failures,
            )
            check(
                sell_row["reason"] == "STOP_LOSS",
                f"{symbol} SELL reason must be STOP_LOSS, got {sell_row['reason']}",
                failures,
            )
            if sell_row["net_pnl"] is not None:
                net_total += sell_row["net_pnl"]

        check(
            math.isclose(
                orch.executor.usdt_balance,
                INITIAL_USDT + net_total,
                abs_tol=1e-6,
            ),
            "USDT balance must equal initial capital + total realized net P&L",
            failures,
        )
        check(
            math.isclose(orch.executor.btc_balance, 0.0, abs_tol=1e-12)
            and math.isclose(orch.executor.eth_balance, 0.0, abs_tol=1e-12),
            "SELL must clear BTC and ETH balances",
            failures,
        )

        # ---- 7) bot_state snapshots ---------------------------------------
        print("[7] bot_state snapshots persisted per asset")
        btc_state = database.load_bot_state("BTC/USDT")
        eth_state = database.load_bot_state("ETH/USDT")
        check(btc_state is not None, "bot_state row missing for BTC/USDT", failures)
        check(eth_state is not None, "bot_state row missing for ETH/USDT", failures)
        if btc_state is not None:
            check(
                btc_state["current_state"] == "COOLDOWN",
                f"BTC state after SELL must be COOLDOWN, "
                f"got {btc_state['current_state']}",
                failures,
            )
            check(
                btc_state["entry_price"] is None,
                "entry_price must be cleared in bot_state after the SELL",
                failures,
            )

        # ---- 8) cooldown candles return the machine to WAITING ------------
        print("[8] Cooldown candles: machines must reset to WAITING")
        for index in range(4):
            feed_round(7 + index, BTC_COOLDOWN[index], ETH_COOLDOWN[index])
        for label in ("BTC", "ETH"):
            state = database.load_bot_state(f"{label}/USDT")
            if state is not None:
                check(
                    state["current_state"] == "WAITING",
                    f"{label} must be WAITING after cooldown, "
                    f"got {state['current_state']}",
                    failures,
                )
            else:
                failures.append(f"bot_state row missing for {label}/USDT")

        # ---- 9) live terminal dashboard renders ---------------------------
        print("[9] Rendering the terminal dashboard once (clear_screen off)")
        orch.render_dashboard()

        # ---- 10) persistence across close/reopen --------------------------
        print("[10] Closing + reopening the ledger (persistence check)")
        database.close()
        reopened = Database(db_path)
        recent = reopened.get_recent_trades(10)
        check(
            len(recent) == 4,
            f"all 4 trades must persist across close/reopen, got {len(recent)}",
            failures,
        )
        check(
            len(recent) > 0
            and recent[0]["side"] == "SELL"
            and recent[0]["symbol"] == "ETH/USDT",
            "get_recent_trades must be newest-first (ETH SELL last executed)",
            failures,
        )
        persisted_state = reopened.load_bot_state("BTC/USDT")
        check(
            persisted_state is not None
            and persisted_state["current_state"] == "WAITING",
            "bot_state must persist across close/reopen",
            failures,
        )

        # ---- 11) crash-recovery: a fresh orchestrator resumes cleanly -----
        recovered = PaperTradingOrchestrator(
            database=reopened,
            clear_screen=False,
            auto_render=False,
        )
        check(
            recovered.strategies["BTC"].get_state().value == "WAITING"
            and not recovered.strategies["BTC"].position_open,
            "a restarted orchestrator must recover WAITING from the ledger",
            failures,
        )

        # ---- report --------------------------------------------------------
        print()
        print("  Ledger contents (chronological):")
        for row in reversed(recent):
            pnl = row["net_pnl"]
            pnl_txt = f"{pnl:+12,.4f}" if pnl is not None else "         n/a"
            print(
                f"    #{row['id']} [{row['timestamp']}] {row['symbol']:<9}"
                f"{row['side']:<4} @ {row['price']:>12,.4f} | "
                f"qty {row['quantity']:>14,.6f} | net {pnl_txt}"
            )
        print()
        print(f"  Final virtual USDT: {orch.executor.usdt_balance:,.4f}")
        print(f"  Total realized net P&L: {net_total:+,.4f} USDT")
        reopened.close()
    finally:
        try:
            database.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    ok = not failures
    print("=" * 78)
    print("OVERALL RESULT:", "PASS" if ok else "FAIL")
    for message in failures:
        print("  !", message)
    print("=" * 78)
    return ok


def main() -> int:
    """Run the Module 6 suite and exit with PASS/FAIL status."""
    return 0 if run_suite() else 1


if __name__ == "__main__":
    sys.exit(main())