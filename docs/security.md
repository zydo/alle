# Security model

What alle defends, what it deliberately trusts, and where the residual risks
are. alle is a local, single-user tool: the design goal is that **nothing on
the network — and no *other local service* you happen to visit in a browser —
can read or change your VPN setup**, while anything running as your OS user is
inside the trust boundary.

## Trust boundary

**Trusted:** processes running as your OS user. They can read `~/.alle/`
(state, credentials, the control-API secret) and can therefore do anything the
CLI can. This is by design — alle does not try to defend against your own
user account, and cannot (nor against root/admin).

**Untrusted:** the network (nothing listens beyond loopback), other OS users
(the state directory is `0700`; secret-bearing files are written private from
the first byte), and — importantly — *other loopback web services* and the
pages they serve. A browser is a confused deputy: it happily carries cookies
and requests between local origins, so the Web UI treats "another local web
app" as an attacker.

**Known gap on multi-user machines:** the per-channel proxy ports and the
router entrypoint are unauthenticated loopback listeners. Another *OS user* on
the same machine may be able to send traffic through your channels (and your
provider account). alle assumes a single-user machine; don't run it where
that assumption fails. (Tracked as backlog: per-installation proxy auth.)

## Credentials at rest

| Secret | Where | Protection |
| --- | --- | --- |
| Provider tokens | `~/.alle/credentials.yaml` | `0600`, never echoed back |
| WireGuard private keys | `~/.alle/state.json`, generated sing-box config | `0600` / `0400` |
| Web UI secret | `~/.alle/control_api.json` | `0600` |
| sing-box stats secret | `~/.alle/clash_api.json` | `0600` |
| Setup bundles (`alle export`) | wherever you save them | `0600` on export; **the file is a secret** |
| Setup rollback journal | `~/.alle/setup-journal.json` | `0600`, transient — holds the pre-change credentials while a compound setup change (token update, bundle apply, provider removal) is in flight, and is used to roll them back if the change fails or crashes before committing |

## The Web UI

Served by the daemon on `127.0.0.1` only. Defenses, layer by layer:

- **Host allow-list** — every request's `Host` must be loopback (or the
  canonical name below); a DNS-rebound `evil.com` pointing at 127.0.0.1 is
  refused.
- **Per-installation hostname** — browsers use
  `http://alle-<random>.localhost:<port>`, not `http://127.0.0.1:<port>`.
  Cookies are scoped to *hosts, not ports*, so a cookie set for `127.0.0.1`
  would be sent to **every** other local web app you ever open on any
  127.0.0.1 port — any of them could capture and replay it. The random
  `*.localhost` name (browsers resolve it to loopback themselves, RFC 6761)
  is a host no other service can occupy, so the session cookie never leaves
  alle. Literal-host page loads are redirected to the canonical name, and a
  session cookie is only ever minted there. Two `ALLE_HOME`s get two names,
  so their sessions can't collide either.
- **Sign-in** — `alle ui` mints a single-use, 2-minute HMAC login token; the
  server exchanges it for a session cookie and refuses replays, so a copy in
  shell or browser history is inert. Manual sign-in pastes the secret from
  `control_api.json`. The persistent secret itself travels only as an
  `Authorization: Bearer` header, never in a URL.
- **Sessions** — `HttpOnly; SameSite=Strict` cookies, idle-limited (30
  minutes without activity), rolled while a tab is open, capped at 12 hours
  from sign-in. The masthead's **Sign out** revokes every session
  immediately (persisted durably, so it survives daemon restarts; if the
  revocation record ever turns unreadable, verification fails closed —
  every session dies until the next sign-in rewrites it); the Bearer secret
  is unaffected.
- **CSRF** — cookie-authenticated mutations additionally require a same-origin
  `Origin` header. Bearer-authenticated requests are exempt: no browser
  attaches an `Authorization` header cross-origin, so scripts and `curl` can
  mutate without faking a browser origin.
- **Readiness proof** — before opening a sign-in link, `alle ui` challenges
  the port with a nonce and requires an HMAC answer, so a foreign process
  squatting the port never receives a tokenized URL.
- **Request hygiene** — strict framing (`Content-Length` validation, 1 MiB
  cap, no transfer encodings), strict JSON typing (malformed input is a 4xx,
  never coerced defaults), unknown-field rejection, socket deadlines, and a
  bounded worker pool.

**Remote access:** never expose or reverse-proxy the port. Tunnel the same
port over SSH (`ssh -L <port>:127.0.0.1:<port> user@host`) and open the
`alle ui` link locally — the `*.localhost` name resolves to your end of the
tunnel.

## Fail-closed routing

- A routing rule whose channel is missing or unbuildable compiles to *block*,
  never to "fall through to the open Internet"; a channel that can't build
  loses its proxy port entirely.
- With the kill switch on, unmatched router traffic is rejected; if sing-box
  dies, the ports close (nothing to leak through) and the daemon supervises
  and restarts it with bounded backoff.
- A config generation sing-box rejects keeps the last known-good generation
  running and is reported, not silently dropped.

## Supply chain

The bundled sing-box is a pinned upstream release, checksum-verified on every
use — a binary that doesn't match the pinned SHA-256 is re-downloaded, never
executed.
