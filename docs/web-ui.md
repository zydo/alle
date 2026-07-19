# The Web UI

`alle` serves a local dashboard from the background daemon — nothing extra to
install. Open it with:

```bash
alle ui
```

This opens your browser to a **Dashboard**, a **Bundle** page, and a **Logs**
page:

- **Router entrypoint** — `http://127.0.0.1:<port>` at the top (click to copy).
- **Channels table** — every channel with Location, Port, Latency, IP, and
  Sent / Received / Down Speed / Up Speed columns. The measured columns stay
  blank until you run a **Probe** (latency + IP + traffic totals) or **Speed
  Test** (adds download/upload) from the row or the column header, with a
  spinner while it runs. **Speed Test All** streams — each channel's row fills
  in the moment its own test completes, instead of all at once at the end. While
  a channel is being tested (or a batch run is in flight) that row's Probe and
  Speed Test buttons are disabled, so a test can't be fired twice at once.
  Rename a channel inline, remove one, and add channels through a provider-guided wizard.
- **Add channel wizard** — pick a provider (an icon-only row of providers plus
  an always-present "+" to add NordVPN or Proton VPN). For token providers like
  NordVPN, choose a **country and city from a searchable list** (no typing); for
  Proton VPN, upload a WireGuard `.conf` (with a link to the portal). Each added
  token provider carries a **gear** to replace its stored token (write-only — the
  token is never shown back); replacing it re-resolves that provider's channels.
- **Router rules** — add/delete rules, **drag to reorder** (first match wins),
  and toggle **Allow Non-VPN Traffic** (the Unmatched row: on lets unmatched
  destinations reach the Internet, off blocks them). A fixed **Priority 0 / LAN**
  row at the top keeps local traffic direct ahead of every rule, with a toggle
  to turn that protection off.
- **Bundle** — download the whole setup as a bundle file (it contains
  credentials — the UI warns first), and upload one to **merge** it in or
  **replace** the whole setup (with a confirmation dialog).
- Start / stop / restart are host/CLI controls (`alle start|stop|restart`); the
  masthead links to the project on GitHub.

## Access model

The browser UI binds to `127.0.0.1` only and is never exposed to the network.
The browser URL uses a per-installation `alle-<random>.localhost` hostname
(which browsers resolve to loopback on their own) so the session cookie is
scoped to alle alone, never shared with other local web apps. To reach the UI
from another machine, forward the **same** port over SSH rather than exposing
it:

```bash
alle status                              # on the remote host: note the Web UI port
ssh -L <port>:127.0.0.1:<port> user@host
# then open the `alle ui` sign-in link locally — it resolves to your tunnel
```

SSH provides the encryption and access control; the browser still reaches alle
on loopback. Do not open or reverse-proxy the alle Web UI port directly to a
network. (Programmatic access is different: the Bearer-authenticated
[REST API](api.md) has a sanctioned opt-in network mode for containers; the
browser cookie path never rides along.)

`alle ui` signs you in automatically. To sign in by hand, paste the `secret`
from `~/.alle/control_api.json` into the login page. Sessions idle out after
30 minutes without an open tab (capped at 12 hours); the masthead's **Sign
out** button revokes every session immediately.

The session and cookie design — why the random hostname exists, CSRF
defenses, revocation — is documented in [security.md](security.md).

## How the UI is tested

Two CI layers guard the Web UI:

- **Static + protocol checks** (`npm run check:web`): JS syntax/import
  resolution, forbidden-sink lint, and Node-level tests of the NDJSON
  speed-stream framing.
- **Real-browser smoke** (`npm run test:browser`, Playwright/Chromium): the
  actual stdlib server and assets, driven in a real browser against a
  synthetic fixture daemon (`tests/browser/fixture_server.py` — deterministic
  state, no network, no credentials, `service.test` canned). It covers the
  login token exchange, the status poll and offline recovery, channel
  enable/disable, ruleset reorder staging/apply, dialog cancellation, the
  Logs page's poll lifecycle, bundle validate/import round-trips, and the
  incremental speed-row stream. Every test also fails on any browser console
  error, uncaught promise rejection, or CSP violation, and dedicated specs
  run axe (WCAG A/AA, with color-contrast as a documented, deliberate
  exception pending a design pass) plus keyboard-only traversals. Lifetime
  acceptance tests pin the async rules: an unmounted page's in-flight
  requests are aborted and produce no stale DOM, toast, or handler effects,
  and every mutation control is single-flight (a double-click sends exactly
  one request).

Run locally: `npm ci && npx playwright install chromium && npm run test:browser`.
