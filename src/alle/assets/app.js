// App shell: hash router, nav, and the single status poll that feeds the
// masthead + the active page. Pages are plain modules with mount/unmount/onStatus.

import { $, api, confirmDialog, createLifetime, dismissDialogs, toast } from "./core.js";
import * as dashboard from "./dashboard.js";
import * as bundle from "./bundle.js";
import * as logs from "./logs.js";
import { followSystem, bindToggle } from "./theme.js";

const pages = { "": dashboard, bundle, logs };
const el = { pill: $("pill"), pillText: $("pill-text"), ver: $("ver"), banner: $("banner") };
let current = null;
let lastStatus = null;
let lifetime = null;
let statusRequest = null;
let statusGeneration = 0;
let appliedGeneration = 0;

function setPill(up, text) { el.pill.classList.toggle("up", up); el.pillText.textContent = text; }

function updateMasthead(s) {
  setPill(!!s.running, s.running ? "running" : "stopped");
  const d = s.daemon || {};
  // Prefer the on-disk package version (single source of truth, read fresh from
  // pyproject via importlib.metadata) over the daemon's startup snapshot, which
  // goes stale until the daemon restarts after an upgrade.
  const ver = d.installed_version || d.version;
  el.ver.textContent = ver ? `v${ver}` : "";
}

function route() {
  const key = location.hash.replace(/^#\/?/, "");
  // Own-property check only: a hash like #/constructor or #/__proto__ must not
  // pick up an Object.prototype member (a truthy function) and crash mount().
  const known = Object.hasOwn(pages, key);
  const page = known ? pages[key] : dashboard;
  const activeKey = known ? key : "";
  lifetime?.close();
  dismissDialogs();
  current?.unmount?.();
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", (a.dataset.route || "") === activeKey));
  const view = $("view");
  view.innerHTML = "";
  current = page;
  lifetime = createLifetime();
  page.mount(view, { refresh: tick, lifetime });
  if (lastStatus) current?.onStatus?.(lastStatus);
}

async function tick() {
  if (statusRequest) return statusRequest;
  const generation = ++statusGeneration;
  statusRequest = api.get("/api/v1/status");
  const { ok, data } = await statusRequest;
  statusRequest = null;
  if (generation < appliedGeneration) return;
  appliedGeneration = generation;
  if (ok) {
    lastStatus = data;
    updateMasthead(data);
    el.banner.classList.remove("show");
    current?.onStatus?.(data);
  } else {
    el.banner.classList.add("show");
    setPill(false, "offline");
  }
}

async function scheduleNextTick() {
  try { await tick(); } finally { setTimeout(scheduleNextTick, 3000); }
}

$("logout").addEventListener("click", async () => {
  await api.post("/api/v1/logout"); // revokes every session, clears the cookie
  location.href = "/"; // back to the sign-in page
});

// Version badge = on-demand update check. The owning source (Homebrew tap or
// PyPI) is contacted only on this click, never on a timer. Upgrade and restart
// ownership stay server-side; the response tells us whether restart is native,
// pending under Homebrew, or requires an explicit brew-services command.
let upgradeBusy = false;
$("ver").addEventListener("click", async () => {
  if (upgradeBusy) return;
  upgradeBusy = true;
  try {
    const check = await api.get("/api/v1/upgrade/check");
    if (!check.ok) { toast(check.error, "err"); return; }
    const source = check.data.channel === "homebrew" ? "the Homebrew tap" : "PyPI";
    if (!check.data.update_available) {
      toast(`No newer stable release is available (installed ${check.data.current}; ` +
        `newest on ${source}: ${check.data.latest}).`);
      return;
    }
    const go = await confirmDialog(
      "Update available",
      `alle ${check.data.latest} is available on ${source} ` +
      `(this is ${check.data.current}). Upgrade now?`,
      { confirmText: "Upgrade" },
    );
    if (!go) return;
    const res = await api.post("/api/v1/upgrade");
    if (!res.ok) { toast(res.error, "err"); return; }
    if (!res.data.changed) {
      toast(`No newer stable release is available (installed ${res.data.after}; ` +
        `newest checked: ${res.data.latest}).`);
    } else if (res.data.restart_required) {
      toast(`Upgraded to ${res.data.after}. Restart with: ${res.data.restart_command}`);
    } else if (res.data.restart_pending) {
      toast(`Upgraded to ${res.data.after} — Homebrew will restart the service shortly…`);
    } else if (res.data.restart?.restarting) {
      toast(`Upgraded to ${res.data.after} — restarting…`);
    } else if (res.data.restart) {
      toast(`Upgraded to ${res.data.after} — daemon restarted.`);
    } else {
      toast(`Upgraded to ${res.data.after}.`);
    }
  } finally {
    upgradeBusy = false;
  }
});

globalThis.addEventListener("hashchange", route);
// appearance: theme-init.js set the class before paint; keep following the OS
// while unpinned and wire the masthead toggle.
followSystem();
bindToggle($("theme"));
route();
await scheduleNextTick();
