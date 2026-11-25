#!/bin/sh
# Installed as /usr/local/bin/antigrafa by deploy/install.sh.
#
# Runs the command as the service account, so that the files it creates (web users,
# log archives, job results, run lock) stay readable by the services. Run as root,
# it switches to that account; run as that account, it runs directly.
set -eu

ACCOUNT=@ACCOUNT@
PROGRAM=@PREFIX@/bin/antigrafa

if [ "$(id -u)" -eq 0 ]; then
    exec runuser -u "$ACCOUNT" -- "$PROGRAM" "$@"
fi
exec "$PROGRAM" "$@"
