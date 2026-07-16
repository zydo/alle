# The setup bundle — backup, migration, and declarative config

One YAML file — the **bundle** — describes an entire alle setup: providers
(with their credentials), channels of both archetypes, rulesets, and the
router toggles. It serves three jobs with the same format:

1. **Backup** — `alle export` now, re-apply after a reinstall.
2. **Migration** — export on one machine, import on another.
3. **Declarative config** — hand-write a bundle from scratch and apply it to a
   fresh install as a startup config. `alle export` is just the convenient way
   to produce one; nothing about the format assumes it was machine-generated.

```bash
alle export                                             # -> alle-backup-<date>-<time>.yaml (0600)
alle import alle-backup-20260709-143022.yaml            # merge into the current setup
alle import alle-backup-20260709-143022.yaml --replace  # REPLACE the current setup (confirms)
```

The Web UI's **Bundle** page offers the same operations (export downloads the
file; import uploads one, merge or replace).

> **The file is a secret.** A bundle contains WireGuard private keys and
> provider access tokens — everything needed to recreate (and use) the setup.
> The CLI writes it `0600`; a browser download lands in your Downloads folder
> with default permissions, so treat it like a password file either way.

## Contents

- [The setup bundle — backup, migration, and declarative config](#the-setup-bundle--backup-migration-and-declarative-config)
  - [Contents](#contents)
  - [Format](#format)
    - [Header](#header)
    - [Providers and channels](#providers-and-channels)
    - [Router and rulesets](#router-and-rulesets)
    - [Hand-written examples](#hand-written-examples)
  - [How a bundle is applied](#how-a-bundle-is-applied)
    - [Validation: all-or-nothing](#validation-all-or-nothing)
    - [Merge (default)](#merge-default)
    - [Replace (`--replace`)](#replace---replace)
    - [Token channels: `wg` is derived state](#token-channels-wg-is-derived-state)
    - [Config channels: `wg` is the config](#config-channels-wg-is-the-config)
  - [What never travels in a bundle](#what-never-travels-in-a-bundle)
  - [Caveats](#caveats)
    - [Cloning a setup to a second machine](#cloning-a-setup-to-a-second-machine)
    - [Old backups can overwrite newer credentials](#old-backups-can-overwrite-newer-credentials)
    - [Smaller notes](#smaller-notes)

## Format

A single YAML document. JSON is accepted too (YAML is a superset of JSON), so
a machine-generated JSON bundle imports unchanged; `alle export` always emits
YAML because hand-written config wants comments.

### Header

```yaml
kind: alle-bundle     # required — identifies the file
bundle_version: 1     # required — a newer version is refused with a clear error
```

### Providers and channels

```yaml
providers:
  nordvpn:                        # a known provider key (nordvpn, protonvpn)
    credential:                   # REQUIRED for token providers
      token: "nordvpn-access-token"   # or token_env / token_file (see below)
    channels:
      united_states_1:            # the channel id — its permanent handle (required)
        country: United States    # required for token providers
        city: ""                  # empty = any city
        label: Streaming — US     # optional display label
        port: 20010               # optional — DECLARE the local proxy port (see below)
        enabled: false            # optional — import held-but-not-dialled (default: true)
        wg: { ... }               # OPTIONAL for token providers (see below)
  protonvpn:                      # config providers have no credential
    channels:
      proton_us_1:                # the channel id (required)
        country: United States    # optional display label ("" -> "(Unknown)")
        city: California          # optional display label
        wg:                       # REQUIRED for config providers
          private_key: "...44-char base64..."
          address: [10.2.0.2/32]
          peer:
            public_key: "...44-char base64..."
            endpoint_host: 185.159.157.1
            endpoint_port: 51820
            preshared_key: null        # optional
            allowed_ips: [0.0.0.0/0, "::/0"]   # optional, this is the default
            keepalive: 25                       # optional, this is the default
```

The `wg` rule follows the two provider archetypes:

- **Token providers (NordVPN):** the provider's `credential` (its access
  token) is **required** — alle needs it to resolve servers and to add
  channels, so a bundle with a NordVPN section but no token is rejected. Each
  channel's `wg` is *optional*: the WireGuard parameters are derived state (the
  API resolves them from the token and location), so a hand-written channel can
  be as small as `{country: Sweden}`. When `wg` is present (an export always
  includes it), it serves as a fallback snapshot, not as the primary source
  ([details below](#token-channels-wg-is-derived-state)).

  Any credential field can be **indirect** instead of inline: `token_env:
  NORDVPN_TOKEN` reads an environment variable, `token_file: /run/secrets/…`
  reads a file (compose/k8s secrets) — exactly one of the three spellings per
  field, resolved at validation time so a missing source is an ordinary
  blocker. This is what lets a hand-written bundle live in version control
  without carrying the secret; `alle export` always writes the stored value
  inline (an export is a backup, not a template). See
  [declarative-config.md](declarative-config.md) for authoring guidance.
- **Config providers (Proton VPN):** `wg` is *required*. There is no API to
  derive anything from — the values from the downloaded `.conf` **are** the
  channel's configuration. Validation rejects a config channel without them.

**What a config channel requires, and what's optional.** The only required
fields are the **channel id** (the mapping key) and **`wg`**. `country` and
`city` are *optional display labels* — for config channels they never affect
routing, identity, or resolution (the id is the sole handle), so an omitted
country simply shows as "(Unknown)". This deliberately matches the `.conf`
import path, which parses country/city best-effort from the filename and
leaves them blank when it can't: a channel imported that way exports with an
empty country and must restore cleanly, so the bundle can't be stricter.
Declarative config is where you'd *fill them in* when you know them —
`{country: United States, city: California, wg: {...}}` — rather than relying
on the filename heuristic. (For **token** channels, by contrast, `country` is
always required — it's the input the provider API resolves a server from, and
`alle validate` checks it against the provider's real country list — see
[token channels](#token-channels-wg-is-derived-state).)

Channel ids are lowercase slugs (`a-z`, `0-9`, `_`) and are the upsert
identity: importing a bundle updates the channel with the same
`provider`/`id` in place. When importing a `.conf`, alle derives this id by
slugging the filename (e.g. `wg_us_ca_842`); in a declarative bundle you write
the id directly as the channel key, so the id — not a filename — is what
names and pins the channel. Keeping the same id as an existing `.conf`-derived
channel means a later re-import of a freshly downloaded `.conf` still updates
it in place.

**`enabled` round-trips, and unstated means keep.** The administrative
enable/disable state (`alle channels disable` — see the CLI reference) is
part of the setup: exports write `enabled` **explicitly on every channel**
(a bundle reader never needs an absent-key rule — same discipline as
`lan_direct`), so applying a backup reproduces the split exactly. In a
hand-written bundle the key is optional and tri-state, like the router
toggles: explicit `true`/`false` applies that state, an **omitted key on
`import` (merge) leaves an existing channel's state untouched** — so
re-applying a bundle (e.g. a container entrypoint importing it on every
start) never undoes an ad-hoc `channels disable` — and a new channel
defaults to enabled. On `import --replace` (restore), omitted simply means
enabled. A **disabled** channel is imported without ever touching the
provider: **no server resolution** (its `country`/`city` are checked against
the provider's location catalog instead — skipped with a note if the catalog
is unreachable), **no probe**, no connection slot occupied. It keeps an
existing same-location channel's live `wg`, else the bundle's `wg` snapshot,
else it lands wg-less and `alle channels enable` resolves a server at that
moment. Two mirrors of the live restrict-only rule, both refused at
validate/apply with nothing changed: a bundle ruleset cannot target a channel
the bundle disables, and an import cannot disable a channel an existing
routing rule still targets.

### Router and rulesets

```yaml
router:
  port: 20000            # optional — declare the router entrypoint's port
  killswitch: false      # block unmatched router traffic (default: false)
  lan_direct: true       # built-in LAN/local direct rules (default: true)
  rulesets:              # priority order — first matching ruleset wins
    - name: Streaming
      target: nordvpn/united_states_1    # <provider>/<channel>, direct, or block
      matchers:
        - netflix.com          # bare strings are inferred, exactly like the CLI:
        - api.example.com      #   domains match themselves + all subdomains
        - 10.8.0.0/16          #   IP/CIDR -> ip_cidr
        - {type: domain_suffix, value: cdn.netflix.com}   # or explicit {type, value}
    - name: Everything else
      target: direct
      matchers: [all]          # the catch-all
```

List order in the file **is** the priority order. Rule/ruleset ids (`r1`,
`rs1`) are internal allocation artifacts — never written to a bundle, always
minted fresh on apply.

### Hand-written examples

Authoring a bundle from scratch — the skeleton, per-provider guides (a
token-only NordVPN entry, translating a Proton VPN `.conf` field by field),
rulesets, and a full worked example — has its own guide:
**[declarative-config.md](declarative-config.md)**. This page is the format
reference and apply semantics that guide builds on.

## How a bundle is applied

### Validation: all-or-nothing

The entire file is checked before anything mutates — every WireGuard field,
matcher, credential shape, and target reference — and rejected as a whole
with a per-entry list, all problems in one pass, **each with the line number**
it occurs on. Network resolution for token channels also happens **before**
the first mutation, so a mid-apply failure cannot leave a half-applied bundle.

Run the same checks without applying anything — a pre-import dry run — with
[`alle validate <file>`](cli-reference.md#alle-validate-file) (or the **Validate
file** button on the Web UI Bundle page), which additionally checks each token
channel's country/city against the provider's real location list. The full
checklist (header, supported providers, token, unique channel ids, country/city,
WireGuard fields, explicit router toggles, ruleset targets and matcher types)
lives with the [`alle validate` reference](cli-reference.md#alle-validate-file).

### Merge (default)

`alle import <file>` — nothing is removed; the bundle is layered onto the
current setup:

- Providers are added if missing; a bundle credential that differs from the
  stored one **replaces it, and the summary says so explicitly** (see
  [caveats](#old-backups-can-overwrite-newer-credentials)).
- Channels upsert by `(provider, id)`: existing ones update in place (local
  port kept), new ids are created (fresh port). A channel that declares a
  `port:` is the exception — the declaration wins, re-pointing an existing
  channel if needed. Channels identical to the bundle are reported as
  unchanged and left untouched.
- **Rulesets always append at the bottom of the priority order.** Under
  first-match-wins an appended block can never hijack existing routing.
  Re-importing the same bundle therefore duplicates its rulesets — the shadow
  lint in `alle routes ls` flags the dead copies; remove and `reorder` as
  needed.
- `killswitch` / `lan_direct` are applied when present in the file, left
  alone when absent.
- A ruleset target may reference a channel from the bundle *or* one that
  already exists locally.

### Replace (`--replace`)

`alle import <file> --replace` — the bundle becomes the entire setup:
providers, channels, credentials, rulesets, and toggles not in the bundle are
**removed**. Destructive — the CLI prompts (or requires `--yes` off-TTY); the
Web UI double-confirms. Two differences from a merge:

- The bundle must be self-contained: ruleset targets must reference channels
  defined in the bundle.
- Credentials are replaced wholesale — a provider in the bundle without a
  `credential` entry ends up with none stored.

### Token channels: `wg` is derived state

For token providers, the authoritative configuration is *provider + token +
location*; the WireGuard parameters are a cache of what those resolve to. The
token itself is **required** (validation rejects a token provider without one),
both so servers can be resolved and because adding channels later needs it.
Both apply modes settle each token channel in this order:

1. **The channel already exists locally with the same country/city** → its
   current live parameters are kept. No API call, no key churn, no needless
   reconnect — re-applying the same bundle on the same machine is a no-op.
2. **New identity, or the location changed** (the migration case) → a **fresh
   server is resolved via the provider token**. The account key is derived
   once per provider, then each channel gets the currently recommended server
   for its location. The bundle's snapshot is ignored.
3. **Fresh resolution fails** — offline, provider API down, or the token is
   rejected → the bundle's **`wg` snapshot is used as a fallback** so the apply
   still succeeds. The summary lists these channels, and the daemon's probe +
   auto-reconnect resolve a fresh server automatically once the API is
   reachable again. (A wg-less token channel whose resolution fails and has no
   snapshot to fall back on fails the apply.)

This is why exports still include the snapshot even though it is usually
ignored: it is the guarantee that a restore works offline and can never be
broken by a provider outage.

### Config channels: `wg` is the config

Proton-style channels are applied exactly as written, always. There is
nothing to re-derive — refreshing them means downloading a new `.conf` from
the provider portal and re-importing it (the stable filename-derived id makes
that an in-place update). This is inherent to the config-provider archetype,
not a bundle limitation.

## What never travels in a bundle

- **Auto-assigned ports.** Local proxy ports and the router entrypoint port
  are local allocations, and `alle export` never serializes them. On apply, a
  channel whose `(provider, id)` already exists keeps its current local port
  (a same-machine restore preserves your app configs); new identities get
  fresh ports. **After migrating to a new machine, repoint apps at the ports
  shown by `alle status`.** The exception is a *hand-written* `port:`
  declaration (see the format above): declared ports apply as written on
  every machine — that is what they are for (compose files, firewall rules) —
  and a declaration that clashes with an existing port is rejected, never
  silently moved.
- **Runtime state.** Probe results, latency/IP measurements, speed tests,
  traffic totals, and reconnect bookkeeping are not exported; a restore
  starts with clean probes and the daemon measures everything anew.
- **Internal ids.** Rule/ruleset ids are re-minted on every apply.

## Caveats

### Cloning a setup to a second machine

A bundle makes it easy to run the *same* setup on two machines at once —
and for token providers both machines then share the **same account-scoped
WireGuard key** (NordVPN derives one key pair per account, not per device).
WireGuard identifies a peer by its public key and routes to the most recent
handshake's address, so **two clients using the same key against the same
server fight over the session** — connectivity flaps on both.

In practice:

- **Migration** (the old machine stops using the setup) — no issue.
- **Cloning** (both machines online) — fine as long as the two machines'
  channels land on *different servers*. The fresh-resolve-on-import behavior
  helps here: a new machine picks the currently recommended server rather
  than reusing the exported one, so collisions are unlikely but not
  impossible (two channels for the same city can still land on the same
  server). If both machines must run the same locations permanently,
  consider separate provider accounts, or accept that auto-reconnect will
  shuffle a failing channel to another server when the conflict bites.

The Web UI shows this warning on export; the same applies to copying
`state.json` around by hand.

### Old backups can overwrite newer credentials

Both merge and `--replace` apply the bundle's credential. If you rotated a provider
token after the backup was made, applying the old bundle replaces the working
token with a revoked one. This never happens silently — the summary prints
`credential for <provider> REPLACED` — but it is your job to re-add the
current token afterwards if the old one is dead.

### Smaller notes

- **Forward compatibility:** a bundle with a `bundle_version` newer than the
  installed alle is refused with an upgrade hint rather than misparsed.
- **Two files, one commit point:** credentials are written before state; the
  state transaction (a single transaction for the whole merge or replace) is
  the commit that triggers the reconcile. The whole apply runs as a *setup
  transaction*: the pre-apply credentials are journalled first, and any
  failure — or a crash, healed automatically on the next setup change or
  daemon start — rolls the credentials back, so an interrupted apply leaves
  the setup exactly as it was.
- **The daemon picks changes up automatically** — `import` ends by ensuring
  the runtime is up, so applying a bundle onto a fresh install starts serving
  without a separate `alle start`.
