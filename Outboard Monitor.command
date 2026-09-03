#!/bin/zsh
# Copyright (c) 2026 Stevieag
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Double-click this file in Finder to open the price monitor.
cd "$(dirname "$0")"
echo "Starting Outboard Price Monitor..."
echo "Your browser will open in a moment. Close this window to stop."
echo
exec /usr/bin/python3 monitor.py serve
