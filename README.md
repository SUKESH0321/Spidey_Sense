# 🕷 Spidey Sense — Binance Spot Paper-Trading Bot

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Mode](https://img.shields.io/badge/mode-PAPER%20ONLY-amber) ![License](https://img.shields.io/badge/license-Apache--2.0-green) ![UI](https://img.shields.io/badge/dashboard-Flask%20%2B%20Vanilla%20JS-58a6ff)

A modular algorithmic **spot-trading bot** for BTC/USDT & ETH/USDT built around a deterministic trend-reversal state machine, portfolio-level risk sizing, institutional-style backtesting, crash-safe SQLite paper execution — and a **real-time dark-mode web dashboard** that replaces the terminal UI.

> **⚠️ 100 % PAPER TRADING.** `TRADING_MODE` is hard-coded to `"PAPER"` in `main.py` and the executor refuses anything else. There is **no code path that can place a real order** on any exchange, and every market-data touchpoint uses Binance's *public* REST API — no API keys are ever required, requested, or read.

---

## ✨ Features

- 🧠 **Deterministic state machine** — WAITING → DOWNTREND → READY_TO_BUY → HOLDING → READY_TO_SELL → COOLDOWN, evaluated on *completed* candles only (no intra-candle noise trading).
- 🛡 **Portfolio risk manager** — sizes every BUY from `position_size_pct` of free cash, caps total invested exposure at **80 %** of equity, enforces a **$5 minimum** notional.
- 💥 **Stop-loss override + post-exit cooldown** — a close at/below `entry × (1 − 5 %)` flattens instantly; after any exit, BUYs are locked out for N candles.
- 🔁 **Zero-look-ahead backtests** — candle-by-candle replay with slippage + fee modelling (Module 4), plus concurrent **multi-asset backtesting sharing one USDT pool** so the global exposure cap becomes a binding constraint (Module 5).
- 💾 **Crash-safe ledger** — SQLite in WAL mode with autocommit; a kill at any instant can never half-write a fill. The live engine **resumes open positions** from persisted state after a restart.
- 📊 **Real-time web dashboard** — dark glassmorphism UI, live KPIs, dual asset cards with reversal-progress gauges, trade log, server-synced clock, working **Pause / Emergency Shutdown** controls.
- 🧪 **Self-contained test suites** — one deterministic verification script per module (`test_module_*.py`), runnable offline (except Module 1, which fetches real data by design).
- 🌐 **Zero credentials** — everything runs on Binance public endpoints; nothing to configure, nothing to leak.

---

## 🏗 Architecture

```
             ┌────────────────────────────  data layer  ────────────────────────────┐
             │  data/historical_data.py   paginated public OHLCV fetcher            │
             │  data/market_data.py       integrity validation (gaps/dupes/order)   │
             │  data/live_stream.py       resilient kline watcher (auto-reconnect)  │
             └───────────────────────────────────┬──────────────────────────────────┘
                                                 ▼  completed OHLCV candle
   ┌──────────────────┐  signal   ┌──────────────────────┐  sized  ┌─────────────────────┐
   │ strategy/        │──────────▶│ risk/risk_manager.py │────────▶│ execution/          │
   │ Module 2 + 3a    │  BUY/SELL │ Module 3b            │  order  │ paper_executor.py   │
   │ state machine    │           │ approve + size       │         │ virtual spot fills  │
   └──────────────────┘           └──────────────────────┘         └──────────┬──────────┘
                                                                              ▼ every fill
        ┌──────────────────────────── database/database.py ◀───────────────────┘
        │   SQLite ledger  ·  trades + bot_state  ·  WAL + autocommit
        └───────────────┬──────────────────────────────────────┬─────────────────
                        ▼                                      ▼
        terminal dashboard (main.py, ASCII)        dashboard/app.py → browser UI
```

### Module map

| Module | Files | Role |
|---|---|---|
| **1** | `data/historical_data.py`, `data/market_data.py` | Paginated public OHLCV fetcher; integrity validation (gaps, duplicates, monotonicity) |
| **2** | `strategy/base_strategy.py`, `btc_strategy.py`, `eth_strategy.py` | Extreme-price-tracking trend/reversal state machine, one isolated instance per asset |
| **3a** | `strategy/base_strategy.py` | Emergency stop-loss override + post-exit BUY lock-out window |
| **3b** | `risk/risk_manager.py` | Capital allocation: position sizing, 80 % exposure cap, minimum notional |
| **4a/4b** | `backtesting/engine.py`, `backtesting/metrics.py` | Zero-look-ahead backtester; metrics + ASCII tear-sheet (net/gross P&L, profit factor, max drawdown, buy-&-hold benchmark) |
| **5** | `backtesting/portfolio_engine.py` | Concurrent multi-asset backtest over a **shared USDT pool**, time-synchronized replay |
| **6** | `main.py`, `execution/paper_executor.py`, `database/database.py`, `data/live_stream.py` | Live paper orchestrator, virtual execution engine, SQLite ledger, resilient market feed |
| **7** | `dashboard/**` | Flask + Jinja2 backend (`app.py`) and the HTML/CSS/JS front-end with its REST control plane |

---

## 🤖 The strategy in one screen

The machine tracks a local **reference peak**, the **lowest low** of the current dip and — while holding — the **highest high** since entry. All math uses percentage moves of *completed* candle closes:

```
WAITING       ── drop ≥ min_trend_move_percent ──────▶ DOWNTREND
DOWNTREND     ── rebound ≥ buy_reversal_threshold ───▶ READY_TO_BUY
READY_TO_BUY  ── green close (confirmation) ─────────▶ HOLDING        ⇒ BUY
HOLDING       ── close ≤ entry×(1−stop_loss_pct) ────▶ COOLDOWN       ⇒ SELL · STOP_LOSS
HOLDING       ── drawdown ≥ sell_reversal_threshold ─▶ READY_TO_SELL
READY_TO_SELL ── red close (confirmation) ───────────▶ COOLDOWN       ⇒ SELL · REVERSAL
COOLDOWN      ── cooldown_candles elapsed ───────────▶ WAITING
```

Current defaults (identical for BTC & ETH): `min_trend_move 2 %` · `buy/sell reversal 3 %` · `stop_loss 5 %` · `cooldown 3 candles` · timeframe `5m`.

**Execution accounting** (paper fills include adverse slippage):

```
BUY  fill = price × (1 + slippage_pct)     cash −= qty×fill + fee
SELL fill = price × (1 − slippage_pct)     cash += qty×fill − fee
net_pnl   = (sell_fill − buy_fill) × qty − entry_fee − exit_fee
```


---

## ⚙️ Requirements

| | |
|---|---|
| **Python** | 3.10 or newer (developed & tested on 3.14) |
| **Packages** | `pandas`, `requests`, `Flask` (Jinja2 ships with Flask) — see `requirements.txt` |
| **Network** | Optional but recommended — Binance public REST for live data/prices. Backtests fall back to synthetic candles; the dashboard falls back to the last ledger price when offline. |
| **Disk / RAM** | Trivial (~150 MB with pandas) |

---

## 🚀 Setup

> All commands below assume the repo root `Spidey_Sense/`. Every runtime script is launched **from inside `trading_bot/`**.

### Option A — virtual environment + `requirements.txt` (recommended)

**Windows PowerShell**
```powershell
cd trading_bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r ..\requirements.txt
```

**Linux / macOS**
```bash
cd trading_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
```

### Option B — plain pip, no venv
```bash
pip install pandas requests flask
```
*(Jinja2 is pulled in automatically as a Flask dependency.)*

### Option C — conda
```bash
conda create -n spidey python=3.12 -y
conda activate spidey
pip install pandas requests flask
```

### Verify the installation
Run the per-module verification suites (each prints its own PASS/FAIL summary):
```bash
cd trading_bot
python test_module_1.py           # live data fetch + integrity report (needs internet)
python test_module_2.py           # state-machine scenarios (offline)
python test_module_3a.py          # stop-loss + cooldown (offline)
python test_module_3b.py          # risk sizing/caps (offline)
python test_module_4.py           # backtest engine + tear sheet (synthetic fallback)
python test_module_5.py           # multi-asset portfolio backtest (offline)
python test_module_6.py           # live engine pipeline -> SQLite (offline)
python test_module_7_dashboard.py # web dashboard API + replay identity (offline)
```
PowerShell one-liner for all of them:
```powershell
foreach ($t in '1','2','3a','3b','4','5','6','7_dashboard') { python "test_module_$t.py" }
```

---

## 🎮 Usage

### 1 · Live paper-trading engine (terminal dashboard)
```bash
cd trading_bot
python main.py
```
Connects the public Binance kline stream for BTC/USDT + ETH/USDT on `5m`, warms up the state machines, then redraws an ASCII dashboard after every completed candle (portfolio value, per-asset state, last 3 trades). **Ctrl+C** flushes final state to SQLite and exits gracefully; the next start resumes open positions from `bot_state`.

### 2 · Web dashboard (standalone — reads the same ledger)
```bash
# terminal 1                          # terminal 2
python main.py                        python dashboard/app.py --seed-demo
```
Then open **http://127.0.0.1:8000**. The UI auto-polls `/api/status` every 2.5 s — no page reloads. `--seed-demo` fills an *empty* ledger once with a realistic sample session so you can explore immediately; it never touches a ledger that already has trades.

<details>
<summary><b>Dashboard CLI flags</b></summary>

| Flag | Default | Env var | Purpose |
|---|---|---|---|
| `--db` | `paper_trading.db` | `SPIDEY_DB_PATH` | Ledger file (relative paths resolve against CWD, then `trading_bot/`) |
| `--host` | `127.0.0.1` | `SPIDEY_DASH_HOST` | Bind address |
| `--port` | `8000` | `SPIDEY_DASH_PORT` | Bind port |
| `--initial-usdt` | `10000` | — | Starting capital assumed when replaying the ledger (must match the engine's) |
| `--seed-demo` | off | — | Seed sample trades only if ledger empty |
| `--no-price-feed` | off | — | Disable Binance ticker polling (ledger prices only) |
| `--log-level` | `INFO` | — | `DEBUG`/`INFO`/… |

</details>


### 3 · Dashboard embedded in the live engine (single process)
The dashboard can run *inside* your bot process, reading executor/strategy state directly and making **Pause / Emergency Shutdown act on live candles**:
```python
# trading_bot/run_with_dashboard.py
import threading

from main import INITIAL_USDT, PaperTradingOrchestrator
from dashboard.app import BotRuntime, create_app, gate_orchestrator

orch = PaperTradingOrchestrator(initial_usdt=INITIAL_USDT, clear_screen=False)
runtime = BotRuntime(
    "paper_trading.db", orchestrator=orch, initial_usdt=INITIAL_USDT,
)
gate_orchestrator(orch, runtime)   # PAUSED/SHUTDOWN now gate process_candle()

app = create_app(runtime)
threading.Thread(
    target=lambda: app.run(host="127.0.0.1", port=8000), daemon=True,
).start()

orch.run_streaming()               # Ctrl+C still shuts down gracefully
```

### 4 · Backtesting
Ready-made runs with tear sheets:
```bash
python test_module_4.py    # single-asset BTC cycle + full metrics report
python test_module_5.py    # BTC+ETH sharing one USDT pool (exposure-cap proof)
```
Or drive the engines directly from Python:
```python
from datetime import datetime, timedelta, timezone

from data.historical_data import fetch_ohlcv              # Module 1 · public REST
from strategy.btc_strategy import BTCStrategy             # Module 2/3a
from risk.risk_manager import RiskManager                 # Module 3b
from backtesting.engine import BacktestEngine             # Module 4a
from backtesting.metrics import MetricsCalculator, format_tear_sheet  # 4b

df = fetch_ohlcv("BTC/USDT", "5m",
                 start=datetime.now(timezone.utc) - timedelta(days=30))

engine = BacktestEngine(BTCStrategy(), RiskManager())     # $10k · 0.1% fee · 0.05% slip
result = engine.run(df)                                   # zero look-ahead replay
print(format_tear_sheet(MetricsCalculator().from_result(result)))
```
For multi-asset runs swap in `backtesting.portfolio_engine.PortfolioBacktester({strategy_map}, risk_manager)` and `.run({"BTC/USDT": df_btc, "ETH/USDT": df_eth})`.

### 5 · Dashboard REST API

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/` | — | Dashboard page |
| `GET` | `/api/status` | — | Full JSON snapshot: portfolio KPIs, per-asset state/gauges/risk chips, price-feed diagnostics, last 25 trades |
| `POST` | `/api/control/pause` | `{}` toggle · `{"paused": true\|false}` explicit | New paused state |
| `POST` | `/api/control/shutdown` | `{"reason": "..."}` optional | Latches kill-switch (irreversible until restart); stops an attached orchestrator gracefully |
| `GET` | `/api/health` | — | Liveness probe |


---

## 🗄 Persistence (SQLite ledger)

`paper_trading.db` (WAL journal, autocommit — crash-safe at every fill):

**`trades`** — one row per virtual fill
| column | notes |
|---|---|
| `timestamp` | ISO-8601 UTC |
| `symbol` / `side` | `BTC/USDT`… · `BUY` \| `SELL` |
| `price` | **execution** price (slippage-adjusted) |
| `quantity` / `cost_usdt` | base qty · notional (BUY cost / SELL proceeds) |
| `fee`, `slippage` | USDT costs |
| `reason` | strategy/risk reason, e.g. `BUY_REVERSAL_CONFIRMED`, `REVERSAL`, `STOP_LOSS` |
| `net_pnl` | realized net P&L on SELL rows, `NULL` on BUYs |

**`bot_state`** — per-symbol strategy snapshot upserted after each candle (`current_state`, `entry_price`, `highest_price`, `lowest_price`, `last_updated`) → powers crash-resume *and* the standalone dashboard's state view.

---

## 🔧 Configuration cheat-sheet

| File | Knobs |
|---|---|
| `config/global_config.py` | `MAX_ACCOUNT_EXPOSURE_PERCENT = 0.80` · `MIN_ORDER_VALUE_USDT = 5.0` |
| `config/btc_config.py` / `eth_config.py` | `timeframe ("5m")` · `min_trend_move_percent (2)` · `buy/sell_reversal_threshold (3)` · `stop_loss_pct (0.05)` · `cooldown_candles (3)` · `position_size_pct (0.50)` |
| `main.py` constants | `INITIAL_USDT = 10_000` · `FEE_PCT = 0.001` · `SLIPPAGE_PCT = 0.0005` · `DB_PATH` |
| `dashboard/app.py` constants | poll TTL, trade-history limits, default host/port (also CLI-overridable) |

Config objects are returned by factory functions → no shared mutable state between assets/processes.

---

## 📁 Project structure

```text
Spidey_Sense/
├── README.md                        ← you are here
├── requirements.txt
├── LICENSE                          Apache-2.0
└── trading_bot/
    ├── main.py                      live paper orchestrator + ASCII dashboard
    ├── dashboard/
    │   ├── app.py                   Flask server · BotRuntime · control plane
    │   ├── templates/index.html
    │   └── static/{css/dashboard.css, js/dashboard.js}
    ├── strategy/                    base + BTC + ETH state machines (M2/3a)
    ├── risk/risk_manager.py         sizing & exposure caps (M3b)
    ├── execution/paper_executor.py  virtual fills -> ledger
    ├── database/database.py         SQLite trades/bot_state (WAL)
    ├── data/                        historical fetch · validation · live stream (M1/M6)
    ├── backtesting/                 engine · metrics · portfolio engine (M4/M5)
    ├── config/                      global + per-asset configs
    ├── test_module_{1,2,3a,3b,4,5,6}.py          per-module suites
    ├── test_module_7_dashboard.py                dashboard suite
    └── paper_trading.db             created at runtime
```

---

## 🧯 Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard shows `OFFLINE` badge | `/api/status` unreachable — check the server terminal; the UI keeps retrying every 2.5 s automatically. |
| Price feed says `ledger-fallback` / `unavailable` | Binance unreachable or region-blocked. Bot logic is unaffected; marks fall back to the last traded ledger price. Retry later or run with `--no-price-feed` to silence probing. |
| Portfolio looks empty / zeros | The dashboard opened a different ledger than the bot wrote. Point both at the same file: `--db C:\absolute\path\paper_trading.db` (or set `SPIDEY_DB_PATH`). Relative paths resolve against your CWD, then `trading_bot/`. |
| Port already in use | `--port 8001`. |
| PowerShell blocks venv activation | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then re-run `Activate.ps1`. |
| `test_module_1.py` fails offline | By design it fetches real Binance history — needs internet. All other suites are fully offline. |
| Start the paper account over | Stop everything, delete `trading_bot/paper_trading.db`, relaunch (add `--seed-demo` for sample data). |

---

## 🛡 Safety guarantees

- `TRADING_MODE` is a hard-coded `"PAPER"` constant; startup **refuses to boot** otherwise.
- `PaperExecutor` only ever mutates an in-memory balance object + local SQLite — no exchange client exists anywhere in the execution path.
- Only **public, key-free** Binance endpoints are contacted (klines + ticker).
- Emergency Shutdown latches irreversibly until process restart; while paused, candles are dropped before they reach strategy/execution.

## 📜 License

Apache-2.0 — see [LICENSE](LICENSE).
