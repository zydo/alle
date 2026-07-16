#!/bin/sh
# alle container entrypoint: converge on the declared setup, then hand PID 1
# to the daemon loop (exec, so `docker stop`'s SIGTERM reaches it directly).
#
# Started as root (the image default), it fixes state-volume ownership and
# drops to the unprivileged `alle` user — unless ALLE_RUN_AS_ROOT=1, which
# TUN/gateway mode needs in v1 (sing-box must be able to create the tun
# device; the capability comes from --cap-add NET_ADMIN + /dev/net/tun).
# Started with --user, it runs as that user and touches no ownership.
set -eu

BUNDLE="${ALLE_BUNDLE:-/etc/alle/bundle.yaml}"
STATE="${ALLE_HOME:-/var/lib/alle}"

run_as() {
    # Root path: run everything as the alle user via setpriv (in util-linux,
    # present in slim). Non-root path: run in place.
    if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
        setpriv --reuid alle --regid alle --init-groups "$@"
    else
        "$@"
    fi
}

if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
    mkdir -p "$STATE"
    chown -R alle:alle "$STATE"
fi

# The bundle is the desired state: `alle import` merges idempotently, so every
# container start converges on it (and a broken bundle fails the start loudly,
# in `docker logs`, instead of running a half-configured VPN).
if [ -f "$BUNDLE" ]; then
    echo "alle-entrypoint: applying $BUNDLE" >&2
    run_as alle import "$BUNDLE"
fi

# exec (not a function call) so "$@" becomes PID 1 and receives docker stop's
# SIGTERM directly.
if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
    exec setpriv --reuid alle --regid alle --init-groups "$@"
fi
exec "$@"
