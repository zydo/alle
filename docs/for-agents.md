# alle for coding agents

Entry point for integrating **alle** into a project. Read this, then fetch only
the pages you need — every link below is a raw URL you can retrieve.

alle keeps **several VPN exits live at once** and picks one **per request** from
a rule table. A single background daemon owns the state; the CLI, Web UI, and
REST API are three faces of it. Use it when a program needs traffic to leave
from different countries at the same time.

**Wrong tool if** your provider is not NordVPN or Proton VPN (the common case —
[compare with gluetun](https://raw.githubusercontent.com/zydo/alle/main/docs/gluetun-comparison.md)),
you need OpenVPN today, one exit for everything is enough, or you want
per-process namespace isolation on Linux rather than per-request routing
([compare with vopono](https://raw.githubusercontent.com/zydo/alle/main/docs/vopono-comparison.md)).

## The model

| Noun                  | Meaning                                                                                             |
| --------------------- | --------------------------------------------------------------------------------------------------- |
| **Provider**          | An account. `nordvpn` (API token) or `protonvpn` (`.conf` import).                                  |
| **Channel**           | One VPN exit — a WireGuard peer with its own local HTTP+SOCKS port. Id looks like `wg_us_1`.        |
| **Ruleset**           | Matchers (domain / CIDR / geosite / geoip) pointing at one target: a channel, `direct`, or `block`. |
| **Router entrypoint** | One HTTP+SOCKS port; traffic sent here is matched top to bottom, first match wins.                  |

## What it can do

Add, probe, rotate, and remove exits at runtime · route by destination to a
specific exit, direct, or blocked · kill switch (unmatched traffic blocked) ·
built-in LAN bypass · per-channel traffic totals and speed tests · whole-machine
capture via TUN mode · whole setup exported and re-imported as one YAML bundle ·
run as a host service or a container, including as a gateway other containers
join.

## Facts that change how you write the code

- The REST base URL is **per-install, not a fixed port** — read `rest_api` from
  `GET /api/v1/status` (or `alle status --json`). Bearer auth, loopback unless
  `ALLE_API_LISTEN` opts in. `GET /health?nonce=` is unauthenticated: use it as
  the readiness gate.
- **Channel proxy ports are OS-assigned** unless explicitly declared. Read them;
  never hardcode.
- **Never invent a channel id** — ids come from provider location data.
- **Rule order is law**, first match wins. `POST /routes/trace` answers which
  rule wins for a destination without sending traffic.
- **A probe is not a connection.** WireGuard is connectionless; health comes
  from the last probe, and no "connected" event will ever arrive.
- Removing or disabling a channel a ruleset targets is **refused** until the
  ruleset is retargeted. Destructive calls accept `dry_run=1`.
- Speed tests stream NDJSON and end with exactly one terminal record.
- `GET /api/v1/export` contains **private keys and tokens**. Never log it.
- CLI credential commands **prompt** unless given `--token` and `--yes`, which
  hangs unattended scripts.
- Prefer declaring the whole setup as a bundle (`POST /validate`, then
  `POST /import`) over a sequence of imperative calls: it is idempotent and
  survives a rebuild.

## Fetch when you need it

| Page                                                                                                                                                                                | When                                              |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| [REST API](https://raw.githubusercontent.com/zydo/alle/main/docs/api.md) · [openapi.yaml](https://raw.githubusercontent.com/zydo/alle/main/docs/openapi.yaml)                       | Writing any code against alle                     |
| [CLI reference](https://raw.githubusercontent.com/zydo/alle/main/docs/cli-reference.md)                                                                                             | Shelling out instead of using REST                |
| [Rule-based routing](https://raw.githubusercontent.com/zydo/alle/main/docs/routing.md)                                                                                              | Designing the rule table, kill switch, LAN bypass |
| [Declarative setup](https://raw.githubusercontent.com/zydo/alle/main/docs/declarative-config.md) · [bundle format](https://raw.githubusercontent.com/zydo/alle/main/docs/bundle.md) | Reproducible setup, CI, rebuilds                  |
| [Docker](https://raw.githubusercontent.com/zydo/alle/main/docs/docker.md) · [Compose walkthrough](https://raw.githubusercontent.com/zydo/alle/main/docs/docker-compose.md)          | Containers, sibling services, secrets             |
| [How it works](https://raw.githubusercontent.com/zydo/alle/main/docs/how-it-works.md)                                                                                               | Debugging surprising behaviour                    |
| [Security model](https://raw.githubusercontent.com/zydo/alle/main/docs/security.md)                                                                                                 | Exposing the API beyond loopback                  |
| [Getting started](https://raw.githubusercontent.com/zydo/alle/main/docs/getting-started.md)                                                                                         | Installing on a host                              |
| [Current status](https://raw.githubusercontent.com/zydo/alle/main/docs/status.md)                                                                                                   | Is this provider or platform supported            |
| [TUN runbook](https://raw.githubusercontent.com/zydo/alle/main/docs/tun-runbook.md)                                                                                                 | Whole-machine capture and rollback                |

Read `GET /api/v1/status` before acting — most mistakes are a stale assumption
about what exists and what is healthy.
