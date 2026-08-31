# alle and vopono

[vopono](https://github.com/jamesmcm/vopono) is a Rust CLI that runs individual
applications through VPN tunnels using temporary Linux network namespaces. It
solves a related but structurally different problem from alle: isolating
**processes**, not routing **requests**.

## The shape of each

**vopono** — you launch an application into a namespace tied to a provider and
server: `vopono exec --provider mullvad --server us-newyork firefox`. That
namespace gets its own network stack, WireGuard or OpenVPN tunnel, and firewall
kill switch; the app runs inside it until it exits. Launching another app with
the same provider/server reuses the namespace; a different server means a new
one. Up to 255 namespaces can be live at once. A `sync` step fetches provider
server lists and configs up front; a `--custom` flag accepts any raw WireGuard
or OpenVPN config for providers vopono doesn't integrate directly, plus
OpenConnect and OpenFortiVPN for enterprise VPNs.

**alle** — one process, many WireGuard channels live at the same time, and a
router entrypoint matches every request by domain, CIDR, or geosite/geoip
category and sends it to a channel, straight out, or nowhere. The unit of
control is the request, not the process that made it — a single already-running
browser can have some domains exit through one channel and others through a
different one, with no relaunch.

## Where they differ

|                             | vopono                                                                                                                                         | alle                                                                           |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Isolation unit              | A Linux network namespace per app/process tree                                                                                                 | One process; a rule table matched per request                                  |
| Choosing the exit           | Which namespace you launch the app into                                                                                                        | A rule table matched per request                                               |
| Splitting one app's traffic | Not possible — the whole namespace shares one tunnel                                                                                           | Yes — different domains from the same app can exit differently                 |
| Providers                   | 9 direct integrations, plus any config via `--custom`                                                                                          | 2 today ([22 planned](vpn-provider-research.md), close to gluetun's list)      |
| Protocols                   | WireGuard + OpenVPN, plus OpenConnect/OpenFortiVPN via `--custom`                                                                              | WireGuard ([OpenVPN planned](vpn-provider-research.md) — sing-box 1.14 ready, provider path not yet built) |
| Kill switch                 | Per-namespace firewall rules (WireGuard and OpenVPN); applied when the namespace is created, not refreshed for apps launched into it afterward | Route rules; unmatched traffic blocked                                         |
| Control                     | CLI (`vopono exec`/`sync`), optional systemd daemon mode                                                                                       | CLI, REST API, Web UI                                                          |
| Credential storage          | OpenVPN credentials stored in plaintext (documented upstream limitation)                                                                       | Provider credentials in `credentials.yaml`, `0600`                             |
| Runs on                     | Linux only — network namespaces are a Linux kernel feature                                                                                     | macOS, Linux, Docker, or as a host service; Windows planned                    |

## Worked example: three exits, routed by destination

Same requirement as the [gluetun comparison](gluetun-comparison.md): a
streaming domain exits in the US, a test endpoint in Japan, a bank in the UK,
everything else goes direct.

### With vopono

vopono's unit is the process, so you launch a separate instance of each app
into its own namespace:

```bash
vopono sync   # fetch provider configs once

vopono exec --provider mullvad --server us-newyork firefox        # streaming
vopono exec --provider mullvad --server jp-tokyo curl https://test.internal.example
vopono exec --provider mullvad --server uk-london firefox         # banking
```

That gets three isolated tunnels running concurrently. What it can't express is
"`netflix.com` goes through the US exit, no matter which app opens it" — the
namespace is the whole network stack for that process tree, so a single browser
window can't have some tabs on the US exit and others on the UK exit. Splitting
streaming and banking means two separate browser launches (and, in practice,
separate profiles so cookies/sessions don't cross), not two rules against one
already-running browser.

### With alle

One process, three channels, and a rule table — the same example as the
gluetun comparison:

```bash
API=http://alle:8080/api/v1
AUTH="Authorization: Bearer $ALLE_API_SECRET"

for c in "United States" "Japan" "United Kingdom"; do
  curl -sS -X POST "$API/channels" -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"provider\":\"nordvpn\",\"country\":\"$c\"}"
done

curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Streaming","target":"nordvpn/wg_us_1","matchers":["netflix.com"]}'
curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Test","target":"nordvpn/wg_jp_1","matchers":["test.internal.example"]}'
curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Bank","target":"nordvpn/wg_gb_1","matchers":["bank.example"]}'
```

The same browser, already running, now sends `netflix.com` through the US
channel and `bank.example` through the UK channel — no second launch, no
separate profile.

## Choose vopono when

- You're on Linux and process-level isolation (a real, kernel-enforced network
  namespace per app) is what you want, not request-level routing.
- Your provider isn't one of alle's supported ones but exports a plain
  WireGuard or OpenVPN config — vopono's `--custom` flag takes it directly, no
  per-provider integration work needed on either side.
- You need OpenVPN, OpenConnect, or OpenFortiVPN today. alle speaks WireGuard
  only; its engine (sing-box 1.14) can now do OpenVPN and OpenConnect, but
  alle's provider path for them is [planned](vpn-provider-research.md), not
  shipped.
- Launching each app fresh into its target VPN fits your workflow — a one-off
  job, a throwaway browser profile — rather than steering an app that's
  already running.

## Choose alle when

- You want traffic split by **destination**, not by which app or process sent
  it — the same browser, different domains, different exits, with nothing to
  relaunch.
- You're not on Linux. Network namespaces are Linux-only, so vopono has no
  macOS or Windows story; alle runs on macOS, Linux, Docker, or as a host
  service.
- You want a REST API and Web UI to manage exits and rules programmatically,
  not just a CLI wrapper around namespace creation.
- You want channels that stay up and get reused across many requests and apps,
  rather than a fresh namespace and process launch per exit.
