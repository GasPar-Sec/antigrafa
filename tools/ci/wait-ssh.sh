#!/bin/sh
# CI: run a command until it succeeds, for at most <seconds> (an SSH command to a VM
# that boots, or reboots).
#   wait-ssh.sh <seconds> <command...>
set -u

limit=$1
shift
start=$(date +%s)
until "$@"; do
    if [ $(($(date +%s) - start)) -ge "$limit" ]; then
        echo "still failing after $limit s: $*" >&2
        exit 1
    fi
    sleep 3
done
echo "ok after $(($(date +%s) - start)) s: $*"
