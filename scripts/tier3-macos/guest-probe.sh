#!/usr/bin/env bash
# Tier 3 environment probe: report what the Darwin guest can actually do
# before any assertions are written against it.
echo "--- guest ---"
sw_vers -productVersion | sed 's/^/macOS /'
uname -m
echo "--- alle ---"
alle version 2>&1 | head -1
echo "--- sudo without password? ---"
sudo -n true 2>&1 && echo "  yes" || echo "  NO"
echo "--- IPv6 upstream? ---"
ifconfig | grep -E "^\w|inet6" | grep -v fe80 | grep -B1 "inet6 2" | head -4 || echo "  no global v6 address"
curl -6 -s -m 4 -o /dev/null -w "  curl -6 -> %{http_code}\n" https://ipv6.google.com 2>/dev/null || echo "  curl -6 failed (no upstream v6)"
echo "--- default routes ---"
route -n get default 2>/dev/null | grep -E "interface|gateway" | sed 's/^/  v4 /'
route -n get -inet6 default 2>/dev/null | grep -E "interface|gateway" | sed 's/^/  v6 /' || echo "  no v6 default"
echo "--- feth (observable egress) available? ---"
sudo ifconfig feth9 create 2>&1 && echo "  feth9 created" && sudo ifconfig feth9 inet6 fd00:5117::2/64 up 2>&1 && ifconfig feth9 | grep inet6 | sed 's/^/  /' && sudo ifconfig feth9 destroy && echo "  (destroyed)" || echo "  feth unavailable"
echo "--- tcpdump ---"
command -v tcpdump || echo "  MISSING"
echo "--- existing utun devices ---"
ifconfig -l | tr ' ' '\n' | grep -c utun | sed 's/^/  /'
