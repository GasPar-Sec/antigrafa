#!/bin/sh
# Install or upgrade Antigrafa on a Linux machine with systemd, as two services
# started at boot: antigrafa-scheduler (recurring backups) and antigrafa-web (HTTPS).
#
#   sudo deploy/install.sh            # from a copy of the repository
#   sudo deploy/install.sh --no-web   # the scheduler only (no Django)
#
# Run it again to upgrade: the configuration, the keys and the data are kept, and the
# services that run are restarted (a running backup is cancelled and cleaned up).
#
# Environment: PYTHON (default python3, 3.11 or later), PREFIX (default /opt/antigrafa).
set -eu

ACCOUNT=antigrafa
PREFIX=${PREFIX:-/opt/antigrafa}
PYTHON=${PYTHON:-python3}
CONF_DIR=/etc/antigrafa
DATA_DIR=/var/lib/antigrafa
LOG_DIR=/var/log/antigrafa
UNIT_DIR=/etc/systemd/system
WRAPPER=/usr/local/bin/antigrafa

WEB=yes
for arg in "$@"; do
    case $arg in
        --no-web) WEB=no ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

SRC=$(cd "$(dirname "$0")/.." && pwd)

# ------------------------------------------------------------------ checks

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
[ -f "$SRC/pyproject.toml" ] || die "$SRC is not a copy of the Antigrafa repository"
[ -d /run/systemd/system ] || die "systemd is not running on this machine"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (PYTHON=python3.12 $0 to pick another)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 or later is needed, $PYTHON is $("$PYTHON" -V 2>&1)"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || die "the venv module is missing (Debian/Ubuntu: apt install python3-venv)"
command -v ssh >/dev/null || die "ssh not found (Debian/Ubuntu: apt install openssh-client)"
command -v runuser >/dev/null || die "runuser not found (util-linux)"

# ------------------------------------------------------------------ account and directories

if ! id "$ACCOUNT" >/dev/null 2>&1; then
    say "creating the system account $ACCOUNT (home $DATA_DIR)"
    NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
        --shell "$NOLOGIN" --comment "Antigrafa backups" "$ACCOUNT"
fi

# The secrets are root's, readable by the services' group; the data is the account's.
install -d -m 0750 -o root -g "$ACCOUNT" "$CONF_DIR"
install -d -m 0750 -o "$ACCOUNT" -g "$ACCOUNT" "$DATA_DIR" "$LOG_DIR"
install -d -m 0700 -o "$ACCOUNT" -g "$ACCOUNT" "$DATA_DIR/.ssh"

if [ ! -e "$CONF_DIR/antigrafa.toml" ]; then
    say "configuration: $CONF_DIR/antigrafa.toml (from antigrafa.example.toml, to edit)"
    install -m 0640 -o root -g "$ACCOUNT" "$SRC/antigrafa.example.toml" "$CONF_DIR/antigrafa.toml"
    NEW_CONFIG=yes
else
    NEW_CONFIG=no
fi

if [ ! -e "$CONF_DIR/id_ed25519" ]; then
    say "SSH key for the nodes: $CONF_DIR/id_ed25519"
    ssh-keygen -q -t ed25519 -N '' -C "antigrafa@$(hostname)" -f "$CONF_DIR/id_ed25519"
fi
# ssh refuses a private key its user could share: the account's own, mode 0600
chown "$ACCOUNT:$ACCOUNT" "$CONF_DIR/id_ed25519"
chmod 0600 "$CONF_DIR/id_ed25519"

# ------------------------------------------------------------------ program

if [ ! -x "$PREFIX/bin/python" ]; then
    say "virtual environment: $PREFIX"
    "$PYTHON" -m venv "$PREFIX"
fi
EXTRAS=""
[ "$WEB" = yes ] && EXTRAS="[web]"
say "installing Antigrafa$EXTRAS from $SRC"
"$PREFIX/bin/pip" install --quiet --upgrade pip
# From a copy in a temporary directory: the build leaves nothing in the repository
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -R "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/LICENSE" "$SRC/src" "$BUILD/"
"$PREFIX/bin/pip" install --quiet --upgrade "$BUILD$EXTRAS"

sed -e "s#@ACCOUNT@#$ACCOUNT#" -e "s#@PREFIX@#$PREFIX#" "$SRC/deploy/antigrafa-wrapper.sh" > "$BUILD/wrapper"
install -m 0755 "$BUILD/wrapper" "$WRAPPER"

# ------------------------------------------------------------------ services

SERVICES=antigrafa-scheduler
[ "$WEB" = yes ] && SERVICES="$SERVICES antigrafa-web"
for service in $SERVICES; do
    sed "s#/opt/antigrafa/#$PREFIX/#" "$SRC/deploy/systemd/$service.service" > "$BUILD/$service.service"
    install -m 0644 "$BUILD/$service.service" "$UNIT_DIR/$service.service"
done
if [ "$WEB" = no ] && [ -e "$UNIT_DIR/antigrafa-web.service" ]; then
    say "removing antigrafa-web (--no-web)"
    systemctl disable --now antigrafa-web.service || true
    rm -f "$UNIT_DIR/antigrafa-web.service"
fi
systemctl daemon-reload
# Enabled: started at every boot. Started now only once configured (an upgrade).
for service in $SERVICES; do
    systemctl enable --quiet "$service.service"
    systemctl try-restart "$service.service"
done

# ------------------------------------------------------------------ what is left to do

say "installed: antigrafa $("$PREFIX/bin/python" -c 'from importlib.metadata import version; print(version("antigrafa"))') in $PREFIX"
if [ "$NEW_CONFIG" = yes ]; then
    cat <<EOF

Next steps (the services start at boot; start them now once configured):

  1. Edit $CONF_DIR/antigrafa.toml: sources, targets, jobs.
     Put the API token in $CONF_DIR/token, readable by the services only:
       install -m 0640 -o root -g $ACCOUNT /dev/null $CONF_DIR/token && editor $CONF_DIR/token
     and the cluster's CA (/etc/pve/pve-root-ca.pem of a node) in $CONF_DIR/pve-root-ca.pem.
  2. On every node, add this key to /root/.ssh/authorized_keys:
       $(cat "$CONF_DIR/id_ed25519.pub")
     Each node's host key is recorded on the first connection
     (StrictHostKeyChecking=accept-new in $CONF_DIR/antigrafa.toml). To test a node:
       runuser -u $ACCOUNT -- ssh -i $CONF_DIR/id_ed25519 root@<node> true
  3. The targets (local directories, NFS shares) must be writable by '$ACCOUNT'.
EOF
    [ "$WEB" = yes ] && cat <<EOF
  4. Create a user of the web page:
       antigrafa web adduser admin
EOF
    cat <<EOF

  Then: systemctl start $SERVICES
  Check: antigrafa job list; journalctl -u antigrafa-scheduler -f
EOF
else
    echo "Services: $(systemctl is-active $SERVICES | paste -sd' ' -) ($SERVICES)"
fi
