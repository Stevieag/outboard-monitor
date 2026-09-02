#!/bin/zsh
# Double-click this file in Finder to open the price monitor.
cd "$(dirname "$0")"
echo "Starting Outboard Price Monitor..."
echo "Your browser will open in a moment. Close this window to stop."
echo
exec /usr/bin/python3 monitor.py serve
