# Running alle in Docker

This page is the **image design and trust model**. For step-by-step compose
recipes (bundle authoring, secrets, verification, day-2 operations,
troubleshooting), see **[docker-compose.md](docker-compose.md)**.

The container image is a deployment profile of the headless core (CLI + Web
UI) — not a fourth surface. Everything container-specific is **opt-in**: a
host install of alle behaves exactly as before (loopback-only proxy ports,
OS-assigned port numbers, `alle daemon install` lifecycle); the image simply
ships with the container knobs turned on.

The official image is [`ziyudo/alle`](https://hub.docker.com/r/ziyudo/alle) on
Docker Hub. The examples use `latest`; pin a release tag instead when you want
reproducible deployments.

Two ways to use it:

1. **Proxy hub** — other containers on the same Docker network (or, if you
   publish ports, other machines) point their egress at
   `alle:<router-port>` or a channel port.
2. **Gateway container (tun mode)** — alle seizes the *container's own*
   network namespace with the same tun + kill-switch core used on hosts;
   other containers join it with `network_mode: service:alle` and get
   full-tunnel VPN. If the tunnel drops with the kill-switch on, joined
   containers go dark instead of leaking — and the host's routes are never
   touched.

## Quickstart (plain `docker run`)

```bash
docker pull ziyudo/alle:latest
docker run -d --name alle --restart unless-stopped \
  --mount type=volume,src=alle-state,dst=/var/lib/alle \
  --mount type=bind,src="$PWD/bundle.yaml",dst=/etc/alle/bundle.yaml,readonly \
  ziyudo/alle:latest
docker exec alle alle status        # manage with the same CLI, via exec
```

The container is configured declaratively: the mounted
[setup bundle](declarative-config.md) is synced on every start (tokens can
stay out of the file via `token_env`/`token_file` — see below). Use the long
`--mount` syntax as shown: with short `-v`, a missing host file silently
mounts an empty *directory* at the bundle path — `--mount` makes it a
`docker run` error instead, and the entrypoint refuses a directory or other
non-regular file at the bundle path rather than skipping it. Run without a
bundle mount for the interactive profile (configure via `docker exec`/Web
UI), or set `ALLE_BUNDLE=none` to declare the no-bundle profile explicitly.

## Image design

- **PID 1 is `alle run`** — the daemon loop in the foreground, logging to
  stderr (`docker logs`) and to `alle.log` (`docker exec <name> alle logs -f`)
  alike. Use a restart policy; there is no launchd/systemd in the container
  and `alle daemon install` refuses accordingly.
- **No sing-box in the image.** The pinned build is downloaded and
  SHA-256-verified on first start, into the state volume — one fetch per
  volume, not per container. (This is deliberate: the image redistributes no
  GPL code. An offline variant with a baked binary is a possible later
  addition.)
- **State lives in one volume** at `/var/lib/alle` (`ALLE_HOME`): providers,
  channels, keys, the generated sing-box config, logs, and the sing-box
  binary cache. Recreate containers freely; keep the volume.
- **Proxy mode is uid 1000 in OCI metadata and at runtime.** A named volume
  inherits the image's ownership; bind-mounted state must be owned by uid 1000.
  The explicit gateway exception uses `user: "0"`, `ALLE_RUN_AS_ROOT=1`, and
  the capability grants below.
- **Trust boundary = the container network.** `ALLE_LISTEN=0.0.0.0` makes
  channel and router proxy ports reachable from the container's network —
  and *only* from it, unless you `-p`-publish a port. Publishing a proxy port
  publishes an unauthenticated proxy: only ever do it onto networks you
  trust. The control API/Web UI stays loopback-only inside the container by
  default; manage with `docker exec <name> alle …`, or opt into the
  Bearer-authenticated REST API for siblings with `ALLE_API_LISTEN` +
  `ALLE_API_SECRET` (see below). (`docker exec` enters as root while the daemon
  runs as the `alle` user — that's safe: a root-run CLI preserves the state
  files' unprivileged owner on every write, so an exec'd mutation can never
  lock the daemon out of its own state dir.)
- **Deterministic ports:** the image sets `ALLE_PORT_BASE=20000`, so ports
  are allocated sequentially from 20000 instead of OS-randomly. For full
  control, declare `port:` per channel (and `router: {port: …}`) in the
  bundle — declared ports are honored as written and collide loudly instead
  of silently moving.
- **HEALTHCHECK** runs `alle health` with a strict exit code. Proxy mode checks
  daemon + sing-box liveness. The gateway profile additionally gates on its
  declared fail-closed policy, accepted runtime generation, TUN interface,
  control API, privileges, and a viable channel.

## Configuration: the bundle is the desired state

Mount a [declarative bundle](declarative-config.md) at
`/etc/alle/bundle.yaml` (override the path with `ALLE_BUNDLE`); the
entrypoint converges on it with `alle sync` on **every start**, and a broken
bundle fails the start loudly in `docker logs`, before anything is imported.

Sync is the *managed* apply mode: everything it creates is marked as owned by
the bundle, and each boot updates or prunes **only that owned state** —

- the same bundle across N restarts is idempotent (state stays
  byte-identical; rulesets never duplicate);
- an edited bundle updates each managed channel/ruleset once, in place, at
  its existing priority position;
- entries removed from the bundle are pruned — including a dropped
  provider's credential — while channels and rulesets you created ad hoc
  (via `docker exec` or the Web UI) are never touched, and a pruned channel
  that one of *your* rules still references is kept and reported instead of
  breaking your routing.

Interactive `alle import` keeps its append/merge semantics — sync provenance
exists only for this startup path.

Keep tokens out of the file with credential indirection (also works on
hosts):

```yaml
kind: alle-bundle
bundle_version: 1
providers:
  nordvpn:
    credential:
      token_env: NORDVPN_TOKEN        # or: token_file: /run/secrets/nordvpn
    channels:
      us_1:
        country: United States
        port: 20010                    # declared → stable → publishable
      sweden_1:
        country: Sweden
        enabled: false                 # held, not dialled (no connection slot)
router:
  port: 20000
  killswitch: false
  lan_direct: true
```

**Channel enable/disable across restarts.** `enabled` in a channel spec is
tri-state: `false` holds the channel without dialling it (its declared port
is **not listening** while disabled — a service pointing at it gets
connection-refused until you enable it), `true` forces it on, and an
**omitted** key means the re-applied bundle *keeps the channel's current
state* — so flipping channels ad hoc (`docker compose exec alle alle
channels disable us_1`, or the Web UI toggle) survives container restarts.
Under a provider connection cap, that lets the bundle declare the whole
stable of servers while you rotate which ones are live at runtime. Disabled
channels never touch the provider at import time (no server resolution — the
country/city are checked against the provider's location catalog instead),
which also means a bundle full of held spares applies fine offline.

## Proxy hub (compose)

```yaml
services:
  alle:
    image: ziyudo/alle:latest
    restart: unless-stopped
    volumes:
      - alle-state:/var/lib/alle
      - type: bind                        # long syntax: a missing host file
        source: ./bundle.yaml             # fails `up` instead of mounting a
        target: /etc/alle/bundle.yaml     # fresh empty directory
        read_only: true
    environment:
      NORDVPN_TOKEN: ${NORDVPN_TOKEN}
    # ports:                     # only if the LAN should reach the proxies
    #   - "20000:20000"          # router entrypoint

  app:
    image: some/app
    environment:
      # any proxy-aware app: point it at the router (rules decide the exit)
      ALL_PROXY: socks5h://alle:20000
    depends_on:
      alle:
        condition: service_healthy

volumes:
  alle-state:
```

Manage the running instance without opening anything:

```bash
docker exec alle alle status
docker exec alle alle test
docker exec alle alle logs -f
```

### Programmatic control: the REST API

When a *sibling service* (not you at a terminal) needs to manage alle —
rotate channels, flip the kill switch, read metrics — expose the REST API to
the compose network instead of scripting `docker exec`:

```yaml
  alle:
    environment:
      ALLE_API_LISTEN: "0.0.0.0:8080"      # explicit opt-in; default stays loopback
      ALLE_API_SECRET: ${ALLE_API_SECRET}  # required — there is no unauthenticated mode
```

Siblings call `http://alle:8080/api/v1/…` with
`Authorization: Bearer $ALLE_API_SECRET`; `/health` gates readiness without
the secret. Full contract in [api.md](api.md), a worked sibling-container
example in [docker-compose.md](docker-compose.md), and the trust analysis in
[security.md](security.md) — in short: the API can export your VPN
credentials, so the secret is mandatory, the port stays unpublished, and the
state volume is never shared as a way to read it.

## Gateway container (tun mode)

The tun core captures only the container's netns — the privilege is granted
at `docker run` time (`sudo`/`setcap`/the macOS helper do not apply in
containers):

```yaml
services:
  alle:
    image: ziyudo/alle:latest
    restart: unless-stopped
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun]
    user: "0"
    environment:
      ALLE_RUN_AS_ROOT: "1"      # v1: tun mode runs as container root
      ALLE_GATEWAY: "1"          # declare tun + kill switch before readiness
      NORDVPN_TOKEN: ${NORDVPN_TOKEN}
    volumes:
      - alle-state:/var/lib/alle
      - type: bind
        source: ./bundle.yaml
        target: /etc/alle/bundle.yaml
        read_only: true

  app:
    image: some/app
    network_mode: service:alle   # ALL of app's traffic rides alle's netns
    depends_on:
      alle:
        condition: service_healthy

volumes:
  alle-state:
```

`ALLE_GATEWAY=1` privilege-checks and declares TUN plus the kill switch before
PID 1 starts. Health stays red—and `service_healthy` dependants stay
unstarted—until the interface, accepted route generation, control plane, and
a viable channel all hold. Activation failure therefore never opens a direct
egress window.

Notes for joined containers (`network_mode: service:alle`): they share alle's
network identity, so any port they serve must be published on the **alle**
service, and they resolve DNS through alle's hijack like everything else in
the netns. With `killswitch` on and the tunnel down, joined containers have
no network — that is the point.

This is namespace-scoped routing, not host takeover. A host application can
instead use a deliberately `-p`-published router/channel proxy port, but that
is per-app proxying and the proxy is unauthenticated: publish only to a trusted
network. Host networking, host PID, `--privileged`, and mounted host-network
control paths are unsupported; they enlarge the trust and failure boundary,
and Docker Desktop's Linux VM cannot transparently take over macOS routing.

## Invariant (why none of this affects your host install)

Container behavior is keyed on explicit env the image sets (`ALLE_CONTAINER`,
`ALLE_LISTEN`, `ALLE_PORT_BASE`, `ALLE_SERVICE`) — none is set on a host, so
binds stay loopback-only, ports stay OS-assigned, and lifecycle stays with
`alle start`/`alle daemon install`. The only container-*detected* behaviors
are refusals and hint text (e.g. `alle daemon install` explaining restart
policies), never a silent change of binds, ports, or lifecycle.
