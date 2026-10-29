"""Dashboard UI: one embedded HTML page, no build step, no dependencies."""

from __future__ import annotations

PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eBay Deal Scanner</title>
<style>
:root {
  --bg: #12151c; --panel: #1a1f29; --panel2: #202634; --border: #2c3444;
  --text: #d7dde8; --muted: #8b96ab; --accent: #4f8ff7; --good: #3fb970;
  --warn: #d29922; --bad: #e5534b; --chip: #262d3d;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, "Segoe UI", sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  padding: 12px 18px; border-bottom: 1px solid var(--border); background: var(--panel); }
header h1 { font-size: 17px; margin: 0; letter-spacing: .3px; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 11.5px; font-weight: 600; }
.badge.shadow { background: #3a2f12; color: #e3b341; }
.badge.live { background: #143425; color: #4bd37b; }
.badge.stale { background: #3d1d1b; color: #ff7b72; }
.spacer { flex: 1; }
main { padding: 16px 18px; max-width: 1500px; margin: 0 auto; }
.cards { display: grid; gap: 12px; grid-template-columns:
  repeat(auto-fit, minmax(170px, 1fr)); margin-bottom: 16px; }
.card { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; }
.card .k { color: var(--muted); font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .6px; }
.card .v { font-size: 19px; font-weight: 600; margin-top: 3px; }
.card .s { color: var(--muted); font-size: 11.5px; margin-top: 2px; }
.meter { height: 5px; background: var(--chip); border-radius: 3px; margin-top: 6px; }
.meter i { display: block; height: 100%; border-radius: 3px; background: var(--good); }
.tabs { display: flex; gap: 6px; margin-bottom: 12px; }
.tabs button { background: var(--panel); color: var(--muted); border: 1px solid var(--border);
  border-radius: 8px 8px 0 0; padding: 7px 16px; cursor: pointer; font-size: 13.5px; }
.tabs button.on { color: var(--text); background: var(--panel2);
  border-bottom-color: var(--panel2); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 12px; margin-bottom: 12px; }
.filters input, .filters select, .decide select {
  background: var(--panel2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 8px; font: inherit; }
.filters input[type=search] { min-width: 200px; }
.filters label { color: var(--muted); font-size: 12px; display: flex;
  align-items: center; gap: 5px; }
.multifilter { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px;
  background: var(--panel2); border: 1px solid var(--border); border-radius: 6px;
  flex-wrap: wrap; }
.multifilter .mf-label { color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: .5px; margin-right: 2px; }
.multifilter label { margin: 0; padding: 2px 10px; border: 1px solid var(--border);
  border-radius: 999px; cursor: pointer; background: var(--panel); color: var(--muted);
  font-size: 12px; user-select: none; }
.multifilter label input { position: absolute; opacity: 0; pointer-events: none; }
.multifilter label:has(input:checked) { background: var(--accent); border-color: var(--accent);
  color: #fff; }
.multifilter label:has(input:focus-visible) { outline: 2px solid var(--accent);
  outline-offset: 1px; }
#reset { background: var(--panel2); color: var(--muted); border: 1px dashed var(--border);
  border-radius: 6px; padding: 5px 10px; cursor: pointer; }
table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--border);
  vertical-align: top; }
th { color: var(--muted); font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .5px; background: var(--panel2); white-space: nowrap; }
tbody tr[data-i] { cursor: pointer; }
tbody tr:hover td { background: #1e2431; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.good { color: var(--good); font-weight: 600; }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
.muted { color: var(--muted); }
.chips span { display: inline-block; background: var(--chip); border-radius: 4px;
  padding: 1px 6px; margin: 1px 3px 1px 0; font-size: 11px; color: var(--muted); }
.chips span.high { background: #41201e; color: #ff9d96; }
.chips span.medium { background: #3a2f12; color: #e3b341; }
.score-cell { font-weight: 700; }
tr.detail td { background: #171c26; padding: 10px 16px; }
.trunc { max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
td .meta { color: var(--muted); font-size: 12px; }
.decide { display: flex; gap: 5px; }
.decide select { font-size: 12px; padding: 3px 5px; width: 110px; }
.decide button { background: var(--chip); color: var(--text); border: 1px solid var(--border);
  border-radius: 5px; cursor: pointer; font-size: 12px; padding: 3px 8px; }
.decide button:disabled { opacity: .5; cursor: default; }
.pager { display: flex; gap: 10px; align-items: center; margin-top: 10px;
  color: var(--muted); font-size: 12.5px; }
.pager button { background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 4px 12px; cursor: pointer; }
.pager button:disabled { opacity: .4; cursor: default; }
#flash { position: fixed; bottom: 16px; right: 16px; background: var(--panel2);
  border: 1px solid var(--border); color: var(--text); border-radius: 8px;
  padding: 8px 14px; display: none; z-index: 5; }
.empty { padding: 30px; text-align: center; color: var(--muted); }
.disclaimer { margin-top: 14px; color: var(--muted); font-size: 12px; }
@media (max-width: 900px) { .trunc { max-width: 260px; } }
</style>
</head>
<body>
<header>
  <h1>eBay Deal Scanner</h1>
  <span id="mode"></span>
  <span id="hb" title="scanner heartbeat"></span>
  <span class="spacer"></span>
  <span id="updated" class="muted" style="font-size:12px"></span>
  <button id="refresh" class="badge" style="border:1px solid var(--border);background:var(--panel2);color:var(--text);cursor:pointer">Refresh</button>
</header>
<main>

<div class="cards" id="cards"></div>

<div class="tabs">
  <button data-tab="listings" class="on">Listings</button>
  <button data-tab="alerts">Alerts</button>
  <button data-tab="scan">Scanner</button>
</div>

<div class="filters" id="f-scan" style="display:none">
  <select id="scan-kind">
    <option value="new_listings">New listings — exact models</option>
    <option value="auctions">Auctions ending soon</option>
    <option value="variant">Variant / misspelling searches</option>
    <option value="bundle">Bundles &amp; lots</option>
    <option value="discovery">Broad discovery searches</option>
    <option value="product">One product — all strategies</option>
  </select>
  <select id="scan-product" style="display:none"><option value="">Product…</option></select>
  <label>Profiles <input type="number" id="scan-limit" min="1" max="25" value="8" style="width:52px"></label>
  <button id="scan-run">Run scan now</button>
  <button id="scan-notify">Test Telegram</button>
  <span id="scan-status" class="muted"></span>
</div>

<div class="filters" id="f-listings">
  <input type="search" id="q" placeholder="Search titles…">
  <select id="product"><option value="">Product: all</option></select>
  <select id="category"><option value="">Category: all</option></select>
  <select id="marketplace">
    <option value="">Market: all</option>
    <option value="EBAY_IT">Italy</option><option value="EBAY_DE">Germany</option>
    <option value="EBAY_AT">Austria</option><option value="EBAY_FR">France</option>
  </select>
  <div class="multifilter" id="condition-boxes"
       title="Condition — tick any combination, none selected means all">
    <span class="mf-label">Condition</span>
    <label><input type="checkbox" value="new">New / open box</label>
    <label><input type="checkbox" value="used_working">Used (working)</label>
    <label><input type="checkbox" value="refurbished">Refurbished</label>
    <label><input type="checkbox" value="for_parts">For parts</label>
    <label><input type="checkbox" value="unknown">Unknown</label>
  </div>
  <input type="hidden" id="condition">
  <select id="buying_option">
    <option value="">Type: all</option><option value="fixed">Fixed</option>
    <option value="auction">Auction</option>
  </select>
  <select id="status">
    <option value="">Status: all</option><option value="active">Active</option>
    <option value="not_seen">Not seen</option><option value="unknown">Unknown</option>
  </select>
  <select id="risk">
    <option value="">Risk: all</option><option value="low">Low</option>
    <option value="medium">Medium</option><option value="high">High</option>
  </select>
  <select id="ship">
    <option value="any">Shipping: any</option><option value="ok">Ships to me</option>
    <option value="unknown">Unknown shipping</option>
  </select>
  <label>Min disc <input type="number" id="min_discount" min="0" max="95" style="width:56px"></label>
  <label>Min save € <input type="number" id="min_savings_eur" min="0" style="width:64px"></label>
  <label>Min score <input type="number" id="min_score" min="0" max="100" style="width:56px"></label>
  <label>Max total € <input type="number" id="max_total_eur" min="0" style="width:72px"></label>
  <select id="ending">
    <option value="">Auctions: all</option><option value="6">Ending &lt;6h</option>
    <option value="24">Ending &lt;24h</option><option value="72">Ending &lt;72h</option>
  </select>
  <label><input type="checkbox" id="deals_only"> Deals only</label>
  <label><input type="checkbox" id="has_alert"> Alerted</label>
  <select id="sort">
    <option value="deal">Sort: deal score</option><option value="discount">Discount</option>
    <option value="savings">Savings</option><option value="total">Total cost</option>
    <option value="price">Price</option><option value="newest">Newest</option>
    <option value="ending">Ending soonest</option>
  </select>
  <select id="limit"><option>25</option><option selected>100</option><option>250</option></select>
  <button id="reset">Reset</button>
</div>

<div id="view"></div>

<div class="pager" id="pager">
  <button id="prev">‹ Prev</button>
  <span id="pageinfo"></span>
  <button id="next">Next ›</button>
</div>

<div class="disclaimer">
  Prices are active eBay <b>asking</b> prices, not verified sold prices. Value estimates
  follow the reference/observed evidence shown per listing — treat low-evidence values
  as provisional and verify condition, seller and shipping before buying.
</div>
</main>
<div id="flash"></div>

<script>
"use strict";
(function main() {
  const $ = id => document.getElementById(id);
  const euro = m => (m == null ? "—" :
    (m/100).toLocaleString("it-IT", {style:"currency", currency:"EUR", maximumFractionDigits:2}));
  const when = s => (s ? new Date(s).toLocaleString([], {dateStyle:"short", timeStyle:"short"}) : "—");
  const DECISIONS = ["good_deal","purchased","wrong_product","defective","unavailable","too_risky","not_needed"];
  const CONDLABEL = {"new": "New", "used_working": "Used (working)",
    "refurbished": "Refurbished", "for_parts": "For parts", "unknown": "Unknown"};
  const PARAMS = ["q","product","category","marketplace","condition","buying_option","status","risk",
    "ship","min_discount","min_savings_eur","min_score","max_total_eur","ending","deals_only",
    "has_alert","sort","limit"];
  let offset = 0, tab = "listings", expanded = null, searchTimer = null, alertsState = "", scanTimer = null;

  const esc = s => (s == null ? "" : String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])));
  const tier = (v) => v == null ? ["muted","—"] :
    v >= 60 ? ["good", v.toFixed(0)] : v >= 35 ? ["warn", v.toFixed(0)] : ["", v.toFixed(0)];

  function flash(msg, ms) {
    const el = $("flash"); el.textContent = msg; el.style.display = "block";
    clearTimeout(flash.t); flash.t = setTimeout(() => el.style.display = "none", ms || 2600);
  }

  async function api(path, init) {
    const res = await fetch(path, init);
    const body = await res.json().catch(() => ({error: "bad response"}));
    if (!res.ok) throw new Error(body.error || res.status);
    return body;
  }

  function qs(extra) {
    const p = new URLSearchParams();
    for (const key of PARAMS) {
      const el = $(key);
      if (!el) continue;
      const val = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
      if (val) p.set(key, val);
    }
    if (p.get("ending")) { p.set("ending_hours", p.get("ending")); p.delete("ending"); }
    for (const [k, v] of Object.entries(extra || {})) v ? p.set(k, v) : p.delete(k);
    if (!p.get("limit")) p.set("limit", "100");
    return p.toString();
  }

  // ------------------------------------------------------------------ stats
  const BASE_TITLE = document.title.replace(/^\(\d+\)\s*/, "") || "Dashboard";
  function setUnreadBadge(n) {
    document.title = n > 0 ? `(${n}) ${BASE_TITLE}` : BASE_TITLE;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 64;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#202634";
    ctx.beginPath(); ctx.arc(32, 32, 32, 0, Math.PI * 2); ctx.fill();
    if (n > 0) {
      ctx.fillStyle = "#e5534b";
      ctx.beginPath(); ctx.arc(32, 32, 32, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "bold 40px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(n > 99 ? "+" : String(n), 32, 34);
    }
    let link = document.querySelector("link[rel='icon']");
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.href = canvas.toDataURL("image/png");
  }

  function renderCards(s) {
    const mode = s.shadow_mode
      ? '<span class="badge shadow">SHADOW MODE — no live alerts</span>'
      : '<span class="badge live">LIVE</span>';
    $("mode").innerHTML = mode;
    let hb = "";
    if (s.heartbeat) {
      const age = (Date.now() - new Date(s.heartbeat.updated_at)) / 1000;
      const cls = age > 600 ? "stale" : "live";
      const text = age > 600 ? "scanner idle" : "scanner running";
      hb = `<span class="badge ${cls}" title="heartbeat ${s.heartbeat.updated_at}">${text}</span>`;
    } else {
      hb = '<span class="badge stale" title="no heartbeat file">scanner not running</span>';
    }
    $("hb").innerHTML = hb;
    const api = s.api.budget != null && s.api.budget > 0
      ? `${s.api.used_today}/${s.api.budget} calls` : `${s.api.used_today} calls`;
    const pct = s.api.budget ? Math.min(100, 100*s.api.used_today/s.api.budget) : 0;
    const lr = s.last_run;
    const cards = [
      {k: "Active listings", v: s.listings.active, s: `${s.listings.new_today} new today · ${s.listings.total} tracked`},
      {k: "Deal candidates", v: s.listings.deals, s: "active with an estimate"},
      {k: "Alerts", v: `${s.alerts.delivered}`, s: `${s.alerts.pending} pending · ${s.alerts.failed} failed · ${s.alerts.shadow} shadow`},
      {k: "API today", v: api.split(" ")[0], s: s.api.budget ? `of ${s.api.budget} budget` : "no configured budget",
       meter: pct},
      {k: "Last search", v: lr ? when(lr.started_at) : "—",
       s: lr ? `${lr.marketplace || ""} "${lr.query || ""}" → ${lr.outcome || "?"} (${lr.items_returned} items)` : "no searches yet"},
    ];
    $("cards").innerHTML = cards.map(c =>
      `<div class="card"><div class="k">${c.k}</div><div class="v">${c.v}</div>
       <div class="s">${c.s}</div>${c.meter != null ? `<div class="meter"><i style="width:${c.meter}%"></i></div>` : ""}</div>`).join("");
    $("updated").textContent = "updated " + new Date(s.generated_at).toLocaleTimeString();
    // Unread = waiting or in-delivery alerts; delivered/shadowed count nowhere.
    setUnreadBadge((s.alerts.pending || 0) + (s.alerts.sending || 0));
  }

  // --------------------------------------------------------------- listings
  function riskChips(flags) {
    if (!flags || !flags.length) return "";
    return `<div class="chips">${flags.map(f => {
      const high = /for_parts|defect|untested|no_return|box_only|bundle_missing/.test(f);
      return `<span class="${high ? "high" : f==="bundle"||f==="with_accessory" ? "medium" : ""}">${esc(f)}</span>`;
    }).join("")}</div>`;
  }

  function listingRow(l) {
    const product = l.product_id
      ? `<span class="muted">${esc(l.product_name || l.product_id)}</span>` : '<span class="muted unmatched">unmatched</span>';
    const opt = l.buying_option === "auction"
      ? `<span class="warn">auction</span><div class="meta">${l.bid_count ?? 0} bids · ends ${when(l.item_end_at)}</div>`
      : "fixed";
    const cond = `<span>${esc(CONDLABEL[l.condition_class] || l.condition_class || "unknown")}</span>`
      + (l.condition_label ? `<div class="meta">${esc(l.condition_label)}</div>` : "");
    const ship = l.shipping_eur_minor == null
      ? (l.shipping_known ? "—" : '<span class="warn">?</span>')
      : euro(l.shipping_eur_minor);
    const score = tier(l.deal_score);
    const src = l.value_source_kind ? `<div class="meta">${esc(l.value_source_kind)}</div>` : "";
    return `<tr data-i="${esc(l.item_id)}" data-open="0">
      <td class="num score-cell ${score[0]}">${score[1]}</td>
      <td class="trunc"><a href="${esc(l.url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">${esc(l.title)}</a>${riskChips(l.risk_flags)}</td>
      <td>${product}</td>
      <td>${esc(l.marketplace)}</td><td>${cond}</td><td>${opt}</td>
      <td class="num">${euro(l.price_eur_minor)}</td>
      <td class="num">${ship}</td>
      <td class="num">${euro(l.total_cost_eur_minor)}</td>
      <td class="num">${euro(l.value_eur_minor)}${src}</td>
      <td class="num">${fmtPct(l.discount_pct)}</td>
      <td class="num good">${euro(l.savings_eur_minor)}</td>
      <td class="num">${l.confidence_score == null ? '<span class="muted">—</span>' : l.confidence_score.toFixed(2)}</td>
      <td class="muted">${when(l.first_seen_at)}</td>
      <td class="decide">${decisionCell(l.item_id)}</td>
    </tr>
    <tr class="detail" data-d="${esc(l.item_id)}" style="display:none"><td colspan="15"></td></tr>`;
  }

  function fmtPct(v) { return v == null ? '<span class="muted">—</span>' :
    `<span class="${v>=30?"good":v>=15?"warn":""}">-${v.toFixed(0)}%</span>`; }

  function decisionCell(itemId) {
    return `<select data-i="${esc(itemId)}"><option value="">decide…</option>${
      DECISIONS.map(d => `<option value="${d}">${d.replace(/_/g," ")}</option>`).join("")}</select>`;
  }

  function detailHtml(l) {
    const seller = [
      l.seller_name ? `seller: ${esc(l.seller_name)}` : null,
      l.seller_feedback_pct != null ? `${l.seller_feedback_pct.toFixed(1)}% pos (${l.seller_feedback_score})` : null,
      l.location_country || l.location_city ? `ships from ${esc([l.location_city, l.location_country].filter(Boolean).join(", "))}` : null,
    ].filter(Boolean).join(" · ");
    const ship = l.ship_to_ok == null ? "shipping eligibility unknown"
      : l.ship_to_ok ? "ships to your country" : "does not ship to your country";
    return `<b>${esc(l.title)}</b><br>
      <span class="meta">${seller || "seller unknown"} · ${esc(l.status)} · seen ${when(l.last_seen_at)}
      · ${esc(ship)}${l.import_charges_eur_minor ? " · import charges " + euro(l.import_charges_eur_minor) : ""}</span><br>
      ${l.description_excerpt ? `<div class="meta" style="margin-top:5px">“${esc(l.description_excerpt)}”</div>` : ""}
      ${l.value_source ? `<div class="meta">value source: ${esc(l.value_source)} · sample ${esc(l.valuation_sample_size ?? "?")}</div>` : ""}
      ${l.max_bid_eur_minor != null ? `<div class="meta good">max bid: ${euro(l.max_bid_eur_minor)} (auction safety margin applied)</div>` : ""}`;
  }

  async function loadListings() {
    const data = await api("/api/listings?" + qs({offset: offset || undefined}));
    const rows = data.items.map(listingRow).join("");
    $("view").innerHTML = rows
      ? `<table><thead><tr>
        <th class="num">Score</th><th>Title</th><th>Product</th><th>Market</th><th>Cond</th>
        <th>Type</th><th class="num">Price</th><th class="num">Ship</th><th class="num">Total</th>
        <th class="num">Value</th><th class="num">Disc</th><th class="num">Save</th>
        <th class="num">Conf</th><th>Seen</th><th>Decision</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="card empty">No listings match the current filters yet. Run <code>python -m src.main once</code> or wait for the scheduler.</div>`;
    $("view").querySelectorAll("tr[data-i]").forEach(row => {
      row.addEventListener("click", (ev) => {
        if (ev.target.closest("a") || ev.target.closest("select") || ev.target.closest("button")) return;
        const id = row.dataset.i;
        const detail = row.nextElementSibling;
        const open = row.dataset.open === "1";
        if (open) { detail.style.display = "none"; row.dataset.open = "0"; }
        else {
          const listing = data.items.find(x => x.item_id === id);
          detail.firstElementChild.innerHTML = listing ? detailHtml(listing) : "";
          detail.style.display = ""; row.dataset.open = "1";
        }
      });
    });
    $("pageinfo").textContent = `${data.total} matching · showing ${offset+1}–${offset+data.items.length}`;
    $("prev").disabled = offset <= 0;
    $("next").disabled = offset + data.items.length >= data.total;
  }

  // ----------------------------------------------------------------- alerts
  async function loadAlerts() {
    const params = new URLSearchParams({limit: "100"});
    if (alertsState) params.set("state", alertsState);
    const data = await api("/api/alerts?" + params.toString());
    const states = ["", "pending", "delivered", "failed", "shadow"];
    const chips = states.map(s =>
      `<button class="badge" data-state="${s}" style="cursor:pointer;border:1px solid var(--border);background:${alertsState===s?"var(--accent)":"var(--chip)"};color:${alertsState===s?"#fff":"var(--muted)"}">${s || "all"}</button>`).join(" ");
    const rows = data.items.map(a => {
      const l = a.item_id;
      return `<tr>
        <td class="muted" style="white-space:nowrap">${when(a.created_at)}</td>
        <td><span class="badge ${a.state==="failed"?"stale":a.state==="pending"?"medium":""}"
             style="background:var(--chip);color:${a.state==="failed"?"var(--bad)":a.state==="pending"?"var(--warn)":"var(--muted)"}">${esc(a.state)}</span></td>
        <td class="trunc">${l ? `<a href="${esc(a.url)}" target="_blank" rel="noreferrer">${esc(a.title || l)}</a>` : `<span class="muted">${esc(a.listing_id || "(listing pruned)")}</span>`}</td>
        <td>${esc(a.kind)}<div class="meta">prio ${a.priority}</div></td>
        <td class="num good">${euro(a.savings_eur_minor)}</td>
        <td class="num">${euro(a.total_cost_eur_minor)}</td>
        <td class="trunc meta">${esc(a.message || "")}</td>
        <td class="trunc meta warn">${esc(a.last_error || "")}</td>
      </tr>`;
    }).join("");
    $("view").innerHTML =
      `<div class="filters">${chips}</div>` +
      (rows ? `<table><thead><tr><th>When</th><th>State</th><th>Listing</th><th>Kind</th>
        <th class="num">Save</th><th class="num">Total</th><th>Message</th><th>Error</th></tr></thead>
        <tbody>${rows}</tbody></table>`
        : '<div class="card empty">No alerts in this state.</div>');
    $("view").querySelectorAll("[data-state]").forEach(btn =>
      btn.addEventListener("click", () => { alertsState = btn.dataset.state; loadAlerts(); }));
  }

  // -------------------------------------------------------------- bootstrap
  async function loadStats() {
    try { renderCards(await api("/api/stats")); }
    catch (exc) { flash("stats failed: " + exc.message); }
  }

  async function loadProducts() {
    try {
      const data = await api("/api/products");
      $("product").innerHTML = '<option value="">Product: all</option>' +
        data.items.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
      $("scan-product").innerHTML = '<option value="">Product…</option>' +
        data.items.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
      $("category").innerHTML = '<option value="">Category: all</option>' +
        data.categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    } catch (exc) { /* dashboard still works without the catalogue */ }
  }

  // ------------------------------------------------------------- scanner
  function scanSummaryLine(st) {
    const sum = st.summary || {};
    const bits = [];
    if (sum.succeeded != null) bits.push(`${sum.succeeded} ok`);
    if (sum.failed) bits.push(`${sum.failed} failed`);
    if (sum.busy) bits.push(`${sum.busy} busy`);
    if (sum.skipped_budget) bits.push(`${sum.skipped_budget} over budget`);
    if (sum.stopped) bits.push(`stopped: ${sum.stopped}`);
    const a = sum.alerts;
    if (a) bits.push(`alerts ${a.delivered} delivered / ${a.failed} failed${a.retried_later ? " / " + a.retried_later + " retry" : ""}`);
    if (sum.quota_remaining != null) bits.push(`${sum.quota_remaining} API calls left today`);
    return bits.join(" · ");
  }

  function scanResultRows(results) {
    return results.map(r => `<tr>
      <td class="muted" style="white-space:nowrap">${esc(r.strategy)}</td>
      <td>${esc(r.marketplace)}</td>
      <td class="trunc">${esc(r.query)}</td>
      <td><span class="${r.outcome === "ok" ? "good" : r.outcome === "error" || r.outcome === "internal_error" ? "bad" : "warn"}">${esc(r.outcome)}</span></td>
      <td class="num">${r.items ?? 0}</td>
      <td class="num">${r.new ?? 0}</td>
      <td class="num">${r.duration_s != null ? r.duration_s + "s" : "—"}</td>
      <td class="trunc meta warn">${esc(r.error || "")}</td>
    </tr>`).join("");
  }

  async function loadScan() {
    if (tab !== "scan") return;
    const data = await api("/api/scan");
    const st = data.scan || {};
    const running = !!st.running;
    const cool = st.cooldown_seconds_left || 0;
    const runBtn = $("scan-run"), notifyBtn = $("scan-notify");
    if (runBtn) runBtn.disabled = running || cool > 0;
    if (notifyBtn) notifyBtn.disabled = running;

    let statusHtml = "";
    if (running) {
      statusHtml = `<div class="card"><div class="k">Scan in progress</div>
        <div class="v">started ${when(st.started_at)}</div>
        <div class="s">running ${esc(JSON.stringify(st.requested || {}))} — results appear here when it finishes</div></div>`;
      $("scan-status").textContent = "scan running…";
      if (scanTimer) clearTimeout(scanTimer);
      scanTimer = setTimeout(loadScan, 2000);
    } else if (st.started_at) {
      const cls = st.error ? "bad" : "good";
      statusHtml = `<div class="card"><div class="k">Last manual scan</div>
        <div class="v ${cls}">${st.error ? "failed" : "finished"}</div>
        <div class="s">${when(st.started_at)} → ${when(st.finished_at)}${st.error ? " · " + esc(st.error) : ""}
        ${scanSummaryLine(st) ? "<br>" + esc(scanSummaryLine(st)) : ""}</div></div>`;
      $("scan-status").textContent = st.error ? "last scan failed" : "last scan finished";
      if (cool > 0) {
        $("scan-status").textContent = `cooldown ${cool}s — throttle between scans to protect the API budget`;
        if (scanTimer) clearTimeout(scanTimer);
        scanTimer = setTimeout(loadScan, 2000);
      }
    } else {
      statusHtml = `<div class="card"><div class="k">Manual scan</div><div class="v">ready</div>
        <div class="s">pick a kind above and click “Run scan now”. The scan reuses the scanner's job locks and API budget, so it is safe to use while the scheduler is running. A short cooldown applies between scans.</div></div>`;
    }

    let candidatesHtml = "";
    const cands = data.candidates || [];
    if (cands.length) {
      const rows = cands.map(c => `<tr>
        <td class="muted" style="white-space:nowrap">${esc(c.state)} · ${when(c.created_at)}</td>
        <td class="trunc">${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">${esc(c.title || c.item_id)}</a>` : `<span class="muted">${esc(c.title || c.item_id || "(listing pruned)")}</span>`}</td>
        <td>${esc(c.kind)}</td>
        <td class="num">${fmtPct(c.discount_pct)}</td>
        <td class="num good">${euro(c.savings_eur_minor)}</td>
        <td class="num">${euro(c.total_cost_eur_minor)}</td>
      </tr>`).join("");
      candidatesHtml = `<b>New deal candidates from this scan</b><table><thead><tr><th>When</th><th>Listing</th><th>Kind</th>
        <th class="num">Disc</th><th class="num">Save</th><th class="num">Total</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }

    let resultsHtml = "";
    if ((st.profile_results || []).length) {
      resultsHtml = `<table><thead><tr><th>Strategy</th><th>Market</th><th>Query</th><th>Outcome</th>
        <th class="num">Items</th><th class="num">New</th><th class="num">Took</th><th>Error</th></tr></thead>
        <tbody>${scanResultRows(st.profile_results)}</tbody></table>`;
    }

    const runsData = data.recent_runs || {items: []};
    const rows = runsData.items.map(r => {
      const dur = r.started_at && r.finished_at
        ? Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000) + "s" : "";
      return `<tr>
        <td class="muted" style="white-space:nowrap">${when(r.started_at)}</td>
        <td class="muted" style="white-space:nowrap">${esc(r.job_key || "")}</td>
        <td class="trunc">${esc(r.marketplace ? r.marketplace + " “" + r.query + "”" : r.query)}${r.pages ? `<div class="meta">${r.pages} page(s)</div>` : ""}</td>
        <td><span class="${r.outcome === "ok" ? "good" : ["api_error","auth_error","internal_error"].includes(r.outcome) ? "bad" : "warn"}">${esc(r.outcome || "?")}</span></td>
        <td class="num">${r.items_returned ?? r.items ?? 0}</td>
        <td class="num">${r.new_items ?? r.new ?? 0}</td>
        <td class="num">${dur}</td>
        <td class="trunc meta warn">${esc(r.error || "")}</td>
      </tr>`;
    }).join("");
    const historyHtml = rows
      ? `<b>Recent searches (scheduled + manual)</b><table><thead><tr><th>When</th><th>Job</th><th>Query</th><th>Outcome</th>
        <th class="num">Items</th><th class="num">New</th><th class="num">Took</th><th>Error</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : '<div class="card empty">No searches recorded yet.</div>';

    $("view").innerHTML = statusHtml + candidatesHtml + resultsHtml + historyHtml;
  }

  async function runScanNow() {
    const kind = $("scan-kind").value;
    const body = {kind, limit: Number($("scan-limit").value || 8)};
    if (kind === "product") {
      body.product_id = $("scan-product").value;
      if (!body.product_id) { flash("pick a product first"); return; }
    }
    try {
      await api("/api/scan", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
      $("scan-status").textContent = "scan started";
      loadScan().catch(e => flash(e.message));
    } catch (exc) { flash("scan failed to start: " + exc.message); }
  }

  async function runNotifyTest() {
    const btn = $("scan-notify"); btn.disabled = true;
    try { await api("/api/scan/notify-test", {method: "POST", body: "{}",
      headers: {"Content-Type": "application/json"}});
      flash("Telegram test sent — check your chat");
    } catch (exc) { flash("Telegram test failed: " + exc.message); }
    finally { btn.disabled = false; }
  }

  $("scan-kind").addEventListener("change", () => {
    $("scan-product").style.display = $("scan-kind").value === "product" ? "" : "none";
  });
  $("scan-run").addEventListener("click", runScanNow);
  $("scan-notify").addEventListener("click", runNotifyTest);

  function switchView(t) {
    tab = t; expanded = null; offset = 0;
    $("f-listings").style.display = t === "listings" ? "" : "none";
    $("f-scan").style.display = t === "scan" ? "" : "none";
    $("pager").style.display = t === "listings" ? "" : "none";
    document.querySelectorAll(".tabs button").forEach(b =>
      b.classList.toggle("on", b.dataset.tab === t));
    const load = {listings: loadListings, alerts: loadAlerts, scan: loadScan}[t];
    (load || loadListings)().catch(exc => flash(exc.message));
  }

  function scheduleListings() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { offset = 0; loadListings().catch(e => flash(e.message)); }, 250);
  }

  // condition is a checkbox chip group; the hidden #condition input carries
  // the comma-joined values the API already understands (e.g. "new,refurbished")
  const condBoxes = () => document.querySelectorAll("#condition-boxes input");
  function syncCondition() {
    $("condition").value = [...condBoxes()].filter(i => i.checked).map(i => i.value).join(",");
  }
  condBoxes().forEach(box => box.addEventListener("change", () => {
    syncCondition(); scheduleListings();
  }));

  for (const key of PARAMS) {
    const el = $(key);
    if (!el) continue;
    if (el.tagName === "SELECT" || el.type === "number") {
      el.addEventListener("change", () => { offset = 0; loadListings().catch(e => flash(e.message)); });
    } else if (el.type === "checkbox") {
      el.addEventListener("change", () => { offset = 0; loadListings().catch(e => flash(e.message)); });
    } else {
      el.addEventListener("input", scheduleListings);
    }
  }
  $("reset").addEventListener("click", () => {
    $("q").value = "";
    for (const key of PARAMS) {
      const el = $(key);
      if (el) { if (el.type === "checkbox") el.checked = false; else if (key !== "limit" && key !== "sort") el.value = ""; }
    }
    condBoxes().forEach(i => i.checked = false);
    syncCondition();
    offset = 0; loadListings().catch(e => flash(e.message));
  });
  $("prev").addEventListener("click", () => {
    offset = Math.max(0, offset - Number($("limit").value || 100));
    loadListings().catch(e => flash(e.message));
  });
  $("next").addEventListener("click", () => {
    offset += Number($("limit").value || 100);
    loadListings().catch(e => flash(e.message));
  });
  $("refresh").addEventListener("click", () => {
    loadStats();
    const load = {listings: loadListings, alerts: loadAlerts, scan: loadScan}[tab];
    (load || loadListings)().catch(e => flash(e.message));
  });
  document.querySelectorAll(".tabs button").forEach(b =>
    b.addEventListener("click", () => switchView(b.dataset.tab)));

  // decisions are routed here so table clicks don't swallow them
  document.addEventListener("change", async (ev) => {
    const sel = ev.target.closest(".decide select");
    if (!sel || !sel.value) return;
    const button = document.createElement("button");
    button.textContent = "save";
    sel.insertAdjacentElement("afterend", button);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api("/api/decision", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({item_id: sel.dataset.i, decision: sel.value})});
        flash("recorded: " + sel.value.replace(/_/g, " "));
        sel.value = "";
      } catch (exc) { flash("failed: " + exc.message); }
      finally { button.remove(); }
    });
  }, {once: false});

  loadStats();
  loadProducts();
  loadListings().catch(exc => flash("database unavailable: " + exc.message));
  setInterval(loadStats, 45000);
})();
</script>
</body>
</html>
"""
