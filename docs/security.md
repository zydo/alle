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

## The container profile (Docker)

The official Docker image shifts the trust boundary **one layer out, by
explicit opt-in**: it sets `ALLE_LISTEN=0.0.0.0`, so the (still
unauthenticated) channel/router proxy ports are reachable from the
*container's network* — the same reasoning as loopback-on-bare-metal, with
the container network in the role of the machine. Consequences:

- **Anything on the container's Docker network can use the proxies** (and
  your provider account). Keep alle on networks whose members you trust, and
  treat a `-p`-published proxy port as publishing an open proxy to whatever
  can reach it — only ever publish onto trusted networks.
- **The Web UI does not widen.** It stays `127.0.0.1`-only inside the
  container; its auth model was built for one loopback caller. Manage via
  `docker exec <name> alle …`.
- **Nothing changes on hosts.** `ALLE_LISTEN` (and the other container knobs)
  default off; container *detection* only ever refuses host-only footguns and
  rephrases hints — it never rebinds or reallocates.
- **Gateway (tun) mode** follows the same tun trust analysis below, scoped to
  the container's netns: the capture is per-container, the host's route table
  is never touched, and the privilege is granted at `docker run` time
  (`--cap-add NET_ADMIN --device /dev/net/tun`) instead of sudo/setcap/helper.
- **Secret indirection** (`token_env`/`token_file` in bundles) names its
  source explicitly — alle still never *scans* the environment for
  credentials. Environment variables are visible to `docker inspect` and any
  process in the container; secret files (compose/k8s secrets) are the
  tighter channel where that matters.

See [docker.md](docker.md) for the image design.

## TUN mode: the elevated trust surface

Explicit-proxy mode (the default) runs entirely as your OS user and the
boundary above is the whole story. **[TUN mode](cli-reference.md#alle-tun-onoff)
widens it**, because creating the TUN device and rewriting the system route
table is privileged. Two things change while TUN mode is on:

- **A privileged component reads the generated config.** In the v1 model you
  run sing-box as root (`sudo … alle tun on`), or on Linux grant the pinned
  binary `cap_net_admin` (`setcap`, see the tun docs). Either way, a component
  with more privilege than your user now reads the generated sing-box config —
  and that config carries **WireGuard private keys** (see the table below). On
  the sudo path the config is still the same `0400` file your user owns; root
  can read it regardless, so this is a widening of *who* touches the keys, not
  a new at-rest exposure. The Linux `setcap` path is narrower: the daemon
  stays your unprivileged user, and only the *binary* holds the capability —
  no root process is involved at all. Prefer `setcap` on Linux for that
  reason.

  On macOS the steady state is the **privileged tun helper** — a root
  LaunchDaemon installed once by `sudo alle helper install` (see
  `alle helper`). It removes the per-toggle sudo prompt: after install, `alle
  tun on` from your normal user-level daemon asks the helper to run sing-box
  as root, and no password is asked again. The helper is deliberately the
  smallest root component that can hold the tun: it **only** launches, stops,
  reloads, and status-checks sing-box against the single fixed config path
  `$ALLE_HOME/singbox.json` — it never parses `state.json`, never runs the
  engine, never sees credentials. The WireGuard keys still reach a root
  process (sing-box itself, exactly as on the sudo path), but the helper adds
  no second consumer of them. The helper authenticates each request by the
  peer's kernel-verified uid (macOS `LOCAL_PEERCRED`), accepting only the one
  installing user (and root); the protocol carries no file paths, so the
  helper cannot be talked into `exec`-ing an arbitrary binary as root. Install
  it only on a machine you trust the installing user on: anyone who can drive
  the helper can ask root to run the pinned sing-box (nothing more, but that
  is a real root-backed action).
- **The tun captures every local user's traffic, not just yours.** A system
  route table is machine-wide: once alle owns the default route, traffic from
  **other OS users** on the machine is pulled through your channels and your
  provider account too. This is the multi-user gap above, made materially
  worse — it is no longer "another user *may* reach your proxy port," it is
  "every user's traffic *is* on your tunnel by default." Do not enable tun
  mode on a shared machine.
- **IPv6 is blocked, not leaked.** The supported providers' WireGuard configs
  are IPv4-only, so IPv6 cannot ride the tunnel. Leaving it alone would let
  every IPv6 connection bypass the VPN and expose the home address next to
  the VPN'd IPv4 — so the tun seizes the IPv6 default route too and rejects
  what it captures ("no IPv6 while on the VPN"). LAN-direct still passes
  local IPv6 when enabled; everything returns to normal when tun is off.

**Kill-switch honesty.** With TUN mode on, enforcement still lives in the
sing-box process. If it crashes, the tun and its routes vanish and the kernel
falls back to the physical route — traffic fails **open** for the ~2s
supervision window. A firewall-anchored always-on kill-switch (macOS PF
anchor, Linux nftables that outlive the process) is future hardening, not part
of the v1 model. See the tun runbook for the recovery path.

## Credentials at rest

| Secret                        | Where                                           | Protection                                                                                                                                                                                                                     |
| ----------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Provider tokens               | `~/.alle/credentials.yaml`                      | `0600`, never echoed back                                                                                                                                                                                                      |
| WireGuard private keys        | `~/.alle/state.json`, generated sing-box config | `0600` / `0400`; in [TUN mode](#tun-mode-the-elevated-trust-surface) the generated config is additionally read by a root sing-box (sudo path) or a `cap_net_admin` binary (Linux setcap path)                                  |
| Web UI secret                 | `~/.alle/control_api.json`                      | `0600`                                                                                                                                                                                                                         |
| sing-box stats secret         | `~/.alle/clash_api.json`                        | `0600`                                                                                                                                                                                                                         |
| Setup bundles (`alle export`) | wherever you save them                          | `0600` on export; **the file is a secret**                                                                                                                                                                                     |
| Setup rollback journal        | `~/.alle/setup-journal.json`                    | `0600`, transient — holds the pre-change credentials while a compound setup change (token update, bundle apply, provider removal) is in flight, and is used to roll them back if the change fails or crashes before committing |

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
executed. A pre-provisioned binary (`ALLE_SINGBOX=<path>`, e.g. an air-gapped
host or a baked image) is held to the same pin: verified on every start, and
a mismatch is a hard error — alle never downloads over or beside a path the
operator chose. The Docker image deliberately ships **no** sing-box; the
container fetches and verifies the pinned build into its state volume on
first start, exactly like a host install.

The same pin-everything discipline covers the build inputs: every GitHub
Action is pinned to an immutable commit SHA, the Dockerfile's base images are
pinned by manifest digest (the tag is only a comment), and Python
dependencies install from the committed `uv.lock`. CI counterparts keep the
pins honest: the image is built and boot-tested on every PR, OSV scans the
lockfile and Trivy scans the built image's layers (weekly and pre-merge, so
advisories published *after* a pin still surface), and Dependabot bumps all
three pin families on the same weekly cadence.
