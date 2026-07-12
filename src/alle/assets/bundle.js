// Bundle page: export the whole setup as one file, and import/restore one.
// Export and import are flattened inline (no modal); the only popup is the
// destructive-replace confirmation.

import { api, toast, confirmDialog } from "./core.js";

let view = null;
let mounted = false;
let refreshStatus = () => { };

// A stable identity for a File, so an in-flight validate/import can tell if the
// user selected a different file while it was awaiting (a stale result must not
// be applied to the new file).
function fileKey(file) {
  return file ? `${file.name}|${file.size}|${file.lastModified}` : "";
}

const ICON_LOCK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>`;
const ICON_DL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v10"/><path d="m8 11 4 4 4-4"/><path d="M5 19h14"/></svg>`;

const PAGE = `
  <div class="bundle-page">
    <section class="bundle-panel rise">
      <div class="bundle-head">
        <div class="bundle-titles">
          <h2 class="bundle-title">Export</h2>
          <p class="bundle-sub">Download the whole setup — providers, channels, rulesets, and router settings — as one file.</p>
        </div>
      </div>
      <div class="bundle-div"></div>
      <div class="bundle-secret">${ICON_LOCK}<span>Contains VPN provider tokens and WireGuard private keys, keep it private.</span></div>
      <ul class="bundle-notes">
        <li>Ports are allocated locally and never travel in a bundle.</li>
        <li>If you clone this setup onto a second machine and run both machines simultaneously, channels sharing the same WireGuard credentials can conflict when they hit the same server.</li>
      </ul>
      <div class="bundle-actions">
        <button class="btn primary btn-icon" type="button" id="bundle-download">${ICON_DL}Download</button>
      </div>
    </section>

    <section class="bundle-panel rise">
      <div class="bundle-head">
        <div class="bundle-titles">
          <h2 class="bundle-title">Import</h2>
          <p class="bundle-sub">Apply a bundle file to this setup.</p>
        </div>
      </div>
      <div class="bundle-drop" id="bundle-drop" role="button" tabindex="0" aria-label="Upload or drop a bundle file">
        <span class="bundle-drop-label" id="bundle-fname">Upload or drop a file</span>
        <span class="bundle-drop-msg" aria-hidden="true">Drop the file to load it</span>
        <input id="bundle-file" type="file" accept=".yaml,.yml,text/yaml,application/yaml,text/plain" hidden>
      </div>
      <div class="bundle-validate">
        <button class="btn ghost" type="button" id="bundle-validate">Validate file</button>
        <span class="bundle-validate-ok" id="bundle-valid-msg"></span>
      </div>
      <p class="form-err pre" id="bundle-err"></p>
      <div class="bundle-ops">
        <div class="bundle-op">
          <div class="bundle-op-text">
            <span class="bundle-op-name">Merge</span>
            <span class="bundle-op-desc">Adds and updates the bundle's channels and rulesets and keeps everything else.</span>
          </div>
          <button class="btn primary" type="button" id="bundle-merge">Merge with existing setup</button>
        </div>
        <div class="bundle-op danger">
          <div class="bundle-op-text">
            <span class="bundle-op-name">Replace</span>
            <span class="bundle-op-desc">Overwrites the whole setup, removing anything not in the bundle.</span>
          </div>
          <button class="btn danger" type="button" id="bundle-replace">Replace existing setup</button>
        </div>
      </div>
      <ul class="bundle-notes">
        <li>Ports are re-allocated locally and never come from the bundle.</li>
        <li>Token-provider channels (e.g. NordVPN) resolve a fresh server via the provider token; the bundle's WireGuard snapshot is used only if that fails.</li>
        <li>Config channels (e.g. Proton VPN .conf) are applied exactly as exported.</li>
      </ul>
    </section>
  </div>`;

export function mount(v, ctx) {
  view = v;
  mounted = true;
  refreshStatus = ctx?.refresh || (() => { });
  view.innerHTML = PAGE;

  view.querySelector("#bundle-download").onclick = async () => {
    // Fetch (not a blind navigation) so a non-2xx response is reported instead
    // of silently downloaded as a fake bundle. The session cookie authenticates.
    const btn = view.querySelector("#bundle-download");
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Downloading…";
    try {
      let res;
      try {
        res = await fetch("/api/v1/export");
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        toast(`Can't reach the daemon: ${detail}`, "err");
        return;
      }
      if (!res.ok) {
        const fallback = `Request failed (${res.status})`;
        // two-arg then: a non-JSON error body falls back without a swallowed catch
        const detail = await res.json().then((b) => b.error || fallback, () => fallback);
        toast(detail, "err");
        return;
      }
      if (!mounted) return;
      const blob = await res.blob();
      if (!mounted) return;
      const disp = res.headers.get("Content-Disposition") || "";
      const m = /filename="([^"]+)"/.exec(disp);
      const a = document.createElement("a");
      const url = URL.createObjectURL(blob);
      a.href = url;
      a.download = m ? m[1] : "alle-backup.yaml";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("Downloaded bundle.");
    } finally {
      if (mounted) { btn.disabled = false; btn.textContent = label; }
    }
  };

  const input = view.querySelector("#bundle-file");
  const fname = view.querySelector("#bundle-fname");
  const err = view.querySelector("#bundle-err");
  const mergeBtn = view.querySelector("#bundle-merge");
  const replaceBtn = view.querySelector("#bundle-replace");
  const validateBtn = view.querySelector("#bundle-validate");
  const okMsg = view.querySelector("#bundle-valid-msg");
  const drop = view.querySelector("#bundle-drop");
  const setName = (name) => {
    fname.textContent = name || "Upload or drop a file";
    fname.classList.toggle("has-file", !!name);
    okMsg.textContent = "";  // a new file invalidates the last check result
  };
  input.onchange = () => setName(input.files[0]?.name || "");

  validateBtn.onclick = async () => {
    err.textContent = ""; okMsg.textContent = "";
    const file = input.files[0];
    if (!file) { err.textContent = "Choose a bundle file."; return; }
    const startedWith = fileKey(file);
    const text = await file.text();
    const label = validateBtn.textContent;
    validateBtn.disabled = true; validateBtn.textContent = "Validating…";
    try {
      const res = await api.post("/api/v1/validate", { text });
      // A different file was selected while we were awaiting: the result is for
      // the old file and must not mark the new one valid (setName already
      // cleared the message); and stop if the page navigated away.
      if (!mounted || fileKey(input.files[0]) !== startedWith) return;
      if (!res.ok) { err.textContent = res.error; return; }
      const d = res.data;
      okMsg.textContent = `Valid — ${d.providers} provider(s), ${d.channels} channel(s), `
        + `${d.rulesets} ruleset(s).` + (d.notes?.length ? ` (${d.notes.join("; ")})` : "");
    } finally {
      if (mounted && fileKey(input.files[0]) === startedWith) {
        validateBtn.disabled = false; validateBtn.textContent = label;
      }
    }
  };

  // the whole box is the control: click (or Enter/Space) opens the picker
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  };

  // …or drop a file straight onto it — hand it to the input so the rest of the
  // flow is identical to picking one
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    stop(e);
    drop.classList.add("drag");
  }));
  ["dragleave", "dragend"].forEach((ev) => drop.addEventListener(ev, (e) => {
    stop(e);
    // ignore leaves onto a child element, so the highlight doesn't flicker
    if (ev === "dragleave" && drop.contains(e.relatedTarget)) return;
    drop.classList.remove("drag");
  }));
  drop.addEventListener("drop", (e) => {
    stop(e);
    drop.classList.remove("drag");
    const file = e.dataTransfer.files?.[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    setName(file.name);
    err.textContent = "";
  });

  async function apply(isRestore) {
    err.textContent = "";
    const file = input.files[0];
    if (!file) { err.textContent = "Choose a bundle file."; return; }
    const startedWith = fileKey(file);
    const text = await file.text();
    if (!mounted || fileKey(input.files[0]) !== startedWith) return;
    if (isRestore && !(await confirmDialog(
      "Replace the entire setup?",
      `Everything not in "${file.name}" — providers, channels, rulesets — will be removed. This cannot be undone.`,
      { confirmText: "Replace setup", danger: true },
    ))) return;
    if (!mounted) return;
    const active = isRestore ? replaceBtn : mergeBtn;
    const label = active.textContent;
    mergeBtn.disabled = true; replaceBtn.disabled = true;
    active.textContent = isRestore ? "Replacing…" : "Merging…";
    try {
      const res = await api.post("/api/v1/import", { text, replace: isRestore });
      if (!mounted) return;
      if (!res.ok) { err.textContent = res.error; return; }
      input.value = ""; setName("");
      toast(bundleSummaryText(res.data));
      bundleFallbackToast(res.data);
      refreshStatus();
    } finally {
      if (mounted) {
        mergeBtn.disabled = false; replaceBtn.disabled = false;
        active.textContent = label;
      }
    }
  }

  mergeBtn.onclick = () => apply(false);
  replaceBtn.onclick = () => apply(true);
}

export function unmount() { view = null; mounted = false; }

function bundleSummaryText(d) {
  if (d.mode === "restore") {
    return `Setup replaced: ${d.providers.length} provider(s), ${d.channels.length} channel(s), ${d.rulesets.length} ruleset(s).`;
  }
  const ch = d.channels || {};
  const parts = [];
  if (ch.created?.length) parts.push(`${ch.created.length} channel(s) added`);
  if (ch.updated?.length) parts.push(`${ch.updated.length} channel(s) updated`);
  if (d.rulesets_added?.length) parts.push(`${d.rulesets_added.length} ruleset(s) appended`);
  if (d.credentials?.added?.length) parts.push(`credential added: ${d.credentials.added.join(", ")}`);
  if (d.credentials?.replaced?.length) parts.push(`credential REPLACED: ${d.credentials.replaced.join(", ")}`);
  if (d.wg_resolved?.length) parts.push(`${d.wg_resolved.length} server(s) resolved fresh`);
  return parts.length ? `Imported — ${parts.join("; ")}.` : "Imported — the setup already matches the bundle.";
}

function bundleFallbackToast(d) {
  const n = d.wg_fallback?.length || 0;
  if (!n) return;
  toast(
    `${n} channel(s) could not resolve a fresh server and used the bundle snapshot — auto-reconnect refreshes them when the provider API is reachable.`,
    "err",
  );
}
