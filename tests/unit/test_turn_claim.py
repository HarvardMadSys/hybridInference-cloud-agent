"""The claim hand-over's steps, each against a store that records what it was asked.

The route-level cases (``tests/servers/test_worker_claim.py``) pin the answers
on the wire. These pin the steps: which way each refusal leaves the claim, and
that a refusal stops the steps after it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agent.application import turn_claim
from cloud_agent.application.owner_access import AGENT_ACCESS_WITHDRAWN, AgentAccessUnavailable
from cloud_agent.application.turn_claim import (
    ClaimRefusal,
    claim_environment,
    claim_fence,
    claim_ssh_access,
    hand_over_claim,
    mcp_bindings,
    require_entitled_repository,
    require_owner_agent_access,
)
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.domain.execution import FAILED, RUNNING
from cloud_agent.environments.environments import EnvironmentAccessRevoked
from cloud_agent.memory import memory_tool
from cloud_agent.source_control.entitlement import RepoAccessUnavailable, RepoNotAllowed
from cloud_agent_shared.runtime_catalog import runtimes_supporting_mcp

_FENCE = {"job_id": "ajob_1", "attempt_id": 7, "lease_generation": 3}


def _claim(**overrides: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "id": "ajob_1",
        "attempt_id": 7,
        "lease_generation": 3,
        "user_id": "owner",
        "thread_id": "athr_1",
        "repo": "owner/name",
        "runtime": runtimes_supporting_mcp()[0],
    }
    claim.update(overrides)
    return claim


def _store() -> Any:
    return SimpleNamespace(
        attempt_commands=SimpleNamespace(release_claim=AsyncMock(), transition=AsyncMock()),
        environments=SimpleNamespace(environment_for_claim=AsyncMock()),
        environment_snapshots=SimpleNamespace(snapshot_for_claim=AsyncMock()),
        ssh_servers=SimpleNamespace(ssh_group_state=AsyncMock(return_value=None)),
        source_control=object(),
        host_pool=object(),
        mcp=object(),
    )


def _released(store: Any) -> None:
    store.attempt_commands.release_claim.assert_awaited_once_with(**_FENCE, retry_after_seconds=30)
    store.attempt_commands.transition.assert_not_awaited()


def _failed(store: Any, detail: str) -> None:
    store.attempt_commands.transition.assert_awaited_once_with(
        **_FENCE, from_states=(RUNNING,), to_state=FAILED, detail=detail
    )
    store.attempt_commands.release_claim.assert_not_awaited()


def test_the_fence_names_the_attempt_and_its_lease() -> None:
    assert claim_fence(_claim()) == _FENCE


async def test_an_unconfirmed_owner_hands_the_claim_back(monkeypatch) -> None:
    monkeypatch.setattr(
        turn_claim, "owner_may_use_agent", AsyncMock(side_effect=AgentAccessUnavailable("down"))
    )
    store = _store()

    with pytest.raises(Unavailable) as raised:
        await require_owner_agent_access(store, object(), _claim())

    assert raised.value.type == "agent_access_unavailable"
    _released(store)


async def test_a_withdrawn_owner_ends_the_turn(monkeypatch) -> None:
    monkeypatch.setattr(turn_claim, "owner_may_use_agent", AsyncMock(return_value=False))
    store = _store()

    with pytest.raises(Refused) as raised:
        await require_owner_agent_access(store, object(), _claim())

    assert (raised.value.kind, raised.value.type) == ("forbidden", "agent_access_denied")
    _failed(store, AGENT_ACCESS_WITHDRAWN)


async def test_a_permitted_owner_leaves_the_claim_alone(monkeypatch) -> None:
    monkeypatch.setattr(turn_claim, "owner_may_use_agent", AsyncMock(return_value=True))
    store = _store()

    await require_owner_agent_access(store, object(), _claim())

    store.attempt_commands.release_claim.assert_not_awaited()
    store.attempt_commands.transition.assert_not_awaited()


async def test_a_temporary_workspace_has_no_repository_to_check(monkeypatch) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(turn_claim, "require_entitled_repo", lookup)

    await require_entitled_repository(_store(), _claim(repo=None), app_credentials=None)

    lookup.assert_not_awaited()


async def test_an_unreadable_repository_lookup_hands_the_claim_back(monkeypatch) -> None:
    monkeypatch.setattr(
        turn_claim,
        "require_entitled_repo",
        AsyncMock(side_effect=RepoAccessUnavailable("GitHub is down")),
    )
    store = _store()

    with pytest.raises(Unavailable) as raised:
        await require_entitled_repository(store, _claim(), app_credentials=None)

    assert (raised.value.type, raised.value.message) == (
        "source_control_unavailable",
        "GitHub is down",
    )
    _released(store)


async def test_a_repository_not_allowed_ends_the_turn(monkeypatch) -> None:
    monkeypatch.setattr(
        turn_claim, "require_entitled_repo", AsyncMock(side_effect=RepoNotAllowed("not yours"))
    )
    store = _store()

    with pytest.raises(Refused) as raised:
        await require_entitled_repository(store, _claim(), app_credentials=None)

    assert raised.value.type == "repo_not_allowed"
    _failed(store, "not yours")


async def test_a_chat_without_an_environment_reads_none() -> None:
    store = _store()

    assert await claim_environment(store, _claim(), [], None) == (None, None)

    store.environments.environment_for_claim.assert_not_awaited()


async def test_an_environment_that_requires_nothing_is_not_handed_over() -> None:
    store = _store()
    store.environments.environment_for_claim.return_value = SimpleNamespace(required=False)

    result = await claim_environment(store, _claim(environment_binding_id="aenv_1"), [], None)

    assert result == (None, None)
    store.environment_snapshots.snapshot_for_claim.assert_not_awaited()


async def test_a_host_that_cannot_apply_the_environment_gets_the_claim_back() -> None:
    store = _store()
    store.environments.environment_for_claim.return_value = SimpleNamespace(required=True)

    with pytest.raises(Unavailable) as raised:
        await claim_environment(store, _claim(environment_binding_id="aenv_1"), [], None)

    assert raised.value.type is None
    assert raised.value.message.startswith("Waiting for environment-capable host")
    _released(store)


async def test_a_revoked_team_environment_ends_the_turn() -> None:
    store = _store()
    store.environments.environment_for_claim.side_effect = EnvironmentAccessRevoked("left team")

    with pytest.raises(Refused) as raised:
        await claim_environment(store, _claim(environment_binding_id="aenv_1"), [], None)

    assert raised.value.type == "team_environment_revoked"
    _failed(store, "left team")


async def test_a_group_without_ssh_servers_needs_no_capability() -> None:
    store = _store()

    assert await claim_ssh_access(store, _claim(), []) == (None, None)

    store.attempt_commands.release_claim.assert_not_awaited()


async def test_a_host_that_cannot_apply_ssh_access_gets_the_claim_back() -> None:
    store = _store()
    store.ssh_servers.ssh_group_state.return_value = {"chats": []}

    with pytest.raises(Unavailable) as raised:
        await claim_ssh_access(store, _claim(), [])

    assert raised.value.message.startswith("Waiting for an SSH-capable host")
    _released(store)


async def test_a_runtime_without_mcp_resolves_no_bindings(monkeypatch) -> None:
    resolve = AsyncMock()
    monkeypatch.setattr(turn_claim, "resolve_for_thread", resolve)

    assert await mcp_bindings(_store(), _claim(runtime="no-such-runtime")) == ([], [])

    resolve.assert_not_awaited()


async def test_the_memory_tools_follow_the_threads_own_bindings(monkeypatch) -> None:
    monkeypatch.setattr(turn_claim, "resolve_for_thread", AsyncMock(return_value=[]))
    monkeypatch.setattr(turn_claim, "split_bindings", lambda _bindings: (["deepwiki"], []))
    monkeypatch.setattr(turn_claim.web_search, "configured", lambda: False)

    aliases, servers = await mcp_bindings(_store(), _claim())

    assert aliases == ["deepwiki", memory_tool.RUNTIME_SERVER_ID]
    assert servers == []


@pytest.mark.parametrize(
    ("answer", "action"),
    [
        (AgentAccessUnavailable("down"), "release"),
        (False, "fail"),
    ],
)
async def test_a_refusal_comes_back_with_what_was_done_and_stops_the_steps(
    monkeypatch, answer, action
) -> None:
    access = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, Exception)
        else AsyncMock(return_value=answer)
    )
    monkeypatch.setattr(turn_claim, "owner_may_use_agent", access)
    lookup = AsyncMock()
    monkeypatch.setattr(turn_claim, "require_entitled_repo", lookup)
    store = _store()

    outcome = await hand_over_claim(
        store, _claim(), app_credentials=None, owners=object(), attestation=None, host="runner-a"
    )

    assert isinstance(outcome, ClaimRefusal)
    assert outcome.action == action
    assert isinstance(outcome.error, Unavailable if action == "release" else Refused)
    lookup.assert_not_awaited()
    store.ssh_servers.ssh_group_state.assert_not_awaited()
