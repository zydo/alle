// Shared helpers: API fetch, escaping, toasts, and a small modal.

export const $ = (id) => document.getElementById(id);

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Build a DOM node from an HTML string (single root).
function node(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// --- API: same-origin, cookie auth. Returns { ok, data, error }. ---
async function req(method, path, body, options = {}) {
  const opt = { method, headers: {}, signal: options.signal };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res; let text;
  try {
    res = await fetch(path, opt);
    text = await res.text();
  } catch (err) {
    if (err?.name === "AbortError") return { ok: false, aborted: true, error: "cancelled" };
    const detail = err instanceof Error ? err.message : String(err);
    return { ok: false, error: `Can't reach the daemon: ${detail}` };
  }
  if (res.status === 401) { location.href = "/"; return { ok: false, error: "unauthorized" }; }
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      return { ok: false, error: `Invalid response from daemon: ${detail}` };
    }
  }
  if (!res.ok) return { ok: false, error: data?.error || `Request failed (${res.status})` };
  return { ok: true, data };
}

export const api = {
  get: (p, o) => req("GET", p, undefined, o),
  post: (p, b, o) => req("POST", p, b || {}, o),
  del: (p, o) => req("DELETE", p, undefined, o),
};

export function createLifetime() {
  const controller = new AbortController();
  let active = true;
  return {
    signal: controller.signal,
    active: () => active,
    close() { active = false; controller.abort(); },
  };
}

export function bytes(n) {
  n = Number(n || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

export function mbps(bps) {
  if (!bps) return "—";
  return `${(Number(bps) / 1_000_000).toFixed(1)} Mbps`;
}

// --- transient notifications ---
// A leading status glyph (the kind's accent color) plus the message — reads as
// a centered system HUD rather than floating text. The message is set via
// textContent so it is never interpreted as HTML.
const TOAST_ICONS = {
  ok: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10" cy="10" r="8.2"/><path d="M6.4 10.4l2.4 2.4 4.8-5"/></svg>',
  err: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10" cy="10" r="8.2"/><path d="M10 5.6v5"/><path d="M10 13.7v.5"/></svg>',
  warn: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 2.7l7.6 13.1H2.4z"/><path d="M10 8.2v3.1"/><path d="M10 13.7v.4"/></svg>',
};

export function toast(msg, kind = "ok") {
  let host = $("toasts");
  if (!host) { host = node('<output id="toasts" aria-live="polite"></output>'); document.body.appendChild(host); }
  const t = node(`<div class="toast ${kind}"></div>`);
  const ico = node(`<span class="toast-ico">${TOAST_ICONS[kind] || TOAST_ICONS.ok}</span>`);
  const txt = document.createElement("span");
  txt.className = "toast-msg";
  txt.textContent = msg;
  t.append(ico, txt);
  t.title = "Click to dismiss";
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  // Long messages (e.g. an error explaining a sudo workaround) need time to
  // read — scale the hold with length, and let a click dismiss early.
  const ms = Math.min(15000, Math.max(3400, 1500 + msg.length * 60));
  const hide = () => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); };
  const timer = setTimeout(hide, ms);
  t.onclick = () => { clearTimeout(timer); hide(); };
}

const dialogClosers = new Set();

function dialogA11y(root, finish) {
  const previous = document.activeElement;
  const background = [...document.body.children].filter((child) => child !== root);
  background.forEach((child) => { child.inert = true; });
  dialogClosers.add(finish);
  const focusable = () => [...root.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  )].filter((item) => !item.hidden);
  const onKey = (event) => {
    if (event.key === "Escape") { event.preventDefault(); finish(false); return; }
    if (event.key !== "Tab") return;
    const items = focusable();
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0]; const last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);
  return () => {
    dialogClosers.delete(finish);
    document.removeEventListener("keydown", onKey);
    background.forEach((child) => { child.inert = false; });
    if (previous?.isConnected) previous.focus();
  };
}

export function dismissDialogs() {
  [...dialogClosers].forEach((close) => close(false));
}

// --- modal: opens with a title + body HTML; returns { root, close }. ---
export function modal(title, bodyHTML) {
  const titleId = `dialog-title-${crypto.randomUUID()}`;
  const root = node(`
    <div class="overlay">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
        <div class="modal-head"><span class="eyebrow" id="${titleId}">${esc(title)}</span>
          <button class="x" aria-label="Close">✕</button></div>
        <div class="modal-body">${bodyHTML}</div>
      </div>
    </div>`);
  document.body.appendChild(root);
  let cleanup = () => { };
  const close = () => {
    if (root.classList.contains("leaving")) return;
    root.classList.add("leaving");
    cleanup();
    // let the mirrored exit animation play before tearing the node down
    setTimeout(() => root.remove(), 180);
  };
  cleanup = dialogA11y(root, close);
  root.querySelector(".x").onclick = close;
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  const first = root.querySelector("input, select, button:not(.x)");
  if (first) first.focus();
  return { root, close };
}

// --- styled confirm: replaces the native browser confirm() with the UI's
//     own modal. Resolves true (confirm) or false (cancel/Escape/backdrop). ---
export function confirmDialog(title, message, { confirmText = "Confirm", cancelText = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    const root = node(`
      <div class="overlay">
        <div class="modal confirm-modal" role="alertdialog" aria-modal="true" aria-label="${esc(title)}">
          <div class="modal-body">
            <p class="confirm-msg">${esc(message)}</p>
            <div class="confirm-actions">
              <button class="btn ghost" type="button" data-cancel>${esc(cancelText)}</button>
              <button class="btn ${danger ? "danger" : "primary"}" type="button" data-confirm>${esc(confirmText)}</button>
            </div>
          </div>
        </div>
      </div>`);
    document.body.appendChild(root);
    let done = false;
    let cleanup = () => { };
    const finish = (val) => {
      if (done) {
        return;
      }
      done = true;
      cleanup();
      root.classList.add("leaving");
      // resolve immediately so app logic isn't delayed; only the teardown waits
      setTimeout(() => root.remove(), 180);
      resolve(val);
    };
    cleanup = dialogA11y(root, finish);
    root.querySelector("[data-cancel]").onclick = () => finish(false);
    root.querySelector("[data-confirm]").onclick = () => finish(true);
    root.addEventListener("click", (e) => { if (e.target === root) finish(false); });
    root.querySelector("[data-confirm]").focus();
  });
}

// --- custom select: a fully styled replacement for native <select>, so the
//     option list matches the UI instead of rendering as OS chrome. The
//     selected value is held in a hidden <input> (same id/name a real select
//     would use), and a "change" event is dispatched on it so existing
//     onchange handlers keep working. ---
export function customSelectHTML(id, options, selectedValue) {
  const sel = options.find((o) => o.value === selectedValue) || options[0];
  const items = options
    .map((o) => `<button type="button" role="option" aria-selected="${o.value === sel.value}" class="cselect-opt${o.value === sel.value ? " selected" : ""}" data-value="${esc(o.value)}">${esc(o.label)}</button>`)
    .join("");
  return `<div class="cselect" data-cselect="${esc(id)}">
    <button type="button" class="cselect-btn" aria-haspopup="listbox" aria-expanded="false"><span class="cselect-label">${esc(sel.label)}</span><span class="cselect-caret" aria-hidden="true"></span></button>
    <input type="hidden" id="${esc(id)}" name="${esc(id)}" value="${esc(sel.value)}">
    <div class="cselect-menu" role="listbox">${items}</div>
  </div>`;
}

function _wireOneCSelect(box) {
  if (box.dataset.cselectWired) return;
  box.dataset.cselectWired = "1";
  const btn = box.querySelector(".cselect-btn");
  const label = box.querySelector(".cselect-label");
  const menu = box.querySelector(".cselect-menu");
  const input = box.querySelector("input");
  const opts = Array.from(box.querySelectorAll(".cselect-opt"));
  const setOpen = (o) => { box.classList.toggle("open", o); btn.setAttribute("aria-expanded", String(o)); };
  btn.onclick = (e) => { e.stopPropagation(); setOpen(!box.classList.contains("open")); };
  opts.forEach((opt) => {
    opt.onclick = (e) => {
      e.stopPropagation();
      input.value = opt.dataset.value;
      label.textContent = opt.textContent;
      opts.forEach((o) => {
        o.classList.toggle("selected", o === opt);
        o.setAttribute("aria-selected", String(o === opt));
      });
      setOpen(false);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      btn.focus();
    };
  });
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault(); setOpen(true);
      (opts.find((o) => o.classList.contains("selected")) || opts[0]).focus();
    }
  });
  menu.addEventListener("keydown", (e) => {
    const i = opts.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); opts[(i + 1) % opts.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); opts[(i - 1 + opts.length) % opts.length].focus(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); document.activeElement.click(); }
    else if (e.key === "Escape") { setOpen(false); btn.focus(); }
  });
}

let _cselectDocBound = false;
export function wireCustomSelects(scope) {
  if (!_cselectDocBound) {
    _cselectDocBound = true;
    document.addEventListener("click", (e) => {
      document.querySelectorAll("[data-cselect].open").forEach((box) => {
        if (!box.contains(e.target)) {
          box.classList.remove("open");
          box.querySelector(".cselect-btn").setAttribute("aria-expanded", "false");
        }
      });
    });
  }
  scope.querySelectorAll("[data-cselect]").forEach(_wireOneCSelect);
}
