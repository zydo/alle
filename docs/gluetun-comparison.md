# alle and gluetun

[gluetun](https://github.com/qdm12/gluetun) is the reference answer to "I want a
VPN inside Docker." It is mature, widely deployed, and supports far more
providers than alle does.

The two projects solve different halves of the problem. gluetun gives one
container **one** VPN tunnel and makes everything that joins it exit there. alle
keeps **several** tunnels live in one process and decides per destination which
one a request leaves through.

## The shape of each

**gluetun** — one tunnel per container. `VPN_SERVICE_PROVIDER`, credentials, and
a server filter are environment variables; the container brings up OpenVPN or
WireGuard, installs a firewall kill switch, and serves DNS-over-TLS plus built-in
HTTP, SOCKS5, and Shadowsocks proxies. Sibling containers attach with
`network_mode: "service:gluetun"` and every packet they send exits through that
one tunnel. Want a second location? Run a second container.

**alle** — many tunnels in one process. Each channel is a WireGuard peer exposed
as its own local HTTP+SOCKS proxy port, and a single router entrypoint matches
each request by domain, CIDR, or geosite/geoip category and sends it to a
channel, straight out, or nowhere. Channels from different providers run at the
same time.

## Where they differ

|                          | gluetun                                        | alle                                                                           |
| ------------------------ | ---------------------------------------------- | ------------------------------------------------------------------------------ |
| Concurrent VPN locations | One per container                              | Many in one process                                                            |
| Choosing the exit        | Which container you attach to                  | A rule table matched per request                                               |
| Providers                | 23, OpenVPN for all of them                    | 2 (growing — [22 planned](vpn-provider-research.md), close to gluetun's list)  |
| Protocols                | OpenVPN + WireGuard                            | WireGuard ([OpenVPN planned](vpn-provider-research.md) — sing-box 1.14 ready, provider path not yet built) |
| Kill switch              | Firewall rules in the container                | Route rules; unmatched traffic blocked                                         |
| Control API              | Status, start/stop, port forwarding, public IP | Status, start/stop, route rules, connection lifecycles, via CLI and REST API   |
| Runs on                  | Docker                                         | Docker, or as a host service (launchd / `systemd --user`)                      |
| Whole-machine capture    | Container netns; LAN devices via the gateway   | Same in a container, plus host TUN mode                                        |

## Worked example: three exits, routed by destination

The requirement: a streaming domain must exit in the US, a test endpoint in
Japan, a bank in the UK, and everything else goes direct. One machine.

### With gluetun

One VPN connection per container, so three locations means three containers —
each with its own credentials, health check, restart policy, and log stream:

```yaml
services:
  gluetun-us:
    image: qmcgaw/gluetun
    cap_add: [NET_ADMIN]
    devices: ["/dev/net/tun:/dev/net/tun"]
    environment:
      VPN_SERVICE_PROVIDER: nordvpn
      VPN_TYPE: wireguard
      WIREGUARD_PRIVATE_KEY: ${US_KEY}
      SERVER_COUNTRIES: United States
    ports: ["8888:8888/tcp"]

  gluetun-jp:   # same again, SERVER_COUNTRIES: Japan, port 8889
  gluetun-uk:   # same again, SERVER_COUNTRIES: United Kingdom, port 8890
```

That gets you three tunnels. It does **not** get you the routing — gluetun has
no rule table, so nothing yet knows that `netflix.com` belongs to the US tunnel.
You supply that layer yourself: a PAC file, an nginx/HAProxy front end that maps
destinations to upstream proxies, or per-application proxy settings. You own it,
and you keep it in sync with the containers by hand.

Then the day-2 costs land. A fourth country is a fourth container and a
redeploy. Rotating an exit means rewriting an env var and restarting a
container, dropping every connection through it. Three tunnels means three
copies of the VPN stack resident.

### With alle

One container, one process, three channels. The connections and the rules are
both created over the API:

```bash
API=http://alle:8080/api/v1
AUTH="Authorization: Bearer $ALLE_API_SECRET"

# three exits, live at the same time
for c in "United States" "Japan" "United Kingdom"; do
  curl -sS -X POST "$API/channels" -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"provider\":\"nordvpn\",\"country\":\"$c\"}"
done

# the routing that gluetun leaves to you
curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Streaming","target":"nordvpn/wg_us_1","matchers":["netflix.com"]}'
curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Test","target":"nordvpn/wg_jp_1","matchers":["test.internal.example"]}'
curl -sS -X POST "$API/routes/rulesets" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"Bank","target":"nordvpn/wg_gb_1","matchers":["bank.example"]}'
```

Sibling containers point at alle's router entrypoint and stop caring: each
request is matched against the table and leaves through the right country, with
everything unmatched going direct.

A fourth country is one more `POST /channels` — no redeploy, no new service, and
the three existing tunnels keep running. Rotation is a `POST` too: add the
replacement, retarget the ruleset, remove the old channel, with nothing else
interrupted. One process holds all of it, so the marginal cost of an exit is a
WireGuard peer and a listening port rather than a container.

### The trade

That flexibility costs you gluetun's provider coverage. If your provider is not
one of alle's two, the example above cannot run at all — which is exactly why
the next section exists.

## Choose gluetun when

- Your provider is one of the 23 it supports and not one of alle's two. This is
  the common case today, and it is a real reason to stop reading here.
- You need OpenVPN today, or a provider that only offers WireGuard through
  gluetun's custom-provider path. (alle's OpenVPN support is still only
  [planned](vpn-provider-research.md): sing-box 1.14 provides the OpenVPN
  client, but alle has not built the provider path on top of it yet.)
- One exit is all you want, and "every container in this stack goes through
  Sweden" is the whole requirement.
- You want provider-side port forwarding for a torrent client — alle has no
  equivalent.
- You want the option that thousands of compose stacks already run.

## Choose alle when

- You need several locations **at once** and want one thing to manage them, not
  one container per location with its own credentials, health check, and restart
  policy.
- The exit depends on *where the traffic is going*, not on which container sent
  it — a US streaming domain, a JP test endpoint, and everything else direct.
- A program should drive the whole lifecycle: create, probe, rotate, and retire
  connections over a REST API that mirrors the CLI exactly.
- You want the same tool on your laptop as in the stack, without Docker.
