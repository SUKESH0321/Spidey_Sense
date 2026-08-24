"""Module 6: Live Paper Trading Orchestrator + Terminal Dashboard.

This is the operational layer of the spot trading bot. It connects the
strategy, risk and config modules to a live Binance public market-data stream
and executes every signal against a *virtual* ledger persisted to local
SQLite:

    live candles  ->  strategy state machine  ->  RiskManager
                   ->  PaperExecutor  ->  SQLite ledger  ->  dashboard

Real exchange execution is deliberately disabled. The module-level
``TRADING_MODE`` constant is hard-coded to ``"PAPER"`` and :func:`main` refuses
to start when it is anything else. There is no path in this module that can
place a real order on Binance.

Shutdown is graceful: a Ctrl+C (``KeyboardInterrupt``) flushes the final
per-asset strategy snapshots to the ledger and closes the SQLite connection
cleanly, so the paper account is never corrupted or half-written.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from data.live_stream import LiveCandleStream
from database.database import Database, utc_now_iso
from execution.paper_executor import PaperExecutor
from risk.risk_manager import RiskManager
from strategy.base_strategy import BaseStrategy, SignalType, TradingState
from strategy.btc_strategy import BTCStrategy
from strategy.eth_strategy import ETHStrategy

logger = logging.getLogger("spidey.paper")

# ----------------------------------------------------------------------
# Hard-coded operating mode. Real execution is OFF. Do not change this while
# live trading is not wired up - the bot may only ever place PAPER trades.
# ----------------------------------------------------------------------
TRADING_MODE = "PAPER"

INITIAL_USDT = 10_000.0
FEE_PCT = 0.001         # 0.1% bilateral trading fee
SLIPPAGE_PCT = 0.0005   # 0.05% adverse fill spread
DB_PATH = "paper_trading.db"
DASH_WIDTH = 80

_ASSET_LABELS = ("BTC", "ETH")


class PaperTradingOrchestrator:
    """Candle -> signal -> risk -> paper-fill -> SQLite -> dashboard pipeline.

    Args:
        database: An already-open :class:`Database`. When omitted a new ledger
            is opened at ``db_path``.
        db_path: SQLite file used when ``database`` is not supplied.
        initial_usdt: Starting virtual USDT capital for :class:`PaperExecutor`.
        fee_pct: Bilateral trading fee fraction (e.g. ``0.001``).
        slippage_pct: Adverse fill spread fraction (e.g. ``0.0005``).
        strategies: ``{label: BaseStrategy}`` map. Defaults to fresh
            BTC/USDT and ETH/USDT strategies from ``config/``.
        clear_screen: Clear the console before each dashboard redraw.
        auto_render: Redraw the dashboard after every processed candle.
    """

    def __init__(
        self,
        *,
        database: Optional[Database] = None,
        db_path: str = DB_PATH,
        initial_usdt: float = INITIAL_USDT,
        fee_pct: float = FEE_PCT,
        slippage_pct: float = SLIPPAGE_PCT,
        strategies: Optional[Dict[str, BaseStrategy]] = None,
        clear_screen: bool = True,
        auto_render: bool = True,
    ) -> None:
        if not 0.0 <= fee_pct < 1.0 or not 0.0 <= slippage_pct < 1.0:
            raise ValueError("fee_pct and slippage_pct must be within [0, 1)")

        self.database = database if database is not None else Database(db_path)
        self.executor = PaperExecutor(self.database, initial_usdt)
        self.risk_manager = RiskManager()
        self.strategies: Dict[str, BaseStrategy] = (
            dict(strategies) if strategies is not None
            else {"BTC": BTCStrategy(), "ETH": ETHStrategy()}
        )
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.latest_prices: Dict[str, float] = {}
        self.clear_screen = bool(clear_screen)
        self.auto_render = bool(auto_render)
        self._shutdown_requested = False

        # Best-effort crash recovery from the persisted ledger.
        for label, strategy in self.strategies.items():
            self._restore_state(label, strategy)

    # ------------------------------------------------------------------
    # State restore (crash-resume)
    # ------------------------------------------------------------------
    def _restore_state(self, label: str, strategy: BaseStrategy) -> None:
        """Rehydrate a strategy's machine from the persisted ``bot_state`` row."""
        row = self.database.load_bot_state(f"{label}/USDT")
        if row is None:
            return
        state_map = {state.value: state for state in TradingState}
        target = state_map.get(str(row["current_state"]))
        if target is None:
            return
        strategy.current_state = target
        strategy.entry_price = row.get("entry_price")
        strategy.highest_price = row.get("highest_price")
        strategy.lowest_price = row.get("lowest_price")
        # A persisted entry price means a position was open when the prior run
        # stopped; honour that so a SELL is still possible after a restart.
        strategy.position_open = strategy.entry_price is not None
        logger.info(
            "[%s] recovered strategy state from ledger -> %s",
            label, row["current_state"],
        )
# ------------------------------------------------------------------
    # Candle pipeline
    # ------------------------------------------------------------------
    def process_candle(self, candle: Dict[str, Any]) -> None:
        """Process ONE completed OHLCV candle (symbol + open/high/low/close).

        Feeds the candle to the owning strategy, executes BUY/SELL signals
        through the paper executor, and persists the post-candle bot state.
        """
        symbol = str(candle["symbol"])
        label = symbol.split("/", 1)[0]
        strategy = self.strategies.get(label)
        if strategy is None:
            raise ValueError(f"no strategy registered for asset {symbol!r}")

        try:
            close = float(candle["close"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candle for {symbol} has no numeric close") from exc

        self.latest_prices[label] = close
        was_open = strategy.position_open
        timestamp = candle.get("timestamp")

        signal = strategy.process_candle(candle)

        if signal is SignalType.BUY and not was_open:
            self._handle_buy(strategy, symbol, label, close, timestamp)
        elif signal is SignalType.SELL and was_open:
            self._handle_sell(strategy, symbol, label, close, timestamp)
        else:
            logger.info("[%s] candle close=%.2f -> %s", label, close, signal.value)

        self._save_bot_state(label, strategy)
        if self.auto_render:
            self.render_dashboard()

    def _handle_buy(
        self,
        strategy: BaseStrategy,
        symbol: str,
        label: str,
        price: float,
        timestamp: Optional[str],
    ) -> None:
        """Size the BUY through the RiskManager and fill it virtually."""
        decision = self.risk_manager.validate_trade(
            asset_config=strategy.config,
            current_price=price,
            available_usdt=self.executor.usdt_balance,
            total_portfolio_value=self.executor.portfolio_value(self.latest_prices),
        )
        if not (decision.is_approved and decision.quantity > 0.0):
            logger.warning(
                "[%s] BUY signal REJECTED by RiskManager: %s",
                label, decision.reason,
            )
            return

        fill = self.executor.execute_buy(
            symbol,
            price,
            decision.quantity,
            self.fee_pct,
            self.slippage_pct,
            decision.reason,
            timestamp=timestamp,
        )
        logger.info(
            "[%s] PAPER BUY executed: qty=%.6f @ fill=%.6f | "
            "notional=%.2f USDT | available=%.2f",
            label, fill.quantity, fill.execution_price,
            fill.notional_usdt, fill.usdt_balance_after,
        )

    def _handle_sell(
        self,
        strategy: BaseStrategy,
        symbol: str,
        label: str,
        price: float,
        timestamp: Optional[str],
    ) -> None:
        """Flatten the open paper position and realise P&L."""
        position = self.executor.get_position(symbol)
        quantity = self.executor.get_asset_balance(symbol)
        if position is None or quantity <= 1e-12:
            logger.warning(
                "[%s] SELL signal but no open paper position; skipping fill",
                label,
            )
            return

        reason = (
            strategy.last_exit_reason.value
            if strategy.last_exit_reason is not None
            else "REVERSAL"
        )
        fill = self.executor.execute_sell(
            symbol,
            price,
            quantity,
            self.fee_pct,
            self.slippage_pct,
            reason,
            entry_price=position.entry_price,
            timestamp=timestamp,
        )
        logger.info(
            "[%s] PAPER SELL (%s): qty=%.6f @ fill=%.6f | Net P&L %+.4f USDT",
            label, reason, fill.quantity, fill.execution_price,
            fill.net_pnl if fill.net_pnl is not None else 0.0,
        )

    def _save_bot_state(self, label: str, strategy: BaseStrategy) -> None:
        """Persist the strategy snapshot plus the actual entry fill price."""
        snap = strategy.get_snapshot()
        position = self.executor.get_position(f"{label}/USDT")
        entry = position.entry_price if position is not None else snap["entry_price"]
        self.database.save_bot_state(
            symbol=f"{label}/USDT",
            current_state=snap["current_state"],
            entry_price=entry,
            highest_price=snap["highest_price"],
            lowest_price=snap["lowest_price"],
            last_updated=utc_now_iso(),
        )

    # ------------------------------------------------------------------
    # Streaming entry point
    # ------------------------------------------------------------------
    def run_streaming(self) -> None:
        """Start the live candle stream and process candles until Ctrl+C."""
        symbols = tuple(strategy.symbol for strategy in self.strategies.values())
        timeframe = next(iter(self.strategies.values())).timeframe

        stream = LiveCandleStream(symbols=symbols, timeframe=timeframe)
        logger.info(
            "Starting %s mode for %s on %s timeframe - press Ctrl+C to stop.",
            TRADING_MODE, ", ".join(symbols), timeframe,
        )
        self.render_dashboard()
        stream.run(self.process_candle)

    # ------------------------------------------------------------------
    # Terminal dashboard
    # ------------------------------------------------------------------
    def render_dashboard(self) -> None:
        """Render the ASCII status dashboard shown in the module spec."""
        width = DASH_WIDTH
        lines: list[str] = []
        lines.append("=" * width)
        lines.append("BINANCE SPOT BOT - PAPER TRADING DASHBOARD".center(width))
        lines.append("=" * width)

        portfolio = self.executor.portfolio_value(self.latest_prices)
        lines.append(
            f"Mode: {TRADING_MODE} | Portfolio Value: ${portfolio:,.2f} | "
            f"Available USDT: ${self.executor.usdt_balance:,.2f}"
        )
        lines.append("-" * width)

        for label in _ASSET_LABELS:
            symbol = f"{label}/USDT"
            strategy = self.strategies.get(label)
            if strategy is None:
                continue
            snap = strategy.get_snapshot()
            price = self.latest_prices.get(label)
            price_txt = f"${price:,.2f}" if price is not None else "---"
            state = snap["current_state"]

            if snap["position_open"] is True and snap["entry_price"] is not None:
                entry = float(snap["entry_price"])
                pnl_pct = (
                    (price - entry) / entry * 100.0
                    if price is not None and entry > 0.0
                    else 0.0
                )
                lines.append(
                    f"{symbol}: Price: {price_txt:<14} | State: {state:<12} | "
                    f"Entry: ${entry:,.2f} | P&L: {pnl_pct:+.2f}%"
                )
            else:
                extreme = snap["lowest_price"]
                if extreme is None:
                    extreme = snap["highest_price"]
                if extreme is None:
                    extreme = snap["reference_price"]
                extreme_txt = f"${extreme:,.2f}" if extreme is not None else "N/A"
                signal = snap["last_signal"]
                lines.append(
                    f"{symbol}: Price: {price_txt:<14} | State: {state:<12} | "
                    f"Extreme: {extreme_txt:<12} | Signal: {signal}"
                )

        lines.append("-" * width)
        lines.append("Recent Trades (Last 3):")
        rows = self.database.get_recent_trades(3)
        if rows:
            for trade in rows:
                ts = str(trade["timestamp"])[:16].replace("T", " ")
                side = trade["side"]
                qty = trade["quantity"]
                if side == "BUY":
                    lines.append(
                        f"- [{ts}] {trade['symbol']} BUY  @ ${trade['price']:,.2f} "
                        f"| Qty: {qty:,.3f} | Cost: ${trade['cost_usdt']:,.2f}"
                    )
                else:
                    net_pnl = trade["net_pnl"]
                    pnl_txt = f"{net_pnl:+,.2f}" if net_pnl is not None else "n/a"
                    lines.append(
                        f"- [{ts}] {trade['symbol']} SELL @ ${trade['price']:,.2f}  "
                        f"| Reason: {trade['reason']} | Net P&L: ${pnl_txt}"
                    )
        else:
            lines.append("- (no trades yet)")
        lines.append("=" * width)
        lines.append("Press Ctrl+C for Graceful Shutdown / Kill-Switch".center(width))

        if self.clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        print("\n".join(lines), flush=True)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Flush final state to SQLite and close the ledger cleanly.

        Safe to call multiple times; a Ctrl+C shutdown path ends here so the
        paper account is never left in a corrupt state.
        """
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        logger.info("Shutdown initiated: flushing per-asset state to SQLite...")
        for label, strategy in self.strategies.items():
            try:
                self._save_bot_state(label, strategy)
            except Exception as exc:
                logger.warning("Could not flush state for %s: %s", label, exc)
        if self.auto_render:
            self.render_dashboard()
        try:
            self.database.close()
        except Exception as exc:
            logger.warning("Error closing SQLite ledger: %s", exc)
        logger.info("Paper engine stopped gracefully. Goodbye.")


def main() -> int:
    """Entry point: wire everything together and run until Ctrl+C."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if TRADING_MODE != "PAPER":
        raise SystemExit(
            "Refusing to start: TRADING_MODE must be 'PAPER'. "
            "Real exchange execution is deliberately disabled."
        )

    logger.info(
        "Initialising BINANCE SPOT BOT in %s mode (ledger: %s)...",
        TRADING_MODE, DB_PATH,
    )
    orchestrator = PaperTradingOrchestrator()

    try:
        orchestrator.run_streaming()
    except KeyboardInterrupt:
        print()
        logger.info("Ctrl+C received - gracefully stopping the paper engine...")
    finally:
        orchestrator.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())