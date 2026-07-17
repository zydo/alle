// Logs page: polling tail view. Streaming is intentionally deferred.

import { api, toast, customSelectHTML, wireCustomSelects } from "./core.js";

let view = null;
let timer = null;
let lines = 200;
let lifetime = null;
let lastText = null;

export function mount(v, ctx) {
  view = v;
  lifetime = ctx?.lifetime || null;
  lastText = null;
  view.innerHTML = `
    <section>
      <div class="section-head"><span class="eyebrow">Logs</span>
        <label class="log-lines">Last ${customSelectHTML("lines", [
    { value: "50", label: "50" },
    { value: "200", label: "200" },
    { value: "500", label: "500" },
  ], "200")} lines</label></div>
      <pre class="log-view" id="log"></pre>
    </section>`;
  wireCustomSelects(view);
  view.querySelector("#lines").onchange = (e) => { lines = Number(e.target.value); refresh(); };
  refresh();
  schedule();
}

export function unmount() {
  if (timer) clearTimeout(timer);
  timer = null;
  lifetime = null;
  lastText = null;
  view = null;
}

function schedule() {
  if (!view) return;
  timer = setTimeout(async () => { await refresh(); schedule(); }, 3000);
}

async function refresh() {
  if (!view) return;
  const pre = view.querySelector("#log");
  const pinned = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  const owned = view;
  const res = await api.get(
    `/api/v1/logs?lines=${lines}`,
    { signal: lifetime?.signal },
  );
  if (!view || view !== owned || lifetime && !lifetime.active()) return;
  if (!res.ok) { toast(res.error, "err"); return; }
  const text = res.data.text || "No log lines yet.";
  if (text === lastText) return;
  lastText = text;
  pre.textContent = text;
  if (pinned) pre.scrollTop = pre.scrollHeight;
}
