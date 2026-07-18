# Ubuntu 24.04, pinned immutably for the release-smoke environment.
FROM ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ENV container=docker

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dbus-user-session \
    systemd \
    systemd-sysv \
    util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 tester

STOPSIGNAL SIGRTMIN+3

# Safe default for accidental/manual runs. The smoke harness must explicitly
# opt back into root to run systemd as PID 1; it then executes install.sh only
# through `runuser -u tester` with the checkout mounted read-only.
USER tester
CMD ["/sbin/init"]
