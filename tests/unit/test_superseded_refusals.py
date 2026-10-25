"""A Host refusing to remove a left-behind container: what release does and says.

Staging, 2026-10-23: the first of two left-behind containers was refused 279
times over three hours. The second was never asked for, and every refusal
reached the operation as the same generic sentence, so nobody could tell
what the Host was waiting for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from tests.unit.test_compute_reconcile import _left_behind, _left_behind_container, harness

from cloud_agent.sandbox_manager_client import SandboxManagerError


def _two_left_behind(h, answer):
    first = _left_behind_container()
    second = {**first, "sandbox_id": "asbx_other", "container_id": "container-other"}
    _left_behind(h)
    h.repository.authorize_superseded_removals.return_value = [
        {
            **{key: value for key, value in item.items() if key != "running"},
            "request_id": "superseded-" + item["sandbox_id"],
        }
        for item in (first, second)
    ]
    h.sandbox_manager.list_sandboxes = AsyncMock(return_value=[first, second])

    def removal(_thread_id, payload):
        refused = answer(payload["sandbox_id"])
        if refused is not None:
            raise refused
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    h.sandbox_manager.discard_sandbox_deletion = AsyncMock(side_effect=removal)


@pytest.mark.asyncio
async def test_a_refused_container_does_not_keep_the_others_from_going(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _two_left_behind(
        h,
        lambda sandbox_id: (
            SandboxManagerError(409, "sandbox archive requires an idle group")
            if sandbox_id == "asbx_moved"
            else None
        ),
    )

    await h.reconciler.execute(h.operation)

    asked = [
        call.args[1]["sandbox_id"]
        for call in h.sandbox_manager.discard_sandbox_deletion.await_args_list
    ]
    assert asked == ["asbx_moved", "asbx_other"]
    h.repository.record_superseded_removal.assert_awaited_once_with(
        request_id="superseded-asbx_other"
    )
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["superseded_removed"] == 1
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_hosts_reason_for_a_refusal_reaches_the_operation(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(
        h, removal=SandboxManagerError(409, "sandbox lifecycle archive must finish before deletion")
    )

    await h.reconciler.execute(h.operation)

    reason = h.operation["result"]["blocked_reasons"][0]
    assert reason["code"] == "superseded_container_not_removed"
    assert "asbx_moved" in reason["message"]
    assert "HTTP 409: sandbox lifecycle archive must finish before deletion" in reason["message"]


@pytest.mark.asyncio
async def test_a_reason_that_is_not_a_plain_sentence_is_reported_by_its_status(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(503, "docker: /var/lib/x: Error 0xDEAD"))

    await h.reconciler.execute(h.operation)

    message = h.operation["result"]["blocked_reasons"][0]["message"]
    assert message.endswith("(asbx_moved, HTTP 503)")


@pytest.mark.asyncio
async def test_a_refusal_for_an_administrator_outranks_one_that_may_pass(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _two_left_behind(
        h,
        lambda sandbox_id: (
            SandboxManagerError(503, "unavailable")
            if sandbox_id == "asbx_moved"
            else SandboxManagerError(422, "refused")
        ),
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert "asbx_other" in h.operation["result"]["blocked_reasons"][0]["message"]


@pytest.mark.parametrize(
    "detail",
    [
        "private token synthetic-secret-0123456789",
        "sandbox archive requires an idle group synthetic-secret-0123456789",
        "docker: /var/lib/synthetic-private-path: failure",
    ],
)
def test_unknown_manager_detail_never_reaches_operation_or_logs(caplog, detail):
    from cloud_agent.application.superseded_removal import superseded_refusal

    error = superseded_refusal(
        {"host": "test-host"},
        {"sandbox_id": "asbx_test", "generation": 1},
        SandboxManagerError(409, detail),
    )

    assert detail not in str(error)
    assert all(detail not in str(record.__dict__) for record in caplog.records)


@pytest.mark.parametrize(
    "detail",
    [
        "sandbox archive generation is stale",
        "sandbox archive identity changed",
        "sandbox discard request identity changed",
        "sandbox archive request cannot be discarded",
        "sandbox discard target is no longer absent",
    ],
)
@pytest.mark.asyncio
async def test_stale_discard_identity_blocks_instead_of_retrying_forever(monkeypatch, detail):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(409, detail))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert detail in h.operation["result"]["blocked_reasons"][0]["message"]
    h.repository.record_superseded_removal.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.parametrize(
    "detail",
    [
        "sandbox lifecycle archive must finish before deletion",
        "sandbox archive requires an idle group",
        "another sandbox archive is in progress",
        "sandbox archive request is stale",
        "shared sandbox lifecycle check is in progress",
        "unknown future manager reason",
    ],
)
@pytest.mark.asyncio
async def test_busy_and_unrecognized_409_keep_the_same_retry(monkeypatch, detail):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(409, detail))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    h.repository.commit_release.assert_not_awaited()
    assert h.sandbox_manager.discard_sandbox_deletion.await_args.args[1]["request_id"] == (
        "superseded-recorded"
    )


@pytest.mark.asyncio
async def test_a_changed_container_identity_is_reported_as_gone_and_blocks(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(
        h, removal=SandboxManagerError(410, "archived sandbox identity changed during deletion")
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert "HTTP 410" in h.operation["result"]["blocked_reasons"][0]["message"]
    h.repository.record_superseded_removal.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.parametrize("log_format", ["text", "json"])
@pytest.mark.asyncio
async def test_refusal_evidence_survives_the_process_formatter(monkeypatch, caplog, log_format):
    import logging

    from cloud_agent.logging_utils import _FORMAT
    from cloud_agent_shared.logging_utils import JsonLogFormatter

    h = harness(monkeypatch, kind="retire", identified=True)
    detail = "sandbox lifecycle archive must finish before deletion"
    _left_behind(h, removal=SandboxManagerError(409, detail))

    await h.reconciler.execute(h.operation)

    (record,) = [r for r in caplog.records if r.message.startswith("superseded_container_refused")]
    formatter = JsonLogFormatter() if log_format == "json" else logging.Formatter(_FORMAT)
    emitted = formatter.format(record)
    for value in (detail, str(h.operation["id"]), "superseded-recorded", "asbx_moved", "409"):
        assert value in emitted
    assert "container_id" in emitted
    assert "retryable" in emitted
    h.repository.commit_release.assert_not_awaited()
