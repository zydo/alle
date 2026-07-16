# alle with Docker Compose — a walkthrough

Step-by-step recipes for running alle as a compose service. This is the
how-to companion to **[docker.md](docker.md)** (the image design and trust
model) — read that page's security notes before publishing any port.

Both recipes share the same shape: one `alle` service holding the VPN
channels, configured **declaratively** by a bundle file that is applied on
every container start, with other services either *pointing at* alle's proxy
ports (proxy hub) or *joining* its network namespace (gateway).

## Contents

- [alle with Docker Compose — a walkthrough](#alle-with-docker-compose--a-walkthrough)
  - [Contents](#contents)
  - [Prerequisites](#prerequisites)
  - [Step 1 — author the bundle](#step-1--author-the-bundle)
  - [Step 2 — proxy hub: point services at alle](#step-2--proxy-hub-point-services-at-alle)
  - [Step 3 — verify](#step-3--verify)
  - [Variant — VPN gateway: route a whole container through alle](#variant--vpn-gateway-route-a-whole-container-through-alle)
  - [Using compose secrets instead of environment variables](#using-compose-secrets-instead-of-environment-variables)
  - [Day-2 operations](#day-2-operations)
  - [Troubleshooting](#troubleshooting)

## Prerequisites

A registry-published image is planned; until then, build it from the repo:

```bash
git clone https://github.com/zydo/alle && cd alle
docker build -t alle .
```

## Step 1 — author the bundle

The bundle is the single input: providers, channels, routing rules, and —
important for compose — **declared ports**, so your compose file can name
them. Save as `bundle.yaml` next to your `compose.yaml`:

```yaml
# bundle.yaml — applied (idempotent merge) on every container start
kind: alle-bundle
bundle_version: 1
providers:
  nordvpn:
    credential:
      token_env: NORDVPN_TOKEN       # resolved from the container env at boot;
                                     # the token never lives in this file
    channels:
      united_states_1:
        country: United States
        port: 20010                  # declared → stable → publishable
      japan_1:
        country: Japan
        port: 20011
router:
  port: 20000                        # the rule-routed entrypoint
  killswitch: false
  lan_direct: true
  rulesets:
    - name: US streaming
      target: nordvpn/united_states_1
      matchers: [netflix.com, hulu.com]
    - name: Everything else direct
      target: direct
      matchers: [all]
```

Notes:

- **Declare `port:` on anything a compose file must reference.** Undeclared
  ports still work but are allocated (from `ALLE_PORT_BASE=20000` in the
  image), so their numbers depend on creation order — fine for
  `docker exec` use, wrong for hard-coded compose wiring.
- **`token_env` / `token_file`** keep the secret out of the file, so
  `bundle.yaml` can live in the same repo as your compose file. Config
  providers (Proton VPN) instead embed their `wg:` block — translate the
  downloaded `.conf` per [declarative-config.md](declarative-config.md).
- Check the file before ever starting a container: `alle validate bundle.yaml`
  (on any machine with alle installed).

## Step 2 — proxy hub: point services at alle

Each service that should exit through a VPN sets an ordinary proxy variable
(or app-level proxy setting) at alle's service name — no privileges, no
network tricks:

```yaml
# compose.yaml
services:
  alle:
    image: alle
    restart: unless-stopped
    volumes:
      - alle-state:/var/lib/alle                 # channels/keys/binary cache survive recreates
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro   # the declarative setup
    environment:
      NORDVPN_TOKEN: ${NORDVPN_TOKEN}            # from .env or your shell
    # Publish ONLY if machines outside this compose network need the proxies.
    # A published proxy port is an unauthenticated open proxy to whatever can
    # reach it — publish onto trusted networks only (see docker.md).
    # ports:
    #   - "20000:20000"

  app:
    image: some/app
    environment:
      # socks5h:// = the app's DNS also resolves through the tunnel
      ALL_PROXY: socks5h://alle:20000            # router → rules pick the exit
      # or pin one exit: socks5h://alle:20010    # the US channel directly
    depends_on:
      alle:
        condition: service_healthy               # image ships a HEALTHCHECK

volumes:
  alle-state:
```

```bash
NORDVPN_TOKEN=... docker compose up -d
```

First start downloads the pinned sing-box into the volume (checksum-verified;
the health check's start period allows for it). Every later start is instant
and offline-safe.

## Step 3 — verify

```bash
docker compose exec alle alle status          # channels healthy, router :20000
docker compose exec alle alle test            # per-channel exit IP + latency
docker compose exec app sh -c \
  'wget -qO- https://api.ipify.org'           # → a VPN exit IP, not your egress
docker compose logs alle                      # the daemon's operation log
```

Anything you would do on a host works through `docker exec alle alle …` —
add channels, edit routes, run speed tests. Prefer editing `bundle.yaml` for
anything permanent, though: it is the version-controlled source of truth, and
the next `docker compose restart alle` converges on it.

## Variant — VPN gateway: route a whole container through alle

For apps that can't speak proxy (or to capture *all* of a container's
traffic), run alle as the network namespace other services join. This is tun
mode scoped to the container — the host's routes are never touched:

```yaml
services:
  alle:
    image: alle
    restart: unless-stopped
    cap_add: [NET_ADMIN]               # tun creation + route table (this netns only)
    devices: [/dev/net/tun]
    environment:
      ALLE_RUN_AS_ROOT: "1"            # v1: tun mode runs as container root
      NORDVPN_TOKEN: ${NORDVPN_TOKEN}
    volumes:
      - alle-state:/var/lib/alle
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro
    # ports for JOINED services are declared HERE (they share this netns):
    # ports:
    #   - "8080:8080"                  # e.g. app's web port

  app:
    image: some/app
    network_mode: service:alle         # every packet rides alle's rules/tunnels
    depends_on:
      alle:
        condition: service_healthy

volumes:
  alle-state:
```

Enable tun once (the flag persists in the volume across restarts):

```bash
docker compose up -d
docker compose exec alle alle tun on
docker compose exec app sh -c 'wget -qO- https://api.ipify.org'   # → VPN exit IP
```

Gateway-mode facts worth knowing:

- **Kill-switch**: `docker compose exec alle alle routes killswitch on` makes
  unmatched traffic *block* instead of going direct — joined containers go
  dark if the tunnel drops, rather than leaking your real egress.
- **Joined containers share alle's network identity**: `network_mode:
  service:alle` means no `ports:` of their own (declare them on the alle
  service), no separate hostname, and DNS answered through alle's hijack.
- **Restart coupling**: recreating the alle container tears down the shared
  netns — compose restarts joined services with it. Expect the pair to bounce
  together.

## Using compose secrets instead of environment variables

Environment variables are visible to `docker inspect`; compose secrets mount
as files, which pairs with `token_file`:

```yaml
services:
  alle:
    image: alle
    restart: unless-stopped
    secrets: [nordvpn_token]
    volumes:
      - alle-state:/var/lib/alle
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro

secrets:
  nordvpn_token:
    file: ./secrets/nordvpn_token     # one line: the access token

volumes:
  alle-state:
```

and in the bundle:

```yaml
credential:
  token_file: /run/secrets/nordvpn_token
```

## Day-2 operations

- **Change the setup** — edit `bundle.yaml`, then `docker compose restart
  alle`. Import is an idempotent merge: changed channels update in place,
  unchanged ones aren't touched, and removed *rulesets* re-append (prune old
  ones with `alle routes …` or do a one-off destructive sync:
  `docker compose exec alle alle import --replace --yes /etc/alle/bundle.yaml`).
- **Upgrade alle** — rebuild the image from a newer checkout, then
  `docker compose up -d` (recreates the container; the volume carries the
  setup over). There is no in-container self-upgrade — the image is
  immutable by design.
- **Backup** — `docker compose exec alle alle export --out - >
  backup.yaml`. The output contains secrets; treat it like a password file.
- **Watch health** — `docker compose ps` shows the health state;
  `docker compose exec alle alle health` gives the strict exit code for
  scripts/monitoring.

## Troubleshooting

| Symptom                                                   | Likely cause / fix                                                                                                                                                                                    |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Container exits immediately at start                      | The bundle failed validation — the entrypoint fails loudly. `docker compose logs alle` shows every blocker with line numbers; fix `bundle.yaml`.                                                      |
| `environment variable 'NORDVPN_TOKEN' is not set` in logs | The variable didn't reach the container: set it in `.env`, the shell, or switch to compose secrets + `token_file`.                                                                                    |
| A service can't reach `alle:20010`                        | The port isn't declared in the bundle (allocation order gave it another number — `docker compose exec alle alle status` shows actual ports), the channel is **disabled** (a disabled channel's port isn't listening — `alle channels ls` shows STATUS; enable it), or the two services are on different compose networks. |
| First start is slow / unhealthy briefly                   | The one-time sing-box download into the volume. The health check's start period covers it; it never repeats while the volume lives.                                                                   |
| `alle tun on` says it needs privileges                    | The gateway variant's three grants are missing: `cap_add: [NET_ADMIN]`, `devices: [/dev/net/tun]`, `ALLE_RUN_AS_ROOT=1` — then recreate the container.                                                |
| Joined container has no network at all                    | Kill-switch doing its job while the tunnel is down (check `docker compose exec alle alle status`), or the alle container restarted and the joined service needs its compose-driven restart to finish. |
| Web UI unreachable from the host                          | By design: it stays loopback-only inside the container (its auth model is built for one loopback caller). Use `docker exec` for management.                                                           |
