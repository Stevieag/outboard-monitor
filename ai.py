# Copyright (c) 2026 Stevieag
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Optional AI assistance, for the questions scraping cannot answer.

Delivery cost is the clearest case. Dealers do not publish a rate for an
outboard - they are bulky freight, so the price is settled at checkout or over
the phone - and reading their policy pages proves it: every one excludes heavy
goods. A model with web search can read the forum posts, the phone quotes people
report, and the pages a crawler cannot parse, and come back with a figure and
its reasoning.

This is the ONLY part of the tool that needs anything installed:

    pip3 install anthropic

Everything else stays pure standard library. Nothing here runs unless you have
both that package and an API key, and the tool works fully without either.
"""
from __future__ import annotations

import json
import os
import re

import core

MODEL = "claude-opus-5"


class AiUnavailable(Exception):
    """No API key, or the anthropic package is not installed."""


def api_key(conn) -> str:
    """The key, from settings or the environment. Settings win."""
    saved = (core.get_setting(conn, "ai_api_key", "") or "").strip()
    return saved or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def available(conn) -> bool:
    """True if an AI command could run right now."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(api_key(conn))


def why_unavailable(conn) -> str:
    """A sentence saying what is missing, for a command to print."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return ("the anthropic package is not installed - run:  "
                "pip3 install anthropic")
    if not api_key(conn):
        return ("no API key set - get one at https://console.anthropic.com/ then:  "
                './monitor.py settings ai_api_key "sk-ant-..."')
    return ""


def _client(conn):
    problem = why_unavailable(conn)
    if problem:
        raise AiUnavailable(problem)
    import anthropic
    return anthropic.Anthropic(api_key=api_key(conn))


def ask(conn, prompt, system, max_searches=6, max_tokens=8000):
    """One question, answered with the web searched. Returns (text, sources).

    Streams because a turn that runs several searches can outlast a plain
    request timeout, and asks for a summarised thinking display so a long pause
    is explicable rather than mysterious.
    """
    client = _client(conn)
    model = (core.get_setting(conn, "ai_model", "") or MODEL).strip()
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=system,
        tools=[{
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": max_searches,
            "user_location": {"type": "approximate", "country": "GB"},
        }],
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        detail = getattr(message, "stop_details", None)
        raise AiUnavailable("the model declined to answer%s"
                            % (" (%s)" % detail.category if detail else ""))

    text = "".join(b.text for b in message.content if b.type == "text")
    sources = []
    for block in message.content:
        if block.type != "web_search_tool_result":
            continue
        found = block.content
        if isinstance(found, list):          # an error result is an object, not a list
            for item in found:
                url = getattr(item, "url", None)
                if url and url not in sources:
                    sources.append(url)
    return text, sources


def as_json(text):
    """The first JSON object in a reply, or None. Models like to add prose."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None
