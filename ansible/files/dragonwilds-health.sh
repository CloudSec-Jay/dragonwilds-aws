#!/usr/bin/bash

set -u

game_healthy=0
falco_healthy=0

if systemctl is-active --quiet dragonwilds.service \
  && [[ "$(podman inspect --format '{{.State.Health.Status}}' rsdw-dedicated 2>/dev/null || true)" == "healthy" ]]; then
  game_healthy=1
fi

if systemctl is-active --quiet falco-modern-bpf.service; then
  falco_healthy=1
fi

# The CloudWatch Agent StatsD listener receives these locally. A failed send is
# harmless: missing-data alarms detect an unavailable agent or health reporter.
printf 'game_healthy:%s|g\nfalco_healthy:%s|g\n' "$game_healthy" "$falco_healthy" \
  > /dev/udp/127.0.0.1/8125 || true
