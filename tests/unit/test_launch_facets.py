"""A launch's facets decide process reuse exactly as the hand-kept list they replaced.

``ResidentTransport.incompatible_fields`` was a hand-written list of sixteen
checks. It is now derived: the launch's identity, each facet's
``differs_from``, then the slot. ``_hand_list`` below is that list as it was,
kept here as the reference while the derivation settles. Every pair drawn from
a pool of launches -- one changed field at a time, two at a time, and the
credential-less probes the runner sends -- must produce the same names, in the
same order, since the order reaches the startup measurement.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent_host.launch.browser import BrowserFacet
from cloud_agent_host.launch.environment import EnvironmentFacet
from cloud_agent_host.launch.facets import PREPARE_ORDER
from cloud_agent_host.launch.github_relay import GitHubRelayFacet
from cloud_agent_host.launch.inference import InferenceFacet
from cloud_agent_host.launch.mcp_relay import McpRelayFacet
from cloud_agent_host.launch.runtime import RuntimeFacet
from cloud_agent_host.launch.sandbox_mcp import SandboxMcpFacet
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransport
from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable
from cloud_agent_shared.sandbox_mcp import SandboxMcpServer
from cloud_agent_shared.shared_protocol import (
    RESIDENCY_GITHUB_CREDENTIAL_PREFIX,
    RESIDENCY_MCP_CREDENTIAL_PREFIX,
    is_residency_github_credential,
    is_residency_mcp_credential,
)


def _fake(label: str) -> str:
    """A placeholder credential, built so a secret scanner never sees a literal."""
    return f"{label}-{'0123456789' * 2}"


_SERVER = SandboxMcpServer(alias="search", command="npx", args=("-y", "search-mcp@1.0.0"))
_KEYED = SandboxMcpServer(
    alias="keyed",
    command="npx",
    args=("-y", "keyed-mcp@1.0.0"),
    env={"KEY": "secret://KEY"},
    secret_env={"KEY": _fake("program-key")},
)
_PLUGINS = ({"plugin": "review@fixture", "marketplace": "owner/marketplace"},)


def _environment(value: str) -> BoundEnvironment:
    return BoundEnvironment(
        binding_id="binding",
        variables=[EnvironmentVariable(name="TOKEN", value=value, secret=True)],
    )


def _hand_list(held: ResidentLaunch, launch: ResidentLaunch) -> tuple[str, ...]:
    """``incompatible_fields`` as it was written by hand, before the facets."""
    held_mcp = held.mcp_credential
    same_mcp = (
        held.mcp_server_ids == launch.mcp_server_ids
        and held.sandbox_mcp_servers == launch.sandbox_mcp_servers
    ) and (
        not launch.mcp_server_ids
        or (
            held.mcp_base_url == launch.mcp_base_url
            and (
                held_mcp == launch.mcp_credential
                or (not launch.mcp_credential and is_residency_mcp_credential(held_mcp))
            )
        )
    )
    same_github = (
        held.github_credential == launch.github_credential
        and (held.git_author_name, held.git_author_email, held.git_coauthor)
        == (launch.git_author_name, launch.git_author_email, launch.git_coauthor)
    ) or (
        not launch.github_credential
        and (not held.github_credential or is_residency_github_credential(held.github_credential))
    )
    checks = (
        ("thread_id", held.thread_id == launch.thread_id),
        ("runtime_id", held.runtime_id == launch.runtime_id),
        ("runtime_version", held.runtime_version == launch.runtime_version),
        ("model", held.model == launch.model),
        ("reasoning_effort", held.reasoning_effort == launch.reasoning_effort),
        ("plugins", held.plugins == launch.plugins),
        ("inference_source", held.inference_source == launch.inference_source),
        ("inference_base_url", held.inference_base_url == launch.inference_base_url),
        ("inference_protocol", held.inference_protocol == launch.inference_protocol),
        (
            "inference_client_protocol",
            held.inference_client_protocol == launch.inference_client_protocol,
        ),
        ("inference_local_token", held.inference_local_token == launch.inference_local_token),
        ("mcp", same_mcp),
        ("github", same_github),
        ("github_repo", held.github_repo == launch.github_repo),
        ("environment", held.environment == launch.environment),
        ("slot_id", held.slot_id == launch.slot_id),
    )
    return tuple(name for name, same in checks if not same)


_BASE = ResidentLaunch(
    thread_id="athr_1",
    runtime_id="claude-code",
    runtime_version="2.1.220",
    native_session_id="native-1",
    model="model-a",
    reasoning_effort="high",
    credential=_fake("owner-key"),
    mcp_server_ids=("deepwiki",),
    mcp_base_url="http://agent-mcp:8000",
    mcp_credential=RESIDENCY_MCP_CREDENTIAL_PREFIX + _fake("mcp"),
    sandbox_mcp_servers=(_SERVER,),
    slot_id=1,
    residency_generation=3,
    plugins=_PLUGINS,
    github_credential=RESIDENCY_GITHUB_CREDENTIAL_PREFIX + _fake("github"),
    github_repo="owner/repo",
    git_author_name="Owner",
    git_author_email="owner@example.com",
    git_coauthor="Agent <agent@example.com>",
    environment=_environment(_fake("env-one")),
    inference_base_url="https://gateway.example/v1",
    inference_protocol="chat_completions",
    inference_source="mconn_" + "0" * 32 + "@1",
    inference_client_protocol="anthropic_messages",
    inference_local_token="a" * 64,
)

#: For every field, values other than the base's. Changing one never makes the
#: launch invalid; the translated pair moves together.
_CHANGES: dict[str, list[dict[str, object]]] = {
    "thread_id": [{"thread_id": "athr_2"}],
    "runtime_id": [{"runtime_id": "codex", "plugins": ()}],
    "runtime_version": [{"runtime_version": None}, {"runtime_version": "2.1.221"}],
    "native_session_id": [{"native_session_id": None}],
    "model": [{"model": "model-b"}],
    "reasoning_effort": [{"reasoning_effort": None}],
    "credential": [{"credential": None}, {"credential": _fake("other-key")}],
    "mcp_server_ids": [{"mcp_server_ids": ()}, {"mcp_server_ids": ("deepwiki", "other")}],
    "mcp_base_url": [{"mcp_base_url": ""}, {"mcp_base_url": "http://other:8000"}],
    "mcp_credential": [
        {"mcp_credential": ""},
        {"mcp_credential": RESIDENCY_MCP_CREDENTIAL_PREFIX + _fake("other")},
        {"mcp_credential": _fake("attempt-mcp")},
    ],
    "sandbox_mcp_servers": [{"sandbox_mcp_servers": ()}, {"sandbox_mcp_servers": (_KEYED,)}],
    "slot_id": [{"slot_id": 0}],
    "residency_generation": [{"residency_generation": 4}],
    "plugins": [{"plugins": ()}],
    "github_credential": [
        {"github_credential": ""},
        {"github_credential": RESIDENCY_GITHUB_CREDENTIAL_PREFIX + _fake("other")},
        {"github_credential": _fake("not-a-placeholder")},
    ],
    "github_repo": [{"github_repo": ""}, {"github_repo": "owner/other"}],
    "git_author_name": [{"git_author_name": ""}, {"git_author_name": "Other"}],
    "git_author_email": [{"git_author_email": "other@example.com"}],
    "git_coauthor": [{"git_coauthor": ""}],
    "environment": [{"environment": None}, {"environment": _environment(_fake("env-two"))}],
    "inference_base_url": [{"inference_base_url": "https://other.example/v1"}],
    "inference_protocol": [{"inference_protocol": "responses"}],
    "inference_source": [{"inference_source": "mconn_" + "0" * 32 + "@2"}],
    "inference_client_protocol": [
        {"inference_client_protocol": "", "inference_local_token": ""},
        {"inference_client_protocol": "responses"},
    ],
    "inference_local_token": [{"inference_local_token": "b" * 64}],
}

#: What the runner's probe looks like: no residency credentials, no identity.
_PROBE = {
    "credential": None,
    "mcp_credential": "",
    "github_credential": "",
    "git_author_name": "",
    "git_author_email": "",
    "git_coauthor": "",
}


def _pool() -> list[ResidentLaunch]:
    single = [replace(_BASE, **change) for options in _CHANGES.values() for change in options]
    probes = [replace(launch, **_PROBE) for launch in [_BASE, *single]]
    return [_BASE, *single, *probes]


def _derived(held: ResidentLaunch, offered: ResidentLaunch) -> tuple[str, ...]:
    return ResidentTransport(launch=held, session=SimpleNamespace()).incompatible_fields(offered)


def test_every_field_of_the_launch_is_varied():
    assert set(_CHANGES) == set(ResidentLaunch.__dataclass_fields__)


@pytest.mark.parametrize(
    "change",
    [change for options in _CHANGES.values() for change in options],
    ids=lambda change: "+".join(change),
)
def test_one_changed_field_names_what_the_hand_list_named(change):
    offered = replace(_BASE, **change)
    for held, launch in ((_BASE, offered), (offered, _BASE)):
        assert _derived(held, launch) == _hand_list(held, launch)
        assert _derived(held, replace(launch, **_PROBE)) == _hand_list(
            held, replace(launch, **_PROBE)
        )


def test_every_pair_of_launches_names_what_the_hand_list_named_in_its_order():
    pool = _pool()
    for held, offered in itertools.product(pool, repeat=2):
        assert _derived(held, offered) == _hand_list(held, offered)
    # Two changes at once, so the order of several names is pinned as well.
    changes = [change for options in _CHANGES.values() for change in options]
    for first, second in itertools.combinations(changes, 2):
        if set(first) & set(second):
            continue
        offered = replace(_BASE, **first, **second)
        assert _derived(_BASE, offered) == _hand_list(_BASE, offered)
        assert _derived(offered, _BASE) == _hand_list(offered, _BASE)


def test_the_trusted_steps_run_gateway_admission_before_the_runtime_and_its_helper():
    assert (BrowserFacet, SandboxMcpFacet, RuntimeFacet, InferenceFacet) == PREPARE_ORDER


def test_each_facet_names_the_secrets_it_brings_into_the_sandbox():
    launch = replace(_BASE, sandbox_mcp_servers=(_KEYED,))
    assert launch.facet(InferenceFacet).secrets() == {
        "inference_credential": _BASE.credential,
        "compat_token": _BASE.inference_local_token,
    }
    assert launch.facet(McpRelayFacet).secrets() == {"mcp_credential": _BASE.mcp_credential}
    assert launch.facet(SandboxMcpFacet).secrets() == _KEYED.secrets()
    assert launch.facet(GitHubRelayFacet).secrets() == {
        "github_credential": _BASE.github_credential
    }
    assert launch.facet(EnvironmentFacet).secrets() == {"TOKEN": _fake("env-one")}
    # Nothing a facet holds reaches a repr.
    for facet in launch.facets:
        for value in facet.secrets().values():
            assert value not in repr(facet)
