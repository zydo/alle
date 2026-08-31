# Tier 2 live-tun sandbox (see docs/tun-runbook.md): a Linux network
# namespace where sing-box tun configs run for real — route seizure, DNS
# hijack, kill-switch — with zero risk to the host's network. Run via
# scripts/tun-sandbox/run.sh, never in CI (needs NET_ADMIN + /dev/net/tun).
FROM debian:trixie@sha256:f324c7ff54321e8d9c588493a20244965938ce0aa50bbd1022d38010e9ffc4b1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dnsutils \
    iproute2 \
    libcap2-bin \
    procps \
    python3 \
    python3-pycountry \
    python3-yaml \
    tcpdump \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home sandbox

WORKDIR /repo

# Default to an unprivileged user so a casual `docker run` of this image gets
# no privileges. The sandbox flows themselves need root (creating TUN devices
# and rewriting the namespace's route table under CAP_NET_ADMIN) — run.sh
# opts in explicitly with `--user 0:0`.
USER sandbox
