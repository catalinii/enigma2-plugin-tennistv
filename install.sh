#!/usr/bin/env bash
#
# Install the Tennis TV plugin on an Enigma2/OpenATV receiver over SSH/SCP.
#
# Usage:
#   ./install.sh root@<receiver-ip>
#   RECEIVER=root@192.168.1.100 ./install.sh
#
# The plugin is copied to /usr/lib/enigma2/python/Plugins/Extensions/TennisTV
# and the Enigma2 GUI is restarted so it appears under the plugin menu.
#
set -euo pipefail

RECEIVER="${1:-${RECEIVER:-}}"
if [ -z "$RECEIVER" ]; then
    echo "Usage: ./install.sh root@<receiver-ip>" >&2
    echo "   or: RECEIVER=root@192.168.1.100 ./install.sh" >&2
    exit 1
fi

PLUGIN_DIR="/usr/lib/enigma2/python/Plugins/Extensions/TennisTV"

echo "Installing to $RECEIVER:$PLUGIN_DIR ..."
ssh "$RECEIVER" "mkdir -p '$PLUGIN_DIR'"
scp plugin.py api.py __init__.py "$RECEIVER:$PLUGIN_DIR/"

echo "Restarting Enigma2 GUI ..."
ssh "$RECEIVER" "if command -v systemctl >/dev/null 2>&1; then systemctl restart enigma2; else killall -9 enigma2; fi"

echo "Done. The plugin should now appear under Plugins -> Tennis TV."
