#!/bin/sh
# CI: wait for systemd to finish booting in the test machine.
#   wait-systemd.sh <command that runs a command in the machine...>
#   wait-systemd.sh sudo podman exec machine
# "degraded" (a unit failed, common in a container) is accepted: the services are
# checked on their own afterwards.
set -u

state=""
for _ in $(seq 90); do
    state=$("$@" systemctl is-system-running --wait 2>/dev/null) || true
    case $state in
        running|degraded) echo "systemd: $state"; exit 0 ;;
    esac
    sleep 2
done
echo "systemd did not finish booting (last state: ${state:-unreachable})" >&2
exit 1
