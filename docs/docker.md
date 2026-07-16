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
- **Runs as user `alle` (uid 1000)** when started as root (the default), after
  fixing volume ownership. TUN/gateway mode needs `ALLE_RUN_AS_ROOT=1` (plus
  the capability grants below).
- **Trust boundary = the container network.** `ALLE_LISTEN=0.0.0.0` makes
  channel and router proxy ports reachable from the container's network —
  and *only* from it, unless you `-p`-publish a port. Publishing a proxy port
  publishes an unauthenticated proxy: only ever do it onto networks you
  trust. The Web UI stays loopback-only inside the container; manage with
  `docker exec <name> alle …`. (`docker exec` enters as root while the daemon
  runs as the `alle` user — that's safe: a root-run CLI preserves the state
  files' unprivileged owner on every write, so an exec'd mutation can never
  lock the daemon out of its own state dir.)
- **Deterministic ports:** the image sets `ALLE_PORT_BASE=20000`, so ports
  are allocated sequentially from 20000 instead of OS-randomly. For full
  control, declare `port:` per channel (and `router: {port: …}`) in the
  bundle — declared ports are honored as written and collide loudly instead
  of silently moving.
- **HEALTHCHECK** runs `alle health` (daemon + sing-box liveness, strict
  exit code).

## Configuration: the bundle is the desired state

Mount a [declarative bundle](declarative-config.md) at
`/etc/alle/bundle.yaml` (override the path with `ALLE_BUNDLE`); the
entrypoint applies it with `alle import` on **every start**, which merges
idempotently — so the container always converges on the declared setup, and a
broken bundle fails the start loudly in `docker logs`.

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
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro
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
    environment:
      ALLE_RUN_AS_ROOT: "1"      # v1: tun mode runs as container root
      NORDVPN_TOKEN: ${NORDVPN_TOKEN}
    volumes:
      - alle-state:/var/lib/alle
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro

  app:
    image: some/app
    network_mode: service:alle   # ALL of app's traffic rides alle's netns
    depends_on:
      alle:
        condition: service_healthy

volumes:
  alle-state:
```

Then enable tun once (persisted in the volume):

```bash
docker exec alle alle tun on
```

Notes for joined containers (`network_mode: service:alle`): they share alle's
network identity, so any port they serve must be published on the **alle**
service, and they resolve DNS through alle's hijack like everything else in
the netns. With `killswitch` on and the tunnel down, joined containers have
no network — that is the point.

## Invariant (why none of this affects your host install)

Container behavior is keyed on explicit env the image sets (`ALLE_CONTAINER`,
`ALLE_LISTEN`, `ALLE_PORT_BASE`, `ALLE_SERVICE`) — none is set on a host, so
binds stay loopback-only, ports stay OS-assigned, and lifecycle stays with
`alle start`/`alle daemon install`. The only container-*detected* behaviors
are refusals and hint text (e.g. `alle daemon install` explaining restart
policies), never a silent change of binds, ports, or lifecycle.
