"""Module 7: Web dashboard server (dark-mode real-time UI over the paper engine).

Serves the browser dashboard that replaces the terminal output of Module 6:

    SQLite ledger ──┐
                    ├──> BotRuntime ──> Flask JSON API ──> Vanilla-JS poller
    PaperExecutor ──┘

Endpoints:
    GET  /                      render templates/index.html
    GET  /api/status            JSON snapshot: portfolio, asset states,
                                reversal gauges, risk flags, recent trades
    POST /api/control/pause     toggle/set the trading pause switch
    POST /api/control/shutdown  latch the kill-switch; embedded orchestrators
                                receive a graceful shutdown()
    GET  /api/health            liveness probe

Two data modes:

* **Standalone** (default) - runs beside an already-running bot and rebuilds
  the virtual account by replaying the ``trades`` ledger plus the persisted
  ``bot_state`` snapshots. Live marks come from Binance's *public* ticker
  endpoint (no API keys), degrading silently to the last ledger price when
  offline. Nothing here can ever place a real order.

* **Embedded** - pass the orchestrator explicitly::

      runtime = BotRuntime(db_path, orchestrator=orch)
      gate_orchestrator(orch, runtime)   # PAUSE/SHUTDOWN gate live candles
      app = create_app(runtime)

Standalone launch (from the ``trading_bot`` directory)::

    python dashboard/app.py                 # http://127.0.0.1:8000
    python dashboard/app.py --seed-demo     # populate sample fills once
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

# ----------------------------------------------------------------------
# Path bootstrap: make `database`, `config`, ... importable no matter which
# working directory the server was started from.
# ----------------------------------------------------------------------
_TRADING_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_TRADING_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_TRADING_BOT_DIR))

from config.btc_config import get_btc_config         # noqa: E402
from config.eth_config import get_eth_config         # noqa: E402
from database.database import Database, utc_now_iso  # noqa: E402

logger = logging.getLogger("spidey.dashboard")

ASSET_LABELS: tuple = ("BTC", "ETH")
DEFAULT_DB_PATH = "paper_trading.db"
DEFAULT_INITIAL_USDT = 10_000.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_EXPOSURE_PERCENT = 0.80        # mirrors config/global_config.py
HISTORY_TRADE_LIMIT = 5_000        # ledger replay window
RECENT_TRADES_LIMIT = 25           # rows pushed to the UI
PRICE_TTL_SECONDS = 4.0            # public-ticker cache lifetime
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"


def _flatten_strategy_cfg(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one asset config into the thresholds the UI renders."""
    strat = raw.get("strategy", {})
    stop = strat.get("stop_loss_pct")
    return {
        "min_trend_move_pct": float(strat.get("min_trend_move_percent", 2.0)),
        "buy_reversal_pct": float(strat.get("buy_reversal_threshold", 3.0)),
        "sell_reversal_pct": float(strat.get("sell_reversal_threshold", 3.0)),
        "stop_loss_pct": float(stop) if stop is not None else None,
        "cooldown_candles": int(strat.get("cooldown_candles", 3)),
        "position_size_pct": float(strat.get("position_size_pct", 0.5)),
    }


def _default_asset_configs() -> Dict[str, Dict[str, Any]]:
    """Per-asset UI thresholds from ``config/`` with hard-coded fallbacks."""
    configs: Dict[str, Dict[str, Any]] = {}
    for label, factory in (("BTC", get_btc_config), ("ETH", get_eth_config)):
        try:
            configs[label] = _flatten_strategy_cfg(factory())
        except Exception as exc:  # pragma: no cover - config files are static
            logger.warning(
                "Could not load %s config (%s); using defaults", label, exc
            )
            configs[label] = _flatten_strategy_cfg({})
    return configs

class BotRuntime:
    """State bridge between the paper-engine artifacts and the web layer.

    Args:
        db_path: SQLite ledger file. Opened lazily with the canonical schema
            (identical to the engine's own :class:`Database`).
        orchestrator: Optional live orchestrator instance. When supplied,
            balances / states / prices are read straight from the executor
            and strategy machines instead of being replayed from disk.
        initial_usdt: Starting capital assumed when replaying a ledger; must
            match the engine's setting to keep the P&L math exact.
        enable_price_feed: Poll Binance's public ticker for live mark prices.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        orchestrator: Any = None,
        initial_usdt: float = DEFAULT_INITIAL_USDT,
        enable_price_feed: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        self.orchestrator = orchestrator
        self.initial_usdt = float(initial_usdt)
        self.enable_price_feed = bool(enable_price_feed)
        self.asset_configs = _default_asset_configs()

        # Control-plane switches guarded by ``_lock``.
        self.paused: bool = False
        self.shutdown_requested: bool = False
        self.shutdown_reason: Optional[str] = None

        self.started_at = utc_now_iso()
        self._started_mono = time.monotonic()

        self._db: Optional[Database] = None
        self._prices: Dict[str, float] = {}
        self._prices_at: float = 0.0
        self._price_source = "idle"

    # ------------------------------------------------------------------
    # Ledger handle / lifecycle
    # ------------------------------------------------------------------
    def ledger(self) -> Database:
        """Public accessor for the lazily-opened SQLite ledger."""
        return self._database()

    def _database(self) -> Database:
        with self._lock:
            if self._db is None:
                self._db = Database(self.db_path)
            return self._db

    def close(self) -> None:
        """Close the lazily-opened ledger connection (safe to call twice)."""
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    # ------------------------------------------------------------------
    # Emergency control plane
    # ------------------------------------------------------------------
    def is_halted(self) -> bool:
        """True while PAUSED or after EMERGENCY SHUTDOWN."""
        with self._lock:
            return self.paused or self.shutdown_requested

    def set_paused(self, paused: bool, reason: str = "manual") -> None:
        """Set the pause switch; ignored once shutdown has been latched."""
        with self._lock:
            if self.shutdown_requested:
                return
            self.paused = bool(paused)
        logger.info("Pause switch -> %s (%s)", self.paused, reason)

    def request_shutdown(self, reason: str = "emergency-button") -> None:
        """Latch the kill-switch; best-effort stop an attached orchestrator.

        Once latched this can never be undone from the web layer -- a fresh
        process start is required, exactly like a hardware kill-switch.
        """
        with self._lock:
            if self.shutdown_requested:
                return
            self.shutdown_requested = True
            self.paused = True
            self.shutdown_reason = reason
        logger.warning("EMERGENCY SHUTDOWN requested (%s)", reason)
        if self.orchestrator is not None:
            threading.Thread(
                target=self._safe_shutdown_orchestrator, daemon=True
            ).start()

    def _safe_shutdown_orchestrator(self) -> None:
        try:
            self.orchestrator.shutdown()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Orchestrator shutdown raised: %s", exc)

    # ------------------------------------------------------------------
    # Live price feed (public Binance ticker, cached + thread-safe)
    # ------------------------------------------------------------------
    def refresh_prices(self, *, force: bool = False) -> Dict[str, float]:
        """Best-effort mark prices keyed by base label (``BTC``/``ETH``).

        Priority: an attached orchestrator's ``latest_prices`` -> cached
        public ticker -> previous cache values -> empty dict (the caller
        then falls back to the last ledger price). Network failures are
        swallowed and logged at debug level; they never raise.
        """
        if self.orchestrator is not None:
            raw = getattr(self.orchestrator, "latest_prices", None) or {}
            orch_prices = {
                str(k).upper(): float(v)
                for k, v in raw.items()
                if isinstance(v, (int, float)) and v > 0
            }
            if orch_prices:
                with self._lock:
                    self._prices = orch_prices
                    self._prices_at = time.monotonic()
                    self._price_source = "orchestrator"
                return dict(orch_prices)

        if not self.enable_price_feed:
            with self._lock:
                self._price_source = "disabled"
            return dict(self._prices)

        with self._lock:
            fresh = (time.monotonic() - self._prices_at) < PRICE_TTL_SECONDS
        if fresh and not force:
            return dict(self._prices)

        fetched: Dict[str, float] = {}
        try:
            import requests  # engine dependency (used by data/live_stream.py)

            resp = requests.get(
                BINANCE_TICKER_URL,
                params={"symbols": '["BTCUSDT","ETHUSDT"]'},
                timeout=2.5,
            )
            resp.raise_for_status()
            for row in resp.json():
                sym = str(row.get("symbol", ""))
                if sym.endswith("USDT"):
                    fetched[sym[: -len("USDT")]] = float(row["price"])
            self._price_source = "binance-public"
        except Exception as exc:
            logger.debug("Public ticker unavailable: %s", exc)
            with self._lock:
                self._price_source = (
                    "ledger-fallback" if self._prices else "unavailable"
                )

        with self._lock:
            if fetched:
                self._prices.update(fetched)
            # Back off even on failure so a dead endpoint is not hammered
            # on every dashboard poll.
            self._prices_at = time.monotonic()
            return dict(self._prices)

    @property
    def price_meta(self) -> Dict[str, Any]:
        """Feed diagnostics surfaced in ``/api/status``."""
        with self._lock:
            age = (
                time.monotonic() - self._prices_at
                if self._prices_at else None
            )
            return {"source": self._price_source, "age_seconds": age}

    # ------------------------------------------------------------------
    # Ledger replay (standalone accounting engine)
    # ------------------------------------------------------------------
    def _replay_ledger(self, trades_asc: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Rebuild the virtual account from the chronological trades table.

        Applies the executor's exact cash identity::

            BUY : usdt -= cost_usdt + fee
            SELL: usdt += cost_usdt - fee ; realized += net_pnl

        Returns balances, average entry prices, realized P&L and win stats,
        reproducing the same numbers a restarted engine would recover.
        """
        usdt = self.initial_usdt
        qty = {label: 0.0 for label in ASSET_LABELS}
        avg_entry: Dict[str, Optional[float]] = {
            label: None for label in ASSET_LABELS
        }
        last_price: Dict[str, Optional[float]] = {
            label: None for label in ASSET_LABELS
        }
        realized = 0.0
        sells = wins = 0

        for t in trades_asc:
            label = str(t.get("symbol", "")).split("/", 1)[0].upper()
            if label not in qty:
                continue
            try:
                cost = float(t["cost_usdt"])
                fee = float(t.get("fee") or 0.0)
                q = float(t["quantity"])
                price = float(t["price"])
            except (KeyError, TypeError, ValueError):
                continue
            last_price[label] = price

            if t.get("side") == "BUY":
                usdt -= cost + fee
                prev_qty = qty[label]
                prev_avg = avg_entry[label] or 0.0
                new_qty = prev_qty + q
                fill_price = (cost / q) if q > 0 else 0.0
                avg_entry[label] = (
                    ((prev_avg * prev_qty) + (fill_price * q)) / new_qty
                    if new_qty > 0 else None
                )
                qty[label] = new_qty
            else:  # SELL
                usdt += cost - fee
                sells += 1
                pnl = t.get("net_pnl")
                if pnl is not None:
                    realized += float(pnl)
                    if float(pnl) > 0:
                        wins += 1
                qty[label] = max(0.0, qty[label] - q)
                if qty[label] <= 1e-12:
                    qty[label] = 0.0
                    avg_entry[label] = None

        return {
            "usdt": usdt,
            "qty": qty,
            "avg_entry": avg_entry,
            "last_price": last_price,
            "realized_pnl": realized,
            "sell_trades": sells,
            "wins": wins,
        }

    # ------------------------------------------------------------------
    # Status assembly
    # ------------------------------------------------------------------
    def collect_status(self) -> Dict[str, Any]:
        """Build the full JSON snapshot consumed by ``dashboard.js``."""
        db = self._database()
        recent = db.get_recent_trades(RECENT_TRADES_LIMIT)
        history_asc = list(reversed(db.get_recent_trades(HISTORY_TRADE_LIMIT)))
        rep = self._replay_ledger(history_asc)

        orch = self.orchestrator
        if orch is not None:
            # ---- live mode: read straight from the running engine -------
            ex = orch.executor
            usdt = float(ex.usdt_balance)
            qty = {
                label: float(getattr(ex, f"{label.lower()}_balance"))
                for label in ASSET_LABELS
            }
            entries: Dict[str, Optional[float]] = {}
            for label in ASSET_LABELS:
                pos = ex.get_position(f"{label}/USDT")
                entries[label] = (
                    float(pos.entry_price)
                    if pos is not None and pos.quantity > 0 else None
                )
            snaps = {
                label: strategy.get_snapshot()
                for label, strategy in orch.strategies.items()
            }
            orch_prices = getattr(orch, "latest_prices", None) or {}
            data_source = "live-orchestrator"
        else:
            # ---- standalone mode: rebuild from the persisted ledger -----
            usdt = rep["usdt"]
            qty = dict(rep["qty"])
            entries = dict(rep["avg_entry"])
            snaps = {}
            for label in ASSET_LABELS:
                row = db.load_bot_state(f"{label}/USDT") or {}
                cfg = self.asset_configs[label]
                snaps[label] = {
                    "current_state": row.get("current_state", "WAITING"),
                    "position_open": row.get("entry_price") is not None,
                    "entry_price": row.get("entry_price"),
                    "highest_price": row.get("highest_price"),
                    "lowest_price": row.get("lowest_price"),
                    "reference_price": None,
                    "last_signal": None,
                    "stop_loss_pct": cfg["stop_loss_pct"],
                    "cooldown_remaining": None,
                    "last_updated": row.get("last_updated"),
                }
            orch_prices = {}
            data_source = "ledger-replay"

        ticker = self.refresh_prices()
        prices: Dict[str, Optional[float]] = {}
        for label in ASSET_LABELS:
            candidate = (
                orch_prices.get(label)
                or ticker.get(label)
                or rep["last_price"][label]
            )
            prices[label] = float(candidate) if candidate else None


        # ---- portfolio aggregation --------------------------------------
        invested = 0.0
        unrealized = 0.0
        valuation_estimated = False
        for label in ASSET_LABELS:
            q = qty[label]
            if q <= 0.0:
                continue
            mark = prices[label] if prices[label] is not None else entries[label]
            if mark is None:
                valuation_estimated = True
                continue
            invested += q * mark
            if entries[label]:
                unrealized += (mark - entries[label]) * q

        total = usdt + invested
        realized = rep["realized_pnl"]
        net = realized + unrealized
        wins = rep["wins"]
        sells = rep["sell_trades"]

        portfolio = {
            "total_value": round(total, 2),
            "available_usdt": round(usdt, 2),
            "invested_value": round(invested, 2),
            "initial_capital": round(self.initial_usdt, 2),
            "exposure_percent": (
                round(invested / total, 4) if total > 0 else 0.0
            ),
            "max_exposure_percent": MAX_EXPOSURE_PERCENT,
            "valuation_estimated": valuation_estimated,
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "net_pnl": round(net, 2),
            "net_pnl_percent": (
                round(net / self.initial_usdt * 100.0, 4)
                if self.initial_usdt > 0 else 0.0
            ),
            "total_trades": len(history_asc),
            "sell_trades": sells,
            "wins": wins,
            "losses": sells - wins,
            "win_rate_percent": (
                round(wins / sells * 100.0, 2) if sells else None
            ),
        }

        assets = {
            label: self._asset_payload(
                label,
                snaps.get(label, {}),
                qty.get(label, 0.0),
                entries.get(label),
                prices.get(label),
            )
            for label in ASSET_LABELS
        }

        with self._lock:
            paused_flag = self.paused
            shutdown_flag = self.shutdown_requested
        system_status = (
            "SHUTDOWN" if shutdown_flag
            else ("PAUSED" if paused_flag else "ONLINE")
        )


        return {
            "ok": True,
            "server_time": utc_now_iso(),
            "uptime_seconds": round(time.monotonic() - self._started_mono, 1),
            "mode": "PAPER",
            "mode_label": "PAPER TRADING",
            "system_status": system_status,
            "paused": paused_flag,
            "shutdown": shutdown_flag,
            "bot_attached": orch is not None,
            "data_source": data_source,
            "price_feed": self.price_meta,
            "portfolio": portfolio,
            "assets": assets,
            "recent_trades": [
                {
                    "id": t.get("id"),
                    "timestamp": t.get("timestamp"),
                    "symbol": t.get("symbol"),
                    "side": t.get("side"),
                    "price": t.get("price"),
                    "quantity": t.get("quantity"),
                    "cost_usdt": t.get("cost_usdt"),
                    "fee": t.get("fee"),
                    "slippage": t.get("slippage"),
                    "reason": t.get("reason") or "",
                    "net_pnl": t.get("net_pnl"),
                }
                for t in recent
            ],
        }


    # ------------------------------------------------------------------
    # Per-asset serialisation + reversal gauge model
    # ------------------------------------------------------------------
    def _asset_payload(
        self,
        label: str,
        snap: Dict[str, Any],
        balance_qty: float,
        entry_from_book: Optional[float],
        price: Optional[float],
    ) -> Dict[str, Any]:
        """Serialise one asset card: state, position, risk, reversal gauge."""
        cfg = self.asset_configs[label]
        state = str(snap.get("current_state", "WAITING")).upper()
        entry = snap.get("entry_price")
        entry = float(entry) if entry is not None else (
            float(entry_from_book) if entry_from_book is not None else None
        )
        position_open = bool(snap.get("position_open")) and entry is not None

        unrealized = unrealized_pct = position_value = None
        if position_open and price is not None and entry:
            position_value = balance_qty * price
            unrealized = (price - entry) * balance_qty
            unrealized_pct = (
                ((price - entry) / entry) * 100.0 if entry > 0 else None
            )

        sl_raw = snap.get("stop_loss_pct")
        stop_loss_pct = (
            float(sl_raw) if sl_raw is not None else cfg["stop_loss_pct"]
        )
        stop_price = (
            entry * (1.0 - stop_loss_pct)
            if (entry is not None and stop_loss_pct) else None
        )

        cooldown_total = cfg["cooldown_candles"]
        cd_raw = snap.get("cooldown_remaining")
        cooldown_left = int(cd_raw) if cd_raw is not None else (
            cooldown_total if state == "COOLDOWN" else 0
        )

        progress = self._compute_progress(
            state=state,
            price=price,
            reference=snap.get("reference_price"),
            lowest=snap.get("lowest_price"),
            highest=snap.get("highest_price"),
            cooldown_left=cooldown_left,
            cfg=cfg,
        )

        def _round2(value: Optional[float]) -> Optional[float]:
            return round(value, 2) if value is not None else None

        return {
            "symbol": f"{label}/USDT",
            "label": label,
            "state": state,
            "price": price,
            "balance_qty": balance_qty,
            "position_open": position_open,
            "entry_price": entry,
            "quantity": balance_qty if position_open else None,
            "position_value": _round2(position_value),
            "unrealized_pnl": _round2(unrealized),
            "unrealized_pnl_percent": (
                round(unrealized_pct, 4) if unrealized_pct is not None else None
            ),
            "trailing_peak": snap.get("highest_price"),
            "trailing_trough": snap.get("lowest_price"),
            "reference_price": snap.get("reference_price"),
            "stop_loss_price": _round2(stop_price),
            "stop_loss_pct": stop_loss_pct,
            "cooldown_remaining": cooldown_left,
            "cooldown_candles": cooldown_total,
            "last_signal": snap.get("last_signal"),
            "thresholds": {
                "min_trend_move_pct": cfg["min_trend_move_pct"],
                "buy_reversal_pct": cfg["buy_reversal_pct"],
                "sell_reversal_pct": cfg["sell_reversal_pct"],
            },
            "progress": progress,
            "last_update": snap.get("last_updated"),
        }


    @staticmethod
    def _compute_progress(
        *,
        state: str,
        price: Optional[float],
        reference: Optional[float],
        lowest: Optional[float],
        highest: Optional[float],
        cooldown_left: int,
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Reversal / cooldown gauge model shared by every asset card.

        Mirrors the state machine's percent math exactly:

        * ``WAITING``       - drop from the local reference peak toward
          ``min_trend_move_pct`` (enters DOWNTREND);
        * ``DOWNTREND`` /
          ``READY_TO_BUY``  - rebound off the trough toward
          ``buy_reversal_pct``;
        * ``HOLDING`` /
          ``READY_TO_SELL`` - drawdown from the peak toward
          ``sell_reversal_pct``;
        * ``COOLDOWN``      - served candles vs the lock-out window.

        ``ratio`` is clamped to ``0..1``; ``None`` renders an idle bar.
        """

        def clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        if state == "COOLDOWN":
            total = max(1, int(cfg["cooldown_candles"]))
            left = max(0, int(cooldown_left))
            served = total - left
            return {
                "kind": "cooldown",
                "label": "COOLDOWN RECOVERY",
                "current": served,
                "threshold": total,
                "unit": "candles",
                "ratio": clamp(served / total),
            }

        if state in ("HOLDING", "READY_TO_SELL"):
            thr = float(cfg["sell_reversal_pct"])
            if price is not None and highest:
                dd = (highest - price) / highest * 100.0
                ratio = clamp(dd / thr) if thr > 0 else 0.0
                current = round(dd, 3)
            else:
                ratio, current = None, None
            return {
                "kind": "sell",
                "label": "SELL REVERSAL PROGRESS",
                "current": current,
                "threshold": thr,
                "unit": "%",
                "ratio": ratio,
            }

        if state in ("DOWNTREND", "READY_TO_BUY"):
            thr = float(cfg["buy_reversal_pct"])
            if price is not None and lowest:
                reb = (price - lowest) / lowest * 100.0
                ratio = clamp(reb / thr) if thr > 0 else 0.0
                current = round(reb, 3)
            else:
                ratio, current = None, None
            return {
                "kind": "buy",
                "label": "BUY REVERSAL PROGRESS",
                "current": current,
                "threshold": thr,
                "unit": "%",
                "ratio": ratio,
            }

        # WAITING: measure the drop from the tracked reference peak.
        thr = float(cfg["min_trend_move_pct"])
        if price is not None and reference:
            drop = (reference - price) / reference * 100.0
            ratio = clamp(drop / thr) if thr > 0 else 0.0
            current = round(drop, 3)
        else:
            ratio, current = None, None
        return {
            "kind": "drop",
            "label": "TREND DROP TRACKING",
            "current": current,
            "threshold": thr,
            "unit": "%",
            "ratio": ratio,
        }


# ----------------------------------------------------------------------
# Embedded-mode helpers / HTTP layer
# ----------------------------------------------------------------------
def gate_orchestrator(orchestrator: Any, runtime: BotRuntime) -> Any:
    """Wrap ``process_candle`` so PAUSE / EMERGENCY SHUTDOWN gate execution.

    While halted, incoming candles are dropped with a warning (the market
    stream itself keeps running, so releasing the pause resumes processing
    on the very next completed candle). Returns the same orchestrator for
    chaining::

        orchestrator = gate_orchestrator(orchestrator, runtime)
    """
    original = orchestrator.process_candle

    def gated(candle: Dict[str, Any]) -> None:
        if runtime.is_halted():
            logger.warning(
                "[%s] trading halted - candle ignored (paused=%s, shutdown=%s)",
                candle.get("symbol"),
                runtime.paused,
                runtime.shutdown_requested,
            )
            return
        original(candle)

    orchestrator.process_candle = gated
    return orchestrator


def create_app(runtime: BotRuntime) -> Flask:
    """Flask application factory bound to a :class:`BotRuntime`."""
    base = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(base / "templates"),
        static_folder=str(base / "static"),
    )
    try:  # keep key order stable for humans diffing the payload
        app.json.sort_keys = False
    except Exception:  # pragma: no cover - older Flask fallback
        app.config["JSON_SORT_KEYS"] = False

    # ---------------- pages ----------------
    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    # ---------------- data API ----------------
    @app.get("/api/status")
    def api_status() -> Any:
        return jsonify(runtime.collect_status())

    @app.get("/api/health")
    def api_health() -> Any:
        return jsonify({"ok": True, "server_time": utc_now_iso()})

    # ---------------- emergency controls ----------------
    @app.post("/api/control/pause")
    def api_pause() -> Any:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict) and "paused" in body:
            target = bool(body["paused"])
        else:
            target = not runtime.paused
        runtime.set_paused(target, reason="web-ui")
        return jsonify({"ok": True, "paused": runtime.paused})

    @app.post("/api/control/shutdown")
    def api_shutdown() -> Any:
        body = request.get_json(silent=True) or {}
        reason = (
            str(body.get("reason", "web-ui"))
            if isinstance(body, dict) else "web-ui"
        )
        runtime.request_shutdown(reason=reason)
        return jsonify({"ok": True, "shutdown": True, "paused": True})

    @app.errorhandler(404)
    def not_found(_: Any) -> Any:
        return jsonify({"ok": False, "error": "not found"}), 404

    return app


# ----------------------------------------------------------------------
# Demo seeding (opt-in only)
# ----------------------------------------------------------------------
# minutes_ago, symbol, side, price, quantity, cost, fee, slippage, reason, net_pnl
_DEMO_TRADES = (
    (240, "BTC/USDT", "BUY",  63100.0, 0.0780, 4921.80, 4.92, 2.46, "BUY_REVERSAL_CONFIRMED", None),
    (195, "BTC/USDT", "SELL", 63990.0, 0.0780, 4991.22, 4.99, 2.46, "REVERSAL",               59.51),
    (150, "BTC/USDT", "BUY",  64250.0, 0.0750, 4818.75, 4.82, 2.41, "BUY_REVERSAL_CONFIRMED", None),
    (140, "ETH/USDT", "BUY",  3150.00, 1.2000, 3780.00, 3.78, 1.89, "BUY_REVERSAL_CONFIRMED", None),
    ( 95, "BTC/USDT", "SELL", 66900.0, 0.0750, 5017.50, 5.02, 2.41, "REVERSAL",              188.91),
    ( 60, "ETH/USDT", "SELL", 3072.00, 1.2000, 3686.40, 3.69, 1.89, "STOP_LOSS",            -101.07),
    ( 35, "BTC/USDT", "BUY",  67100.0, 0.0740, 4965.40, 4.97, 2.36, "BUY_REVERSAL_CONFIRMED", None),
)


def seed_demo_data(db: Database, initial_usdt: float = DEFAULT_INITIAL_USDT) -> int:
    """Insert a realistic sample session **only when the ledger is empty**.

    Lets the dashboard be explored before the engine has ever run; it never
    mixes demo rows with real ones. Returns the number of rows inserted.
    """
    del initial_usdt  # kept for signature symmetry / future capital seeding
    if db.get_recent_trades(1):
        return 0
    anchor = datetime.now(timezone.utc)
    for row in _DEMO_TRADES:
        (minutes_ago, symbol, side, price, quantity,
         cost, fee, slippage, reason, pnl) = row
        ts = (anchor - timedelta(minutes=minutes_ago)) \
            .replace(microsecond=0).isoformat()
        db.save_trade({
            "timestamp": ts,
            "symbol": symbol,
            "side": side,
            "price": price,
            "quantity": quantity,
            "cost_usdt": cost,
            "fee": fee,
            "slippage": slippage,
            "reason": reason,
            "net_pnl": pnl,
        })
    db.save_bot_state(
        "BTC/USDT", current_state="HOLDING",
        entry_price=67100.0, highest_price=68120.0, lowest_price=None,
    )
    db.save_bot_state(
        "ETH/USDT", current_state="DOWNTREND",
        entry_price=None, highest_price=None, lowest_price=2938.50,
    )
    return len(_DEMO_TRADES)


def _resolve_db_path(raw: str) -> Path:
    """Resolve a relative ledger path against CWD, then the project dir."""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    for candidate in (Path.cwd() / p, _TRADING_BOT_DIR / p):
        if candidate.exists():
            return candidate
    return Path.cwd() / p


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: serve the dashboard standalone."""
    parser = argparse.ArgumentParser(
        description="Spidey Sense - paper-trading web dashboard"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("SPIDEY_DB_PATH", DEFAULT_DB_PATH),
        help="SQLite ledger path (default: paper_trading.db)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("SPIDEY_DASH_HOST", DEFAULT_HOST)
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("SPIDEY_DASH_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--initial-usdt", type=float, default=DEFAULT_INITIAL_USDT,
        help="starting capital assumed when replaying the ledger",
    )
    parser.add_argument(
        "--seed-demo", action="store_true",
        help="insert sample paper fills once if the ledger is empty",
    )
    parser.add_argument(
        "--no-price-feed", action="store_true",
        help="disable Binance public ticker polling (ledger prices only)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = _resolve_db_path(args.db)
    print("=" * 64)
    print(" SPIDEY SENSE - PAPER TRADING WEB DASHBOARD")
    print(f"   ledger : {db_path}")
    print(
        "   prices : "
        + ("Binance public ticker" if not args.no_price_feed
           else "disabled (ledger fallback)")
    )
    print("=" * 64)

    runtime = BotRuntime(
        str(db_path),
        initial_usdt=args.initial_usdt,
        enable_price_feed=not args.no_price_feed,
    )
    if args.seed_demo:
        seeded = seed_demo_data(runtime.ledger(), args.initial_usdt)
        print(
            f"   demo   : seeded {seeded} sample fills"
            if seeded else "   demo   : ledger not empty - skipped"
        )

    app = create_app(runtime)
    url = f"http://{args.host}:{args.port}"
    print(f"   ui     : {url}  (Ctrl+C to stop)")
    try:
        app.run(
            host=args.host,
            port=args.port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())