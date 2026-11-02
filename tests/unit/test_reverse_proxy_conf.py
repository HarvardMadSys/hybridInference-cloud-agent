"""The reverse-proxy snippet keeps the two properties that are easy to lose.

`deploy/reverse-proxy/agents-under-parent-site.conf` is applied by hand — the
proxy in front of these hosts is not version-controlled anywhere — so this
file is the reviewed text an operator pastes. Two details in it fail quietly:

**Prefix stripping is a trailing slash.** `proxy_pass http://host:8000/`
replaces the matched location with `/`; `proxy_pass http://host:8000` forwards
it intact. Drop the slash and every control-plane route 404s, which reads as
the service being down rather than as a proxy fault. The web location needs
the opposite — Next is mounted at the prefix and must keep seeing it.

**SSE dies with buffering on.** nginx holds the response until its buffer
fills or the request ends, so job output arrives in one lump at the end, or
never. A run looks hung while it is working, which is indistinguishable from
a stuck sandbox.

Textual, and deliberately so: this is a config file no test can execute, and
the alternative to checking the text is checking nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONF = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "reverse-proxy"
    / "agents-under-parent-site.conf"
)


def _location_block(prefix: str) -> str:
    """Return the body of `location <prefix> { ... }`."""
    text = CONF.read_text()
    start = text.index(f"location {prefix} {{")
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated location {prefix}")


def test_the_api_location_strips_its_prefix() -> None:
    """Without the trailing slash the control plane sees `/agents/api/...`."""
    block = _location_block("/agents/api/")
    upstream = re.search(r"proxy_pass\s+(\S+);", block)
    assert upstream, "no proxy_pass in the api location"
    assert upstream.group(1).endswith("/"), (
        "proxy_pass must end in `/` so nginx replaces the matched prefix; "
        f"got {upstream.group(1)!r}, which forwards /agents/api/ to the "
        "control plane and 404s every route"
    )


def test_the_web_location_keeps_its_prefix() -> None:
    """Next is mounted at `/agents` by basePath and must keep receiving it."""
    block = _location_block("/agents/")
    upstream = re.search(r"proxy_pass\s+(\S+);", block)
    assert upstream, "no proxy_pass in the web location"
    assert not upstream.group(1).endswith("/"), (
        "proxy_pass must NOT end in `/` here — stripping the prefix would "
        f"leave Next serving /agents/* from paths it does not have; got "
        f"{upstream.group(1)!r}"
    )


@pytest.mark.parametrize(
    "directive",
    ["proxy_buffering off;", "proxy_cache off;", "X-Accel-Buffering no"],
)
def test_the_api_location_does_not_buffer(directive: str) -> None:
    """Buffered SSE is a job that looks hung until it finishes."""
    assert directive in _location_block("/agents/api/"), (
        f"{directive!r} is missing; job output would arrive in one lump at "
        "the end of the run, or not at all"
    )


def test_a_stream_is_not_closed_for_being_idle() -> None:
    """The default 60s read timeout cuts a healthy stream mid-tool-call."""
    block = _location_block("/agents/api/")
    timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", block)
    assert timeout and int(timeout.group(1)) >= 600, (
        "proxy_read_timeout must outlast a quiet stretch in a stream; the "
        "default closes healthy connections during a long tool call"
    )


def test_the_bare_prefix_redirects_rather_than_falling_through() -> None:
    """`/agents` is what a person types and what an old bookmark holds."""
    assert "location = /agents {" in CONF.read_text(), (
        "without an exact-match location, /agents with no trailing slash "
        "falls through to the gateway and 404s"
    )
