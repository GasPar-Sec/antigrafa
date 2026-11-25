#!/bin/sh
# CI, inside the test machine (as root): check that the services installed by
# deploy/install.sh run.
#
#   check-services.sh             after the first start; leaves a mark in /run
#   check-services.sh --rebooted  after a reboot: the mark must be gone (/run is emptied
#                                 at every boot), and the services back on their own
set -eu

SERVICES="antigrafa-scheduler antigrafa-web"
MARK=/run/antigrafa-ci-mark
WAIT=120  # seconds for the services to be up (the web server creates its certificate)

fail() {
    echo "FAIL: $*" >&2
    systemctl status --no-pager $SERVICES >&2 || true
    journalctl --no-pager -n 100 -u antigrafa-scheduler -u antigrafa-web >&2 || true
    exit 1
}

if [ "${1:-}" = --rebooted ]; then
    [ ! -e "$MARK" ] || fail "the machine did not reboot ($MARK is still there)"
fi

for service in $SERVICES; do
    [ "$(systemctl is-enabled "$service")" = enabled ] || fail "$service is not enabled (not started at boot)"
done

https_ok() {
    /opt/antigrafa/bin/python - <<'EOF'
import ssl, sys, urllib.request
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE  # self-signed certificate, created by the service
try:
    sys.exit(urllib.request.urlopen("https://127.0.0.1:8443/", context=context, timeout=5).status != 200)
except OSError:
    sys.exit(1)
EOF
}

all_active() {
    # One at a time: with several units, is-active succeeds when any one is active
    for service in $SERVICES; do
        systemctl is-active --quiet "$service" || return 1
    done
}

waited=0
until all_active && https_ok; do
    [ "$waited" -lt "$WAIT" ] || fail "services not up after ${WAIT} s"
    sleep 2
    waited=$((waited + 2))
done
echo "ok: $SERVICES active, web page answers over HTTPS (after ${waited} s)"

/usr/local/bin/antigrafa job list > /tmp/job-list.txt 2>&1 || fail "antigrafa job list failed: $(cat /tmp/job-list.txt)"
grep -q '^scheduler: running' /tmp/job-list.txt || fail "the scheduler is not seen running: $(cat /tmp/job-list.txt)"
echo "ok: $(grep '^scheduler:' /tmp/job-list.txt)"

# Every file of the services is the service account's: nothing created by root that
# the services could not read or write after a restart
foreign=$(find /var/lib/antigrafa /var/log/antigrafa ! -user antigrafa)
[ -z "$foreign" ] || fail "files not owned by antigrafa: $foreign"
echo "ok: /var/lib/antigrafa and /var/log/antigrafa owned by antigrafa"

if [ "${1:-}" != --rebooted ]; then
    touch "$MARK"
fi
