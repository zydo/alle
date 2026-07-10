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
async function req(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return { ok: false, error: `Can't reach the daemon: ${detail}` };
  }
  if (res.status === 401) { location.href = "/"; return { ok: false, error: "unauthorized" }; }
  const text = await res.text();
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
  get: (p) => req("GET", p),
  post: (p, b) => req("POST", p, b || {}),
  del: (p) => req("DELETE", p),
};

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
export function toast(msg, kind = "ok") {
  let host = $("toasts");
  if (!host) { host = node('<div id="toasts"></div>'); document.body.appendChild(host); }
  const t = node(`<div class="toast ${kind}"></div>`);
  t.textContent = msg;
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, 3400);
}

// --- modal: opens with a title + body HTML; returns { root, close }. ---
export function modal(title, bodyHTML) {
  const root = node(`
    <div class="overlay">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head"><span class="eyebrow">${esc(title)}</span>
          <button class="x" aria-label="Close">✕</button></div>
        <div class="modal-body">${bodyHTML}</div>
      </div>
    </div>`);
  document.body.appendChild(root);
  const close = () => { root.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  root.querySelector(".x").onclick = close;
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  document.addEventListener("keydown", onKey);
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
    const finish = (val) => {
      if (done) {
        return;
      }
      done = true;
      root.remove();
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        finish(false);
      }
      // Enter is NOT a global confirm: it would fire confirm even when Cancel
      // has focus. Native button activation handles Enter/Space on whichever
      // button is focused (the confirm button is auto-focused, so Enter
      // confirms by default; tab to Cancel + Enter cancels).
    };
    root.querySelector("[data-cancel]").onclick = () => finish(false);
    root.querySelector("[data-confirm]").onclick = () => finish(true);
    root.addEventListener("click", (e) => { if (e.target === root) finish(false); });
    document.addEventListener("keydown", onKey);
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
    .map((o) => `<button type="button" class="cselect-opt${o.value === sel.value ? " selected" : ""}" data-value="${esc(o.value)}">${esc(o.label)}</button>`)
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
      opts.forEach((o) => o.classList.toggle("selected", o === opt));
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
