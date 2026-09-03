#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Catch JavaScript the dashboard serves that a browser cannot parse.

The page's script is built from a Python string, so a "\n" written with one
backslash becomes a REAL line break in the served JavaScript. A string literal
cannot span a line, so that is a syntax error - and a syntax error anywhere in
the block silently disables every handler on the page: no populate, no check,
no progress. Nothing in an HTTP-level test notices, because the server still
returns 200 with the broken script inside.

    ./tests/check_js.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def unterminated_strings(js):
    """Line numbers where a string literal is still open at end of line."""
    bad = []
    for number, line in enumerate(js.splitlines(), 1):
        quote = None
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "/" and index + 1 < len(line) and line[index + 1] == "/":
                break
            index += 1
        if quote:
            bad.append((number, line.strip()[:72]))
    return bad


def main():
    import web
    problems = unterminated_strings(web.JS)
    if problems:
        print("FAIL  dashboard JavaScript has unterminated string literals:")
        for number, text in problems:
            print("   line %d: %s" % (number, text))
        print("\nA \\n in the JS template needs escaping as \\\\n in web.py,")
        print("or Python turns it into a real newline and breaks the script.")
        return 1
    # a real newline inside a quoted run is the specific failure above; also
    # guard the raw template against the same mistake creeping back
    print("PASS  dashboard JavaScript parses (no unterminated strings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
