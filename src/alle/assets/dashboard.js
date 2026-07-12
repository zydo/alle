// Dashboard page: the consolidated control surface (entrypoint, channels, routes).

import { api, esc, toast, modal, confirmDialog, customSelectHTML, wireCustomSelects, bytes, mbps } from "./core.js";

const GAUGE = `<svg class="ico" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/><path d="m13.4 10.6 2.6-2.6"/><path d="M3.5 18a9 9 0 1 1 17 0"/></svg>`;
const GRIP = `<svg class="ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="9" cy="18" r="1.5"/><circle cx="15" cy="6" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="15" cy="18" r="1.5"/></svg>`;
const GRAB = `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 11V6a2 2 0 0 0-4 0"/><path d="M14 10V4a2 2 0 0 0-4 0v2"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/></svg>`;
const COPY = `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const GEAR = `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
const PEN = `<svg class="pen" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;
const PROVIDER_ICONS = {
  nordvpn: "/nordvpn.svg",
  protonvpn: "/protonvpn.svg",
};

const SHELL = `
  <div class="dashboard-shell">
    <section class="entry compact dash-entry rise" id="entry" hidden>
      <div class="entry-row">
        <span class="entry-bead" aria-hidden="true"></span>
        <span class="entry-label">Router Entrypoint</span>
        <span class="entry-div" aria-hidden="true"></span>
        <span class="entry-addr"><span class="scheme">http://</span><span id="entry-addr"></span><span class="entry-copy">${COPY}</span></span>
        <span class="entry-tun">
          <span class="entry-bead" id="tun-bead" aria-hidden="true"></span>
          <span class="entry-label">TUN</span>
          <span class="tun-pop" id="tun-note" role="tooltip"></span>
          <span class="toggle" id="tun-toggle" role="button" tabindex="0" aria-label="Toggle system-wide TUN mode" aria-describedby="tun-note"><span class="toggle-knob"></span></span>
        </span>
      </div>
    </section>
    <section class="dash-panel dash-panel-channels rise">
      <div class="table-title"><span class="eyebrow">Channels</span></div>
      <div id="channels"></div>
    </section>
    <section class="dash-panel dash-panel-routes rise">
      <div class="table-title"><span class="eyebrow">Router rules</span></div>
      <p class="route-banner">When a matcher (domain or IP) appears in multiple rules, <b>the first match wins</b>. Drag to reorder.</p>
      <div id="routes"></div>
    </section>
  </div>`;

let el = {};
let status = null;
let rulesets = [];
let router = null;
let catalog = [];
let measured = new Map();
let busy = new Set();
let dragRuleId = null;
let paused = false;
// Pending ruleset-id order staged by dragging; null when there is no un-applied
// change. Apply posts this order; refreshRoutes clears it.
let pendingIds = null;
let refreshStatus = () => { };

export function mount(view, ctx) {
  refreshStatus = ctx?.refresh || (() => { });
  view.innerHTML = SHELL;
  el = {
    entry: view.querySelector("#entry"), entryAddr: view.querySelector("#entry-addr"),
    tunToggle: view.querySelector("#tun-toggle"), tunNote: view.querySelector("#tun-note"),
    tunBead: view.querySelector("#tun-bead"),
    channels: view.querySelector("#channels"), routes: view.querySelector("#routes"),
    probeAll: null, speedAll: null,
  };
  el.tunToggle.onclick = toggleTun;
  view.addEventListener("click", (e) => {
    const t = e.target.closest("[data-copy]");
    if (t?.dataset.copy) { copy(t.dataset.copy); e.stopPropagation(); }
  });
  el.channels.addEventListener("click", onChannelClick);
  el.routes.addEventListener("click", onRouteClick);
  el.routes.addEventListener("dragstart", onRouteDragStart);
  el.routes.addEventListener("dragover", onRouteDragOver);
  el.routes.addEventListener("drop", onRouteDrop);
  el.routes.addEventListener("dragend", onRouteDragEnd);
  refreshRoutes();
}

export function unmount() { el = {}; status = null; rulesets = []; router = null; measured = new Map(); busy = new Set(); dragRuleId = null; paused = false; pendingIds = null; }

export function onStatus(s) {
  status = s;
  if (!el.entry) return;
  renderEntry(s);
  if (!paused) renderChannels();
  // Never rebuild the routes DOM while a drag is in progress — doing so
  // detaches the node being dragged and reverts the staged order. The routes
  // panel is reconciled on demand by refreshRoutes(); the periodic tick only
  // needs to refresh channels/entry above.
  if (router && !dragRuleId) renderRoutes();
}

function renderEntry(s) {
  const r = s.router;
  if (!r) { el.entry.hidden = true; return; }
  el.entry.hidden = false;
  const addr = el.entry.querySelector(".entry-addr");
  if (r.port) {
    el.entryAddr.textContent = `127.0.0.1:${r.port}`;
    addr.dataset.copy = `http://127.0.0.1:${r.port}`;
    addr.classList.add("copyable");
    addr.title = "Click to copy";
  } else {
    el.entryAddr.textContent = "(assigned on next start)";
    delete addr.dataset.copy;
    addr.classList.remove("copyable");
    addr.title = "";
  }
  renderTun(r);
}

function renderTun(r) {
  const on = !!r.tun;
  el.tunToggle.classList.toggle("on", on);
  el.tunBead.classList.toggle("live", on);
  // The scope framing lives here: with tun on, the kill-switch (unmatched →
  // block) is genuinely system-wide, and the user must see that shift.
  let note = "Off — only apps pointed at the proxy ports are routed";
  if (on) {
    note = r.killswitch
      ? "On — all system traffic follows the rules; kill-switch is system-wide"
      : "On — all system traffic follows the rules; unmatched goes direct";
  }
  el.tunNote.textContent = note;
}

async function toggleTun() {
  const enabled = !status?.router?.tun;
  const res = await api.post("/api/v1/tun", { enabled });
  if (!res.ok) { toast(res.error, "err"); return; }
  if (status) status.router = res.data.router || status.router;
  router = res.data.router || router;
  renderEntry(status);
  renderRoutes();
  refreshStatus();
  toast(enabled
    ? "TUN mode on — all system traffic now follows the routing rules."
    : "TUN mode off — only traffic pointed at the proxy ports is routed.");
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${text}`);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    toast(`Couldn't copy to clipboard: ${detail}`, "err");
  }
}

function chanKey(c) { return `${c.provider}/${c.name}`; }
function loc(c) { return c.city && !["(Unknown)", "(Any City)"].includes(c.city) ? `${c.city}, ${c.country}` : c.country; }
function spin(key, kind, icon) { return busy.has(key) || busy.has(`${key}:${kind}`) ? '<span class="spinner small"></span>' : icon; }
// A channel's action buttons are locked while it's being tested (either kind) or
// while a batch run is in flight — so e.g. a channel's Probe is greyed while its
// own Speed Test is running, and no per-channel test can fire mid "Test All".
function chanBusy(key) {
  return (
    busy.has(key) || busy.has(`${key}:probe`) || busy.has(`${key}:speed`)
    || busy.has("all:probe") || busy.has("all:speed")
  );
}
function isSet(value) { return value !== null && value !== undefined; }
function syncHeaderBusy() {
  el.probeAll = el.channels?.querySelector("#probe-all");
  el.speedAll = el.channels?.querySelector("#speed-all");
  if (!el.probeAll) return;
  el.probeAll.innerHTML = busy.has("all:probe") ? '<span class="spinner small"></span>' : "◉";
  el.speedAll.innerHTML = busy.has("all:speed") ? '<span class="spinner small"></span>' : GAUGE;
  el.probeAll.disabled = busy.has("all:probe") || busy.has("all:speed");
  el.speedAll.disabled = busy.has("all:probe") || busy.has("all:speed");
}

function chanRow(c) {
  const key = chanKey(c);
  const m = measured.get(key) || {};
  const speed = m.speed_result || {};
  // No STATE column (space is tight): when a channel is not healthy, show its
  // short state word in the IP cell instead of leaving it blank — the verbose
  // reason is the cell's tooltip. Healthy/pending-with-no-IP-yet stays empty.
  const okStates = new Set(["Active", "Healthy", "Pending", ""]);
  const st = c.state || "";
  const showState = st && !okStates.has(st);
  // Compact the state word for the narrow IP cell (the full form is the tooltip):
  // "Reconnecting (4)" → "Reconnecting", "Reconnect failed" → "Failed".
  // Plain string ops (no regex): drop a trailing "(...)" suffix if present.
  let shortSt = st;
  if (shortSt.endsWith(")")) {
    const open = shortSt.lastIndexOf("(");
    if (open > 0) shortSt = shortSt.slice(0, open).trimEnd();
  }
  if (shortSt === "Reconnect failed") shortSt = "Failed";
  const probeDetail = c.probe?.detail;
  const tip = probeDetail ? `${st} — ${probeDetail}` : st;
  let ipCell = `<span class="ip"></span>`;
  if (m.ip) {
    ipCell = `<span class="ip copyable" data-copy="${esc(m.ip)}" title="Click to copy">${esc(m.ip)}</span>`;
  } else if (showState) {
    ipCell = `<span class="ip chan-state warn" title="${esc(tip)}">${esc(shortSt)}</span>`;
  }
  return `<div class="row dashchan body" data-provider="${esc(c.provider)}" data-id="${esc(c.name)}">
    <span class="chan-label"><button class="name edit" data-label="${esc(c.label || "")}" title="Rename">${esc(c.label || c.name)}</button>
      <div class="ref">${esc(key)}</div></span>
    <span class="loc" title="${esc(loc(c))}">${esc(loc(c))}</span>
    <span class="port copyable" data-copy="http://127.0.0.1:${esc(c.port_number)}" title="Click to copy">${esc(c.port)}</span>
    ${ipCell}
    <span class="lat">${isSet(m.latency_ms) ? `${esc(m.latency_ms)} ms` : ""}</span>
    <span class="mono">${isSet(m.sent) ? bytes(m.sent) : ""}</span>
    <span class="mono">${isSet(m.received) ? bytes(m.received) : ""}</span>
    <span class="mono">${speed.download_bps ? mbps(speed.download_bps) : ""}</span>
    <span class="mono">${speed.upload_bps ? mbps(speed.upload_bps) : ""}</span>
    <span class="row-actions channel-actions">
      <button class="icon-btn" title="Probe" aria-label="Probe" data-probe${chanBusy(key) ? " disabled" : ""}>${spin(key, "probe", "◉")}</button>
      <button class="icon-btn" title="Speed Test" aria-label="Speed Test" data-speed${chanBusy(key) ? " disabled" : ""}>${spin(key, "speed", GAUGE)}</button>
      <button class="icon-btn danger" title="Remove" aria-label="Remove" data-remove>×</button>
    </span>
  </div>`;
}

function renderChannels() {
  const chans = status?.channels || [];
  const addRow = `<div class="row dashchan add" data-add-channel role="button" tabindex="0"><span class="add-cell" aria-hidden="true">＋</span><span class="add-label">Add Channel</span></div>`;
  if (!chans.length) {
    el.channels.innerHTML = `<div class="grid">${addRow}</div>`;
    return;
  }
  const head = `<div class="row dashchan head"><span>Channel</span><span>Location</span><span>Port</span><span>IP</span><span>Latency</span><span>Sent</span><span>Received</span><span>Down SPD</span><span>Up SPD</span><span class="row-actions channel-actions channel-all-actions"><button class="icon-btn" id="probe-all" title="Probe All" aria-label="Probe all" data-probe-all>◉</button><button class="icon-btn" id="speed-all" title="Speed Test All" aria-label="Speed test all" data-speed-all>${GAUGE}</button></span></div>`;
  el.channels.innerHTML = `<div class="grid">${head}${chans.map(chanRow).join("")}${addRow}</div>`;
  syncHeaderBusy();
}

// Per-row speed-button busy keys: each channel's gauge spins until its own
// result arrives, so the user sees which channels are still pending during an
// all-channels run. Cleared per row as it lands and again in runTest's finally.
function speedBusyKeys(channel) {
  if (channel) return [`${chanKey(channel)}:speed`];
  return (status?.channels || []).map((c) => `${chanKey(c)}:speed`);
}

async function runTest(channel, speed) {
  const key = channel ? chanKey(channel) : "all";
  const testKind = speed ? "speed" : "probe";
  const busyKey = channel ? `${key}:${testKind}` : `all:${testKind}`;
  busy.add(key); busy.add(busyKey);
  const speedKeys = speed ? speedBusyKeys(channel) : [];
  speedKeys.forEach((k) => busy.add(k));
  syncHeaderBusy();
  renderChannels();
  try {
    if (speed) {
      toast("Speed Test In Progress.");
      await runSpeedStream(channel);
    } else {
      const res = await api.post("/api/v1/test", { speed: false, channel: channel ? channel.name : null });
      if (!res.ok) { toast(res.error, "err"); return; }
      // Test rows carry everything the table shows — fresh probe results and
      // the cumulative sent/received totals; there is no separate metrics call.
      for (const row of res.data.channels || []) {
        measured.set(`${row.provider}/${row.name}`, row);
      }
      renderChannels();
      toast("Probe complete.");
      refreshStatus();
    }
  } finally {
    busy.delete(key); busy.delete(busyKey);
    speedKeys.forEach((k) => busy.delete(k));
    syncHeaderBusy();
    renderChannels();
  }
}

// A speed test streams one result row per channel as it finishes (NDJSON), so
// each row's IP/latency/download/upload fills in live instead of all at once at
// the end. Probe-all stays on the single-shot api.post path above.
async function runSpeedStream(channel) {
  const res = await startSpeedStream(channel);
  if (!res) return;                 // network/401/non-stream error already toasted
  if (await consumeSpeedStream(res.body)) return;  // mid-stream error already toasted
  toast("Speed Test Complete.");
  refreshStatus();
}

async function startSpeedStream(channel) {
  let res;
  try {
    res = await fetch("/api/v1/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speed: true, channel: channel ? channel.name : null }),
    });
  } catch (err) {
    toast(`Can't reach the daemon: ${err instanceof Error ? err.message : String(err)}`, "err");
    return null;
  }
  if (res.status === 401) { location.href = "/"; return null; }
  if (!res.ok || !res.body) {
    toast(await streamErrorMsg(res), "err");
    return null;
  }
  return res;
}

async function streamErrorMsg(res) {
  try { const t = await res.text(); if (t) return JSON.parse(t).error || `Request failed (${res.status})`; } catch { /* keep default */ }
  return `Request failed (${res.status})`;
}

// Read the NDJSON body line by line, applying each row as it lands. Returns true
// if an error event was seen (so the caller skips the success toast/refresh).
async function consumeSpeedStream(body) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let errored = false;
  for (; ;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line && applySpeedEvent(line)) errored = true;
    }
  }
  return errored;
}

function applySpeedEvent(line) {
  let evt;
  try { evt = JSON.parse(line); } catch { return false; }
  if (evt.type === "row" && evt.data) {
    const r = evt.data;
    // Each streamed row already carries post-test sent/received totals.
    measured.set(`${r.provider}/${r.name}`, r);
    busy.delete(`${r.provider}/${r.name}:speed`);
    renderChannels();
  } else if (evt.type === "error" && evt.data) {
    toast(evt.data.error || "Speed test failed.", "err");
    return true;
  }
  // "done" carries only summary counts; nothing to render.
  return false;
}

async function onChannelClick(e) {
  if (e.target.closest("[data-add-channel]")) { openAddChannel(); return; }
  if (e.target.closest("[data-probe-all]")) return runTest(null, false);
  if (e.target.closest("[data-speed-all]")) return runTest(null, true);
  const row = e.target.closest(".row.dashchan.body");
  if (!row) return;
  const channel = (status?.channels || []).find((c) => c.provider === row.dataset.provider && c.name === row.dataset.id);
  if (!channel) return;
  if (e.target.closest("[data-probe]")) return runTest(channel, false);
  if (e.target.closest("[data-speed]")) return runTest(channel, true);
  if (e.target.closest("[data-remove]")) return removeChannel(channel);
  const nameBtn = e.target.closest(".name.edit");
  if (nameBtn) startRelabel(row, channel, nameBtn.dataset.label);
}

async function removeChannel(c) {
  if (!(await confirmDialog("Remove channel", `Remove ${c.provider}/${c.name}?`, { confirmText: "Remove", danger: true }))) return;
  const res = await api.del(`/api/v1/channels/${c.provider}/${c.name}`);
  if (res.ok) { measured.delete(chanKey(c)); toast(`Removed ${c.name}.`); refreshStatus(); }
  else toast(res.error, "err");
}

function startRelabel(rowEl, c, current) {
  paused = true;
  const cell = rowEl.querySelector(".chan-label");
  const ref = cell.querySelector(".ref").outerHTML;
  cell.innerHTML = `<input class="relabel" value="${esc(current)}" placeholder="${esc(c.name)}" spellcheck="false" maxlength="80">${ref}`;
  const input = cell.querySelector(".relabel"); input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) {
      return;
    }
    done = true;
    paused = false;
    if (save) {
      const res = await api.post(`/api/v1/channels/${c.provider}/${c.name}/label`, { label: input.value.trim() });
      if (res.ok) toast(input.value.trim() ? "Label updated." : "Label cleared."); else toast(res.error, "err");
    }
    refreshStatus();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); finish(true); } else if (e.key === "Escape") finish(false); });
  input.addEventListener("blur", () => finish(true));
}

async function refreshRoutes() {
  const res = await api.get("/api/v1/routes");
  if (!res.ok) { toast(res.error, "err"); return; }
  rulesets = res.data.rulesets || [];
  router = res.data.router || {};
  pendingIds = null; // fresh from the server — no staged change
  renderRoutes();
}

function targetLabel(target) {
  if (target === "direct") return { name: "Direct", ref: "No VPN" };
  if (target === "block") return { name: "Block", ref: "Drop Traffic" };
  const c = (status?.channels || []).find((x) => chanKey(x) === target);
  return c ? { name: c.label || c.name, ref: target } : { name: target, ref: "channel ref" };
}

function rulesetDisplayName(rs) {
  return rs.name || targetLabel(rs.target).name;
}

function orderedRulesets() {
  if (!pendingIds) return rulesets;
  const byId = new Map(rulesets.map((rs) => [rs.id, rs]));
  const ordered = [];
  for (const id of pendingIds) {
    if (byId.has(id)) ordered.push(byId.get(id));
  }
  // rulesets not in the staged order (e.g. added mid-drag) keep their place at the end
  for (const rs of rulesets) {
    if (!pendingIds.includes(rs.id)) ordered.push(rs);
  }
  return ordered;
}

function rulesetBar(rs, index) {
  const channelLabel = targetLabel(rs.target).name;
  const name = rulesetDisplayName(rs);
  const isChannel = rs.target !== "direct" && rs.target !== "block";
  const rules = rs.rules || [];
  const hasAll = rules.some((r) => r.type === "all");
  const n = rules.length;
  const addrLabel = n === 1 ? "matcher" : "matchers";
  const addrsInner = hasAll
    ? `<span class="ct">All</span>${PEN}`
    : `<span class="ct">${n}</span><span class="lb">${esc(addrLabel)}</span>${PEN}`;
  const via = isChannel
    ? `<span class="rule-via"><span class="vw">via</span><span class="vch">${esc(channelLabel)}</span></span>`
    : "";
  return `<div class="rule-row" draggable="true" data-id="${esc(rs.id)}">
    <div class="rule-handle" title="Drag to reorder" aria-label="Drag ${esc(name)} to reorder">
      <span class="hh rest">${GRIP} Priority ${index + 1}</span>
      <span class="hh sort">${GRAB} Sort</span>
    </div>
    <div class="rule-name" title="${esc(name)}">${esc(name)}</div>
    <button class="rule-addrs" data-id-edit="${esc(rs.id)}" title="Edit matchers">${addrsInner}</button>
    ${via}
    <button class="icon-btn danger rule-del" title="Remove ruleset" aria-label="Remove ruleset" data-id-remove="${esc(rs.id)}">×</button>
  </div>`;
}

function renderRoutes() {
  const list = orderedRulesets();
  const lanOn = router?.lan_direct !== false;
  const lanBar = `<div class="rule-row lan${lanOn ? "" : " off"}">
    <div class="lan-priority">Priority 0</div>
    <div class="lan-text">${lanOn
      ? `LAN traffic (printers, NAS, router admin, local discovery, etc.) bypass VPN`
      : `LAN traffic follows the rules below; local devices (printers, NAS, router admin, local discovery, etc.) may not be reachable`}</div>
    <span class="toggle ${lanOn ? "on" : ""}" data-lan-toggle role="button" tabindex="0" aria-label="Toggle LAN/local direct"><span class="toggle-knob"></span></span>
  </div>`;
  const addRow = `<div class="rule-row add" data-add-rule role="button" tabindex="0"><span class="rule-add-cell" aria-hidden="true">＋</span><span class="add-label">Add Rule</span></div>`;
  const bars = list.map((rs, i) => rulesetBar(rs, i)).join("");
  const ks = !!router?.killswitch;
  const allow = !ks;
  const unmatchedRow = `<div class="rule-row unmatched${allow ? "" : " off"}">
    <div class="unmatched-priority">Unmatched</div>
    <div class="unmatched-text">For all other Internet traffic that is not matched by any of the VPN rulesets above, control whether you want it to go to the Internet.</div>
    <div class="unmatched-control"><span class="unmatched-label">Allow Non-VPN Traffic</span><span class="toggle ${allow ? "on" : ""}" data-unmatched-toggle role="button" tabindex="0" aria-label="Toggle allow non-VPN traffic"><span class="toggle-knob"></span></span></div>
  </div>`;
  const body = lanBar + bars + addRow + unmatchedRow;
  const dirty = !!pendingIds;
  const applyBar = dirty ? `<div class="apply-bar">
    <span class="apply-copy">Order changed — drag more, or apply to save.</span>
    <span class="apply-actions"><button class="btn ghost" id="dash-reorder-cancel">Cancel</button><button class="btn primary" id="dash-reorder-apply">Apply new order</button></span>
  </div>` : "";
  el.routes.innerHTML = `<div class="ruleset-list ${dirty ? "dirty" : ""}">${body}</div>${applyBar}`;
  el.routes.querySelector("[data-unmatched-toggle]").onclick = () => toggleKillswitch({ target: { checked: !ks } });
  el.routes.querySelector("[data-lan-toggle]").onclick = () => toggleLanDirect(!lanOn);
  if (dirty) {
    el.routes.querySelector("#dash-reorder-apply").onclick = applyReorder;
    el.routes.querySelector("#dash-reorder-cancel").onclick = cancelReorder;
  }
}

async function onRouteClick(e) {
  if (e.target.closest("[data-add-rule]")) { openAddRule(); return; }
  const editId = e.target.closest("[data-id-edit]")?.dataset.idEdit;
  if (editId) { openEditRuleset(editId); return; }
  const removeId = e.target.closest("[data-id-remove]")?.dataset.idRemove;
  if (!removeId) return;
  const rs = rulesets.find((r) => r.id === removeId);
  const name = rs ? rulesetDisplayName(rs) : removeId;
  if (!(await confirmDialog("Remove ruleset", `Remove the ruleset "${name}"?`, { confirmText: "Remove", danger: true }))) return;
  const res = await api.del(`/api/v1/routes/rulesets/${encodeURIComponent(removeId)}`);
  if (res.ok) { toast(`Removed ${name}.`); refreshRoutes(); refreshStatus(); } else toast(res.error, "err");
}

function onRouteDragStart(e) {
  const card = e.target.closest(".rule-row:not(.add):not(.lan):not(.unmatched)");
  if (!card) return;
  dragRuleId = card.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  card.classList.add("dragging");
}

function onRouteDragOver(e) {
  const over = e.target.closest(".rule-row:not(.add):not(.lan):not(.unmatched)");
  if (!over || !dragRuleId || over.dataset.id === dragRuleId) return;
  e.preventDefault();
  const list = over.parentElement;
  const dragged = list.querySelector(`[data-id="${CSS.escape(dragRuleId)}"]`);
  const after = e.clientY > over.getBoundingClientRect().top + over.offsetHeight / 2;
  list.insertBefore(dragged, after ? over.nextSibling : over);
}

async function onRouteDrop(e) {
  const list = e.target.closest(".ruleset-list");
  if (!list || !dragRuleId) return;
  e.preventDefault();
  list.querySelectorAll(".dragging").forEach((r) => r.classList.remove("dragging"));
  stageDraggedOrder();
}

function onRouteDragEnd() {
  // Always fires when a drag finishes (drop or cancel). Clear the in-progress
  // flag so the periodic status tick may reconcile the routes panel again, and
  // stage the order as a fallback for drags that ended without a drop event.
  el.routes?.querySelectorAll(".dragging").forEach((r) => r.classList.remove("dragging"));
  if (dragRuleId) {
    dragRuleId = null;
    stageDraggedOrder();
  }
}

function stageDraggedOrder() {
  const list = el.routes?.querySelector(".ruleset-list");
  if (!list) return;
  const ids = [...list.querySelectorAll(".rule-row:not(.add):not(.lan):not(.unmatched)")].map((card) => card.dataset.id);
  // Stage the new order locally — nothing is sent until Apply is clicked.
  const persisted = rulesets.map((rs) => rs.id);
  const next = JSON.stringify(ids) === JSON.stringify(persisted) ? null : ids;
  if (next !== pendingIds) {
    pendingIds = next;
    renderRoutes();
  }
}

async function applyReorder() {
  if (!pendingIds) return;
  const btn = el.routes.querySelector("#dash-reorder-apply");
  if (btn) { btn.disabled = true; btn.textContent = "Applying…"; }
  const res = await api.post("/api/v1/routes/reorder", { ids: pendingIds });
  if (!res.ok) {
    toast(res.error, "err");
    if (btn) { btn.disabled = false; btn.textContent = "Apply new order"; }
    return;
  }
  toast("Order applied.");
  await refreshRoutes();
  refreshStatus();
}

function cancelReorder() {
  pendingIds = null;
  renderRoutes();
}

async function toggleLanDirect(enabled) {
  const res = await api.post("/api/v1/routes/lan", { enabled });
  if (!res.ok) { toast(res.error, "err"); renderRoutes(); return; }
  router = res.data.router || router;
  renderRoutes();
  refreshStatus();
  toast(enabled ? "LAN direct on — local traffic bypasses the rules." : "LAN direct off — local traffic follows your rules.");
}

async function toggleKillswitch(e) {
  const enabled = e.target.checked;
  const res = await api.post("/api/v1/routes/killswitch", { enabled });
  if (!res.ok) { toast(res.error, "err"); renderRoutes(); return; }
  router = res.data.router || router;
  renderRoutes();
  refreshStatus();
  toast(enabled ? "Non-VPN traffic is now blocked." : "Non-VPN traffic is now allowed.");
}

async function ensureCatalog() {
  if (catalog.length) return catalog;
  const res = await api.get("/api/v1/providers/catalog");
  if (res.ok) catalog = res.data.providers || [];
  return catalog;
}

function providerIcon(provider) {
  return PROVIDER_ICONS[provider] || "";
}

function trimTrailingPunctuation(text) {
  let out = text.trimEnd();
  while (out && ".,;:".includes(out[out.length - 1])) out = out.slice(0, -1).trimEnd();
  return out;
}

function providerGuide(p) {
  if (p.kind === "config") {
    const base = trimTrailingPunctuation(
      (p.config_help || "Generate a WireGuard configuration in the provider's portal.")
        .split("then add it as a channel")[0]);
    return { text: `${base}. You'll add the .conf as a channel next.`, url: p.url };
  }
  return {
    text: p.help || "Generate an access token in your provider's account portal, then paste it below.",
    url: p.url,
  };
}

function providerCard(p) {
  const logo = providerIcon(p.provider) ? `<img src="${providerIcon(p.provider)}" alt="">` : `<span class="prov-fallback">${esc(p.display_name[0])}</span>`;
  const tile = `<button class="prov-tile" data-provider="${esc(p.provider)}" title="${esc(p.display_name)}" aria-label="${esc(p.display_name)}">${logo}</button>`;
  // Token providers get a gear to manage (replace) their stored credential; a
  // sibling button, since it can't nest inside the tile button.
  const gear = p.kind === "token"
    ? `<button class="prov-gear" data-settings="${esc(p.provider)}" title="Provider settings" aria-label="${esc(p.display_name)} settings">${GEAR}</button>`
    : "";
  return `<div class="prov-tile-wrap">${tile}${gear}</div>`;
}

// Summarize a token replacement for a toast: how many channels were re-resolved
// with the new credential (and how many kept their old server pending reconnect).
function tokenReplaceToast(data) {
  const ch = data?.channels || { resolved: [], failed: [] };
  const parts = [`Updated ${data?.display_name || "provider"}.`];
  if (ch.resolved?.length) parts.push(`Re-resolved ${ch.resolved.length} channel(s).`);
  if (ch.failed?.length) parts.push(`${ch.failed.length} will retry on reconnect.`);
  return parts.join(" ");
}

async function openAddChannel() {
  await ensureCatalog();
  const m = modal("Add channel", `<div id="wizard"></div>`);
  m.root.querySelector(".modal").classList.add("wide");
  const wiz = m.root.querySelector("#wizard");
  const st = { provider: null, country: null, city: null, countriesData: null, citiesForCountry: [] };

  async function renderProviders() {
    const provRes = await api.get("/api/v1/providers");
    const added = provRes.ok ? provRes.data.providers : [];
    const cards = added.map(providerCard).join("");
    const addTile = `<button class="prov-tile add" data-add-provider title="Add provider" aria-label="Add provider"><span class="prov-add">+</span></button>`;
    wiz.innerHTML = `<div class="provider-row">${cards}${addTile}</div>`;
    wiz.onclick = (e) => {
      const gear = e.target.closest("[data-settings]");
      if (gear) { e.stopPropagation(); return renderProviderSettings(added.find((p) => p.provider === gear.dataset.settings)); }
      const card = e.target.closest("[data-provider]");
      if (card) { st.provider = card.dataset.provider; return renderForProvider(); }
      if (e.target.closest("[data-add-provider]")) return renderAddProvider();
    };
  }

  // Provider settings: manage an already-added token provider's credential. The
  // token is write-only — never fetched or pre-filled, only its presence/mask is
  // shown — and the field is dropped from the DOM on every re-render.
  function renderProviderSettings(p) {
    if (!p) return renderProviders();
    const cat = catalog.find((x) => x.provider === p.provider) || {};
    const g = providerGuide(cat);
    const status = p.has_token
      ? `Token stored (<code>${esc(p.credential || "present")}</code>).`
      : "No token stored.";
    wiz.innerHTML = `<form id="ps">
      <p class="field-guide"><b>${esc(p.display_name)}</b> — ${status}${g.url ? ` <a href="${esc(g.url)}" target="_blank" rel="noopener">Open portal ↗</a>` : ""}</p>
      <label class="field"><span>Replace token</span><input name="token" type="password" autocomplete="off" spellcheck="false" placeholder="paste a new token"></label>
      <p class="hint">Replacing the token re-resolves this provider's channels with the new credential. The token is never shown back.</p>
      <p class="form-err" id="pserr"></p>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Replace token</button></div>
    </form>`;
    wiz.onclick = null;
    wiz.querySelector("[data-back]").onclick = renderProviders;
    wiz.querySelector("#ps").onsubmit = async (e) => {
      e.preventDefault();
      const err = wiz.querySelector("#pserr"); err.textContent = "";
      const input = wiz.querySelector('[name="token"]');
      const token = input.value.trim();
      if (!token) { err.textContent = "Enter a token."; return; }
      const res = await api.post(`/api/v1/providers/${p.provider}/token`, { creds: { token } });
      input.value = "";  // never keep the token in the DOM after submit
      if (!res.ok) { err.textContent = res.error; return; }
      if (res.data?.unchanged) toast(`${res.data.display_name} already has that token.`, "warn");
      else toast(tokenReplaceToast(res.data));
      refreshStatus();
      renderProviders();
    };
  }

  function renderForProvider() {
    const p = catalog.find((x) => x.provider === st.provider) || {};
    return p.kind === "config" ? renderConfigStep(p) : renderCountryStep();
  }

  async function renderAddProvider() {
    const provRes = await api.get("/api/v1/providers");
    const added = provRes.ok ? provRes.data.providers : [];
    const addable = catalog.filter((p) => !added.some((a) => a.provider === p.provider));
    if (!addable.length) {
      wiz.innerHTML = `<div class="empty inset">All supported providers are already added.</div><div class="confirm-actions"><button class="btn" type="button" data-back>Back</button></div>`;
      wiz.querySelector("[data-back]").onclick = renderProviders;
      return;
    }
    const opts = addable.map((p) => ({ value: p.provider, label: p.display_name }));
    wiz.innerHTML = `<form id="pf">
      <label class="field"><span>Provider</span>${customSelectHTML("prov", opts, opts[0].value)}</label>
      <div id="guide"></div><div id="pfields"></div>
      <p class="form-err" id="perr"></p>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Add provider</button></div>
    </form>`;
    wireCustomSelects(wiz);
    const sel = wiz.querySelector("#prov"), guide = wiz.querySelector("#guide"), fields = wiz.querySelector("#pfields"), err = wiz.querySelector("#perr");
    const sync = () => {
      const p = catalog.find((x) => x.provider === sel.value);
      const g = providerGuide(p);
      const guideLink = g.url ? ` <a href="${esc(g.url)}" target="_blank" rel="noopener">Open portal ↗</a>` : "";
      guide.innerHTML = `<p class="field-guide">${esc(g.text)}${guideLink}</p>`;
      fields.innerHTML = p.kind === "config" ? "" : p.fields.map((f) => `<label class="field"><span>${esc(f.label)}</span><input name="${esc(f.key)}" type="${f.secret ? "password" : "text"}" autocomplete="off" spellcheck="false"></label>`).join("");
    };
    sel.onchange = sync; sync();
    wiz.querySelector("[data-back]").onclick = renderProviders;
    wiz.querySelector("#pf").onsubmit = async (e) => {
      e.preventDefault(); err.textContent = "";
      const p = catalog.find((x) => x.provider === sel.value);
      const body = { provider: p.provider, creds: {} };
      if (p.kind !== "config") p.fields.forEach((f) => { body.creds[f.key] = wiz.querySelector(`[name="${f.key}"]`).value.trim(); });
      const res = await api.post("/api/v1/providers", body);
      if (!res.ok) { err.textContent = res.error; return; }
      toast(`Added ${res.data.display_name || p.display_name}.`);
      st.provider = p.provider;
      renderForProvider();
    };
  }

  function renderConfigStep(p) {
    const g = providerGuide(p);
    wiz.innerHTML = `<form id="cf">
      <p class="field-guide">${esc(g.text)}${g.url ? ` <a href="${esc(g.url)}" target="_blank" rel="noopener">Open portal ↗</a>` : ""}</p>
      <label class="field"><span>WireGuard .conf</span>
        <div class="filepicker">
          <button class="btn" type="button" data-pick>Choose file</button>
          <span class="file-name" data-file-name>No file chosen</span>
          <input id="conf" type="file" accept=".conf,text/plain" hidden>
        </div></label>
      <label class="field"><span>Label <em>(optional)</em></span><input id="label" placeholder="e.g. Streaming — US" spellcheck="false"></label>
      <p class="form-err" id="cerr"></p>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Add Channel</button></div>
    </form>`;
    const conf = wiz.querySelector("#conf"), fname = wiz.querySelector("[data-file-name]");
    wiz.querySelector("[data-pick]").onclick = () => conf.click();
    conf.onchange = () => { fname.textContent = conf.files[0] ? conf.files[0].name : "No file chosen"; };
    wiz.querySelector("[data-back]").onclick = renderProviders;
    wiz.querySelector("#cf").onsubmit = async (e) => {
      e.preventDefault();
      const err = wiz.querySelector("#cerr"); err.textContent = "";
      const file = conf.files[0];
      if (!file) { err.textContent = "Choose a .conf file."; return; }
      const body = { provider: st.provider, label: wiz.querySelector("#label").value.trim(), conf_name: file.name, conf_text: await file.text() };
      const res = await api.post("/api/v1/channels", body);
      if (!res.ok) { err.textContent = res.error; return; }
      const name = res.data?.channel?.label || res.data?.channel?.id || "channel";
      m.close();
      // A byte-identical re-upload of an existing .conf is a no-op — tell the
      // user the channel already exists instead of a misleading "Added".
      if (res.data?.unchanged) toast(`${name} already exists with identical settings.`, "warn");
      else toast(`${res.data?.updated ? "Updated" : "Added"} ${name}.`);
      refreshStatus();
    };
  }

  async function renderCountryStep() {
    wiz.innerHTML = `<p class="field-guide">Pick a country for the new channel.</p><div class="loc-loading">Loading countries…</div>`;
    if (!st.countriesData) {
      const res = await api.get(`/api/v1/locations?provider=${encodeURIComponent(st.provider)}`);
      if (!res.ok) { wiz.innerHTML = `<p class="form-err">${esc(res.error)}</p><div class="confirm-actions"><button class="btn" type="button" data-back>Back</button></div>`; wiz.querySelector("[data-back]").onclick = renderProviders; return; }
      st.countriesData = res.data;
    }
    const data = st.countriesData;
    if (!data.available) {
      wiz.innerHTML = `<p class="field-guide">${esc(data.help || "This provider does not expose a locations API.")}</p><div class="confirm-actions"><button class="btn" type="button" data-back>Back</button></div>`;
      wiz.querySelector("[data-back]").onclick = renderProviders;
      return;
    }
    const items = data.countries.map((c) => `<button class="loc-card" data-country="${esc(c.country)}"><b>${esc(c.country)}</b><small>${c.cities.length} ${c.cities.length === 1 ? "city" : "cities"}</small></button>`).join("");
    wiz.innerHTML = `<p class="field-guide">Pick a country.</p>
      <input class="loc-search" id="loc-search" placeholder="Filter countries…" spellcheck="false" autocomplete="off">
      <div class="loc-grid" id="loc-grid">${items}</div>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button></div>`;
    const grid = wiz.querySelector("#loc-grid");
    wiz.querySelector("#loc-search").oninput = (e) => {
      const q = e.target.value.toLowerCase();
      [...grid.children].forEach((b) => { b.style.display = b.dataset.country.toLowerCase().includes(q) ? "" : "none"; });
    };
    wiz.querySelector("[data-back]").onclick = renderProviders;
    grid.onclick = (e) => {
      const card = e.target.closest("[data-country]");
      if (!card) return;
      st.country = card.dataset.country;
      st.citiesForCountry = data.countries.find((c) => c.country === st.country).cities;
      renderCityStep();
    };
    wiz.querySelector("#loc-search").focus();
  }

  function renderCityStep() {
    const anyCard = `<button class="loc-card" data-city=""><b>Any city</b><small>fastest available</small></button>`;
    const items = st.citiesForCountry.map((ci) => `<button class="loc-card" data-city="${esc(ci)}"><b>${esc(ci)}</b></button>`).join("");
    wiz.innerHTML = `<p class="field-guide">Pick a city in ${esc(st.country)} — or any.</p>
      <input class="loc-search" id="loc-search" placeholder="Filter cities…" spellcheck="false" autocomplete="off">
      <div class="loc-grid" id="loc-grid">${anyCard}${items}</div>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button></div>`;
    const grid = wiz.querySelector("#loc-grid");
    wiz.querySelector("#loc-search").oninput = (e) => {
      const q = e.target.value.toLowerCase();
      [...grid.children].forEach((b) => { const v = (b.dataset.city || "any city"); b.style.display = v.toLowerCase().includes(q) ? "" : "none"; });
    };
    wiz.querySelector("[data-back]").onclick = renderCountryStep;
    grid.onclick = (e) => {
      const card = e.target.closest("[data-city]");
      if (!card) return;
      st.city = card.dataset.city;
      renderLabelStep();
    };
    wiz.querySelector("#loc-search").focus();
  }

  function renderLabelStep() {
    const where = st.city ? `${st.city}, ${st.country}` : st.country;
    wiz.innerHTML = `<form id="lf">
      <p class="field-guide">New channel in <b>${esc(where)}</b>.</p>
      <label class="field"><span>Label <em>(optional)</em></span><input id="label" placeholder="e.g. Streaming — US" spellcheck="false"></label>
      <p class="form-err" id="lerr"></p>
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Add Channel</button></div>
    </form>`;
    wiz.querySelector("[data-back]").onclick = renderCityStep;
    wiz.querySelector("#lf").onsubmit = async (e) => {
      e.preventDefault();
      const err = wiz.querySelector("#lerr"); err.textContent = "";
      const body = { provider: st.provider, country: st.country, city: st.city, label: wiz.querySelector("#label").value.trim() };
      const res = await api.post("/api/v1/channels", body);
      if (!res.ok) { err.textContent = res.error; return; }
      m.close(); toast(`Added ${body.label || res.data?.channel?.id || "channel"}.`); refreshStatus();
    };
    wiz.querySelector("#label").focus();
  }

  await renderProviders();
}

function routeTargetOptions() {
  return [
    { value: "direct", label: "Direct — No VPN" },
    { value: "block", label: "Block — Drop Traffic" },
    ...((status?.channels || []).map((c) => ({ value: chanKey(c), label: c.label || c.name }))),
  ];
}

function matcherInputHTML(buttonText) {
  return `<div class="match-group">
      <label class="field check" id="match-all"><input type="checkbox" id="all-traffic"><span><b>All traffic</b> — match everything (catch-all).</span></label>
      <div class="or-divider" aria-hidden="true"><span>or</span></div>
      <label class="field" id="match-list"><span>Specific domains / IPs (one per line)</span><textarea id="matchers" placeholder="netflix.com\napi.openai.com\n10.8.0.0/16"></textarea>
        <div class="match-examples">
          <div class="mx-title">Examples (a domain matches itself and all its subdomains):</div>
          <div class="mx-item"><code>netflix.com</code>the domain plus any <code>*.netflix.com</code></div>
          <div class="mx-item"><code>api.anthropic.com</code>that host plus anything under it</div>
          <div class="mx-item"><code>185.98.169.31</code>that exact IPv4 address</div>
          <div class="mx-item"><code>185.81.1.1/16</code>the whole 185.81.0.0/16 IPv4 CIDR range</div>
          <div class="mx-item"><code>2001:db8::1</code>that exact IPv6 address</div>
          <div class="mx-item"><code>2001:db8::/32</code>the 2001:db8::/32 IPv6 CIDR range</div>
        </div></label>
    </div>
    <p class="form-err" id="err"></p><button class="btn primary" type="submit">${esc(buttonText)}</button>`;
}

function wireMatchers(root) {
  const cb = root.querySelector("#all-traffic");
  const ta = root.querySelector("#matchers");
  const list = root.querySelector("#match-list");
  if (!cb || !ta) return;
  const sync = () => {
    const on = cb.checked;
    ta.disabled = on;
    if (on) ta.value = "";
    list?.classList.toggle("is-disabled", on);
  };
  cb.onchange = sync;
  sync();
}

function matcherEntries(root) {
  if (root.querySelector("#all-traffic")?.checked) return [{ value: "all" }];
  return root.querySelector("#matchers").value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean).map((value) => ({ value }));
}

function openEditRuleset(rulesetId) {
  const rs = rulesets.find((r) => r.id === rulesetId);
  if (!rs) return;
  const opts = routeTargetOptions();
  const via = opts.some((o) => o.value === rs.target) ? rs.target : (opts[0]?.value || "direct");
  const curName = rs.name && rs.name !== rs.target ? rs.name : "";
  const hasAll = (rs.rules || []).some((r) => r.type === "all");
  const curMatchers = (rs.rules || []).filter((r) => r.type !== "all").map((r) => r.value || "").join("\n");
  const m = modal("Edit ruleset", `<form id="rf">
    <label class="field"><span>Name</span><input id="name" placeholder="Streaming" spellcheck="false"></label>
    <label class="field"><span>Via channel</span>${customSelectHTML("target", opts, via)}</label>
    ${matcherInputHTML("Save")}
  </form>`);
  wireCustomSelects(m.root);
  m.root.querySelector("#name").value = curName;
  m.root.querySelector("#all-traffic").checked = hasAll;
  m.root.querySelector("#matchers").value = curMatchers;
  wireMatchers(m.root);
  const err = m.root.querySelector("#err");
  m.root.querySelector("#rf").onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    const name = m.root.querySelector("#name").value.trim();
    if (!name) { err.textContent = "Name is required."; return; }
    const matchers = matcherEntries(m.root);
    if (!matchers.length) { err.textContent = "Add at least one matcher."; return; }
    const target = m.root.querySelector("#target").value;
    const btn = m.root.querySelector('button[type="submit"]');
    btn.disabled = true; btn.textContent = "Saving…";
    const res = await api.post(`/api/v1/routes/rulesets/${encodeURIComponent(rulesetId)}/update`, { name, target, matchers });
    if (!res.ok) { err.textContent = res.error; btn.disabled = false; btn.textContent = "Save"; return; }
    m.close(); toast(`Saved ${name}.`); refreshRoutes(); refreshStatus();
  };
  m.root.querySelector("#name").focus();
}

function openAddRule() {
  const m = modal("Add ruleset", `<form id="rf">
    <label class="field"><span>Name</span><input id="name" placeholder="Streaming" spellcheck="false"></label>
    <label class="field"><span>Via channel</span>${customSelectHTML("target", routeTargetOptions(), routeTargetOptions()[0]?.value || "direct")}</label>
    ${matcherInputHTML("Create ruleset")}
  </form>`);
  wireCustomSelects(m.root);
  wireMatchers(m.root);
  const err = m.root.querySelector("#err");
  m.root.querySelector("#rf").onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    const name = m.root.querySelector("#name").value.trim();
    if (!name) { err.textContent = "Name is required."; return; }
    const matchers = matcherEntries(m.root);
    if (!matchers.length) { err.textContent = "Add at least one matcher."; return; }
    const target = m.root.querySelector("#target").value;
    const res = await api.post("/api/v1/routes/rulesets", { name, target, matchers });
    if (!res.ok) { err.textContent = res.error; return; }
    m.close(); toast(`Created ${res.data.ruleset.name}.`); refreshRoutes(); refreshStatus();
  };
  m.root.querySelector("#name").focus();
}
