# The alle REST API

alle's daemon (`alle run`) serves a REST API at `/api/v1` — the same interface
the bundled Web UI uses, exposed for scripts, sibling containers, and anything
else that manages alle programmatically. It is a 1:1 projection of the same
service layer the CLI drives: everything the CLI can do to channels,
providers, routing, and lifecycle, the API can do.

This document is the public contract. Endpoints not listed here (the login
and session machinery the browser UI uses, the `/` page and its assets) are
internal and may change without notice.

A machine-readable OpenAPI 3.1 description of the same contract lives in
[openapi.yaml](openapi.yaml) — use it for client codegen and request
validation; this page stays the prose authority where the two could ever
disagree.

## Reaching the API

`alle status` prints the canonical REST base explicitly, and `alle status
--json` exposes it as `rest_api`. It is the Web UI origin plus `/api/v1`: both
surfaces share one listener, but REST requests use the separate Bearer contract
below. Status never prints the secret.

**Host installs (default):** the server binds `127.0.0.1` on a per-install
port recorded in `~/.alle/control_api.json` (`{"address", "secret", "host"}`,
mode 0600). The port is stable across restarts.

**Network exposure (opt-in):** set `ALLE_API_LISTEN=<host>[:<port>]` — e.g.
`0.0.0.0:8080` in a compose file — and the server binds there instead. This
is an explicit operator decision, like publishing a port; nothing (including
the Docker image) sets it by default. A port-less value keeps the minted
contract port; an invalid value is logged and falls back to loopback — a typo
narrows, never widens. See the [security notes](#security-model) before
exposing.

## Authentication

Send the API secret as a Bearer token on every request:

```
Authorization: Bearer <secret>
```

The secret comes from, in order:

1. `ALLE_API_SECRET` — injected directly (compose `.env` interpolation).
2. `ALLE_API_SECRET_FILE` — path to a file holding the secret (compose
   secrets, k8s mounts). Trailing whitespace is stripped.
3. Otherwise: the minted per-install secret in `control_api.json`.

Set at most one of the two variables; setting both, an unreadable file, or a
value shorter than 16 characters makes the server **refuse to start** (logged
as `api: refusing to start`) rather than serve with a secret the operator did
not intend. The injected value must be set for the daemon *and* for any local
CLI or other API-client usage on the same machine (in a container, image-level
env covers both).

Cookie sessions, one-time login tokens, `POST /api/v1/login`, and
`POST /api/v1/logout` exist for the browser UI only and are **out of
contract** for API clients.

### Readiness without the secret

`GET /health?nonce=<string≤128>` is unauthenticated and answers
`{"proof": <HMAC(secret, "health:"+nonce)>}` — proof the process behind the
port is alle, without transporting the secret. Use it (or the Docker image's
built-in `HEALTHCHECK`) to gate dependents; a plain `HEAD /health` works as a
liveness probe.

## Conventions

- Requests and responses are JSON (`Content-Type: application/json`);
  request bodies are strictly validated — unknown fields, wrong types,
  missing `Content-Type`, malformed JSON, and bodies over 1 MiB are 4xx
  errors, never coerced.
- Errors are `{"error": "<message>"}` with:
  `400` invalid input or a refused operation (the message is human-readable
  and specific — blocker lists, provider rejections);
  `401` missing/wrong credential; `403` Host/Origin refusal; `404` unknown
  resource; `405` known resource, wrong method (with an `Allow` header);
  `413`/`415` framing; `503` a single-flight job (speed test, bundle import,
  token refresh) is already running.
- Boolean fields are strict JSON booleans: the *string* `"false"` is a 400.
- `?dry_run=` on destructive DELETEs accepts exactly `1`, `true`, `0`,
  `false`; anything else is a 400 (a typo must never turn a preview into the
  real removal). Body `dry_run` fields are plain booleans.
- Channel *refs* use the CLI grammar: a bare id (`wg_us_1`), a qualified
  `provider/channel` (`nordvpn/wg_us_1`), or a glob (`wg_us_*`). A bare id matching
  channels under several providers is refused; a glob may span providers.
- `/api/v1` is the versioned contract. Additive response fields may appear;
  request schemas, methods, and paths only change with the version.

## Read endpoints

| Endpoint                                                 | Returns                                                                                                                                                                                                                     |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/v1/status`                                     | The `alle status --json` snapshot: `running`, `state`, `router` (port, rule count, killswitch, lan_direct, tun), `daemon` (versions, skew), `web_ui` URL, `channels` (per-channel port, location, health, enabled), counts. |
| `GET /api/v1/providers`                                  | Added providers with channel counts (`alle providers ls --json`).                                                                                                                                                           |
| `GET /api/v1/providers/catalog`                          | The known-provider registry: names, kinds (`token`/`config`), credential fields.                                                                                                                                            |
| `GET /api/v1/channels`                                   | All channels with ports, locations, enabled state (`alle channels ls --json`).                                                                                                                                              |
| `GET /api/v1/routes`                                     | Routing rules/rulesets, kill switch, LAN policy (`alle routes ls --json`). Includes the built-in LAN-direct block's fixed contents read-only (`lan.cidrs`, `lan.udp_ports`) — visible without toggling anything.            |
| `GET /api/v1/locations?provider=<name>[&country=<name>]` | The provider's country/city catalog (API providers). `provider` is required.                                                                                                                                                |
| `GET /api/v1/metrics[?channel=<ref>]`                    | Cumulative per-channel traffic totals (`sent`, `received`, `updated_at` per channel). A cheap read — probes nothing; unlike `POST /test` it touches no network.                                                             |
| `GET /api/v1/logs[?lines=N]`                             | `{"text": "<last N log lines>"}`, N clamped to 1–1000, default 200.                                                                                                                                                         |
| `GET /api/v1/export`                                     | The full setup bundle as a YAML download. **Contains WireGuard private keys and provider tokens** — treat the response like a password file.                                                                                |

## Providers

| Endpoint                                      | Body                           | Effect                                                                                                                                                                                       |
| --------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/v1/providers`                      | `{"provider", "creds"?}`       | Add a provider. Token providers validate the credential first; re-posting an added one **replaces** its token and re-resolves its channels (idempotent add). Config providers take no creds. |
| `POST /api/v1/providers/<name>/token`         | `{"creds"}`                    | Replace an added token provider's credential (write-only; never returned).                                                                                                                   |
| `DELETE /api/v1/providers/<name>[?dry_run=1]` | —                              | Remove one provider and its channels. `dry_run` returns the removal plan.                                                                                                                    |
| `POST /api/v1/providers/remove`               | `{"names": [...], "dry_run"?}` | Batch removal, same semantics.                                                                                                                                                               |

## Channels

| Endpoint                                          | Body                                                                      | Effect                                                                                                                                                                 |
| ------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/v1/channels`                           | `{"provider", "country"?, "city"?, "label"?, "conf_text"?, "conf_name"?}` | Add a channel: `country`(+`city`) for API providers, `conf_text`(+`conf_name`) for config providers — mutually exclusive, exactly as the CLI's `--country`/`--config`. |
| `POST /api/v1/channels/<prov>/<id>/label`         | `{"label"}`                                                               | Rename the display label.                                                                                                                                              |
| `POST /api/v1/channels/<prov>/<id>/enabled`       | `{"enabled"}`                                                             | Enable/disable one channel. Disabling a channel a routing rule targets is refused with the blocker list.                                                               |
| `POST /api/v1/channels/enabled`                   | `{"refs": [...], "enabled", "provider"?, "all"?, "dry_run"?}`             | Batch enable/disable with the full CLI grammar (`refs` + optional provider scope, or `all` + `provider`). All-or-nothing: one blocked channel refuses the whole batch. |
| `DELETE /api/v1/channels/<prov>/<id>[?dry_run=1]` | —                                                                         | Remove one channel.                                                                                                                                                    |
| `POST /api/v1/channels/remove`                    | `{"refs": [...], "provider"?, "all"?, "dry_run"?}`                        | Batch removal, one state transaction.                                                                                                                                  |

## Routing

| Endpoint                                          | Body                             | Effect                                                                                                                                                                                   |
| ------------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/v1/routes/rulesets`                    | `{"name", "target", "matchers"}` | Create a ruleset. `matchers` entries are `{"value", "type"?}` objects or bare strings (domain/suffix/CIDR forms as in the CLI).                                                          |
| `POST /api/v1/routes/rulesets/<id>`               | `{"matchers"}`                   | Append matchers.                                                                                                                                                                         |
| `POST /api/v1/routes/rulesets/<id>/rename`        | `{"name"}`                       | Rename.                                                                                                                                                                                  |
| `POST /api/v1/routes/rulesets/<id>/target`        | `{"target"}`                     | Retarget.                                                                                                                                                                                |
| `POST /api/v1/routes/rulesets/<id>/update`        | `{"name", "target", "matchers"}` | Replace name+target+matchers atomically.                                                                                                                                                 |
| `DELETE /api/v1/routes/rulesets/<id>[?dry_run=1]` | —                                | Remove a ruleset.                                                                                                                                                                        |
| `DELETE /api/v1/routes/<id>[?dry_run=1]`          | —                                | Remove one rule.                                                                                                                                                                         |
| `POST /api/v1/routes/move`                        | `{"ids": [...], "ruleset"}`      | Move matcher(s) into another ruleset in one transaction (they adopt its target; an emptied source ruleset dissolves).                                                                    |
| `POST /api/v1/routes/reorder`                     | `{"ids": [...], "flat"?}`        | Reorder rules/rulesets.                                                                                                                                                                  |
| `POST /api/v1/routes/killswitch`                  | `{"enabled"}`                    | Kill switch on/off. `enabled` is required — a missing field never silently disables it.                                                                                                  |
| `GET /api/v1/routes/geo`                          | —                                | Geo data source and cache state, incl. `upstreams` — the plaintext category-browsing URLs.                                                                                               |
| `POST /api/v1/routes/geo`                         | `{"action", "source"?}`          | `refresh` re-downloads referenced categories; `source` switches the upstream (sagernet/metacubex). Never auto-updates.                                                                   |
| `GET /api/v1/routes/geo/categories`               | `?kind=`, `?q=`                  | Search available category names offline (from the manifest recorded at refresh; empty until the first refresh).                                                                          |
| `POST /api/v1/routes/trace`                       | `{"destination"}`                | Which rule wins for a destination (offline dry-run; the DNS lookup for a domain is the only network I/O). Returns the verdict, the winning rule, a human reason, and the full rule walk. |
| `POST /api/v1/routes/lan`                         | `{"enabled"}`                    | LAN-direct policy.                                                                                                                                                                       |
| `POST /api/v1/tun`                                | `{"enabled"}`                    | TUN mode on/off. Privilege failures are a 400 whose message carries the platform recipe.                                                                                                 |

## Probes, lifecycle, bundles

| Endpoint                         | Body                                            | Effect                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/v1/test`              | `{"speed"?, "channel"?}`                        | Probe channels now. `speed: false` (default) returns one JSON result. `speed: true` streams `application/x-ndjson`: `{"type":"row","data":…}` per channel as it finishes, then exactly one `{"type":"done"…}` or `{"type":"error"…}` terminal record; 503 if a test is already running.                                                   |
| `POST /api/v1/lifecycle/start`   | `{}`                                            | Start the runtime.                                                                                                                                                                                                                                                                                                                        |
| `POST /api/v1/lifecycle/stop`    | `{}`                                            | Stop it (channels kept).                                                                                                                                                                                                                                                                                                                  |
| `POST /api/v1/lifecycle/restart` | `{}`                                            | Restart.                                                                                                                                                                                                                                                                                                                                  |
| `GET /api/v1/upgrade/check`      | —                                               | Ask the owning channel for the latest stable release: the Homebrew tap for brew, or PyPI for uv tool/pipx/pip. Returns `{"channel", "current", "latest", "update_available"}` and never checks in the background. Refuses a container, checkout, or unknown channel; prerelease opt-in is CLI-only.                                       |
| `POST /api/v1/upgrade`           | `{}`                                            | Upgrade alle to a newer stable release via its owning manager (Homebrew/uv tool/pipx/pip). Returns the checked `latest`, the delegated `command` when one ran, before/after versions, and restart disposition. Refuses a container, checkout, or unknown channel with 400; 503 while another upgrade runs. Prerelease opt-in is CLI-only. |
| `GET /api/v1/backup`             | —                                               | Scheduled-backup settings and on-disk rotation state.                                                                                                                                                                                                                                                                                     |
| `POST /api/v1/backup`            | `{"enabled"?, "dir"?, "every_hours"?, "keep"?}` | Configure scheduled local backups. Omitted fields keep their value; an explicit `enabled: true` writes a first backup immediately. Strict types — a string never toggles the schedule. Backups are written by the daemon (local file I/O only), `0600` in a user-owned `0700` directory.                                                  |
| `POST /api/v1/backup/now`        | `{}`                                            | Write one backup into the rotation directory immediately (works while the schedule is off).                                                                                                                                                                                                                                               |
| `POST /api/v1/validate`          | `{"text"}`                                      | Validate a setup bundle; blockers come back in the 400 message.                                                                                                                                                                                                                                                                           |
| `POST /api/v1/import`            | `{"text", "replace"}`                           | Apply a bundle: `replace: false` = idempotent merge, `true` = destructive whole-setup replace. 503 while another import runs.                                                                                                                                                                                                             |

An upgrade response may contain `restart` when alle restarted or scheduled its
own service restart. A brew-supervised daemon instead reports
`restart_pending: true` with `restart_owner: "homebrew"`; a running brew-owned
daemon that is not supervised by Homebrew reports `restart_required: true` and
the explicit `restart_command`. No restart field is present when the daemon was
not running or no package change was needed.

## Security model

The full threat model lives in `docs/security.md`; the API-relevant parts:

- **Auth is always required.** There is no unauthenticated mode, in Docker or
  anywhere else — this API can export your VPN credentials and disable your
  kill switch. Provision the secret instead (one `.env` line in compose).
- **The network bind is a trust statement.** `ALLE_API_LISTEN=0.0.0.0` makes
  the API reachable by everything on the container network (and the LAN, if
  you also publish the port). Bearer-over-plain-HTTP is the accepted model
  *inside* a private compose network — the same trust you give a database
  password there. Crossing hosts or untrusted networks needs a
  TLS-terminating reverse proxy in front; alle does not do TLS.
- **The browser UI does not follow the API onto the network.** On a network
  bind, only Bearer-authenticated requests and `/health` accept a
  non-loopback `Host`; the login page, assets, and cookie sessions stay
  loopback-only.
- **Never share alle's state volume** with another container to "read the
  secret" — the volume also holds `credentials.yaml` (WireGuard private keys,
  provider tokens). Inject the secret instead.

## Example: a sibling container in compose

```yaml
# .env
ALLE_API_SECRET=change-me-openssl-rand-hex-32
```

```yaml
services:
  alle:
    image: ziyudo/alle:latest
    restart: unless-stopped
    environment:
      ALLE_API_LISTEN: "0.0.0.0:8080"
      ALLE_API_SECRET: ${ALLE_API_SECRET}
    volumes:
      - alle-state:/var/lib/alle
      - ./bundle.yaml:/etc/alle/bundle.yaml:ro

  manager:
    image: alpine/curl
    depends_on:
      alle:
        condition: service_healthy
    environment:
      ALLE_API_SECRET: ${ALLE_API_SECRET}
    command: >
      sh -c 'curl -s -H "Authorization: Bearer $$ALLE_API_SECRET"
      http://alle:8080/api/v1/status'

volumes:
  alle-state:
```

More calls, from any sibling:

```bash
AUTH="Authorization: Bearer $ALLE_API_SECRET"
curl -s -H "$AUTH" http://alle:8080/api/v1/channels
curl -s -H "$AUTH" http://alle:8080/api/v1/metrics
curl -s -H "$AUTH" -X POST -H 'Content-Type: application/json' \
  -d '{"refs":["us_*"],"enabled":false,"provider":"nordvpn"}' \
  http://alle:8080/api/v1/channels/enabled
curl -s -H "$AUTH" -X POST -H 'Content-Type: application/json' \
  -d '{"enabled":true}' http://alle:8080/api/v1/routes/killswitch
```
