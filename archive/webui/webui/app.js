"use strict";

const $ = (sel) => document.querySelector(sel);
const api = {
  async get(p) { return (await fetch(p)).json(); },
  async send(method, p, body) {
    const r = await fetch(p, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
    return data;
  },
};

const state = {
  channels: [],
  providers: [],   // configured providers (with token_set / preview / brand)
  catalog: [],     // all supported providers (for Add provider)
  locations: {},
  conn: {},   // id -> { latency, ipv4, ipv6 }
  health: {}, // id -> last seen health (to detect "just connected")
  failed: {}, // id -> true if last connect attempt failed (keep error/log link until a success)
  busy: {},
  view: "channels",
  editingId: null,
  provEditing: null,
};

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function iconStyle(b) {
  const icon = b && b.icon ? b.icon : "_generic";
  const color = (b && b.color) || "#64748b";
  return `--brand:${color};--icon:url('/icons/${icon}.svg')`;
}

// inline line-icons (inherit currentColor, scale with the button)
const _svg = (inner) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const ICON = {
  copy: _svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  gear: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84a.48.48 0 0 0-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.48.48 0 0 0-.59.22L2.74 8.87a.49.49 0 0 0 .12.61l2.03 1.58c-.05.3-.07.62-.07.94 0 .32.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.13.22.38.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.06.24.25.41.48.41h3.84c.23 0 .43-.17.48-.41l.36-2.54c.59-.24 1.12-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.61l-2.01-1.58zM12 15.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2z"/></svg>',
  logs: _svg('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>'),
  x: _svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
};

// ---- channel card ----------------------------------------------------------
function healthClass(ch) {
  const s = ch.status || {};
  if (s.state === "running" && s.health === "healthy") return "ok";
  if (s.state === "running" && s.health === "starting") return "warn";
  if (s.state === "missing") return "bad";
  if (s.state === "running") return "bad";
  return "warn";
}

function cardHTML(ch) {
  const s = ch.status || {};
  const busy = state.busy[ch.id];
  const b = ch.providerBrand || {};
  const c = state.conn[ch.id] || {};
  const latText = !ch.enabled || c.latency == null ? "—" : Math.round(c.latency) + " ms";
  const ips = ch.enabled ? [c.ipv4, c.ipv6].filter(Boolean) : [];
  const ipHTML = ips.length
    ? `<div class="exit-ips">${ips.map((ip) =>
      `<a href="https://whatismyipaddress.com/ip/${encodeURIComponent(ip)}" target="_blank" rel="noreferrer">${esc(ip)}</a>`
    ).join("")}</div>`
    : "";
  const cls = ["card", "channel"];
  if (!ch.enabled) cls.push("disabled");
  if (busy) cls.push("busy");
  const dis = busy ? "disabled" : "";
  const sw = busy === "toggle"
    ? '<span class="spinner"></span>'
    : `<span class="txt">${ch.enabled ? "ON" : "OFF"}</span>`;
  const dot = ch.enabled ? `<span class="health-dot ${healthClass(ch)}"></span>` : "";
  const failing = (ch.enabled && healthClass(ch) === "bad") || !!state.failed[ch.id];
  const name = ch.providerName || ch.provider;
  return `
  <article class="${cls.join(" ")}" data-id="${ch.id}">
    <div class="card-head">
      <div class="badge" title="${esc(name)}"><span class="prov-icon" style="${iconStyle(b)}"></span>${dot}</div>
      <div class="head-main">
        <h3 class="c-title">${esc(ch.country)}${ch.city ? ` <span class="c-sub">${esc(ch.city)}</span>` : ""}</h3>
        ${ch.name ? `<div class="pills"><span class="pill name-pill">${esc(ch.name)}</span></div>` : ""}
      </div>
      <div class="head-actions">
        <button class="icon" data-act="logs" title="View log">${ICON.logs}</button>
        <button class="icon" data-act="edit" ${dis} title="Edit">${ICON.gear}</button>
        <button class="icon danger" data-act="delete" ${dis} title="Delete">${ICON.x}</button>
        <button class="switch ${ch.enabled ? "on" : ""}" data-act="toggle" ${dis}
                title="${ch.enabled ? "Disable" : "Enable"}">${sw}</button>
      </div>
    </div>
    <div class="card-body">
      <div class="bcol"><span class="bcol-lbl">Proxy</span><span class="bcol-val mono">127.0.0.1:${ch.port}
        <button class="copy" data-act="copy" title="Copy proxy URL">${ICON.copy}</button></span></div>
      <div class="bcol"><span class="bcol-lbl">IP</span>
        ${ipHTML || `<span class="bcol-val dim">—</span>`}</div>
      <div class="bcol"><span class="bcol-lbl">Latency</span><span class="bcol-val mono">${latText}</span></div>
      <div class="bcol"><span class="bcol-lbl">Uptime</span><span class="bcol-val mono">${esc(s.uptime || "—")}</span></div>
      <div class="bcol"><span class="bcol-lbl">Data rx / tx</span><span class="bcol-val mono">${esc(s.netio || "—")}</span></div>
    </div>
    ${failing ? `<button class="card-error" data-act="logs">⚠ Not connecting — view log</button>` : ""}
  </article>`;
}

function render() {
  const empty = !state.channels.length;
  $("#empty").classList.toggle("hidden", !empty);
  $("#add-tile").classList.toggle("hidden", empty);
  $("#cards").innerHTML = empty ? "" : state.channels.map(cardHTML).join("");
}
function patchCard(id) {
  const node = document.querySelector(`.card[data-id="${id}"]`);
  const ch = state.channels.find((c) => c.id === id);
  if (node && ch) node.outerHTML = cardHTML(ch);
}

// ---- data ------------------------------------------------------------------
async function refresh(full = false) {
  const channels = await api.get("/api/channels");
  const sameSet = JSON.stringify(channels.map((c) => c.id).sort()) ===
    JSON.stringify(state.channels.map((c) => c.id).sort());
  // a channel that just transitioned to healthy -> probe its exit IP + latency once
  let newlyHealthy = false;
  for (const ch of channels) {
    const h = (ch.status || {}).health;
    if (h === "healthy") {
      if (state.health[ch.id] !== "healthy") newlyHealthy = true;
      delete state.failed[ch.id]; // a successful connection clears the last failure
    }
    state.health[ch.id] = h;
  }
  state.channels = channels;
  if (full || !sameSet) render();
  else for (const ch of channels) patchCard(ch.id);
  if (newlyHealthy) probeConnectivity(true);
  resumePending();
}

// A channel that is enabled but still in Docker's "starting" state has never
// connected yet (e.g. the page was reloaded mid-connect). Resume the same
// connect animation + wait so it resolves to ON or reverts to OFF — the
// transient state no longer lives only in this tab.
function resumePending() {
  for (const ch of state.channels) {
    if (ch.enabled && !state.busy[ch.id] && (ch.status || {}).health === "starting") {
      state.busy[ch.id] = "toggle";
      patchCard(ch.id);
      confirmConnected(ch.id);
    }
  }
}
async function loadProviders() { state.providers = await api.get("/api/providers"); }
async function loadInfo() {
  const info = await api.get("/api/info");
  const v = info.version ? "v" + info.version : "";
  $("#version").textContent = v;
  $("#about-ver").textContent = v;
}
async function ensureLocations(provider) {
  if (!state.locations[provider]) {
    const data = await api.get(`/api/locations?provider=${encodeURIComponent(provider)}`);
    state.locations[provider] = data.countries || {};
  }
  return state.locations[provider];
}

// ---- views -----------------------------------------------------------------
const TITLES = { channels: "VPN Channels", providers: "Providers", about: "About" };
function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + view).classList.remove("hidden");
  $("#page-title").textContent = TITLES[view];
  const ch = view === "channels", pr = view === "providers";
  $("#top-actions").style.display = ch || pr ? "flex" : "none";
  $("#btn-latency").classList.toggle("hidden", !ch);
  $("#btn-new").classList.toggle("hidden", !ch);
  $("#btn-add-provider").classList.toggle("hidden", !pr);
  if (pr) loadProviders().then(renderProviders).catch((e) => toast(e.message, true));
}

function renderProviders() {
  const list = state.providers || [];
  $("#providers-list").innerHTML = list.map((p) => `
    <article class="card prov-card" data-key="${p.key}">
      <div class="badge"><span class="prov-icon" style="${iconStyle(p)}"></span></div>
      <div class="prov-meta">
        <div class="p-name">${esc(p.name)}</div>
        <div class="p-token">${p.token_set ? "token " + esc(p.token_preview) : "no token set"}</div>
      </div>
      <div class="prov-actions">
        <span class="pill dot-pill ${p.token_set ? "ok" : "bad"}" data-role="status">${p.token_set ? "configured" : "no token"}</span>
        <button class="btn outline sm" data-pact="validate">Validate</button>
        <button class="btn outline sm" data-pact="update">Update token</button>
        <button class="icon danger" data-pact="remove" title="Remove">🗑</button>
      </div>
    </article>`).join("") ||
    `<div class="notice"><p>No providers yet. Add one with its access token to start creating channels.</p>
       <button class="btn primary" id="prov-empty-add">Add a provider</button></div>`;
  const ea = $("#prov-empty-add");
  if (ea) ea.addEventListener("click", () => openProviderModal());
}

async function onProvidersClick(e) {
  const btn = e.target.closest("[data-pact]");
  if (!btn) return;
  const card = e.target.closest(".prov-card");
  const key = card.dataset.key;
  const act = btn.dataset.pact;
  if (act === "update") return openProviderModal(key);
  if (act === "remove") {
    if (!confirm(`Remove ${key}? Any channels using it must be deleted first.`)) return;
    try { await api.send("DELETE", `/api/providers/${key}`); await loadProviders(); renderProviders(); toast("Provider removed"); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (act === "validate") {
    const status = card.querySelector('[data-role="status"]');
    btn.disabled = true; const o = btn.textContent; btn.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api.send("POST", `/api/providers/${key}/validate`);
      toast(r.valid ? "Token is valid" : `Invalid token: ${r.error}`, !r.valid);
      if (status) { status.className = "pill dot-pill " + (r.valid ? "ok" : "bad"); status.textContent = r.valid ? "valid" : "invalid"; }
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; btn.textContent = o; }
  }
}

// ---- provider modal --------------------------------------------------------
async function openProviderModal(presetKey) {
  state.provEditing = presetKey || null;
  if (!state.catalog.length) state.catalog = await api.get("/api/providers/catalog");
  const sel = $("#p-provider");
  sel.innerHTML = state.catalog.map((c) => `<option value="${c.key}">${esc(c.name)}</option>`).join("");
  sel.disabled = !!presetKey;
  if (presetKey) sel.value = presetKey;
  $("#prov-title").textContent = presetKey ? "Update token" : "Add provider";
  $("#p-token").value = "";
  $("#prov-error").classList.add("hidden");
  updateProvHint();
  $("#prov-scrim").classList.remove("hidden");
  $("#prov-modal").classList.remove("hidden");
  $("#p-token").focus();
}
function closeProviderModal() {
  $("#prov-modal").classList.add("hidden");
  $("#prov-scrim").classList.add("hidden");
}
function updateProvHint() {
  const hints = {
    nordvpn: 'NordVPN: <a href="https://my.nordaccount.com" target="_blank" rel="noreferrer">my.nordaccount.com</a> → Manual setup → generate an access token.',
  };
  $("#p-hint").innerHTML = hints[$("#p-provider").value] ||
    "Paste the provider's access token. It is stored only on this machine.";
}
async function submitProvider(e) {
  e.preventDefault();
  const provider = $("#p-provider").value;
  const token = $("#p-token").value.trim();
  if (!token) { const el = $("#prov-error"); el.textContent = "Enter the access token."; return el.classList.remove("hidden"); }
  const btn = $("#p-submit"); btn.disabled = true; const o = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span> Checking…';
  try {
    await api.send("POST", "/api/providers", { provider, token });
    closeProviderModal();
    await loadProviders();
    if (state.view === "providers") renderProviders();
    toast("Provider saved");
  } catch (err) {
    const el = $("#prov-error"); el.textContent = err.message; el.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

// ---- channel drawer --------------------------------------------------------
async function openDrawer(channel) {
  await loadProviders();
  const usable = state.providers.filter((p) => p.token_set);
  state.editingId = channel ? channel.id : null;
  $("#drawer-title").textContent = channel ? "Edit channel" : "Add channel";
  $("#scrim").classList.remove("hidden");
  $("#drawer").classList.remove("hidden");

  if (!usable.length && !channel) {
    $("#no-provider").classList.remove("hidden");
    $("#channel-form").classList.add("hidden");
    return;
  }
  $("#no-provider").classList.add("hidden");
  $("#channel-form").classList.remove("hidden");
  $("#f-submit").textContent = channel ? "Save changes" : "Add channel";
  $("#form-error").classList.add("hidden");
  $("#form-note").classList.add("hidden");

  const opts = usable.slice();
  if (channel && !opts.find((p) => p.key === channel.provider))
    opts.push({ key: channel.provider, name: channel.providerName || channel.provider });
  const fp = $("#f-provider");
  fp.innerHTML = opts.map((p) => `<option value="${p.key}">${esc(p.name)}</option>`).join("");
  fp.value = channel ? channel.provider : opts[0].key;
  await populateCountries(channel ? channel.country : "");
  await populateCities(channel ? channel.city : "");
}
function closeDrawer() { $("#drawer").classList.add("hidden"); $("#scrim").classList.add("hidden"); }
async function populateCountries(selected) {
  const countries = await ensureLocations($("#f-provider").value);
  const fc = $("#f-country");
  fc.innerHTML = `<option value="">Select a country</option>` +
    Object.keys(countries).sort().map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  fc.disabled = false;
  if (selected) fc.value = selected;
}
async function populateCities(selected) {
  const provider = $("#f-provider").value;
  const country = $("#f-country").value;
  const cities = (state.locations[provider] || {})[country] || [];
  const fc = $("#f-city");
  fc.innerHTML = `<option value="">Any city</option>` +
    cities.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  fc.disabled = !country;
  if (selected) fc.value = selected;
}
function maybeRelocationNote() {
  const note = $("#form-note");
  if (!state.editingId) return note.classList.add("hidden");
  const ch = state.channels.find((c) => c.id === state.editingId);
  if (!ch) return;
  const changed = $("#f-provider").value !== ch.provider ||
    $("#f-country").value !== ch.country || $("#f-city").value !== (ch.city || "");
  note.textContent = "Changing location restarts this channel (others are untouched).";
  note.classList.toggle("hidden", !changed);
}
async function submitForm(e) {
  e.preventDefault();
  const editingId = state.editingId;
  const before = editingId ? state.channels.find((c) => c.id === editingId) : null;
  const payload = {
    provider: $("#f-provider").value,
    country: $("#f-country").value,
    city: $("#f-city").value,
  };
  if (!payload.country) { const el = $("#form-error"); el.textContent = "Pick a country."; return el.classList.remove("hidden"); }
  const submit = $("#f-submit"); submit.disabled = true; submit.innerHTML = '<span class="spinner"></span>';
  try {
    if (editingId) await api.send("PUT", `/api/channels/${editingId}`, payload);
    else await api.send("POST", "/api/channels", payload);
  } catch (err) {
    const el = $("#form-error"); el.textContent = err.message; el.classList.remove("hidden");
    submit.disabled = false; submit.textContent = editingId ? "Save changes" : "Add channel";
    return;
  }
  submit.disabled = false; submit.textContent = editingId ? "Save changes" : "Add channel";
  closeDrawer();

  const relocated = before &&
    (payload.provider !== before.provider || payload.country !== before.country ||
      payload.city !== (before.city || ""));
  if (editingId && relocated && before.enabled) {
    // location changed on a live channel: the new server may not connect, so run
    // the connect animation again and fall back to OFF if it can't establish.
    state.busy[editingId] = "toggle";
    await refresh(true);
    toast("Updated — reconnecting…");
    await confirmConnected(editingId);
  } else {
    await refresh(true);
    toast(editingId ? "Channel updated" : "Channel created");
  }
}

// ---- card actions ----------------------------------------------------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CONNECT_TIMEOUT = 50000; // ms to wait for a channel to become healthy

async function onCardsClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = e.target.closest(".card").dataset.id;
  const ch = state.channels.find((c) => c.id === id);
  const act = btn.dataset.act;
  if (act === "logs") return openLogModal(id);
  if (act === "edit") return openDrawer(ch);
  if (act === "copy") {
    navigator.clipboard?.writeText(`http://127.0.0.1:${ch.port}`);
    return toast(`Copied http://127.0.0.1:${ch.port}`);
  }
  if (state.busy[id]) return;
  if (act === "toggle") return toggleChannel(id, ch);
  if (act === "delete") {
    if (!confirm(`Delete channel on port ${ch.port} (${ch.label})? This removes its container.`)) return;
    return deleteChannel(id);
  }
}

// Poll until the channel reports healthy; return false on timeout / gone / disabled.
async function waitForHealthy(id, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(2500);
    let chans;
    try { chans = await api.get("/api/channels"); } catch { continue; }
    state.channels = chans;
    const ch = chans.find((c) => c.id === id);
    if (!ch || !ch.enabled) return false;
    if ((ch.status || {}).health === "healthy") return true;
    patchCard(id); // keep the spinner; refresh other fields
  }
  return false;
}

// Channel is enabled and (re)starting (spinner already shown). Wait until healthy;
// if it never connects, turn it back OFF and leave it off.
async function confirmConnected(id) {
  const ok = await waitForHealthy(id, CONNECT_TIMEOUT);
  delete state.busy[id];
  if (!ok) {
    state.failed[id] = true; // keep the failure + log link visible until a success
    await api.send("POST", `/api/channels/${id}/disable`).catch(() => { });
    await refresh(true);
    return toast("Couldn't establish a connection — left off", true);
  }
  await refresh(true);
  probeOne(id); // immediately read this channel's IP + latency — no Test click needed
  toast("Channel connected");
}

// Probe just one channel's connectivity (fast, used right after it connects).
async function probeOne(id) {
  try {
    const r = await api.send("POST", `/api/connectivity?id=${encodeURIComponent(id)}`);
    Object.assign(state.conn, r);
    patchCard(id);
  } catch (err) {
    /* ignore — the heartbeat will retry */
  }
}

async function toggleChannel(id, ch) {
  const enabling = !ch.enabled;
  state.busy[id] = "toggle";
  patchCard(id);
  try {
    await api.send("POST", `/api/channels/${id}/${enabling ? "enable" : "disable"}`);
    if (enabling) return confirmConnected(id); // spinner stays until healthy / reverts to OFF
    delete state.conn[id]; // clear IP/latency when turned off
    delete state.busy[id];
    await refresh(true);
    toast("Channel disabled"); // disable request already waits for the container to stop
  } catch (err) {
    delete state.busy[id];
    patchCard(id);
    toast(err.message, true);
  }
}

async function deleteChannel(id) {
  state.busy[id] = "delete";
  patchCard(id);
  try {
    await api.send("DELETE", `/api/channels/${id}`);
    delete state.conn[id];
    delete state.failed[id];
    delete state.busy[id];
    await refresh(true);
    toast("Channel deleted");
  } catch (err) {
    delete state.busy[id];
    patchCard(id);
    toast(err.message, true);
  }
}

// ---- log modal -------------------------------------------------------------
let logChannelId = null;
async function openLogModal(id) {
  logChannelId = id;
  const ch = state.channels.find((c) => c.id === id);
  $("#log-title").textContent = `Log — ${ch ? ch.label : id}`;
  $("#log-error").classList.add("hidden");
  $("#log-body").textContent = "loading…";
  $("#log-scrim").classList.remove("hidden");
  $("#log-modal").classList.remove("hidden");
  loadLog(id);
}
async function loadLog(id) {
  try {
    const r = await api.get(`/api/channels/${id}/logs`);
    const body = $("#log-body");
    body.textContent = r.logs || "(no logs)";
    const e = $("#log-error");
    if (r.issue) {
      const notice = r.issue.level === "notice";
      e.textContent = (notice ? "ⓘ " : "⚠ ") + r.issue.message;
      e.className = "log-error" + (notice ? " notice" : "");
    } else {
      e.className = "log-error hidden";
    }
    body.scrollTop = body.scrollHeight;
  } catch (err) {
    $("#log-body").textContent = err.message;
  }
}
function closeLogModal() {
  $("#log-modal").classList.add("hidden");
  $("#log-scrim").classList.add("hidden");
}

let probing = false;
async function probeConnectivity(silent) {
  if (probing) return;
  probing = true;
  try {
    Object.assign(state.conn, (await api.send("POST", "/api/connectivity")) || {});
    render();
    if (!silent) toast("Connectivity tested");
  } catch (err) {
    if (!silent) toast(err.message, true);
  } finally {
    probing = false;
  }
}
async function runConnectivity() {
  const btn = $("#btn-latency"); const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Testing…';
  await probeConnectivity(false);
  btn.disabled = false; btn.innerHTML = orig;
}

// ---- toast -----------------------------------------------------------------
let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

// ---- wire up ---------------------------------------------------------------
document.querySelectorAll(".nav-item").forEach((n) => n.addEventListener("click", () => switchView(n.dataset.view)));
$("#btn-new").addEventListener("click", () => openDrawer(null));
$("#add-tile").addEventListener("click", () => openDrawer(null));
$("#btn-latency").addEventListener("click", runConnectivity);
$("#btn-add-provider").addEventListener("click", () => openProviderModal());
$("#cards").addEventListener("click", onCardsClick);
$("#providers-list").addEventListener("click", onProvidersClick);
$("#channel-form").addEventListener("submit", submitForm);
$("#provider-form").addEventListener("submit", submitProvider);
$("#p-provider").addEventListener("change", updateProvHint);
$("#f-provider").addEventListener("change", async () => { await populateCountries(""); await populateCities(""); maybeRelocationNote(); });
$("#f-country").addEventListener("change", async () => { await populateCities(""); maybeRelocationNote(); });
$("#f-city").addEventListener("change", maybeRelocationNote);
$("#go-add-provider").addEventListener("click", () => { closeDrawer(); switchView("providers"); openProviderModal(); });
document.querySelectorAll('[data-action="close"]').forEach((b) => b.addEventListener("click", closeDrawer));
document.querySelectorAll('[data-action="new"]').forEach((b) => b.addEventListener("click", () => openDrawer(null)));
document.querySelectorAll('[data-action="prov-close"]').forEach((b) => b.addEventListener("click", closeProviderModal));
document.querySelectorAll('[data-action="log-close"]').forEach((b) => b.addEventListener("click", closeLogModal));
$("#log-refresh").addEventListener("click", () => loadLog(logChannelId));
$("#scrim").addEventListener("click", closeDrawer);
$("#prov-scrim").addEventListener("click", closeProviderModal);
$("#log-scrim").addEventListener("click", closeLogModal);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeProviderModal(); closeLogModal(); } });

loadInfo().catch(() => { });
refresh(true).catch((e) => toast(e.message, true)); // probes any already-healthy channel
const startView = location.hash.replace("#", "");
if (["providers", "about"].includes(startView)) switchView(startView);
setInterval(() => { if (state.view === "channels") refresh(false).catch(() => { }); }, 5000);
setInterval(() => { if (state.view === "channels") probeConnectivity(true); }, 60000);
