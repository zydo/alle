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

BUNDLE="${ALLE_BUNDLE-/etc/alle/bundle.yaml}"
STATE="${ALLE_HOME:-/var/lib/alle}"

fail() {
	echo "alle-entrypoint: ERROR: $*" >&2
	exit 1
}

run_as() {
	# Root path: run everything as the alle user via setpriv (in util-linux,
	# present in slim). Non-root path: run in place.
	if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
		setpriv --reuid alle --regid alle --init-groups "$@"
	else
		"$@"
	fi
}

# Ownership commands below take $STATE from the environment — refuse values
# whose repair sweep could touch the system: relative paths, the root
# directory, and system prefixes are configuration errors, not state dirs.
case "$STATE" in
/) fail "ALLE_HOME=/ is not a state directory" ;;
/bin | /bin/* | /dev | /dev/* | /etc | /etc/* | /lib | /lib/* | /proc | /proc/* | /sbin | /sbin/* | /sys | /sys/* | /usr | /usr/*) \
	fail "ALLE_HOME=$STATE points into a system prefix — mount state elsewhere (default /var/lib/alle)" ;;
/*) ;;
*) fail "ALLE_HOME=$STATE must be an absolute path" ;;
esac

# Started as root (the explicit override profile — the image USER is 1000):
# initialize the state dir once and repair ONLY wrong-owned entries, instead
# of an unconditional recursive chown on every restart. A named volume
# already inherits the image's alle-owned /var/lib/alle, so on a healthy
# steady state this is a read-only scan.
if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
	mkdir -p -- "$STATE"
	find "$STATE" ! -user alle -exec chown -h -- alle:alle {} +
fi

# The bundle is the desired state: `alle sync` converges the managed setup on
# it every start (repeat boots are idempotent; edits/removals touch only what
# sync itself created), and any problem fails the start loudly in
# `docker logs` — *before* anything is imported — instead of running a
# half-configured VPN. The mount contract fails loud too: quickstarts use
# long `--mount` syntax so a missing host file is an engine error, and a
# directory at the bundle path (what short `-v` creates from a missing host
# file) is refused here rather than silently skipped. Only the explicit
# no-bundle profile (ALLE_BUNDLE set empty or "none") and the interactive
# default profile (nothing mounted, ALLE_BUNDLE unset) skip the sync.
if [ -z "$BUNDLE" ] || [ "$BUNDLE" = "none" ]; then
	echo "alle-entrypoint: no-bundle profile (ALLE_BUNDLE=${BUNDLE:-}) — starting unconfigured" >&2
elif [ -d "$BUNDLE" ]; then
	fail "$BUNDLE is a directory, not a bundle file — a short '-v host.yaml:$BUNDLE' \
mount with a missing host file creates a directory; use \
'--mount type=bind,src=/abs/path/bundle.yaml,dst=$BUNDLE,readonly' so a missing \
source fails at docker run instead"
elif [ -e "$BUNDLE" ] && [ ! -f "$BUNDLE" ]; then
	fail "$BUNDLE exists but is not a regular file — mount a bundle file there, \
or set ALLE_BUNDLE=none for the explicit no-bundle profile"
elif [ ! -e "$BUNDLE" ]; then
	if [ -n "${ALLE_BUNDLE+set}" ]; then
		fail "ALLE_BUNDLE=$BUNDLE does not exist — mount the bundle file, or set \
ALLE_BUNDLE=none for the explicit no-bundle profile"
	fi
	echo "alle-entrypoint: no bundle at $BUNDLE — starting unconfigured (interactive profile)" >&2
else
	echo "alle-entrypoint: syncing $BUNDLE" >&2
	run_as alle sync "$BUNDLE"
fi

# Gateway profile (ALLE_GATEWAY=1): privilege-check and declare the
# fail-closed TUN + kill-switch data plane BEFORE the daemon starts, so
# readiness (the HEALTHCHECK) can gate Compose dependants on the declared
# contract instead of a manual post-start toggle. Missing root mode,
# /dev/net/tun, or NET_ADMIN fails the start loudly right here.
if [ "${ALLE_GATEWAY:-}" = "1" ]; then
	echo "alle-entrypoint: gateway profile — declaring TUN + kill switch" >&2
	run_as alle gateway init
fi

# exec (not a function call) so "$@" becomes PID 1 and receives docker stop's
# SIGTERM directly.
if [ "$(id -u)" = "0" ] && [ "${ALLE_RUN_AS_ROOT:-}" != "1" ]; then
	exec setpriv --reuid alle --regid alle --init-groups "$@"
fi
exec "$@"
