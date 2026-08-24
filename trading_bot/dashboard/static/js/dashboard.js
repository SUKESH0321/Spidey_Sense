"use strict";

/* ================================================================
 * Spidey Sense - Binance Spot Paper Bot :: dashboard.js
 * Vanilla-JS live view over GET /api/status (polled every 2.5 s).
 * No frameworks, no build step, no page reloads.
 * ================================================================ */

const POLL_MS  = 2500;
const CLOCK_MS = 1000;
const FLASH_MS = 850;

const $ = (id) => document.getElementById(id);

/* ---------------- formatting helpers ---------------- */
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const usd = (v, d = 2) =>
  (v == null || Number.isNaN(+v)) ? "--"
    : (+v).toLocaleString("en-US", { style: "currency", currency: "USD",
        minimumFractionDigits: d, maximumFractionDigits: d });

const signedUsd = (v, d = 2) =>
  (v == null || Number.isNaN(+v)) ? "--" : (v < 0 ? "-" : "+") + usd(Math.abs(v), d);

const pctStr = (v, d = 2) =>
  (v == null || Number.isNaN(+v)) ? "--" : (v < 0 ? "" : "+") + (+v).toFixed(d) + "%";

const fmtQty = (v) =>
  (v == null || Number.isNaN(+v)) ? "--"
    : (+v).toLocaleString("en-US", { maximumFractionDigits: 8 });

const timeHM = (iso) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "--";

const tradeTime = (iso) => {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/* ---------------- runtime state ---------------- */
let serverOffsetMs = 0;   // clock skew vs the server's ISO timestamp
let failStreak = 0;
let pollBusy = false;
let uiShutdown = false;

const ASSETS = ["BTC", "ETH"];
const prevPrices = {};
const flashTimers = {};

/* ---------------- header status + banners ---------------- */
function setStatus(status, connected) {
  const badge = $("sys-status");
  let label = status || "OFFLINE";
  let cls = "badge ";
  if (!connected)               { cls += "badge-off";   label = "OFFLINE"; }
  else if (status === "SHUTDOWN") cls += "badge-crit";
  else if (status === "PAUSED")   cls += "badge-warn";
  else                            cls += "badge-online";
  badge.className = cls;
  $("sys-status-text").textContent = label;
}

function setBanner(data) {
  const el = $("alert-banner");
  if (data.shutdown) {
    el.className = "banner crit"; el.hidden = false;
    $("alert-banner-icon").textContent = "⏻";
    $("alert-banner-text").textContent =
      "EMERGENCY SHUTDOWN ENGAGED — the engine ignores every candle until restarted.";
  } else if (data.paused) {
    el.className = "banner warn"; el.hidden = false;
    $("alert-banner-icon").textContent = "⏸";
    $("alert-banner-text").textContent =
      "TRADING PAUSED — signals are being suppressed. Press RESUME to re-arm the engine.";
  } else {
    el.hidden = true;
  }
}

/* ================= KPI CARDS ================= */
function applyPortfolio(p, data) {
  $("kpi-total").textContent = usd(p.total_value);
  $("kpi-total-sub").textContent =
    `initial ${usd(p.initial_capital)}${p.valuation_estimated ? " · est." : ""}`;
  $("kpi-source").textContent =
    data.data_source === "live-orchestrator" ? "LIVE BOT" : "LEDGER";

  /* available USDT + exposure meter */
  $("kpi-usdt").textContent = usd(p.available_usdt);
  const expPct = (p.exposure_percent ?? 0) * 100;
  const maxExp = (p.max_exposure_percent ?? 0.8) * 100;
  const expFill = $("kpi-exposure-fill");
  expFill.style.width = `${Math.min(100, expPct).toFixed(1)}%`;
  const meter = expFill.parentElement;
  meter.classList.toggle("danger", expPct >= maxExp - 1e-9);
  meter.classList.toggle("warn", expPct >= maxExp * 0.75 && expPct < maxExp - 1e-9);
  $("kpi-exposure-text").textContent = `EXPOSURE ${expPct.toFixed(1)}% / ${maxExp.toFixed(0)}%`;

  /* net P&L ($ and %) */
  const pnlEl = $("kpi-pnl");
  pnlEl.textContent = signedUsd(p.net_pnl);
  pnlEl.classList.toggle("pos", p.net_pnl > 0);
  pnlEl.classList.toggle("neg", p.net_pnl < 0);
  const pctBadge = $("kpi-pnl-pct");
  pctBadge.textContent = pctStr(p.net_pnl_percent);
  pctBadge.classList.toggle("pos", p.net_pnl > 0);
  pctBadge.classList.toggle("neg", p.net_pnl < 0);
  $("kpi-pnl-sub").textContent =
    `realized ${signedUsd(p.realized_pnl)} · unrealized ${signedUsd(p.unrealized_pnl)}`;

  /* trades + win-rate meter */
  $("kpi-trades").textContent = String(p.total_trades ?? 0);
  const wrFill = $("kpi-winrate-fill");
  const wrText = $("kpi-winrate-text");
  if (p.win_rate_percent == null) {
    wrText.textContent = "no closed trades yet";
    wrFill.style.width = "0%";
  } else {
    wrText.textContent = `${p.win_rate_percent.toFixed(1)}% WIN (${p.wins}W / ${p.losses}L)`;
    wrFill.style.width = `${Math.min(100, p.win_rate_percent).toFixed(1)}%`;
  }
}

/* ================= ASSET CARDS ================= */
function applyAsset(L, a) {
  /* live price + tick flash */
  const priceEl = $("price-" + L);
  if (a.price != null) {
    priceEl.textContent = usd(a.price);
    const prev = prevPrices[L];
    if (prev != null && a.price !== prev) {
      const cls = a.price > prev ? "flash-up" : "flash-down";
      priceEl.classList.remove("flash-up", "flash-down");
      void priceEl.offsetWidth;               /* restart CSS animation */
      priceEl.classList.add(cls);
      clearTimeout(flashTimers[L]);
      flashTimers[L] = setTimeout(
        () => priceEl.classList.remove(cls), FLASH_MS
      );
    }
    prevPrices[L] = a.price;
  } else {
    priceEl.textContent = "--";
  }

  /* state badge (colour keyed off data-state in CSS) */
  const st = $("state-" + L);
  st.dataset.state = a.state || "UNKNOWN";
  st.textContent = (a.state || "—").replace(/_/g, " ");

  /* position details */
  $("entry-" + L).textContent = a.entry_price != null ? usd(a.entry_price) : "--";
  $("size-" + L).textContent =
    a.position_open && a.quantity != null ? `${fmtQty(a.quantity)} ${L}` : "FLAT";
  $("peak-" + L).textContent = a.trailing_peak != null ? usd(a.trailing_peak) : "--";
  $("trough-" + L).textContent = a.trailing_trough != null ? usd(a.trailing_trough) : "--";

  /* unrealized P&L strip */
  const strip = $("pnlstrip-" + L);
  strip.classList.toggle("on", !!a.position_open);
  strip.classList.remove("pos", "neg");
  if (a.position_open && a.unrealized_pnl != null) {
    strip.classList.add(a.unrealized_pnl >= 0 ? "pos" : "neg");
    $("pnlval-" + L).textContent = signedUsd(a.unrealized_pnl);
    $("pnlpct-" + L).textContent = pctStr(a.unrealized_pnl_percent);
  } else {
    $("pnlval-" + L).textContent = "--";
    $("pnlpct-" + L).textContent = "";
  }

  /* reversal / cooldown gauge */
  const pr = a.progress || {};
  const fillEl = $("progfill-" + L);
  fillEl.className = "progress-fill" + (pr.kind ? ` kind-${pr.kind}` : "");
  fillEl.style.width = pr.ratio != null ? `${(pr.ratio * 100).toFixed(1)}%` : "0%";
  $("proglab-" + L).textContent = pr.label || "—";
  $("progthr-" + L).textContent =
    pr.kind === "cooldown"
      ? `${pr.current ?? 0}/${pr.threshold ?? 0} candles`
      : pr.current != null
        ? `${(+pr.current).toFixed(2)}% / ${+(pr.threshold ?? 0).toFixed(2)}%`
        : `-- / ${(pr.threshold ?? 0).toFixed(2)}%`;

  const cap = $("progcap-" + L);
  if (pr.ratio == null) {
    cap.textContent = "awaiting market data…";
  } else if (pr.kind === "buy") {
    cap.textContent = `rebound ${(+pr.current).toFixed(2)}% off the trough — ` +
      `${+(pr.threshold ?? 0).toFixed(2)}% arms READY_TO_BUY`;
  } else if (pr.kind === "sell") {
    cap.textContent = `drawdown ${(+pr.current).toFixed(2)}% from the peak — ` +
      `${+(pr.threshold ?? 0).toFixed(2)}% arms the exit`;
  } else if (pr.kind === "cooldown") {
    cap.textContent = `BUY re-arms in ${a.cooldown_remaining} candle` +
      (a.cooldown_remaining === 1 ? "" : "s");
  } else {
    cap.textContent = `price fell ${pr.current != null ? (+pr.current).toFixed(2) : "0.00"}% ` +
      `from reference — ${+(pr.threshold ?? 0).toFixed(2)}% enters DOWNTREND`;
  }

  /* chips: signal / stop-loss / cooldown */
  const sig = String(a.last_signal || "NONE").toUpperCase();
  const sigEl = $("sig-" + L);
  sigEl.textContent = `SIGNAL ${sig}`;
  sigEl.className = "chip" + (
    sig === "BUY"   ? " chip-green"
    : sig === "SELL"  ? " chip-red"
    : sig === "HOLD"  ? " chip-blue"
    : ""
  );

  const stopEl = $("stop-" + L);
  stopEl.textContent = a.stop_loss_price != null
    ? `STOP-LOSS ${usd(a.stop_loss_price)} (-${(a.stop_loss_pct * 100).toFixed(1)}%)`
    : "STOP-LOSS OFF";
  stopEl.className = "chip" + (a.position_open ? " chip-red" : "");

  const coolEl = $("cool-" + L);
  const cooling = (a.cooldown_remaining ?? 0) > 0;
  coolEl.hidden = !cooling;
  if (cooling) {
    coolEl.textContent = `COOLDOWN ${a.cooldown_remaining}/${a.cooldown_candles}`;
  }
  coolEl.className = "chip chip-amber";

  /* last update stamp */
  $("upd-" + L).textContent = a.last_update
    ? `state ${timeHM(a.last_update)}`
    : a.price != null ? "live ticker" : "no feed yet";
}

/* ================= RECENT TRADES TABLE ================= */
function rowHtml(t) {
  const sideCls = t.side === "BUY" ? "pill-buy" : "pill-sell";
  let reasonCell;
  if (t.side !== "SELL" || !t.reason) {
    reasonCell = `<span class="dimcell">—</span>`;
  } else {
    const r = String(t.reason);
    const rc = r.includes("STOP")     ? "reason stop"
             : r.includes("REVERSAL") ? "reason rev"
             : "reason";
    reasonCell = `<span class="${rc}">${esc(r)}</span>`;
  }
  const hasPnl = t.net_pnl != null && !Number.isNaN(+t.net_pnl);
  const pnlCls = hasPnl ? (t.net_pnl >= 0 ? "pnl-pos" : "pnl-neg") : "dimcell";
  return `<tr>
    <td class="time">${esc(tradeTime(t.timestamp))}</td>
    <td class="asset-cell">${esc(String(t.symbol || "").split("/")[0])}</td>
    <td><span class="pill ${sideCls}">${esc(t.side)}</span></td>
    <td>${usd(t.price)}</td>
    <td>${fmtQty(t.quantity)}</td>
    <td>${usd(t.cost_usdt)}</td>
    <td>${usd(t.fee, 3)}</td>
    <td>${reasonCell}</td>
    <td class="${pnlCls}">${hasPnl ? signedUsd(t.net_pnl) : "—"}</td>
  </tr>`;
}

function renderTrades(rows) {
  const body = $("trades-body");
  $("trades-count").textContent =
    `${rows.length} FILL${rows.length === 1 ? "" : "S"} SHOWN`;
  body.innerHTML = rows.length
    ? rows.map(rowHtml).join("")
    : `<tr class="placeholder"><td colspan="9">No paper fills recorded yet.</td></tr>`;
}

/* ================= CONTROLS / POLLING ================= */
function reflectControls(data) {
  const pauseBtn = $("btn-pause");
  const killBtn = $("btn-shutdown");
  pauseBtn.disabled = !!data.shutdown;
  killBtn.disabled = !!data.shutdown;
  const resumed = !data.paused && !data.shutdown;
  pauseBtn.classList.toggle("btn-go", data.paused && !data.shutdown);
  pauseBtn.classList.toggle("btn-warn", resumed);
  pauseBtn.innerHTML =
    data.paused ? "▶&nbsp;RESUME TRADING" : "⏸&nbsp;PAUSE TRADING";
}

async function controlPost(url, body) {
  $("btn-pause").disabled = true;
  $("btn-shutdown").disabled = true;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    return await r.json();
  } finally {
    setTimeout(() => poll(), 120);
  }
}

async function poll() {
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    failStreak = 0;
    uiShutdown = !!data.shutdown;
    serverOffsetMs = new Date(data.server_time).getTime() - Date.now();
    setStatus(data.system_status, true);
    setBanner(data);
    reflectControls(data);
    applyPortfolio(data.portfolio, data);
    for (const L of ASSETS) applyAsset(L, data.assets[L]);
    renderTrades(data.recent_trades || []);

    const src = (data.price_feed && data.price_feed.source) || "n/a";
    $("conn-note").textContent =
      `polling /api/status every ${POLL_MS / 1000}s · prices: ${src.replace(/-/g, " ")}`;
  } catch (err) {
    failStreak += 1;
    setStatus(null, false);
    $("conn-note").textContent =
      `connection lost (${failStreak}) — retrying…`;
  } finally {
    pollBusy = false;
  }
}

function tickClock() {
  const now = new Date(Date.now() + serverOffsetMs);
  const p = (n) => String(n).padStart(2, "0");
  $("clock-time").textContent =
    `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  $("clock-date").textContent = now.toLocaleDateString([], {
    weekday: "short", month: "short", day: "2-digit",
  });
}

function init() {
  $("btn-pause").addEventListener("click", () => controlPost("/api/control/pause"));
  $("btn-shutdown").addEventListener("click", () => {
    if (!confirm(
      "Trigger EMERGENCY SHUTDOWN?\n\nThe engine will ignore all further "
      + "candles until it is restarted."
    )) return;
    controlPost("/api/control/shutdown", { reason: "web-ui" });
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });

  tickClock();
  setInterval(tickClock, CLOCK_MS);
  poll();
  setInterval(poll, POLL_MS);
}

document.addEventListener("DOMContentLoaded", init);