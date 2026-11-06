"""A residency's credentials are masked in a turn's output, like the claim's.

The runner's redactor is built from the claim when the turn starts. A
residency's MCP and GitHub relay credentials (``amr.``, ``agh.``) arrive only
when the member's process is opened, so the attempt then adds every launch
facet's ``secrets()`` to it (``launch_secrets``, ``ControlPlane.add_secrets``).
These tests run that path end to end into the events a control plane stores,
and walk every launch facet: a field marked
:data:`~cloud_agent_host.launch.facet.SECRET` must be named by its facet's
``secrets()``, and every value a facet keeps out of its ``repr`` must reach
the launch's masked set.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from dataclasses import fields, is_dataclass, replace
from typing import Any

import httpx
from tests.support.owners import residency_connection
from tests.unit.test_agent_runner import _probe_then_replace, _run_shared_boundary_case

import cloud_agent_host.launch
from cloud_agent_host.control_plane_client import ControlPlane
from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_host.launch.facets import launch_secrets
from cloud_agent_host.launch.github_relay import GitHubRelayFacet
from cloud_agent_host.launch.inference import InferenceFacet
from cloud_agent_host.launch.mcp_relay import McpRelayFacet
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.runtimes import NormalizedEvent
from cloud_agent_host.turn import shared_live_attempt
from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable
from cloud_agent_shared.sandbox_mcp import SandboxMcpServer
from cloud_agent_shared.secret_redaction import MIN_SECRET_CHARS
from cloud_agent_shared.shared_protocol import (
    RESIDENCY_GITHUB_CREDENTIAL_PREFIX,
    RESIDENCY_MCP_CREDENTIAL_PREFIX,
)


def _fake(label: str) -> str:
    """A placeholder credential, built so a secret scanner never sees a literal."""
    return f"{label}-{'0123456789' * 2}"


_MCP = RESIDENCY_MCP_CREDENTIAL_PREFIX + _fake("residency-mcp")
_GITHUB = RESIDENCY_GITHUB_CREDENTIAL_PREFIX + _fake("residency-github")
_KEYED = SandboxMcpServer(
    alias="keyed",
    command="npx",
    args=("-y", "keyed-mcp@1.0.0"),
    env={"KEY": "secret://KEY"},
    secret_env={"KEY": _fake("program-key")},
)
#: Every credential-bearing field filled, the translated plan included.
_FULL = ResidentLaunch(
    thread_id="athr_1",
    runtime_id="claude-code",
    runtime_version="2.1.220",
    native_session_id="native-1",
    model="model-a",
    reasoning_effort="high",
    credential=_fake("owner-key"),
    mcp_server_ids=("deepwiki",),
    mcp_base_url="http://agent-mcp:8000",
    mcp_credential=_MCP,
    sandbox_mcp_servers=(_KEYED,),
    slot_id=1,
    residency_generation=3,
    github_credential=_GITHUB,
    github_repo="owner/repo",
    git_author_name="Owner",
    git_author_email="owner@example.com",
    git_coauthor="Agent <agent@example.com>",
    environment=BoundEnvironment(
        binding_id="binding",
        variables=[EnvironmentVariable(name="TOKEN", value=_fake("env-token"))],
    ),
    inference_base_url="https://gateway.example/v1",
    inference_protocol="chat_completions",
    inference_source="mconn_" + "0" * 32 + "@1",
    inference_client_protocol="anthropic_messages",
    inference_local_token="a" * 64,
)


class _Plane:
    """A control plane that stores every event it is sent."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/events:batch"):
            self.events.extend(body["events"])
        return httpx.Response(201, json={})


def _control(plane: _Plane, secrets: dict[str, str] | None = None) -> ControlPlane:
    """A real worker client whose only wire is ``plane``, its redactor built from ``secrets``."""
    control = ControlPlane("https://control.test", "test-token")
    control.close()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(plane))
    control.job_id = "ajob_1"
    control.add_secrets(secrets or {})
    return control


def _facet_classes() -> set[type[LaunchFacet]]:
    """Every ``LaunchFacet`` subclass the launch package defines."""
    for module in pkgutil.iter_modules(cloud_agent_host.launch.__path__):
        importlib.import_module(f"cloud_agent_host.launch.{module.name}")
    pending, found = list(LaunchFacet.__subclasses__()), set()
    while pending:
        cls = pending.pop()
        found.add(cls)
        pending.extend(cls.__subclasses__())
    return found


def _marked_fields() -> set[tuple[type[LaunchFacet], str]]:
    return {
        (cls, item.name)
        for cls in _facet_classes()
        if is_dataclass(cls)
        for item in fields(cls)
        if item.metadata.get("secret")
    }


def test_the_launch_carries_every_facet():
    assert {type(facet) for facet in _FULL.facets} == _facet_classes()


def test_the_marked_fields_are_the_launchs_own_credentials():
    # Adding a credential to a facet adds it here, where review sees it.
    assert _marked_fields() == {
        (InferenceFacet, "credential"),
        (InferenceFacet, "local_token"),
        (McpRelayFacet, "credential"),
        (GitHubRelayFacet, "credential"),
    }


def test_every_marked_field_is_named_by_its_facets_secrets():
    for facet in _FULL.facets:
        for item in fields(facet):
            if not item.metadata.get("secret"):
                continue
            value = getattr(facet, item.name)
            assert item.repr is False, (type(facet).__name__, item.name)
            # The full launch fills it, so none passes unseen.
            assert value, (type(facet).__name__, item.name)
            assert value in facet.secrets().values(), (type(facet).__name__, item.name)


def _held_secrets(value: Any) -> list[str]:
    """The values a field kept out of a repr holds: itself, or what its holder names."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, tuple):
        return [secret for item in value for secret in _held_secrets(item)]
    secrets = value.secrets().values() if hasattr(value, "secrets") else ()
    return [secret for secret in secrets if len(secret.strip()) >= MIN_SECRET_CHARS]


def test_every_value_kept_out_of_a_repr_is_masked_with_the_launch():
    """A facet may hold a credential another facet names; the launch masks it once."""
    masked = set(launch_secrets(_FULL).values())
    holders: list[tuple[str, str, Any]] = [
        ("ResidentLaunch", item.name, getattr(_FULL, item.name))
        for item in fields(_FULL)
        if not item.repr
    ]
    holders += [
        (type(facet).__name__, item.name, getattr(facet, item.name))
        for facet in _FULL.facets
        for item in fields(facet)
        if not item.repr
    ]
    missing = [
        (owner, name)
        for owner, name, value in holders
        for secret in _held_secrets(value)
        if secret not in masked
    ]
    assert missing == []
    assert {_MCP, _GITHUB, _KEYED.secret_env["KEY"]} <= masked


def test_launch_secrets_names_the_residency_credentials():
    values = launch_secrets(_FULL)
    assert values["mcp_credential"] == _MCP
    assert values["github_credential"] == _GITHUB
    assert values["inference_credential"] == _fake("owner-key")


def test_launch_secrets_leaves_a_short_value_out():
    """As for a sandbox program's key: masking ``"1"`` would blank every 1 in the output."""
    short = replace(_FULL, mcp_credential="amr.x", github_credential="agh.y")
    values = launch_secrets(short)
    assert "mcp_credential" not in values and "github_credential" not in values


def test_an_added_value_never_takes_a_masked_ones_place():
    plane = _Plane()
    control = _control(plane, {"TOKEN": _fake("claim-token")})
    try:
        control.add_secrets({"TOKEN": _fake("launch-token"), "again": _fake("claim-token")})
        control.append_event(
            NormalizedEvent(
                "tool_result",
                {"content": f"{_fake('claim-token')} {_fake('launch-token')}"},
            )
        )
        control.flush()
    finally:
        control.close()

    assert plane.events[0]["payload"] == {"content": "[secret:TOKEN] [secret:TOKEN@1]"}


def test_an_open_preview_masks_an_added_value_and_keeps_its_held_tail():
    """The preview is rebuilt as a reset would rebuild it, without dropping what it held."""
    claim_token = _fake("claim-token")
    plane = _Plane()
    control = _control(plane, {"TOKEN": claim_token})
    try:
        # The tail might begin the claim's token, so the preview holds it back.
        control.append_event(
            NormalizedEvent("message_delta", {"preview_id": "p1", "delta": "a " + claim_token[:6]})
        )
        control.add_secrets({"mcp_credential": _MCP})
        control.append_event(
            NormalizedEvent(
                "message_delta", {"preview_id": "p1", "delta": claim_token[6:] + " b " + _MCP}
            )
        )
        control.finish("succeeded")
    finally:
        control.close()

    previews = "".join(event["payload"]["delta"] for event in plane.events)
    assert previews == "a [secret:TOKEN] b [secret:mcp_credential]"


def test_residency_credentials_the_member_prints_are_stored_masked(monkeypatch):
    """The attempt opens a residency; what the member prints of it is stored masked."""
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://relay")
    plane = _Plane()
    real = _control(plane)

    def member_prints(**_kwargs: Any) -> tuple[int, str, list[Any], None, None]:
        printed = f"MCP={_MCP} GH_TOKEN={_GITHUB}"
        real.append_event(NormalizedEvent("tool_result", {"content": printed}))
        real.append_event(NormalizedEvent("message", {"text": printed}))
        half = len(_GITHUB) // 2
        for delta in ("token " + _MCP[:7], _MCP[7:] + " " + _GITHUB[:half], _GITHUB[half:]):
            real.append_event(
                NormalizedEvent("message_delta", {"preview_id": "p1", "delta": delta})
            )
        return 0, "", [], None, None

    def configure(control: Any, supervisor: Any, _finish: Any) -> None:
        _probe_then_replace(
            control,
            supervisor,
            residency={
                "mcp_credential": _MCP,
                "github_credential": _GITHUB,
                **residency_connection("claude-code"),
            },
        )
        control.add_secrets.side_effect = real.add_secrets
        monkeypatch.setattr(shared_live_attempt, "_stream_shared_agent_turn", member_prints)

    try:
        result, _control_mock, supervisor, _finish = _run_shared_boundary_case(
            monkeypatch,
            0,
            None,
            "claude-code",
            configure=configure,
            job_changes={"mcp_bindings": [":platform:memory"]},
        )
        real.finish("succeeded")
    finally:
        real.close()

    assert result == 0
    opened = supervisor.ensure_shared_resident.mock_calls[-1].kwargs["launch"]
    assert (opened.mcp_credential, opened.github_credential) == (_MCP, _GITHUB)
    stored = json.dumps(plane.events)
    assert _MCP not in stored and _GITHUB not in stored
    masked = "MCP=[secret:mcp_credential] GH_TOKEN=[secret:github_credential]"
    assert plane.events[0]["payload"] == {"content": masked}
    assert plane.events[1]["payload"] == {"text": masked}
    previews = "".join(
        event["payload"]["delta"]
        for event in plane.events
        if event["event_type"] == "message_delta"
    )
    assert previews == "token [secret:mcp_credential] [secret:github_credential]"
