#!/bin/sh
# CI, inside the test machine (as root): install Antigrafa with deploy/install.sh, as a
# first install would, then do what the administrator does next: a web user, and start
# the services. The example configuration is kept: its jobs fail (no token), which is
# enough to see the services run.
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)

sh "$SRC/deploy/install.sh"
# Through /usr/local/bin/antigrafa, as root: the file must still be the service's
echo 'ci-password-0123456789' | /usr/local/bin/antigrafa web adduser admin --password-stdin
systemctl start antigrafa-scheduler antigrafa-web
