"""Verify archive-first deletion ordering and failure/retry boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import sandbox_manager_routing
from cloud_agent.logging_utils import JsonLogFormatter
from cloud_agent.ports.sandbox import DeletionFailureDecision
from cloud_agent.sandbox_archive import ArchiveReceipt, ArchiveUpload
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.workers import sandbox_deletion as worker

SHA256 = "a" * 64
KEY = "sandbox-archives/group/request/archive.tar"
URI = f"s3://test-archive-bucket/{KEY}"


def setup(monkeypatch, *, state="pending"):
    """Build strict port-shaped fakes and track destructive ordering."""
    events = []
    claim = {
        "id": "group",
        "origin_thread_id": "origin",
        "host": "host-one",
        "generation": 4,
        "container_id": "container-one",
        "deletion_request_id": "request",
        "deletion_claim_token": "claim-token",
        "deletion_attempt_correlation_id": "00000000-0000-0000-0000-000000000001",
        "deletion_attempt_number": 1,
        "deletion_state": state,
        "deletion_archive_key": URI if state == "archived" else None,
        "deletion_sha256": SHA256 if state == "archived" else None,
        "deletion_size_bytes": 1024 if state == "archived" else None,
        "deletion_cleanup_required": False,
        "deletion_cleanup_archive_key": None,
    }
    receipt = {
        "request_id": "request",
        "container_id": "container-one",
        "archive_key": KEY,
        "sha256": SHA256,
        "size_bytes": 1024,
    }

    def event(name, result):
        async def call(*_, **__):
            events.append(name)
            return result

        return call

    port = SimpleNamespace(
        claim_due_admin_sandbox_deletion=AsyncMock(return_value=claim),
        recover_reclaimed_admin_sandbox_discard=AsyncMock(return_value=False),
        record_admin_sandbox_archive=AsyncMock(side_effect=event("record", True)),
        complete_admin_sandbox_deletion=AsyncMock(side_effect=event("complete", True)),
        fail_admin_sandbox_deletion=AsyncMock(
            return_value=DeletionFailureDecision(
                recorded=True,
                retry_scheduled=True,
                retry_at=None,
                terminal_reason=None,
            )
        ),
        renew_admin_sandbox_deletion_claim=AsyncMock(return_value=True),
        update_admin_sandbox_deletion_attempt_stage=AsyncMock(return_value=True),
        finish_admin_sandbox_deletion_attempt=AsyncMock(return_value=True),
    )
    sandbox_manager = SimpleNamespace(
        shared_chat_sandbox_status=AsyncMock(
            side_effect=event(
                "status",
                {
                    "thread_id": "origin",
                    "origin_thread_id": "origin",
                    "sandbox_id": "group",
                    "generation": 4,
                    "container_id": "container-one",
                    "running": False,
                    "docker_status": "stopped",
                    "terminal_count": 0,
                    "operation_count": 0,
                    "busy": False,
                    "group_busy": False,
                    "snapshotting": False,
                },
            )
        ),
        prepare_sandbox_deletion=AsyncMock(side_effect=event("prepare", receipt)),
        upload_sandbox_deletion_archive=AsyncMock(
            side_effect=event("upload", {**receipt, "uploaded": True})
        ),
        commit_sandbox_deletion=AsyncMock(
            side_effect=event("delete", {**receipt, "deleted": True})
        ),
        abort_sandbox_deletion=AsyncMock(return_value={**receipt, "aborted": True}),
    )
    archives = SimpleNamespace(
        available=True,
        key_for=lambda *_: KEY,
        prepare_upload=AsyncMock(
            return_value=ArchiveUpload("https://s3.invalid/signed-capability", KEY, {})
        ),
        verify_archive=AsyncMock(
            side_effect=event(
                "verify", ArchiveReceipt(URI, "test-archive-bucket", KEY, SHA256, 1024, "version")
            )
        ),
    )
    store = SimpleNamespace(
        sandbox=port,
        host_pool=SimpleNamespace(get_host=AsyncMock(return_value={"host": "host-one"})),
    )
    monkeypatch.setattr(worker, "sandbox_manager_for_host", lambda _: sandbox_manager)
    return store, sandbox_manager, archives, events


async def run(store, archives):
    return await worker.delete_one_archived_sandbox(
        store,
        archive_factory=lambda: archives,  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_discard_removes_the_container_without_an_archive(monkeypatch):
    store, sandbox_manager, archives, events = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    sandbox_manager.discard_sandbox_deletion = AsyncMock(
        return_value={
            "request_id": "request",
            "container_id": "container-one",
            "deleted": True,
        }
    )
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=True)
    assert await run(store, archives)
    assert events == []
    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.upload_sandbox_deletion_archive.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_deletion.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_discard.assert_awaited_once()
    sandbox_manager.discard_sandbox_deletion.assert_awaited_once_with(
        "origin",
        {
            "request_id": "request",
            "generation": 4,
            "expected_container_id": "container-one",
            "sandbox_id": "group",
        },
    )


@pytest.mark.asyncio
async def test_attempt_stage_telemetry_failure_does_not_block_deletion(monkeypatch, caplog):
    store, sandbox_manager, archives, events = setup(monkeypatch)
    store.sandbox.update_admin_sandbox_deletion_attempt_stage = AsyncMock(
        side_effect=RuntimeError("ledger unavailable")
    )

    assert await run(store, archives)

    assert "delete" in events
    store.sandbox.complete_admin_sandbox_deletion.assert_awaited_once()
    store.sandbox.fail_admin_sandbox_deletion.assert_not_awaited()
    assert any(
        record.message == "Sandbox deletion attempt telemetry update failed"
        for record in caplog.records
    )
    sandbox_manager.commit_sandbox_deletion.assert_awaited_once()


@pytest.mark.asyncio
async def test_discard_on_a_reclaimed_machine_finishes_without_its_host(monkeypatch):
    """The container went with the machine; its host would never answer the request."""
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    store.host_pool.get_host.return_value = {"host": "host-one", "compute_node_id": "node-one"}
    sandbox_manager.discard_sandbox_deletion = AsyncMock()
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=True)

    assert await run(store, archives)

    store.sandbox.complete_admin_sandbox_discard.assert_awaited_once_with(
        sandbox_id="group",
        request_id="request",
        claim_token="claim-token",
        generation=4,
        container_id="container-one",
        attempt_correlation_id="00000000-0000-0000-0000-000000000001",
        reclaimed_quiet_seconds=600,
    )
    sandbox_manager.discard_sandbox_deletion.assert_not_awaited()
    store.sandbox.fail_admin_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_on_a_provider_machine_still_asks_its_host_until_it_is_reclaimed(
    monkeypatch,
):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    store.host_pool.get_host.return_value = {"host": "host-one", "compute_node_id": "node-one"}
    sandbox_manager.discard_sandbox_deletion = AsyncMock(
        return_value={"request_id": "request", "container_id": "container-one", "deleted": True}
    )
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(
        side_effect=lambda **kwargs: "reclaimed_quiet_seconds" not in kwargs
    )

    assert await run(store, archives)

    sandbox_manager.discard_sandbox_deletion.assert_awaited_once()
    calls = store.sandbox.complete_admin_sandbox_discard.await_args_list
    assert [call.kwargs.get("reclaimed_quiet_seconds") for call in calls] == [600, None]
    assert all(
        call.kwargs["attempt_correlation_id"] == "00000000-0000-0000-0000-000000000001"
        for call in calls
    )
    store.sandbox.fail_admin_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_reclaimed_recovery_runs_before_claiming_external_attempt(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    store.sandbox.recover_reclaimed_admin_sandbox_discard.return_value = True
    sandbox_manager.discard_sandbox_deletion = AsyncMock()

    assert await run(store, archives)

    store.sandbox.recover_reclaimed_admin_sandbox_discard.assert_awaited_once_with(
        reclaimed_quiet_seconds=600
    )
    store.sandbox.claim_due_admin_sandbox_deletion.assert_not_awaited()
    store.host_pool.get_host.assert_not_awaited()
    sandbox_manager.discard_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_discard_retries_without_aborting_or_uploading(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    sandbox_manager.discard_sandbox_deletion = AsyncMock(
        side_effect=RuntimeError("lost acknowledgement")
    )
    assert await run(store, archives)
    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is True
    assert "without an archive" in failure["error"]
    assert "lost acknowledgement" not in failure["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "same credential"),
        (403, "same credential"),
        (404, "container inventory and sandbox manager version"),
        (409, "exact container is stopped and idle"),
        (410, "reviewed container on that host"),
        (422, "sandbox manager is up to date"),
        (429, "sandbox manager is busy"),
        (503, "Check its sandbox manager logs"),
    ],
)
async def test_discard_reports_safe_sandbox_manager_status_without_exposing_details(
    monkeypatch, caplog, status, reason
):
    store, broker, archives, _ = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    broker.discard_sandbox_deletion = AsyncMock(
        side_effect=SandboxManagerError(
            status, "PRIVATE credential at https://private.invalid/?token=PRIVATE"
        )
    )
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=True)
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert reason in failure["error"]
    assert failure["retain_pending"] is True
    assert failure["request_id"] == "request"
    assert failure["container_id"] == "container-one"
    store.sandbox.complete_admin_sandbox_discard.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    record = caplog.records[-1]
    assert record.sandbox_manager_status == status
    assert record.request_phase == "discard"
    output = failure["error"] + JsonLogFormatter().format(record)
    assert "PRIVATE" not in output
    assert "private.invalid" not in output
    assert "claim-token" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_error", "reason"),
    [
        (httpx.ReadTimeout, "timed out before confirming removal"),
        (httpx.ConnectTimeout, "timed out before confirming removal"),
        (httpx.ConnectError, "could not be reached"),
    ],
)
async def test_discard_distinguishes_timeout_from_connection_failure(
    monkeypatch, caplog, transport_error, reason
):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    failure_cause = SandboxManagerError(503, "PRIVATE broker credential")
    failure_cause.__cause__ = transport_error("https://private.invalid/?token=PRIVATE")
    broker.discard_sandbox_deletion = AsyncMock(side_effect=failure_cause)
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert reason in failure["error"]
    assert failure["retain_pending"] is True
    output = failure["error"] + JsonLogFormatter().format(caplog.records[-1])
    assert "PRIVATE" not in output
    assert "private.invalid" not in output


_SILENT = datetime.now(UTC) - timedelta(hours=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "waits_for"),
    [
        (
            {
                "host": "host-one",
                "enrolled": True,
                "status": "offline",
                "last_heartbeat_at": _SILENT,
            },
            "The owning host is offline, and only it can remove the container: "
            "deletion finishes when the host reconnects.",
        ),
        (
            {
                "host": "host-one",
                "enrolled": True,
                "status": "offline",
                "last_heartbeat_at": _SILENT,
                "compute_node_id": "node-one",
            },
            "The owning host is offline, and only it can remove the container: "
            "deletion finishes when the host reconnects, "
            "or without it once its provider confirms the machine is gone.",
        ),
        (
            {
                "host": "host-one",
                "enrolled": False,
                "status": "disabled",
                "last_heartbeat_at": _SILENT,
            },
            "The owning host was removed from the pool, and only it can remove the container: "
            "deletion finishes if that host is enrolled again.",
        ),
        (
            # Still reporting: the sandbox manager's own connection is the problem.
            {
                "host": "host-one",
                "enrolled": True,
                "status": "online",
                "last_heartbeat_at": datetime.now(UTC) + timedelta(hours=1),
                "compute_node_id": "node-one",
            },
            "The owning host's sandbox manager could not be reached. Check its connectivity.",
        ),
    ],
)
async def test_discard_says_which_host_it_waits_for_and_what_ends_the_wait(
    monkeypatch, host, waits_for
):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    store.host_pool.get_host.return_value = host
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=False)
    failure_cause = SandboxManagerError(503, "PRIVATE broker credential")
    failure_cause.__cause__ = httpx.ConnectError("https://private.invalid/?token=PRIVATE")
    broker.discard_sandbox_deletion = AsyncMock(side_effect=failure_cause)

    assert await run(store, archives)

    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is True
    assert failure["error"] == (
        f"Container removal without an archive could not be confirmed. {waits_for} "
        "Deletion will be retried."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "credential", "reason"),
    [
        (None, "PRIVATE", "host is no longer registered"),
        ({"host": "host-one"}, "PRIVATE", "has not advertised a sandbox manager URL"),
        (
            {"host": "host-one", "sandbox_manager_url": "ftp://PRIVATE@private.invalid"},
            "PRIVATE",
            "URL is invalid or refused",
        ),
        (
            {"host": "host-one", "sandbox_manager_url": "https://private.invalid"},
            "",
            "control plane has no sandbox manager credential",
        ),
    ],
)
async def test_discard_reports_safe_sandbox_manager_configuration_errors(
    monkeypatch, caplog, host, credential, reason
):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    store.host_pool.get_host.return_value = host
    monkeypatch.setattr(worker, "sandbox_manager_for_host", lambda _: None)
    monkeypatch.setattr(
        sandbox_manager_routing,
        "setting_env",
        lambda name: (
            credential if name == sandbox_manager_routing.ENV_SANDBOX_MANAGER_TOKEN else None
        ),
    )
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert reason in failure["error"]
    assert failure["retain_pending"] is True
    assert failure["failure_code"] == "sandbox_manager_resolution_rejected"
    assert failure["retry_class"] == "operator_action"
    record = caplog.records[-1]
    assert record.request_phase == "resolve_sandbox_manager"
    assert record.sandbox_manager_status is None
    output = failure["error"] + JsonLogFormatter().format(record)
    assert "PRIVATE" not in output
    assert "private.invalid" not in output
    broker.prepare_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_change",
    [{"request_id": "another-request"}, {"container_id": "another-container"}, {"deleted": False}],
)
async def test_discard_receipt_failure_keeps_exact_request_pending(
    monkeypatch, caplog, receipt_change
):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    broker.discard_sandbox_deletion = AsyncMock(
        return_value={
            "request_id": "request",
            "container_id": "container-one",
            "deleted": True,
            **receipt_change,
        }
    )
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=True)
    assert await run(store, archives)
    store.sandbox.complete_admin_sandbox_discard.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is True
    assert failure["request_id"] == "request"
    assert failure["container_id"] == "container-one"
    assert caplog.records[-1].request_phase == "validate_discard_receipt"


@pytest.mark.asyncio
async def test_archive_broker_failures_keep_generic_private_diagnostics(monkeypatch, caplog):
    store, broker, archives, _ = setup(monkeypatch)
    broker.upload_sandbox_deletion_archive.side_effect = SandboxManagerError(
        401, "PRIVATE https://private.invalid/?token=PRIVATE"
    )
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["error"] == (
        "S3 archive or sandbox deletion could not complete. "
        "The container has not been deleted. Refresh details before retrying."
    )
    record = caplog.records[-1]
    assert record.request_phase == "archive_delete"
    assert record.sandbox_manager_status == 401
    output = failure["error"] + JsonLogFormatter().format(record)
    assert "PRIVATE" not in output
    assert "private.invalid" not in output


@pytest.mark.asyncio
async def test_deletion_waits_for_verified_s3_object_and_committed_receipt(monkeypatch):
    store, sandbox_manager, archives, events = setup(monkeypatch)
    assert await run(store, archives)
    assert events == ["status", "prepare", "upload", "verify", "record", "delete", "complete"]
    store.sandbox.record_admin_sandbox_archive.assert_awaited_once_with(
        sandbox_id="group",
        request_id="request",
        claim_token="claim-token",
        generation=4,
        container_id="container-one",
        archive_key=URI,
        sha256=SHA256,
        size_bytes=1024,
        archive_version_id="version",
    )
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retain_pending", "outcome", "failure_code", "retry_class"),
    [
        (410, False, "failed", "sandbox_manager_precondition", "operator_action"),
        (503, True, "retrying", "sandbox_manager_unavailable", "transient"),
        (429, True, "retrying", "sandbox_manager_unavailable", "transient"),
        (422, False, "failed", "sandbox_manager_rejected", "permanent"),
        (403, False, "failed", "sandbox_manager_precondition", "operator_action"),
    ],
)
async def test_preflight_failure_never_starts_archive_preparation(
    monkeypatch, status_code, retain_pending, outcome, failure_code, retry_class
):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    sandbox_manager.shared_chat_sandbox_status.side_effect = SandboxManagerError(
        status_code, "private host response"
    )

    assert await run(store, archives)

    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is retain_pending
    attempt = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert attempt["failure_code"] == failure_code
    assert attempt["sandbox_manager_status"] == status_code
    assert attempt["retry_class"] == retry_class
    assert attempt["retain_pending"] is retain_pending
    assert attempt["cleanup_outcome"] == "not_required"
    stages = [
        call.kwargs["stage"]
        for call in store.sandbox.update_admin_sandbox_deletion_attempt_stage.await_args_list
    ]
    assert stages[-1] == "stop_verification"
    assert "private host response" not in failure["error"]


@pytest.mark.asyncio
async def test_a_dead_container_is_archived_and_deleted(monkeypatch):
    """Force deletion accepts a dead container, and so does the host's export."""
    store, sandbox_manager, archives, events = setup(monkeypatch)
    status = await sandbox_manager.shared_chat_sandbox_status("origin", timeout=10)
    events.clear()
    sandbox_manager.shared_chat_sandbox_status.side_effect = None
    sandbox_manager.shared_chat_sandbox_status.return_value = {**status, "docker_status": "dead"}

    assert await run(store, archives)

    assert events == ["prepare", "upload", "verify", "record", "delete", "complete"]
    store.sandbox.fail_admin_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("docker_status", ["running", "paused", "restarting", None])
async def test_a_container_neither_stopped_nor_dead_is_never_prepared(monkeypatch, docker_status):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    status = await sandbox_manager.shared_chat_sandbox_status("origin", timeout=10)
    sandbox_manager.shared_chat_sandbox_status.side_effect = None
    sandbox_manager.shared_chat_sandbox_status.return_value = {
        **status,
        "docker_status": docker_status,
    }

    assert await run(store, archives)

    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["failure_code"] == "stop_verification_rejected"


@pytest.mark.asyncio
async def test_busy_host_preflight_never_starts_archive_preparation(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    sandbox_manager.shared_chat_sandbox_status.side_effect = None
    sandbox_manager.shared_chat_sandbox_status.return_value = {
        "thread_id": "origin",
        "origin_thread_id": "origin",
        "sandbox_id": "group",
        "generation": 4,
        "container_id": "container-one",
        "running": False,
        "docker_status": "stopped",
        "terminal_count": 0,
        "operation_count": 1,
        "busy": False,
        "group_busy": False,
        "snapshotting": False,
    }

    assert await run(store, archives)

    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is False
    attempt = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert attempt["failure_code"] == "stop_verification_rejected"
    assert attempt["retry_class"] == "operator_action"
    assert attempt["retain_pending"] is False


@pytest.mark.asyncio
async def test_attempt_ledger_records_each_archive_stage(monkeypatch):
    store, _sandbox_manager, archives, _events = setup(monkeypatch)
    assert await run(store, archives)
    stages = [
        call.kwargs["stage"]
        for call in store.sandbox.update_admin_sandbox_deletion_attempt_stage.await_args_list
    ]
    assert stages == [
        "sandbox_manager_resolution",
        "stop_verification",
        "archive_prepare",
        "upload_authorization",
        "archive_upload",
        "archive_verification",
        "durable_receipt",
        "container_removal",
        "database_finalization",
    ]
    store.sandbox.complete_admin_sandbox_deletion.assert_awaited_once_with(
        sandbox_id="group",
        request_id="request",
        claim_token="claim-token",
        generation=4,
        container_id="container-one",
        attempt_correlation_id="00000000-0000-0000-0000-000000000001",
    )


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_gate_deletion(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    store.sandbox.update_admin_sandbox_deletion_attempt_stage.side_effect = RuntimeError(
        "telemetry unavailable"
    )
    assert await run(store, archives)
    sandbox_manager.commit_sandbox_deletion.assert_awaited_once()
    store.sandbox.complete_admin_sandbox_deletion.assert_awaited_once()


@pytest.mark.asyncio
async def test_stalled_telemetry_is_bounded_and_does_not_gate_deletion(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    stalled_stage = False

    async def update_stage(**_kwargs):
        nonlocal stalled_stage
        if not stalled_stage:
            stalled_stage = True
            await asyncio.get_running_loop().create_future()
        return True

    store.sandbox.update_admin_sandbox_deletion_attempt_stage.side_effect = update_stage

    assert await asyncio.wait_for(run(store, archives), timeout=1.5)
    sandbox_manager.commit_sandbox_deletion.assert_awaited_once()
    store.sandbox.complete_admin_sandbox_deletion.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_diagnostics_do_not_change_retry_or_cleanup_policy(monkeypatch, caplog):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = SandboxManagerError(
        503, "private https://signed.invalid/token"
    )
    assert await run(store, archives)
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is False
    result = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert result["failure_code"] == "sandbox_manager_unavailable"
    assert result["sandbox_manager_status"] == 503
    assert result["retry_class"] == "transient"
    assert result["cleanup_outcome"] == "acknowledged"
    assert result["attempt_correlation_id"] == "00000000-0000-0000-0000-000000000001"
    assert "signed.invalid" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_failure_state_write_does_not_claim_lease_expired(monkeypatch):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = SandboxManagerError(
        503, "sandbox manager unavailable"
    )
    store.sandbox.fail_admin_sandbox_deletion.side_effect = RuntimeError("database unavailable")

    assert await run(store, archives)

    store.sandbox.fail_admin_sandbox_deletion.assert_awaited_once()
    store.sandbox.finish_admin_sandbox_deletion_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_log_uses_database_retry_decision(monkeypatch, caplog):
    store, sandbox_manager, archives, _events = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = SandboxManagerError(
        503, "sandbox manager unavailable"
    )
    store.sandbox.fail_admin_sandbox_deletion.return_value = DeletionFailureDecision(
        recorded=True,
        retry_scheduled=False,
        retry_at=None,
        terminal_reason="retry_budget_exhausted",
    )

    assert await run(store, archives)

    warning = next(
        record
        for record in caplog.records
        if record.getMessage() == "Sandbox archive/delete request did not complete"
    )
    assert warning.retry_scheduled is False
    assert warning.retry_at is None
    assert warning.terminal_reason == "retry_budget_exhausted"
    assert warning.failure_transition_recorded is True


@pytest.mark.asyncio
async def test_upload_failure_preserves_container_and_releases_only_accepted_claim(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = RuntimeError("private signed URL")
    assert await run(store, archives)
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_awaited_once()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is False
    assert "private signed URL" not in failure["error"]


@pytest.mark.asyncio
async def test_expired_claim_cannot_abort_a_newer_workers_host_barrier(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = RuntimeError("upload failed")
    store.sandbox.renew_admin_sandbox_deletion_claim.return_value = False
    assert await run(store, archives)
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_abort_keeps_request_retryable_and_admission_closed(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    sandbox_manager.upload_sandbox_deletion_archive.side_effect = RuntimeError("upload failed")
    sandbox_manager.abort_sandbox_deletion.side_effect = RuntimeError("host unreachable")
    assert await run(store, archives)
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True
    renewal = store.sandbox.renew_admin_sandbox_deletion_claim.call_args.kwargs
    assert renewal["for_cleanup"] is True
    assert renewal["cleanup_archive_key"] == KEY


@pytest.mark.asyncio
async def test_uncertain_abort_retry_only_cleans_up_original_request(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_cleanup_required"] = True
    claim["deletion_cleanup_archive_key"] = "original-prefix/group/request/archive.tar"
    archives.available = False
    sandbox_manager.abort_sandbox_deletion.return_value = {
        "request_id": "request",
        "container_id": "container-one",
        "archive_key": claim["deletion_cleanup_archive_key"],
        "aborted": True,
    }
    assert await run(store, archives)
    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.upload_sandbox_deletion_archive.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_awaited_once()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is False


@pytest.mark.asyncio
async def test_wrong_container_receipt_never_uploads_or_deletes(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    sandbox_manager.prepare_sandbox_deletion.side_effect = None
    sandbox_manager.prepare_sandbox_deletion.return_value = {
        "request_id": "request",
        "container_id": "another-container",
        "archive_key": KEY,
        "sha256": SHA256,
        "size_bytes": 1024,
    }
    assert await run(store, archives)
    archives.prepare_upload.assert_not_awaited()
    sandbox_manager.upload_sandbox_deletion_archive.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_s3_verification_failure_never_permits_deletion(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    archives.verify_archive.side_effect = RuntimeError("wrong checksum")
    assert await run(store, archives)
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_database_checkpoint_keeps_admission_and_host_barrier_closed(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    store.sandbox.record_admin_sandbox_archive.side_effect = RuntimeError("connection lost")
    assert await run(store, archives)
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True


@pytest.mark.asyncio
async def test_durable_archive_retry_checks_original_object_without_overwriting_it(monkeypatch):
    store, sandbox_manager, archives, events = setup(monkeypatch, state="archived")
    assert await run(store, archives)
    assert events == ["verify", "delete", "complete"]
    archives.verify_archive.assert_awaited_once_with(URI, SHA256, 1024)
    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.upload_sandbox_deletion_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_delete_response_retains_archive_and_retries_without_reopening(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch, state="archived")
    sandbox_manager.commit_sandbox_deletion.side_effect = RuntimeError("lost acknowledgement")
    assert await run(store, archives)
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_deletion.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is True
    assert "verified archive is retained" in failure["error"]


@pytest.mark.asyncio
async def test_unconfigured_s3_refuses_all_host_actions(monkeypatch):
    store, sandbox_manager, archives, _ = setup(monkeypatch)
    archives.available = False
    assert await run(store, archives)
    sandbox_manager.prepare_sandbox_deletion.assert_not_awaited()
    sandbox_manager.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_preserves_checkpoint_for_restart(monkeypatch):
    import asyncio

    store, sandbox_manager, archives, _ = setup(monkeypatch, state="archived")
    sandbox_manager.commit_sandbox_deletion.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run(store, archives)
    sandbox_manager.abort_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True


@pytest.mark.asyncio
async def test_requests_that_stay_due_do_not_stop_the_deleted_session_sweep(monkeypatch):
    import asyncio

    swept_again = asyncio.Event()
    sweeps = 0

    async def sweep(_store):
        nonlocal sweeps
        sweeps += 1
        if sweeps > 1:
            swept_again.set()
        return 0

    async def always_due(_store):
        # Failing requests retried a minute later can leave one due at every pass.
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(worker, "retire_deleted_session_sandboxes", sweep)
    monkeypatch.setattr(worker, "delete_one_archived_sandbox", always_due)
    task = asyncio.create_task(
        worker.coordinate_sandbox_deletions(
            SimpleNamespace(),  # type: ignore[arg-type]
            interval_seconds=0.01,
            deleted_sessions_interval_seconds=0.01,
        )
    )
    try:
        await asyncio.wait_for(swept_again.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
