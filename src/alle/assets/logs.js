// Logs page: polling tail view. Streaming is intentionally deferred.

import { api, esc, toast, customSelectHTML, wireCustomSelects } from "./core.js";

let view = null;
let timer = null;
let lines = 200;

export function mount(v) {
  view = v;
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
  timer = setInterval(refresh, 3000);
}

export function unmount() {
  if (timer) clearInterval(timer);
  timer = null;
  view = null;
}

async function refresh() {
  if (!view) return;
  const pre = view.querySelector("#log");
  const pinned = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  const res = await api.get(`/api/v1/logs?lines=${lines}`);
  if (!view) return;
  if (!res.ok) { toast(res.error, "err"); return; }
  pre.textContent = res.data.text || "No log lines yet.";
  if (pinned) pre.scrollTop = pre.scrollHeight;
}
