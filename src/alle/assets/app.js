// App shell: hash router, nav, and the single status poll that feeds the
// masthead + the active page. Pages are plain modules with mount/unmount/onStatus.

import { $, api } from "./core.js";
import * as dashboard from "./dashboard.js";
import * as logs from "./logs.js";

const pages = { "": dashboard, logs };
const el = { pill: $("pill"), pillText: $("pill-text"), ver: $("ver"), banner: $("banner") };
let current = null;
let lastStatus = null;

function setPill(up, text) { el.pill.classList.toggle("up", up); el.pillText.textContent = text; }

function updateMasthead(s) {
  setPill(!!s.running, s.running ? "running" : "stopped");
  const d = s.daemon || {};
  el.ver.textContent = d.version ? `v${d.version}` : "";
}

function route() {
  const key = location.hash.replace(/^#\/?/, "");
  const page = pages[key] || dashboard;
  const activeKey = pages[key] ? key : "";
  if (current && current.unmount) current.unmount();
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", (a.dataset.route || "") === activeKey));
  const view = $("view");
  view.innerHTML = "";
  current = page;
  page.mount(view, { refresh: tick });
  if (lastStatus && current.onStatus) current.onStatus(lastStatus);
}

async function tick() {
  const { ok, data } = await api.get("/api/v1/status");
  if (ok) {
    lastStatus = data;
    updateMasthead(data);
    el.banner.classList.remove("show");
    if (current && current.onStatus) current.onStatus(data);
  } else {
    el.banner.classList.add("show");
    setPill(false, "offline");
  }
}

async function loop() { await tick(); setTimeout(loop, 3000); }

window.addEventListener("hashchange", route);
route();
loop();
