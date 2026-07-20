# The official alle container image — the opt-in container profile.
#
# Design (see docs/docker.md):
# * The container boundary is the trust boundary: ALLE_LISTEN=0.0.0.0 makes
#   channel/router proxy ports reachable on the container network; nothing is
#   reachable beyond it unless the operator publishes a port. The Web UI stays
#   loopback-only — manage via `docker exec <name> alle …`.
# * No sing-box is baked into this image (GPL option A): the pinned build is
#   fetched and checksum-verified on first start, into the state volume, so a
#   container recreate never re-downloads.
# * PID 1 is `alle run` (the daemon loop, foreground, logs on stderr);
#   restart policies replace launchd/systemd. `docker stop` = clean SIGTERM,
#   and foreground ownership stops/reaps sing-box + tears down TUN in the
#   stop grace period.
# * The image USER is 1000 (the `alle` user) — proxy mode is non-root in the
#   image *metadata*, so `runAsNonRoot`-style admission checks can verify it.
#   Gateway mode is the explicit root override: user: "0" + ALLE_RUN_AS_ROOT=1
#   + ALLE_GATEWAY=1 (plus --cap-add NET_ADMIN --device /dev/net/tun).
#   A named state volume inherits the image's alle-owned /var/lib/alle; for a
#   bind-mounted state dir either chown it to uid 1000 or run the root
#   override, where the entrypoint repairs only wrong-owned entries.

# Base images pinned by immutable digest — same discipline as the SHA-pinned
# CI Actions; the trailing comment records the tag each digest was resolved
# from (2026-07-20). The digests are the multi-arch manifest lists.
# python 3.14-slim
FROM python@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
ARG ALLE_WHEEL_SHA256
LABEL org.opencontainers.image.alle-wheel-sha256=$ALLE_WHEEL_SHA256
RUN useradd --uid 1000 --user-group --home-dir /var/lib/alle --create-home \
    --shell /usr/sbin/nologin alle \
    && mkdir -p /etc/alle
COPY docker/requirements.lock /tmp/requirements.lock
COPY dist/*.whl /tmp/
RUN set -eu; \
    wheel=$(find /tmp -maxdepth 1 -name 'alle_proxy-*.whl' -type f); \
    [ "$(printf '%s\n' $wheel | wc -l)" = 1 ]; \
    actual=$(sha256sum "$wheel" | awk '{print $1}'); \
    [ -z "$ALLE_WHEEL_SHA256" ] || [ "$actual" = "$ALLE_WHEEL_SHA256" ]; \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock; \
    pip install --no-cache-dir --no-deps "$wheel"; \
    python -c 'import importlib.metadata as m; assert m.version("pycountry") == "26.2.16"'; \
    python -c 'import importlib.metadata as m; assert m.version("pyyaml") == "6.0.3"'; \
    rm /tmp/*.whl /tmp/requirements.lock
COPY docker/entrypoint.sh /usr/local/bin/alle-entrypoint
RUN chmod 0755 /usr/local/bin/alle-entrypoint

# The container profile knobs (each opt-in in core; the image opts in):
#   ALLE_CONTAINER  — authoritative "in a container" signal (guardrails/hints)
#   ALLE_SERVICE    — CLI calls via `docker exec` never self-spawn a second
#                     daemon around PID 1
#   ALLE_HOME       — state volume mount point
#   ALLE_LISTEN     — proxy inbounds bind the container network, not loopback
#   ALLE_PORT_BASE  — deterministic ports (publishable ahead of time); declare
#                     ports in the bundle for full control
ENV ALLE_CONTAINER=1 \
    ALLE_SERVICE=1 \
    ALLE_HOME=/var/lib/alle \
    ALLE_LISTEN=0.0.0.0 \
    ALLE_PORT_BASE=20000

VOLUME /var/lib/alle

# Numeric so runAsNonRoot admission checks can prove the non-root contract
# (a named user would be rejected as unverifiable). uid 1000 = the alle user.
USER 1000:1000

# Generous start period: the first ever start downloads sing-box into the volume.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["alle", "health"]

ENTRYPOINT ["alle-entrypoint"]
CMD ["alle", "run"]
