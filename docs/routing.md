# Rule-based routing

Besides the per-channel ports, `alle` runs one **router entrypoint** — a single
local HTTP+SOCKS proxy that dispatches each connection by rule to a channel, to
`direct` (no VPN), or to `block`. The entrypoint is always on: with no rules it
is a transparent pass-through, and traffic only uses a VPN exit once you wire a
rule to one. Its port is assigned once and stays stable (`alle status` shows it),
so apps and future OS-level profiles can point at it permanently.

```bash
alle routes ruleset create Streaming --via nordvpn/united_states_1 --domain netflix.com --domain hulu.com
alle routes ruleset create LocalDirect --via direct --cidr 192.168.0.0/16
alle routes ruleset create BlockTrackers --via block --domain tracker.example.com
alle routes ruleset create DefaultVPN --via nordvpn/japan_1 --all
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
  IP/CIDR, and `--all` for a catch-all. A matcher that can never win because
  an earlier ruleset covers it is flagged as *shadowed* in `routes ls`.
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
