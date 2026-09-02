#!/bin/zsh
# Double-click to run the monitor so other devices on your home wifi can see it.
# macOS may ask whether to allow incoming connections - say Allow.
cd "$(dirname "$0")"
echo "Starting Outboard Price Monitor on your home network..."
echo "Close this window to stop it."
echo
exec /usr/bin/python3 monitor.py serve --lan --no-open
