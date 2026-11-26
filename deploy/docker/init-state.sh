#!/bin/sh
set -eu
mkdir -p /state/runtime /state/cowrie
chmod 0755 /state /state/cowrie
chmod 0700 /state/runtime
chown 10001:10001 /state/runtime /state/cowrie
