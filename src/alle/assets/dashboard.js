// Dashboard page: the consolidated control surface (entrypoint, channels, routes).

import { api, esc, toast, modal, confirmDialog, customSelectHTML, wireCustomSelects, bytes, mbps } from "./core.js";

const GAUGE = `<svg class="ico" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/><path d="m13.4 10.6 2.6-2.6"/><path d="M3.5 18a9 9 0 1 1 17 0"/></svg>`;

const SHELL = `
  <div class="dashboard-shell">
    <section class="entry compact dash-entry rise" id="entry" hidden>
      <div class="entry-head"><span class="eyebrow">Router entrypoint</span></div>
      <p class="entry-addr"><span class="scheme">http://</span><span id="entry-addr"></span></p>
    </section>
    <section class="dash-panel dash-panel-channels rise">
      <div class="table-title"><span class="eyebrow">Channels</span>
        <div class="header-actions">
          <button class="icon-btn" id="probe-all" title="Probe all" aria-label="Probe all">◉</button>
          <button class="icon-btn" id="speed-all" title="Speed test all" aria-label="Speed test all">${GAUGE}</button>
          <button class="btn primary" id="add-channel">+ Add channel</button>
        </div>
      </div>
      <div id="channels"></div>
    </section>
    <section class="dash-panel dash-panel-routes rise">
      <div class="table-title"><span class="eyebrow">Router rules</span>
        <button class="btn primary" id="add-rule">+ Add rule</button></div>
      <p class="route-banner">Per-channel ports take priority and reach their VPN directly. Rules below apply only to the router entrypoint — first match wins.</p>
      <div id="routes"></div>
    </section>
  </div>`;

let el = {};
let status = null;
let rules = [];
let router = null;
let catalog = [];
let measured = new Map();
let busy = new Set();
let dragRuleId = null;
let paused = false;
let refreshStatus = () => { };

export function mount(view, ctx) {
  refreshStatus = (ctx && ctx.refresh) || (() => { });
  view.innerHTML = SHELL;
  el = {
    entry: view.querySelector("#entry"), entryAddr: view.querySelector("#entry-addr"),
    channels: view.querySelector("#channels"), routes: view.querySelector("#routes"),
    probeAll: view.querySelector("#probe-all"), speedAll: view.querySelector("#speed-all"),
  };
  view.querySelector("#probe-all").onclick = () => runTest(null, false);
  view.querySelector("#speed-all").onclick = () => runTest(null, true);
  view.querySelector("#add-channel").onclick = openAddChannel;
  view.querySelector("#add-rule").onclick = openAddRule;
  view.addEventListener("click", (e) => {
    const t = e.target.closest("[data-copy]");
    if (t && t.dataset.copy) { copy(t.dataset.copy); e.stopPropagation(); }
  });
  el.channels.addEventListener("click", onChannelClick);
  el.routes.addEventListener("click", onRouteClick);
  el.routes.addEventListener("dragstart", onRouteDragStart);
  el.routes.addEventListener("dragover", onRouteDragOver);
  el.routes.addEventListener("drop", onRouteDrop);
  refreshRoutes();
}

export function unmount() { el = {}; status = null; rules = []; router = null; measured = new Map(); busy = new Set(); dragRuleId = null; paused = false; }

export function onStatus(s) {
  status = s;
  if (!el.entry) return;
  renderEntry(s);
  if (!paused) renderChannels();
  if (router) renderRoutes();
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
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${text}`);
  } catch (_) {
    toast("Couldn't copy to clipboard.", "err");
  }
}

function chanKey(c) { return `${c.provider}/${c.name}`; }
function loc(c) { return c.city && !["(Unknown)", "(Any City)"].includes(c.city) ? `${c.city}, ${c.country}` : c.country; }
function spin(key, kind, icon) { return busy.has(key) || busy.has(`${key}:${kind}`) ? '<span class="spinner small"></span>' : icon; }
function syncHeaderBusy() {
  if (!el.probeAll) return;
  el.probeAll.innerHTML = `${busy.has("all:probe") ? '<span class="spinner small"></span>' : "◉"}`;
  el.speedAll.innerHTML = busy.has("all:speed") ? '<span class="spinner small"></span>' : GAUGE;
  el.probeAll.disabled = busy.has("all:probe") || busy.has("all:speed");
  el.speedAll.disabled = busy.has("all:probe") || busy.has("all:speed");
}

function chanRow(c) {
  const key = chanKey(c);
  const m = measured.get(key) || {};
  const speed = m.speed_result || {};
  return `<div class="row dashchan body" data-provider="${esc(c.provider)}" data-id="${esc(c.name)}">
    <span class="chan-label"><button class="name edit" data-label="${esc(c.label || "")}" title="Rename">${esc(c.label || c.name)}</button>
      <div class="ref">${esc(key)}</div></span>
    <span class="loc" title="${esc(loc(c))}">${esc(loc(c))}</span>
    <span class="port copyable" data-copy="http://127.0.0.1:${esc(c.port_number)}" title="Click to copy">${esc(c.port)}</span>
    <span class="lat">${m.latency_ms == null ? "" : `${esc(m.latency_ms)} ms`}</span>
    <span class="ip">${esc(m.ip || "")}</span>
    <span class="mono">${m.metrics ? bytes(m.metrics.sent) : ""}</span>
    <span class="mono">${m.metrics ? bytes(m.metrics.received) : ""}</span>
    <span class="mono">${speed.download_bps ? mbps(speed.download_bps) : ""}</span>
    <span class="mono">${speed.upload_bps ? mbps(speed.upload_bps) : ""}</span>
    <span class="row-actions channel-actions">
      <button class="icon-btn" title="Probe" aria-label="Probe" data-probe>${spin(key, "probe", "◉")}</button>
      <button class="icon-btn" title="Speed Test" aria-label="Speed Test" data-speed>${spin(key, "speed", GAUGE)}</button>
      <button class="icon-btn danger" title="Remove" aria-label="Remove" data-remove>×</button>
    </span>
  </div>`;
}

function renderChannels() {
  const chans = (status && status.channels) || [];
  if (!status?.running && chans.length === 0) {
    el.channels.innerHTML = `<div class="stopped"><div class="big">Stopped</div>alle isn't running. Use Start above or <code>alle start</code>.</div>`;
    return;
  }
  if (!chans.length) {
    el.channels.innerHTML = `<div class="empty"><div class="big">No channels</div>Use + Add channel to choose a provider and create one.</div>`;
    return;
  }
  const head = `<div class="row dashchan head"><span>Channel</span><span>Location</span><span>Port</span><span>Latency</span><span>IP</span><span>Sent</span><span>Received</span><span>Down Speed</span><span>Up Speed</span><span></span></div>`;
  el.channels.innerHTML = `<div class="grid">${head}${chans.map(chanRow).join("")}</div>`;
}

async function runTest(channel, speed) {
  const key = channel ? chanKey(channel) : "all";
  const busyKey = channel ? `${key}:${speed ? "speed" : "probe"}` : `all:${speed ? "speed" : "probe"}`;
  busy.add(key); busy.add(busyKey);
  syncHeaderBusy();
  renderChannels();
  const res = await api.post("/api/v1/test", { speed, channel: channel ? channel.name : null });
  const metrics = await api.get("/api/v1/metrics");
  busy.delete(key); busy.delete(busyKey);
  syncHeaderBusy();
  if (!res.ok) { toast(res.error, "err"); renderChannels(); return; }
  const metricMap = new Map((metrics.ok ? metrics.data.channels : []).map((c) => [`${c.provider}/${c.name}`, c]));
  for (const row of res.data.channels || []) {
    measured.set(`${row.provider}/${row.name}`, { ...row, metrics: metricMap.get(`${row.provider}/${row.name}`) });
  }
  renderChannels();
  toast(speed ? "Speed test complete." : "Probe complete.");
  refreshStatus();
}

async function onChannelClick(e) {
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
    if (done) return; done = true; paused = false;
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
  rules = res.data.rules || [];
  router = res.data.router || {};
  renderRoutes();
}

function targetLabel(target) {
  if (target === "direct") return { name: "Direct", ref: "No VPN" };
  if (target === "block") return { name: "Block", ref: "Drop traffic" };
  const c = (status?.channels || []).find((x) => chanKey(x) === target);
  return c ? { name: c.label || c.name, ref: target } : { name: target, ref: "channel ref" };
}

const MATCH_TYPE_LABEL = { domain: "domain", domain_suffix: "domain suffix", ip_cidr: "ip cidr", all: "all traffic" };

function routeRow(r) {
  const target = targetLabel(r.target);
  const valueLine = r.type === "all" ? "all traffic" : (r.value || "");
  const typeLine = MATCH_TYPE_LABEL[r.type] || r.type;
  return `<div class="row route body" draggable="true" data-id="${esc(r.id)}"><span class="route-handle" title="Drag to reorder" aria-label="${esc(r.id)}">⋮⋮</span>
    <span class="route-match"><div class="name">${esc(valueLine)}</div><div class="ref">${esc(typeLine)}</div></span>
    <span class="target-label"><div class="name">${esc(target.name)}</div><div class="ref">${esc(target.ref)}</div></span>
    <span class="row-actions"><button class="icon-btn danger" title="Remove" aria-label="Remove" data-route-remove="${esc(r.id)}">×</button></span></div>`;
}

function renderRoutes() {
  const head = `<div class="row route head"><span aria-hidden="true"></span><span>Match</span><span>Via Channel</span><span></span></div>`;
  const body = rules.length ? rules.map(routeRow).join("") : `<div class="empty inset"><div class="big">No rules</div>Unmatched traffic currently goes ${router?.unmatched || "direct"}.</div>`;
  const on = !!router?.killswitch;
  const kill = `<div class="kill-card">
    <div class="kill-card-head">
      <div class="kill-copy">
        <span class="kill-title">Unmatched Traffic</span>
        <p class="kill-content">For traffic that matches none of the channels and rules above, this controls whether it reaches the Internet directly (no VPN) or is blocked. Off by default, so you keep normal Internet access alongside your VPN traffic.</p>
      </div>
      <div class="kill-toggle-wrap">
        <span class="kill-toggle-label">Kill-switch</span>
        <span class="kill-toggle-state">${on ? "ON" : "OFF"}</span>
        <button class="toggle ${on ? "on danger" : ""}" id="dash-kill" role="switch" aria-checked="${on}" aria-label="Kill-switch" title="Block traffic that matches no rule"><span class="toggle-knob"></span></button>
      </div>
    </div>
    <div class="kill-states">
      <div class="kill-state"><span class="kill-state-label">Kill-switch off</span><span class="kill-state-desc">Unmatched traffic goes direct — normal Internet access for anything not routed through a channel or rule.</span></div>
      <div class="kill-state"><span class="kill-state-label">Kill-switch on</span><span class="kill-state-desc">Unmatched traffic is blocked — only matched traffic reaches the Internet. Per-channel ports keep working.</span></div>
    </div>
  </div>`;
  el.routes.innerHTML = `<div class="grid route-grid">${rules.length ? head : ""}${body}</div>${kill}`;
  el.routes.querySelector("#dash-kill").onclick = () => toggleKillswitch({ target: { checked: !on } });
}

async function onRouteClick(e) {
  const id = e.target.closest("[data-route-remove]")?.dataset.routeRemove;
  if (!id) return;
  if (!(await confirmDialog("Remove rule", `Remove rule ${id}?`, { confirmText: "Remove", danger: true }))) return;
  const res = await api.del(`/api/v1/routes/${encodeURIComponent(id)}`);
  if (res.ok) { toast(`Removed ${id}.`); refreshRoutes(); refreshStatus(); } else toast(res.error, "err");
}

function onRouteDragStart(e) {
  const row = e.target.closest(".row.route.body");
  if (!row) return;
  dragRuleId = row.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  row.classList.add("dragging");
}

function onRouteDragOver(e) {
  const over = e.target.closest(".row.route.body");
  if (!over || !dragRuleId || over.dataset.id === dragRuleId) return;
  e.preventDefault();
  const grid = over.parentElement;
  const dragged = grid.querySelector(`[data-id="${CSS.escape(dragRuleId)}"]`);
  const after = e.clientY > over.getBoundingClientRect().top + over.offsetHeight / 2;
  grid.insertBefore(dragged, after ? over.nextSibling : over);
}

async function onRouteDrop(e) {
  const grid = e.target.closest(".route-grid");
  if (!grid || !dragRuleId) return;
  e.preventDefault();
  grid.querySelectorAll(".dragging").forEach((r) => r.classList.remove("dragging"));
  const ids = [...grid.querySelectorAll(".row.route.body")].map((r) => r.dataset.id);
  dragRuleId = null;
  const res = await api.post("/api/v1/routes/reorder", { ids });
  if (!res.ok) { toast(res.error, "err"); refreshRoutes(); return; }
  rules = res.data.rules || [];
  router = res.data.router || router;
  renderRoutes();
  toast(res.data.changed ? "Routes reordered." : "Routes already in that order.");
  refreshStatus();
}

async function toggleKillswitch(e) {
  const enabled = e.target.checked;
  const res = await api.post("/api/v1/routes/killswitch", { enabled });
  if (!res.ok) { toast(res.error, "err"); renderRoutes(); return; }
  router = res.data.router || router;
  renderRoutes();
  refreshStatus();
  toast(enabled ? "Kill-switch on — unmatched traffic is blocked." : "Kill-switch off — unmatched traffic goes direct.");
}

async function ensureCatalog() {
  if (catalog.length) return catalog;
  const res = await api.get("/api/v1/providers/catalog");
  if (res.ok) catalog = res.data.providers || [];
  return catalog;
}

function providerIcon(provider) {
  return provider === "nordvpn" ? "/nordvpn.svg" : provider === "protonvpn" ? "/protonvpn.svg" : "";
}

function providerGuide(p) {
  if (p.kind === "config") {
    const base = (p.config_help || "Generate a WireGuard configuration in the provider's portal.")
      .split("then add it as a channel")[0].replace(/[.,;:\s]+$/, "");
    return { text: `${base}. You'll add the .conf as a channel next.`, url: p.url };
  }
  return {
    text: p.help || "Generate an access token in your provider's account portal, then paste it below.",
    url: p.url,
  };
}

function providerCard(p) {
  const logo = providerIcon(p.provider) ? `<img src="${providerIcon(p.provider)}" alt="">` : `<span class="prov-fallback">${esc(p.display_name[0])}</span>`;
  return `<button class="prov-tile" data-provider="${esc(p.provider)}" title="${esc(p.display_name)}" aria-label="${esc(p.display_name)}">${logo}</button>`;
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
    wiz.innerHTML = `<p class="field-guide">Choose a provider for the new channel.</p><div class="provider-row">${cards}${addTile}</div>`;
    wiz.onclick = (e) => {
      const card = e.target.closest("[data-provider]");
      if (card) { st.provider = card.dataset.provider; return renderForProvider(); }
      if (e.target.closest("[data-add-provider]")) return renderAddProvider();
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
      wiz.innerHTML = `<div class="empty inset">All providers are already added.</div><div class="confirm-actions"><button class="btn" type="button" data-back>Back</button></div>`;
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
      guide.innerHTML = `<p class="field-guide">${esc(g.text)}${g.url ? ` <a href="${esc(g.url)}" target="_blank" rel="noopener">Open portal ↗</a>` : ""}</p>`;
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
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Add channel</button></div>
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
      m.close(); toast(`Added ${(res.data.channel || {}).label || (res.data.channel || {}).id || "channel"}.`); refreshStatus();
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
      <div class="confirm-actions"><button class="btn ghost" type="button" data-back>Back</button><button class="btn primary" type="submit">Add channel</button></div>
    </form>`;
    wiz.querySelector("[data-back]").onclick = renderCityStep;
    wiz.querySelector("#lf").onsubmit = async (e) => {
      e.preventDefault();
      const err = wiz.querySelector("#lerr"); err.textContent = "";
      const body = { provider: st.provider, country: st.country, city: st.city, label: wiz.querySelector("#label").value.trim() };
      const res = await api.post("/api/v1/channels", body);
      if (!res.ok) { err.textContent = res.error; return; }
      m.close(); toast(`Added ${body.label || (res.data.channel || {}).id || "channel"}.`); refreshStatus();
    };
    wiz.querySelector("#label").focus();
  }

  await renderProviders();
}

function routeTargetOptions() {
  return [
    { value: "direct", label: "Direct — no VPN" },
    { value: "block", label: "Block — drop traffic" },
    ...((status?.channels || []).map((c) => ({ value: chanKey(c), label: c.label ? `${c.label} — ${chanKey(c)}` : chanKey(c) }))),
  ];
}

function openAddRule() {
  const m = modal("Add route", `<form id="rf">
    <label class="field"><span>Match type</span>${customSelectHTML("type", [
    { value: "domain_suffix", label: "Domain suffix" },
    { value: "domain", label: "Exact domain" },
    { value: "ip_cidr", label: "IP / CIDR" },
    { value: "all", label: "All traffic" },
  ], "domain_suffix")}</label>
    <label class="field" id="value-field"><span>Value</span><input id="value" placeholder="example.com" spellcheck="false"></label>
    <label class="field"><span>Via channel</span>${customSelectHTML("target", routeTargetOptions(), "direct")}</label>
    <p class="form-err" id="err"></p><button class="btn primary" type="submit">Add rule</button>
  </form>`);
  wireCustomSelects(m.root);
  const type = m.root.querySelector("#type"), field = m.root.querySelector("#value-field"), value = m.root.querySelector("#value"), err = m.root.querySelector("#err");
  const sync = () => { const all = type.value === "all"; field.hidden = all; value.disabled = all; if (all) value.value = ""; };
  type.onchange = sync; sync();
  m.root.querySelector("#rf").onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    const res = await api.post("/api/v1/routes", { type: type.value, value: value.value.trim(), target: m.root.querySelector("#target").value });
    if (!res.ok) { err.textContent = res.error; return; }
    m.close(); toast(`Added rule ${res.data.rule.id}.`); refreshRoutes(); refreshStatus();
  };
}
