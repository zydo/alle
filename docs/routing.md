# Rule-based routing

Besides the per-channel ports, `alle` runs one **router entrypoint** — a single
local HTTP+SOCKS proxy that dispatches each connection by rule to a channel, to
`direct` (no VPN), or to `block`. The entrypoint is always on: with no rules it
is a transparent pass-through, and traffic only uses a VPN exit once you wire a
rule to one. Its port is assigned once and stays stable (`alle status` shows it),
so apps and future OS-level profiles can point at it permanently.

```bash
alle routes ruleset create Streaming --via nordvpn/wg_us_1 --domain netflix.com --domain hulu.com
alle routes ruleset create LocalDirect --via direct --cidr 192.168.0.0/16
alle routes ruleset create BlockTrackers --via block --domain tracker.example.com
alle routes ruleset create DefaultVPN --via nordvpn/wg_jp_1 --all
alle routes ls
```

## How rules work

- **Rulesets** are the authoring model: a named, ordered block of matchers that
  all share one exit (`<provider>/<channel>`, `direct`, or `block`). Block order
  is priority: **first matching ruleset wins**. Reorder blocks with
  `alle routes reorder rs3 rs1 rs2`.
- **Matchers** inside a ruleset are unordered because same-target matchers
  commute. Use `--domain` for a destination domain — it matches the domain
  *and all of its subdomains* (dot-boundary) — `--cidr` for destination
  IP/CIDR, `--geosite`/`--geoip` for community rule-set categories (see
  [Geo matchers](#geo-matchers) below), and `--all` for a catch-all. A
  matcher that can never win because an earlier ruleset covers it is flagged
  as *shadowed* in `routes ls`.
- **Unmatched traffic goes direct** — without a VPN — like other modern VPN
  clients. To block unmatched traffic instead (a kill-switch for the router
  entrypoint), turn it on explicitly: `alle routes killswitch on`. Per-channel
  ports are never affected by the kill-switch.
- **LAN/local traffic stays direct by default.** Built-in rules for private,
  link-local, and multicast ranges — plus the well-known UDP ports of LAN
  housekeeping protocols (DHCP 67/68, SSDP 1900, mDNS 5353), which cover their
  unicast legs — are compiled ahead of every user rule, so a catch-all VPN rule
  never cuts off printers, NAS boxes, router admin pages, or LAN discovery —
  the same protection mainstream VPN clients ship. Inspect or disable with
  `alle routes lan [on|off]` (leaving it on is recommended).
- **Channels referenced by rules cannot be removed.** `alle channels rm` (and
  `alle providers rm`, for any of its channels) refuses while a rule targets the
  channel, listing every referencing rule and the exact `alle routes rm …` to
  run first. Remove the rules, then the channel — routing config never changes
  as a side effect of something else.
- Per-channel ports keep working exactly as before, with or without rules — the
  router is an addition, never a replacement.

## The built-in LAN block: one toggle, fixed contents

The LAN-direct block is deliberately **not configurable**: one toggle
(`alle routes lan on|off`), a fixed list of ranges and UDP ports, and full
transparency (`alle routes lan -v`; the `lan.cidrs`/`lan.udp_ports` fields of
`GET /api/v1/routes`). The contents encode protocol facts — private/link-local/
multicast ranges, DHCP/SSDP/mDNS ports — not preferences, and the block sits in
the most privileged position in the rule table: ahead of every user rule and
outside the kill-switch. An editable list there would have ugly failure modes
in both directions (removing entries breaks LAN in ways that surface much
later; adding entries silently punches permanent tunnel bypasses — port 53
would re-open exactly the DNS leak the hijack ordering prevents).

Customization lives in **user rules** instead, where ordering is explicit and
the shadow lint watches your back:

- **Need something extra to go direct?** Give it its own ruleset and order it
  above your catch-all. Example — a network that uses CGNAT space the built-in
  list rightly doesn't cover, such as Tailscale's `100.64.0.0/10`:

  ```bash
  alle routes ruleset create Tailscale --via direct --cidr 100.64.0.0/10
  alle routes ls                        # note the ids — new rulesets append last
  alle routes reorder rs4 rs1 rs2 rs3   # put Tailscale ahead of the catch-all
  ```

  If you skip the reorder while a catch-all covers it, `routes ls` flags the
  Tailscale matcher as shadowed — nothing fails silently.

- **Need *less* excluded than the built-in block?** Turn it off
  (`alle routes lan off`) and recreate just the ranges you want as your own
  `direct` ruleset — user rules can express the whole CIDR list.

If you hit a network where the fixed list is genuinely wrong and user rules
cannot express the fix, please open an issue: the planned evolution, should a
real case appear, is *subtractive-only* overrides (disabling individual
built-in entries — which only ever narrows the bypass surface), never
user-added entries.

## Related

- The Web UI's Router rules page edits the same rules visually, with drag
  reordering — see [web-ui.md](web-ui.md).
- Rules round-trip through the declarative bundle
  ([declarative-config.md](declarative-config.md)).
- Command syntax: [`alle routes`](cli-reference.md) in the CLI reference.
- Fail-closed semantics (what happens when a rule's channel is missing) are
  in [security.md](security.md).

## Geo matchers

`--geosite` and `--geoip` match traffic against community-maintained databases:

- **geosite** matches by destination domain category — e.g. `netflix`,
  `google`, `category-ads-all`, `apple@cn`. The data comes from the
  [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)
  project, compiled to sing-box rule-set format by
  [SagerNet/sing-geosite](https://github.com/SagerNet/sing-geosite).
- **geoip** matches by destination IP's country — e.g. `us`, `cn`, `de`. The
  data comes from GeoLite2, compiled by
  [SagerNet/sing-geoip](https://github.com/SagerNet/sing-geoip).

```bash
# Route Netflix through a US channel
alle routes ruleset create Streaming --via nordvpn/wg_us_1 --geosite netflix

# Block known ad/tracker domains
alle routes ruleset create "Ad block" --via block --geosite category-ads-all

# Route all Chinese IPs direct (no VPN)
alle routes ruleset create "CN direct" --via direct --geoip cn
```

### Looking up categories and their contents (plaintext)

The `.srs` files alle downloads are binary, but everything in them is
browsable as plaintext at the source:

- **geosite** — category names and their full domain lists live in
  [v2fly/domain-list-community's `data/` directory](https://github.com/v2fly/domain-list-community/tree/master/data):
  one file per category, one domain per line. The filename is the category
  name (`data/netflix` → `--geosite netflix`); a file can `include:` other
  files, and lines tagged `@ads` etc. power attribute forms like
  `google@ads`. To see exactly which domains `geosite:netflix` matches, read
  [`data/netflix`](https://github.com/v2fly/domain-list-community/blob/master/data/netflix).
- **geoip** — categories are simply lowercase
  [ISO 3166-1 alpha-2 country codes](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2)
  (`us`, `de`, `cn`, …), plus `private` for RFC1918/link-local space. The IP
  ranges come from GeoLite2 and change with its updates; there is no
  stable per-country plaintext to link, but the code *is* the category name.

Offline, after at least one `alle routes geo refresh`, the recorded manifest
answers "what names exist" without any network:

```bash
alle routes geo ls netflix           # search categories matching "netflix"
alle routes geo ls --kind geosite    # list geosite categories (first 50)
alle routes geo ls cn --json         # scripting form
```

The same list backs `GET /api/v1/routes/geo/categories?q=…`, typo
suggestions on failed adds ("did you mean: …"), and the upstream links shown
in the Web UI's rule editor and in `alle routes geo` status output.

### Data fetching and updates

Geo data is **fetched on demand, never auto-updated** — consistent with alle's
no-background-traffic posture:

- The first time you add a rule referencing a category, alle downloads the
  matching `.srs` file from the upstream and caches it locally
  (`<state>/rulesets/`). Each file is a few KB; only referenced categories are
  fetched, not a monolithic database.
- Bundle imports that reference uncached categories also fetch them at apply
  time — this is the second networked step (besides token provider resolution).
- To update: `alle routes geo refresh` re-pins both databases to the current
  upstream and re-downloads every referenced category. Run this when you want
  fresh ad-block lists or newer IP data.

Integrity: each cached file is sha256-verified against a recorded digest on
every use. The upstream publishes no signatures, so the model is commit-pinning
— the branch head commit is resolved at fetch time, and the immutable
commit-pinned raw URL is used for the download.

Switching the upstream: `alle routes geo source metacubex` (alternative:
[MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat), which
includes `-lite` variants). Categories will be re-fetched on the next
`refresh`.
