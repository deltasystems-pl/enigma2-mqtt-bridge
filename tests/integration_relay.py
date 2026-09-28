"""The companion integration's half of the relay handshake, verbatim - its request parser, its
answer, and its copy of the address rule.

Copied from `custom_components/enigma2_mqtt/relay.py` (and the one helper it imports from
`release_index.py`, and the command topic and publish of `box.py`) of
deltasystems-pl/hass-enigma2-mqtt at commit 8548144, the head of its relay pull request, so
that the plugin is tested against what Home Assistant actually sends and accepts - not against
a description of it. The functions are unchanged but for this docstring, the imports they need,
and `answer` and `command_topic`/`PUBLISH`, which are the integration's own expressions lifted
out of `async_answer_relay_request` and `Box.async_publish_cmd` so a test can call them. Never
edit what is below; replace the file from a later commit of the integration instead.

The integration is MIT-licensed; its licence, which applies to the code below:

    MIT License

    Copyright (c) 2026 deltasystems-pl

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --- release_index.py -------------------------------------------------------------

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def is_version(value: Any) -> bool:
    """Whether `value` is a plain `N.N.N`."""
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


# --- relay.py -----------------------------------------------------------------------

RELAY_PATH = "/api/enigma2_mqtt/relay/"
# A request is a few dozen bytes; anything longer is not one.
RELAY_REQUEST_MAX_BYTES = 1024

_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
# The only address the receiver accepts from Home Assistant (plugin `updatehelper.py`,
# RELAY_URL, and TRANSACTION.md section 7): its host - an IPv4 address or a host name, no user
# part - a port if any, the fixed path and a 43-character token, nothing after it.
_RELAY_URL = re.compile(
    r"https?://[A-Za-z0-9.-]{1,253}(?::([0-9]{1,5}))?"
    + re.escape(RELAY_PATH)
    + r"[A-Za-z0-9_-]{43}",
    re.ASCII,
)


def relay_url_ok(url: str) -> bool:
    """Whether the receiver accepts `url` as an address from Home Assistant."""
    match = _RELAY_URL.fullmatch(url)
    return match is not None and (match.group(1) is None or 0 < int(match.group(1)) < 65536)


def _parse_request(payload: Any) -> dict[str, Any] | None:
    # Measured in bytes before anything is decoded or parsed.
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "replace")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > RELAY_REQUEST_MAX_BYTES:
        return None
    try:
        body = json.loads(bytes(payload).decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    request_id, version = body.get("id"), body.get("version")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return None
    if not is_version(version):
        return None
    # `serial` - the receiver's own index - is not needed: Home Assistant answers from the
    # index it holds, and the receiver verifies the bytes against its own.
    return {"id": request_id, "version": version}


def answer(request: dict[str, Any], version: str, base: str, token: str, expires: float) -> str:
    """The payload of `cmd/relay`, exactly as `async_answer_relay_request` builds it."""
    return json.dumps(
        {
            "id": request["id"],
            "version": version,
            "url": f"{base}{RELAY_PATH}{token}",
            "expires": int(expires),
        },
        separators=(",", ":"),
    )


# --- box.py -------------------------------------------------------------------------


def command_topic(base_topic: str, node_id: str, name: str) -> str:
    """Return a command topic of one box."""
    return f"{base_topic}/{node_id}/cmd/{name}"


# `Box.async_publish_cmd("relay", ...)`: `mqtt.async_publish(..., qos=qos, retain=False)`
# with its default `qos: int = 1`. And `Box._relay_request_received` drops a retained request.
PUBLISH = {"qos": 1, "retain": False}
