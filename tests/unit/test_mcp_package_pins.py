"""An import pins a pasted command's package to the release that is current now."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from cloud_agent.mcp import package_pins
from cloud_agent.mcp.config_import import parse_import
from cloud_agent.mcp.launch import build_launch_plan
from cloud_agent.mcp.models import ExecutionLocation

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.mcp.config_import import ImportDraft


def _registry(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]
) -> list[str]:
    """Serve lookups with ``answer``; the URLs asked for, in order."""
    asked: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return answer(request)

    releases = package_pins.Releases(transport=httpx.MockTransport(serve))
    monkeypatch.setattr(package_pins, "releases", releases)
    return asked


def _npm(version: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json={"name": "x", "version": version})


def _pypi(version: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json={"info": {"version": version}})


def _draft(command: str, *args: str) -> ImportDraft:
    paste = {"mcpServers": {"server": {"command": command, "args": list(args)}}}
    (draft,) = parse_import(json.dumps(paste)).drafts
    return draft


async def _pinned(command: str, *args: str) -> ImportDraft:
    return await package_pins.pin_draft(_draft(command, *args))


@pytest.mark.parametrize(
    "reference", ["@playwright/mcp", "@playwright/mcp@latest"], ids=["bare", "latest"]
)
async def test_a_bare_npm_name_is_pinned_to_its_current_release(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    asked = _registry(monkeypatch, _npm("0.0.83"))
    draft = await _pinned("npx", "-y", reference, "--headless")

    assert draft.launch is not None
    assert draft.launch.args == ("-y", "@playwright/mcp@0.0.83", "--headless")
    assert asked == ["https://registry.npmjs.org/@playwright/mcp/latest"]
    assert "pinned to @playwright/mcp 0.0.83, its current release" in draft.warnings
    # What is stored is what the sandbox will accept.
    build_launch_plan(draft.launch, connection_id="draft", location=ExecutionLocation.SANDBOX)


async def test_an_npm_tag_is_looked_up_as_that_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _registry(monkeypatch, _npm("2.0.0-rc.1"))
    draft = await _pinned("npx", "server@next")

    assert draft.launch is not None and draft.launch.args == ("server@2.0.0-rc.1",)
    assert asked == ["https://registry.npmjs.org/server/next"]


@pytest.mark.parametrize(
    ("args", "pinned", "url"),
    [
        (("mcp-server-fetch",), "mcp-server-fetch==2025.4.7", "mcp-server-fetch"),
        (("mcp-server-fetch@latest",), "mcp-server-fetch==2025.4.7", "mcp-server-fetch"),
        (("Mcp_Server.Fetch",), "Mcp_Server.Fetch==2025.4.7", "mcp-server-fetch"),
        (
            ("--from", "browser-use[cli]", "browser-use", "--mcp"),
            "browser-use[cli]==2025.4.7",
            "browser-use",
        ),
    ],
    ids=["bare", "latest", "normalized", "extras"],
)
async def test_a_bare_pypi_name_is_pinned_with_double_equals(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...], pinned: str, url: str
) -> None:
    asked = _registry(monkeypatch, _pypi("2025.4.7"))
    draft = await _pinned("uvx", *args)

    assert draft.launch is not None
    assert pinned in draft.launch.args
    assert asked == [f"https://pypi.org/pypi/{url}/json"]
    build_launch_plan(draft.launch, connection_id="draft", location=ExecutionLocation.SANDBOX)


async def test_uv_at_syntax_is_rewritten_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _registry(monkeypatch, _pypi("9.9.9"))
    draft = await _pinned("uvx", "mcp-server-fetch@2025.4.7")

    assert draft.launch is not None and draft.launch.args == ("mcp-server-fetch==2025.4.7",)
    assert asked == []
    assert not any("pinned" in warning for warning in draft.warnings)


@pytest.mark.parametrize(
    ("command", "args"),
    [("npx", ("-y", "@scope/server@1.2.3")), ("uvx", ("server==1.2.3",)), ("node", ("x.js",))],
    ids=["npm", "pypi", "not-a-package"],
)
async def test_an_exact_release_or_a_plain_program_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, command: str, args: tuple[str, ...]
) -> None:
    asked = _registry(monkeypatch, _npm("9.9.9"))
    draft = _draft(command, *args)

    assert await package_pins.pin_draft(draft) == draft
    assert asked == []


@pytest.mark.parametrize(
    ("command", "reference", "example"),
    [
        ("npx", "server@^1.2", "server@1.2.3"),
        ("npx", "server@1", "server@1.2.3"),
        ("uvx", "server>=1.2", "server==1.2.3"),
        ("uvx", "server@beta", "server==1.2.3"),
    ],
)
async def test_a_range_is_kept_with_what_to_write_instead(
    monkeypatch: pytest.MonkeyPatch, command: str, reference: str, example: str
) -> None:
    asked = _registry(monkeypatch, _npm("9.9.9"))
    draft = await _pinned(command, reference)

    assert draft.launch is not None and draft.launch.args == (reference,)
    assert asked == []
    assert f"name one release of server, for example {example}" in draft.warnings


@pytest.mark.parametrize(
    "answer",
    [
        lambda _request: httpx.Response(404, json={"error": "not found"}),
        lambda _request: httpx.Response(200, json={"version": "latest"}),
        lambda _request: httpx.Response(200, text="not json"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request)),
    ],
    ids=["unknown", "not-a-release", "unreadable", "unreachable"],
)
async def test_a_failed_lookup_keeps_the_paste_and_says_why(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], Any]
) -> None:
    _registry(monkeypatch, answer)
    draft = await _pinned("npx", "-y", "@scope/server")

    assert draft.launch is not None and draft.launch.args == ("-y", "@scope/server")
    assert any("could not be looked up" in warning for warning in draft.warnings)


async def test_a_found_release_is_reused_for_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    asked: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json={"version": f"1.0.{len(asked)}"})

    releases = package_pins.Releases(transport=httpx.MockTransport(serve), clock=lambda: now[0])
    assert await releases.current("npm", "server", "latest") == "1.0.1"
    assert await releases.current("npm", "server", "latest") == "1.0.1"
    now[0] = package_pins.CACHE_S + 1
    assert await releases.current("npm", "server", "latest") == "1.0.2"
    assert len(asked) == 2


async def test_an_import_pins_each_command_and_leaves_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, _npm("3.1.0"))
    paste = {
        "mcpServers": {
            "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
            "docs": {"url": "https://docs.example.com/mcp"},
        }
    }
    result = await package_pins.pin_import(parse_import(json.dumps(paste)))

    drafts = {draft.label: draft for draft in result.drafts}
    github, docs = drafts["github"], drafts["docs"]
    assert github.launch is not None
    assert github.launch.args == ("-y", "@modelcontextprotocol/server-github@3.1.0")
    assert docs.url == "https://docs.example.com/mcp" and docs.launch is None
