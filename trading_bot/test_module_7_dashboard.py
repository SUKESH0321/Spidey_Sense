"""Module 7 verification: web dashboard server + ledger-replay engine.

Runs fully offline against a temporary SQLite ledger:

  1. drives :class:`PaperExecutor` through one BUY + one SELL with hand-
     computable fees/slippage, then snapshots ``bot_state``;
  2. boots the Flask dashboard via the Werkzeug test client (no port) and
     asserts ``GET /``, ``GET /api/status``, static assets and the
     PAUSE / EMERGENCY-SHUTDOWN control endpoints;
  3. verifies the replay identity: dashboard balances equal the executor's
     own balances to the cent, and P&L / win-rate match the ledger math;
  4. checks ``seed_demo_data`` populates an empty ledger exactly once.

Run from the trading_bot directory:

    python test_module_7_dashboard.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from database.database import Database
from execution.paper_executor import PaperExecutor

from dashboard.app import BotRuntime, create_app, seed_demo_data

_INITIAL_USDT = 10_000.0
_FEE = 0.001
_SLIP = 0.0005

_results = []


def check(cond, label):
    _results.append((bool(cond), label))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def approx(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


def build_ledger(db_path):
    """One deterministic round-trip; returns (db, executor, expected_net).

    Math (fee 0.1%, slippage 0.05%):
      BUY  fill = 60000 * 1.0005 = 60030.00 ; notional 3001.50 ; fee 3.0015
      SELL fill = 61800 * 0.9995 = 61769.10 ; proceeds 3088.455; fee 3.088455
      net_pnl   = (61769.10 - 60030.00) * 0.05 - 3.0015 - 3.088455
    """
    db = Database(db_path)
    ex = PaperExecutor(db, _INITIAL_USDT)

    buy = ex.execute_buy(
        "BTC/USDT", 60_000.0, 0.05, _FEE, _SLIP, "UNIT_TEST_ENTRY"
    )
    sell = ex.execute_sell(
        "BTC/USDT", 61_800.0, 0.05, _FEE, _SLIP,
        "UNIT_TEST_EXIT_REVERSAL", entry_price=buy.execution_price,
    )
    expected_net = (
        (sell.execution_price - buy.execution_price) * 0.05
        - buy.fee - sell.fee
    )
    db.save_bot_state(
        "BTC/USDT", current_state="WAITING",
        entry_price=None, highest_price=None, lowest_price=None,
    )
    return db, ex, expected_net

def main():
    """Drive every Module 7 check offline; returns 0 on success."""
    tmp = tempfile.mkdtemp(prefix="spidey_module7_")
    print(f"\n[1] temporary ledger opened: {tmp}")
    try:
        db_path = str(Path(tmp) / "paper.db")
        db, ex, expected_net = build_ledger(db_path)

        runtime = BotRuntime(
            db_path, initial_usdt=_INITIAL_USDT, enable_price_feed=False
        )
        client = create_app(runtime).test_client()

        print("\n[2] GET /api/status")
        r = client.get("/api/status")
        check(r.status_code == 200, "status endpoint answers 200")
        data = r.get_json()
        p = data["portfolio"]

        print("\n[3] replay identity vs executor")
        check(approx(p["available_usdt"], ex.usdt_balance),
              f"USDT replay {p['available_usdt']:.4f} == executor "
              f"{ex.usdt_balance:.4f}")
        check(approx(data["assets"]["BTC"]["balance_qty"], ex.btc_balance),
              "BTC quantity matches executor book")
        check(approx(p["realized_pnl"], expected_net, 0.01),
              f"realized P&L == {expected_net:+.4f} (fee/slippage exact)")
        check(approx(p["net_pnl"], p["realized_pnl"], 0.001),
              "flat book -> net P&L == realized P&L")
        check(approx(p["unrealized_pnl"], 0.0, 0.001),
              "no open position -> zero unrealized P&L")
        check(p["total_trades"] == 2, "two ledger fills counted")
        check(p["wins"] == 1 and p["losses"] == 0,
              "one winning exit recorded")
        check(approx(p["win_rate_percent"], 100.0, 0.01),
              "win rate == 100%")
        check(approx(p["net_pnl_percent"],
                     expected_net / _INITIAL_USDT * 100.0, 0.001),
              "net P&L percent consistent")

        print("\n[4] asset payloads & gauges")
        btc = data["assets"]["BTC"]
        eth = data["assets"]["ETH"]
        check(btc["state"] == "WAITING",
              "BTC state restored from bot_state row")
        check(eth["state"] == "WAITING", "ETH defaults to WAITING")
        check(btc["price"] is not None,
              "BTC falls back to last ledger price when feed disabled")
        check(btc["price"] is not None and eth["price"] is None,
              "ETH has no price source yet (never traded)")
        check(btc["progress"]["kind"] == "drop",
              "WAITING gauge tracks trend drop")
        check(len(data["recent_trades"]) == 2,
              "recent_trades carries both fills")
        check(data["recent_trades"][0]["side"] == "SELL",
              "newest trade first")
        check(data["system_status"] == "ONLINE", "system ONLINE at boot")

        print("\n[5] emergency control plane")
        pr = client.post("/api/control/pause")
        check(pr.status_code == 200 and pr.get_json()["paused"] is True,
              "PAUSE toggles on")
        check(client.get("/api/status").get_json()["system_status"] == "PAUSED",
              "status reflects PAUSED")
        pr2 = client.post("/api/control/pause", json={"paused": False})
        check(pr2.get_json()["paused"] is False,
              "explicit resume body honoured")
        sd = client.post("/api/control/shutdown")
        check(sd.status_code == 200 and sd.get_json()["shutdown"] is True,
              "EMERGENCY SHUTDOWN latches")
        after = client.get("/api/status").get_json()
        check(after["system_status"] == "SHUTDOWN",
              "status reflects SHUTDOWN")

        print("\n[6] page + static assets")
        page = client.get("/")
        check(page.status_code == 200, "index renders 200")
        check(b"SPIDEY" in page.data.upper(), "brand present in HTML")
        check(client.get("/static/css/dashboard.css").status_code == 200,
              "dashboard.css served")
        check(client.get("/static/js/dashboard.js").status_code == 200,
              "dashboard.js served")
        db.close()
        runtime.close()

        print("\n[7] demo seeding")
        demo_path = str(Path(tmp) / "demo.db")
        demo_rt = BotRuntime(demo_path, enable_price_feed=False)
        n = seed_demo_data(demo_rt.ledger())
        check(n >= 6, f"seeded {n} sample fills into empty ledger")
        check(seed_demo_data(demo_rt.ledger()) == 0,
              "second seed call is a no-op on a non-empty ledger")
        dp = create_app(demo_rt).test_client() \
                                .get("/api/status").get_json()["portfolio"]
        check(dp["total_trades"] == n, "demo fills visible via /api/status")
        check(dp["wins"] == 2 and dp["losses"] == 1,
              "demo win/loss tally correct (2W / 1L)")
        demo_rt.close()

        failed = [lbl for ok, lbl in _results if not ok]
        print("\n" + "=" * 62)
        print(f" MODULE 7 RESULT: {len(_results) - len(failed)}/{len(_results)}"
              " checks passed")
        for lbl in failed:
            print(f"   FAILED -> {lbl}")
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())