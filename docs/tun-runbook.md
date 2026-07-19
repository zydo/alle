# TUN mode — recovery runbook, testing tiers, and the manual e2e checklist

This document must stay usable **without network access**: read the runbook
before the first live `alle tun on` on any machine, not after something broke.

## Recovery runbook

TUN mode means sing-box owns the machine's default route. Everything below is
ordered from gentlest to bluntest; start at 1.

1. **`alle tun off`** — flips the flag in state.json; the daemon reconciles
   sing-box without the tun within a second or two. This is the one command
   that always expresses the right intent.
2. **If the CLI is broken:** `alle stop` — stops the daemon *and* sing-box. A
   cleanly terminated sing-box restores everything it took: the utun device,
   its routes, and the kill-switch all die with the process.
3. **Never `pkill sing-box` as recovery.** Supervision restarts it with the
   same tun config within ~2 seconds — the network comes back and then
   vanishes again. Kill the *owner* (`alle stop`) or flip the *state*
   (`alle tun off`), never the process.
4. **Verify the routes are back:** `netstat -rn` (or `route -n get 1.1.1.1`)
   — the default route must sit on the physical interface (`en0`, `eth0`),
   with no `utun*`/`alle-tun` owning `0.0.0.0/1` + `128.0.0.0/1`.
5. **Flush DNS** (macOS): `sudo dscacheutil -flushcache && sudo killall -HUP
   mDNSResponder` — cached answers from the hijack window can otherwise
   outlive it.
6. **Worst case: reboot.** Nothing persists across a reboot — the utun and
   routes are process-lifetime. But state.json still says `tun: true`, so run
   `alle tun off` before the next `alle start`, or the daemon will
   re-activate it.

**Pending trial?** If tun was enabled with `alle tun on --trial <seconds>`
and never confirmed, the detached watchdog reverts it automatically at the
deadline — losing your SSH session *is* the recovery. `alle tun` shows the
pending trial and its remaining time.

## The three-tier testing map

Standing rule: live tun configs never run on the dev host during agent
sessions. What runs where:

| Tier | Environment                                    | What runs there                                                                                                                                                                | Setup / reset                                                                                                                                                                                                                                                                  |
| ---- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1    | dev host                                       | config-generation tests (`uv run pytest`), `sing-box check` of generated configs — ~90% of the work, and all of CI                                                             | nothing beyond the repo checkout                                                                                                                                                                                                                                               |
| 2    | Linux container (`scripts/tun-sandbox/run.sh`) | live tun: route seizure, DNS hijack, per-rule matching, kill-switch, loopback bypass, teardown — in an isolated network namespace, zero host risk                              | Docker only; containers are `--rm` (nothing persists except the git-ignored `.tun-sandbox-cache/` binary cache)                                                                                                                                                                |
| 3    | macOS guest VM (Tart)                          | Darwin specifics: utun naming, Darwin `auto_route`, `strict_route` (upstream documents no macOS semantics), mDNSResponder/DNS-hijack interplay — the checklist below, over SSH | `brew trust cirruslabs/cli && brew install cirruslabs/cli/tart`; `tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest tahoe-base` — SSH (`admin`/`admin`, passwordless sudo) **is** enabled in the base image; give the first boot a few minutes before concluding otherwise |
| —    | dev host, live                                 | **final acceptance only**: human at keyboard, `--trial` armed                                                                                                                  | see checklist step 1                                                                                                                                                                                                                                                           |

Tier 2 entrypoints:

```bash
scripts/tun-sandbox/run.sh                                        # design-assumption smoke
scripts/tun-sandbox/run.sh /repo/scripts/tun-sandbox/engine-smoke.sh  # the engine-GENERATED config, live
scripts/tun-sandbox/run.sh /repo/scripts/tun-sandbox/setcap-smoke.sh  # Linux setcap (no-root) privilege path
scripts/tun-sandbox/run.sh bash                                   # interactive shell
```

Related but distinct: tun-in-a-container is also a supported *product* shape
now — the Docker image's gateway mode (`docs/docker.md`,
`docs/docker-compose.md`) runs the same tun core inside the container's own
netns with `--cap-add NET_ADMIN --device /dev/net/tun`. The Tier 2 sandbox is
its test harness ancestor; the runbook's host-safety rules don't apply there
because a container netns is exactly the isolation this tier map exists to
provide.

Tier 3 connect / reset recipe (verified 2026-07-11 against
`macos-tahoe-base`, macOS 26.5):

```bash
tart run tahoe-base --no-graphics &          # boot headless; wait for IP + port 22
IP=$(tart ip tahoe-base)
sshpass -p admin ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@$IP
# guest install: rsync the checkout to ~/alle (exclude .git/.venv/caches),
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#   ~/.local/bin/uv tool install ~/alle
# reset (fresh guest): tart delete <clone> && tart clone tahoe-base <clone>
# stop: tart stop tahoe-base
```

Guest facts that matter for the checklist: the vmnet NAT subnet
(`192.168.64.0/24`) stays on-link, so the **SSH session survives tun
activation** (auto_route's `0/1`+`128/1` split never captures it); and the
privilege gate will (correctly) refuse `sudo alle tun on` while an
admin-owned daemon is running — `alle stop` first, exactly as checklist
step 1 says.

## Manual e2e checklist (Darwin)

Run top to bottom in the Tier 3 VM first; on the dev host only as final
acceptance. **"Tested" for TUN mode v1 = the config-shape tests in CI plus
this checklist executed on Darwin — both, explicitly.**

Preconditions: alle installed, at least one healthy channel (`alle test`), at
least one domain rule targeting it, this runbook read.

1. **Bring-up under trial.** `alle stop`, then
   `sudo ALLE_HOME="$HOME/.alle" alle tun on --trial 120`.
   Expect: `alle status` shows the `TUN` line; `ifconfig` shows the utun
   (engine emits `utun225` on Darwin — if creation fails because the name is
   taken, that is a finding: record it); `route -n get 1.1.1.1` resolves via
   the utun.
2. **Traffic flows.** `curl https://1.1.1.1/cdn-cgi/trace` (IP literal) and a
   domain fetch both succeed; a domain matching a channel rule exits with the
   channel's IP (compare against `alle test`).
3. **DNS hijack.** `dig @9.9.9.9 example.com` returns an answer (a query
   aimed at a foreign resolver was hijacked and answered by sing-box);
   ordinary app resolution (Safari/curl by hostname) works; `scutil --dns`
   still lists resolvers without errors.
4. **LAN direct.** With `alle routes lan on` (default), a LAN device
   (printer, router admin page) is reachable.
5. **Kill-switch scope.** `alle routes killswitch on`: unmatched egress is
   blocked (`curl` to an unruled IP fails), rule-matched traffic still flows,
   and loopback services stay reachable (open the Web UI). Expect alle's own
   provider API calls to be blocked in this posture (documented limitation).
   `alle routes killswitch off` restores.
6. **Confirm + clean disable.** `alle tun confirm` keeps it on past the
   window. Then `alle tun off`: default route returns to the physical
   interface (`route -n get 1.1.1.1` → `en0`), connectivity intact, utun
   gone.
7. **Crash drill (fails open — verify, don't assume).**
   TUN on again, then `sudo pkill -9 sing-box`: connectivity returns on the
   physical route within a beat, and supervision restarts sing-box with the
   tun (~2s window). Confirm the restart re-seizes routes, then `alle tun
   off`. This is the documented crash-window behavior.
8. **Trial auto-revert drill.** `alle tun on --trial 30`, do **not** confirm:
   at the deadline the watchdog flips tun off and the log shows
   `TUN trial expired without confirmation`.
9. **mDNSResponder interplay (Tier 3 focus).** After steps 1–8, Bonjour
   basics still work: `dns-sd -B _ssh._tcp` finds LAN services with tun on
   (multicast is LAN-direct) and after teardown.
10. **IPv6 — per-provider policy, never leaked.** With tun on (on an
    IPv6-capable network):
    - If **no** enabled channel is v6-capable (e.g. a NordVPN-only fleet):
      `route -n get -inet6 2606:4700:4700::1111` resolves via the utun, and
      `curl -6 https://ifconfig.co` **fails** (the blanket `::/0` reject)
      rather than printing your home IPv6. `curl -4` still exits via the
      channel. After `tun off`, `curl -6` works again on the physical
      interface.
    - If a v6-capable channel exists (e.g. a Proton VPN server with a global
      v6 address): `curl -6` to a v6 destination that matches a rule → routes
      through the capable channel; to a destination matching no rule → fails
      (the trailing `::/0` reject, not a leak); through a v4-only channel's
      rule → fails (the per-rule v6 guard). Verify with `alle test` — the
      IPV6 column shows the channel's v6 exit when carried.
