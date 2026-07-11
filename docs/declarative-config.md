# Writing an alle setup as declarative config

alle is normally driven imperatively — `alle providers add`, `alle channels
add`, the Web UI. But the **same setup can be written as one YAML file** and
applied in a single step:

```bash
alle import my-setup.yaml     # merge the file into the current setup
```

That file is a *bundle* (the same format `alle export` produces). This guide
is about **authoring one by hand, provider by provider** — for provisioning a
new machine, version-controlling a setup, sharing a template, or bulk-creating
channels. For the complete field reference, the apply semantics (`import`
merge vs `--replace`), backup/migration, and caveats, see
**[bundle.md](bundle.md)**.

## Contents

- [Writing an alle setup as declarative config](#writing-an-alle-setup-as-declarative-config)
  - [Contents](#contents)
  - [The skeleton](#the-skeleton)
  - [NordVPN (token / API providers)](#nordvpn-token--api-providers)
  - [Proton VPN (config / `.conf` providers)](#proton-vpn-config--conf-providers)
  - [Rulesets](#rulesets)
  - [A complete example](#a-complete-example)
  - [Applying it](#applying-it)

## The skeleton

Every bundle has a two-line header and, optionally, `providers` and a
`router`:

```yaml
kind: alle-bundle      # required — identifies the file
bundle_version: 1      # required
providers:
  # per-provider entries — see below
router:
  killswitch: false    # optional (default false)
  lan_direct: true     # optional (default true)
  rulesets: []         # optional
```

The two archetypes fill their `providers` entry differently: **token
providers** carry a credential and let alle derive WireGuard material;
**config providers** carry no credential and you paste WireGuard material from
a downloaded `.conf`.

## NordVPN (token / API providers)

A token provider **requires** a `credential` block with its access token —
alle needs it to resolve WireGuard servers and to add channels, so a NordVPN
section without a token is rejected. Its channels are just a location you
pick, resolved from the token at apply time. **The recommended entry fills
only four things** — the token, each channel's country, an optional city, and
a readable label — and **leaves `wg` out** entirely, since it is derived
state, not something you write.

```yaml
providers:
  nordvpn:
    credential:
      token: "nordvpn-access-token"        # from https://my.nordaccount.com/dashboard/nordvpn/access-tokens (see below)
    channels:
      united_states_new_york_1:            # id — follow alle's convention (below)
        country: United States             # required — the resolver needs it
        city: New York                     # optional — omit for "any city"
        label: Work                        # optional but recommended
      sweden_1:                            # <country>_<n> when there's no city
        country: Sweden
        label: Default
```

Field rules for a token channel:

| Field                | Required?        | Notes                                                                                                                                      |
| -------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| channel id (the key) | **yes**          | lowercase slug (`a-z`, `0-9`, `_`); the permanent handle used by rulesets and status. See the convention below.                            |
| `country`            | **yes**          | it is the input the provider API resolves a server from.                                                                                   |
| `city`               | no               | omit for the fastest city in the country.                                                                                                  |
| `label`              | no (recommended) | friendly display name shown in status and the Web UI.                                                                                      |
| `wg`                 | **no — omit**    | resolved from the token at apply. If you *do* include one (an export always does), it is only a fallback used when the API is unreachable. |

**Channel id convention.** Use the same scheme alle applies when it names
channels itself: **`<country>_<n>`**, or **`<country>_<city>_<n>`** when you
pin a city — slugged (lowercase, non-alphanumerics → `_`), with `_<n>`
distinguishing multiple channels in the same location. So `United States` →
`united_states_1`, `United States` + `New York` → `united_states_new_york_1`,
a second Sweden channel → `sweden_2`. Any valid slug works, but matching the
convention keeps hand-written and alle-created channels consistent.

**Finding valid countries and cities.** The `country` and `city` you write
must match what NordVPN's API knows, so look them up rather than guessing —
two ways:

1. **With the alle CLI (authoritative — it reads the exact list alle resolves
   against):**

   ```bash
   alle locations nordvpn                             # every country + its city count
   alle locations nordvpn --country "United States"   # the cities in one country
   ```

   This reads NordVPN's *public* location list, so it works even before you
   add a token or the provider. Add `--json` to script it, or `--refresh` to
   force a re-fetch. Names match case-insensitively — copy the spelling this
   prints into your YAML, and a name that doesn't appear here won't resolve.

2. **On the NordVPN website:** browse the server locations at
   <https://nordvpn.com/servers/> for a human overview of what's available.
   The console spelling can differ slightly from the API's, so if a name you
   copied from the site doesn't resolve, cross-check it with
   `alle locations nordvpn` — the CLI is authoritative.

Getting the token: https://my.nordaccount.com/dashboard/nordvpn/access-tokens →
generate an access token. It is a secret — see
[the security note](#applying-it).

## Proton VPN (config / `.conf` providers)

A config provider has **no credential**. Each channel is authored from one
WireGuard `.conf` downloaded from the provider console (Downloads → WireGuard
configuration). `wg` is **required** — the values in the file *are* the
channel's configuration, since there is no API to derive them from.

Take a real download, `wg-US-CA-842.conf`:

```ini
[Interface]
# US-CA#842
PrivateKey = WEVH5Kek……………………SjSXo=
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
PublicKey = 2RxTx5co……………………JWmDA=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 79.127.185.162:51820
PersistentKeepalive = 25
```

Every WireGuard field maps straight across; only the identity and the labels
come from outside the file:

| Bundle field                              | Comes from                               | Notes                                                                                                                                                                                                |
| ----------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| channel id (`wg_us_ca_842`)               | the **filename**, slugged                | `wg-US-CA-842.conf` → drop `.conf`, lowercase, non-alnum → `_`. This is alle's own convention when it imports a `.conf`; matching it keeps a later re-import of a refreshed file an in-place update. |
| `wg.private_key`                          | `[Interface] PrivateKey`                 | required                                                                                                                                                                                             |
| `wg.address`                              | `[Interface] Address`                    | a YAML list                                                                                                                                                                                          |
| `wg.peer.public_key`                      | `[Peer] PublicKey`                       | required                                                                                                                                                                                             |
| `wg.peer.endpoint_host` / `endpoint_port` | `[Peer] Endpoint`, split on the last `:` |                                                                                                                                                                                                      |
| `wg.peer.allowed_ips`                     | `[Peer] AllowedIPs`                      | optional; omit for the `0.0.0.0/0, ::/0` default                                                                                                                                                     |
| `wg.peer.keepalive`                       | `[Peer] PersistentKeepalive`             | optional; defaults to 25                                                                                                                                                                             |
| `wg.peer.preshared_key`                   | `[Peer] PresharedKey`, if present        | optional; Proton configs usually have none (`null`)                                                                                                                                                  |
| `country` / `city`                        | **not in the file**                      | optional but recommended — copy them from the console where you downloaded the config. The filename parses best-effort (`US`/`CA`), but the console is authoritative.                                |
| `label`                                   | **you choose it**                        | optional but recommended                                                                                                                                                                             |

`[Interface] DNS` and the `# …` comment lines are ignored (alle reads only the
fields sing-box acts on), so they have no bundle equivalent.

The resulting entry — the same shape `alle export` produces:

```yaml
providers:
  protonvpn:                       # config provider — no credential block
    channels:
      wg_us_ca_842:                # from the filename (slugged)
        country: United States     # from the console (recommended)
        city: San Jose             # from the console (recommended)
        label: San Jose (Proton VPN)   # your choice (recommended)
        wg:
          private_key: WEVH5Kek……………………SjSXo=
          address:
            - 10.2.0.2/32
          peer:
            public_key: 2RxTx5co……………………JWmDA=
            endpoint_host: 79.127.185.162
            endpoint_port: 51820
            preshared_key: null
            allowed_ips:
              - 0.0.0.0/0
              - "::/0"             # quote the bare IPv6 default so YAML keeps it a string
            keepalive: 25
```

> The `private_key` above is masked for the doc. A real bundle carries the
> full key — that is why the file is a secret (see below).

## Rulesets

Rulesets point matched traffic at a channel, `direct`, or `block`. **List
order is priority — the first matching ruleset wins.** Matchers can be bare
strings (inferred exactly like `alle routes ruleset create`) or explicit
`{type, value}` mappings.

```yaml
router:
  rulesets:
    - name: Work traffic
      target: nordvpn/united_states_new_york_1   # <provider>/<channel-id>, direct, or block
      matchers:
        - github.com                   # domain -> github.com + *.github.com
        - api.example.com              # domains always cover their subdomains
        - 10.8.0.0/16                  # IP/CIDR -> ip_cidr
        - {type: domain_suffix, value: cdn.example.com}   # explicit form
    - name: Streaming via California
      target: protonvpn/wg_us_ca_842
      matchers: [netflix.com, hulu.com]
    - name: Everything else
      target: direct
      matchers: [all]                  # the catch-all
```

A `target` must name a channel that exists — one defined in this bundle, or
(for `import`) one already on the machine.

## A complete example

Both archetypes plus routing, in one applyable file:

```yaml
# my-setup.yaml — apply with: alle import my-setup.yaml
kind: alle-bundle
bundle_version: 1
providers:
  nordvpn:
    credential: {token: "nordvpn-access-token"}
    channels:
      united_states_new_york_1: {country: United States, city: New York, label: Work}
      sweden_1: {country: Sweden, label: Default}
  protonvpn:
    channels:
      wg_us_ca_842:
        country: United States
        city: California
        label: San Jose (Proton VPN)
        wg:
          private_key: WEVH5Kek……………………SjSXo=
          address: [10.2.0.2/32]
          peer:
            public_key: 2RxTx5co……………………JWmDA=
            endpoint_host: 79.127.185.162
            endpoint_port: 51820
            preshared_key: null
            allowed_ips: [0.0.0.0/0, "::/0"]
            keepalive: 25
router:
  killswitch: true
  rulesets:
    - name: Work traffic
      target: nordvpn/united_states_new_york_1
      matchers: [github.com, api.example.com]
    - name: Streaming via California
      target: protonvpn/wg_us_ca_842
      matchers: [netflix.com]
    - name: Everything via Sweden
      target: nordvpn/sweden_1
      matchers: [all]
```

## Applying it

```bash
alle validate my-setup.yaml             # check it first — every problem with line numbers
alle import my-setup.yaml               # merge into the current setup
alle import my-setup.yaml --replace     # or REPLACE the whole setup (confirms first)
```

Run `alle validate` while authoring: it checks the whole file at once (kind,
supported providers, token presence, unique channel ids, country/city against
the provider's real list, WireGuard fields, explicit router toggles, ruleset
targets and matcher types) and points at the line of each problem.

A few things to know — all covered in full in [bundle.md](bundle.md):

- **The file is a secret.** It holds WireGuard private keys and provider
  tokens. Keep it private; alle writes exported files `0600`.
- **The whole file is validated first** and rejected as a whole (per-entry
  errors) on any problem — an apply never half-applies.
- **Token channels resolve a fresh server** via the token at apply time;
  config channels apply exactly as written.
- **Ports are not set from the file** — they are allocated locally. After
  applying on a new machine, point apps at the ports from `alle status`.
- **Don't run one setup on two machines at once** without care — token
  channels share one account-scoped WireGuard key and can conflict
  ([details](bundle.md#cloning-a-setup-to-a-second-machine)).
