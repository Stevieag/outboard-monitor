#!/bin/zsh
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Double-click to run the monitor so other devices on your home wifi can see it.
# macOS may ask whether to allow incoming connections - say Allow.
cd "$(dirname "$0")"
echo "Starting Outboard Price Monitor on your home network..."
echo "Close this window to stop it."
echo
exec /usr/bin/python3 monitor.py serve --lan --no-open
