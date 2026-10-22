"""A heartbeat's answer is made of the policy its own write read.

The write reads the settings, archive policy and agent versions under the beat's
own lock, so the answer needs no read of its own. A part that does not decode
is read again once the beat's work is done, and fails only the answer. These
cases reuse the pool suite's fake store.
"""

from __future__ import annotations

from typing import Any

import pytest

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    _TARGET,
    _enabled_archive,
    _enroll,
    _live_group,
    client,
    residency_client,
)

from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError


async def test_a_heartbeat_answers_from_the_policy_its_own_write_read(client, monkeypatch):
    """The policy the beat's write share-locks is the one the answer is made of.

    Nothing reads it again, and with no agent update waiting, nothing tries
    to switch one.
    """
    async_client, store = client
    credential = await _enroll(async_client, store, capabilities=["sandbox-checkpoints-v1"])
    _enabled_archive(store, checkpoints_enabled=False)
    store.global_settings = {"agent_timeout_s": 5400}
    store.runtime_upgrades["codex"] = {**_TARGET, "version": "0.162.1", "activated_at": "x"}

    async def reread() -> Any:
        raise AssertionError("the heartbeat read the policy again")

    for name in (
        "host_settings",
        "sandbox_archive_settings",
        "runtime_download_targets",
        "activate_ready_runtime_upgrades",
    ):
        monkeypatch.setattr(store, name, reread)
    answer = (
        await async_client.post(
            "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
        )
    ).json()

    assert answer["settings"]["agent_timeout_s"] == 5400
    assert answer["sandbox_archive_settings"]["revision"] == 5
    assert answer["sandbox_archive_settings"]["checkpoints_enabled"] is False
    assert [target["version"] for target in answer["runtime_downloads"]] == ["0.162.1"]


async def test_the_beat_that_switches_an_update_answers_with_what_it_switched_to(
    client, monkeypatch
):
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.runtime_upgrades["claude-code"] = dict(_TARGET)
    switched = [{"runtime": "claude-code", "version": "2.1.300", "before": "b", "nonce": "n"}]

    async def after_the_switch() -> list[dict[str, str]]:
        return switched

    monkeypatch.setattr(store, "runtime_download_targets", after_the_switch)
    answer = (
        await async_client.post(
            "/v1/agent/host/heartbeat",
            json={"runtime_versions_available": {"claude-code": ["2.1.220", "2.1.300"]}},
            headers={"Authorization": f"Bearer {credential}"},
        )
    ).json()

    assert store.runtime_versions == {"claude-code": "2.1.300"}
    assert answer["runtime_downloads"] == switched


@pytest.mark.parametrize(
    ("part", "error", "message"),
    [
        ("archive_settings", SandboxArchivePolicyError, "region is required"),
        ("host_settings", TypeError, "host settings"),
        ("runtime_policy", TypeError, "agent versions"),
    ],
)
async def test_a_policy_that_does_not_decode_fails_only_the_answer(
    residency_client, part: str, error: type[Exception], message: str
):
    """The beat's own work is done before a policy part that does not decode fails it.

    The heartbeat now reads the policy in its own write. A stored archive
    policy, set of global settings or record of agent versions that does not
    decode used to fail only the answer, with the error of its own read, once
    the beat had extended its residencies and reconciled its observations,
    and it still does. A beat that cannot read whether an agent update waits
    still tries to switch one, which reads the updates itself, as every beat
    did then.
    """
    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    setattr(store, f"{part}_undecodable", True)
    store.beat_answers = {("th-1", 1): ""}

    with pytest.raises(error, match=message):
        await client.post(
            "/v1/agent/host/heartbeat",
            json={"retained_sandboxes": [_live_group(("th-1", 1))]},
            headers={"Authorization": f"Bearer {credential}"},
        )

    assert store.beat_batches[0] == [("th-1", 1)]
    assert [observation["sandbox_id"] for observation in store.sandbox_observations] == ["asbx_1"]
    # No update waits in the others, and this beat's read cannot say.
    assert store.activation_attempts == (1 if part == "runtime_policy" else 0)


@pytest.mark.parametrize(
    ("part", "error"),
    [("archive_settings", SandboxArchivePolicyError), ("host_settings", TypeError)],
)
async def test_a_policy_that_does_not_decode_still_lets_the_beat_switch_an_update(
    client, part: str, error: type[Exception]
):
    """The agent update a beat completes switches before the policy fails the answer.

    Until the beat read the policy itself, the switch came before the answer
    read the policy, and a part that did not decode failed only the answer.
    Reading the part again first kept every waiting update from switching for
    as long as it stayed broken.
    """
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.runtime_upgrades["claude-code"] = dict(_TARGET)
    setattr(store, f"{part}_undecodable", True)

    with pytest.raises(error):
        await async_client.post(
            "/v1/agent/host/heartbeat",
            json={"runtime_versions_available": {"claude-code": ["2.1.220", "2.1.300"]}},
            headers={"Authorization": f"Bearer {credential}"},
        )

    assert store.runtime_versions == {"claude-code": "2.1.300"}
    assert store.runtime_upgrades["claude-code"]["activated_at"]
