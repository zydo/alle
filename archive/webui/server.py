"""Local web server: serves the alle UI and a JSON API over the channels."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from alle import __version__, credentials, daemon, locations, paths
from alle.channels import Store
from alle.engine import Engine, detect_issue
from alle.providers import ProviderError, brand, display_name, preview, provider_wg, supported

WEBUI = Path(__file__).resolve().parent / "webui"
_CONTENT = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}


class App:
    """Holds shared state guarded by a single lock (handlers run on threads)."""

    def __init__(self):
        self.store = Store.load()
        self.engine = Engine(self.store)
        self.lock = threading.Lock()
        self.root = paths.state_dir()

    def sync(self):
        """Ensure the applier daemon is running; it makes sing-box match the files.

        The web UI is just another author — it never reconciles directly."""
        daemon.ensure_running()

    def reload(self):
        """Re-read the configs/ directory after a mutation so payloads are fresh."""
        self.store = Store.load()
        self.engine = Engine(self.store)

    # ---- channel payload ---------------------------------------------------
    def channels_payload(self) -> list[dict]:
        status = self.engine.status()
        out = []
        for ch in self.store.channels:
            out.append(
                {
                    **ch.to_public(),
                    "label": ch.label,
                    "providerName": display_name(ch.provider),
                    "providerBrand": brand(ch.provider),
                    "status": status.get(ch.id, {}),
                }
            )
        return out

    def providers_payload(self) -> list[dict]:
        have = set(credentials.configured())
        return [
            {
                **brand(key),
                "token_set": key in have,
                "token_preview": preview(key, credentials.get(key) or {}) if key in have else "",
            }
            for key in supported()
        ]


def _send_json(h: BaseHTTPRequestHandler, code: int, obj) -> None:
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by run()

    def log_message(self, *args):  # quiet
        pass

    # ---- helpers -----------------------------------------------------------
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _static(self, path: str) -> None:
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (WEBUI / name).resolve()
        if not str(file).startswith(str(WEBUI)) or not file.is_file():
            self.send_error(404)
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        if not p.startswith("/api/"):
            return self._static(p)
        if p == "/api/info":
            return _send_json(self, 200, {"version": __version__})
        if p == "/api/providers/catalog":
            return _send_json(self, 200, [brand(k) for k in supported()])
        if p == "/api/providers":
            with self.app.lock:
                return _send_json(self, 200, self.app.providers_payload())
        if p == "/api/locations":
            return self._locations(parse_qs(url.query))
        if p.startswith("/api/channels/") and p.endswith("/logs"):
            return self._logs(p.split("/")[3])
        if p == "/api/channels":
            with self.app.lock:
                self.app.reload()  # configs/ on disk is the source of truth
                return _send_json(self, 200, self.app.channels_payload())
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/channels":
            return self._create()
        if p == "/api/connectivity":
            cid = (parse_qs(urlparse(self.path).query).get("id") or [None])[0]
            with self.app.lock:  # snapshot only; network probes run lock-free below
                chans = list(self.app.store.channels)
            if cid:  # probe a single channel (e.g. right after it connects)
                chans = [c for c in chans if c.id == cid]
            return _send_json(self, 200, self.app.engine.connectivity(chans))
        if p.startswith("/api/channels/") and p.endswith("/enable"):
            return self._toggle(p.split("/")[3], True)
        if p.startswith("/api/channels/") and p.endswith("/disable"):
            return self._toggle(p.split("/")[3], False)
        self.send_error(404)

    def do_PUT(self):
        p = urlparse(self.path).path
        if p.startswith("/api/channels/"):
            return self._edit(p.split("/")[3])
        self.send_error(404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/channels/"):
            return self._delete(p.split("/")[3])
        self.send_error(404)

    # ---- handlers ----------------------------------------------------------
    def _logs(self, cid):
        with self.app.lock:
            ch = self.app.store.get(cid)
        if ch is None:
            return _send_json(self, 404, {"error": "no such channel"})
        text = self.app.engine.logs(ch)
        return _send_json(self, 200, {"logs": text, "issue": detect_issue(text)})

    def _locations(self, q):
        provider = (q.get("provider") or ["nordvpn"])[0]
        if provider not in supported():
            return _send_json(self, 400, {"error": f"unknown provider {provider!r}"})
        if locations.needs_refresh(self.app.root, provider):
            locations.update(self.app.root, [provider])
        locs = locations.load(self.app.root, provider)
        return _send_json(self, 200, {"countries": locs})

    # Handlers only author files (and ensure the applier runs); the daemon makes
    # sing-box match. The UI is just another author of ~/.alle/configs/.
    def _create(self):
        body = self._body()
        provider, country = body.get("provider"), body.get("country")
        if provider not in supported() or not country:
            return _send_json(self, 400, {"error": "provider and country are required"})
        with self.app.lock:
            self.app.reload()
            try:  # resolve now so a bad credential/location fails before writing
                wg = provider_wg(provider, country, body.get("city") or "")
            except ProviderError as e:
                return _send_json(self, 400, {"error": str(e)})
            ch = self.app.store.add_provider(
                provider, country, body.get("city") or "", wg, body.get("name") or ""
            )
            daemon.ensure_running()
            return _send_json(self, 201, ch.to_public())

    def _edit(self, cid):
        body = self._body()
        with self.app.lock:
            self.app.reload()
            ch = self.app.store.get(cid)
            if ch is None:
                return _send_json(self, 404, {"error": "no such channel"})
            if body.get("enabled") is not None:
                ch = self.app.store.set_enabled(cid, bool(body["enabled"]))
            daemon.ensure_running()
            return _send_json(self, 200, ch.to_public())

    def _toggle(self, cid, enable):
        with self.app.lock:
            self.app.reload()
            ch = self.app.store.set_enabled(cid, enable)
            if ch is None:
                return _send_json(self, 404, {"error": "no such channel"})
            daemon.ensure_running()
            return _send_json(self, 200, ch.to_public())

    def _delete(self, cid):
        with self.app.lock:
            self.app.reload()
            ch = self.app.store.remove(cid)
            if ch is None:
                return _send_json(self, 404, {"error": "no such channel"})
            daemon.ensure_running()
            return _send_json(self, 200, {"ok": True})


def run(port: int = 8200, open_browser: bool = True) -> None:
    Handler.app = App()
    # reconcile channels in the background so the UI is responsive immediately
    threading.Thread(target=Handler.app.sync, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"alle serving {url}", flush=True)
    if open_browser:
        import webbrowser

        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
