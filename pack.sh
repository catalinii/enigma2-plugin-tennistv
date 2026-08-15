#!/usr/bin/env bash
#
# Pack the plugin into a tarball for manual installation (FTP/SCP to the box).
#
# Usage:
#   ./pack.sh
#
# Produces "enigma2-plugin-tennistv-<version>.tar.gz". Extract it on the
# receiver under /usr/lib/enigma2/python/Plugins/Extensions/TennisTV/ or just
# use install.sh to deploy it over SSH.
#
set -euo pipefail

cd "$(dirname "$0")"

VERSION="1.0.0"
TARBALL="enigma2-plugin-tennistv-${VERSION}.tar.gz"

rm -f "$TARBALL"
tar czf "$TARBALL" plugin.py api.py __init__.py

echo "Created $TARBALL"
