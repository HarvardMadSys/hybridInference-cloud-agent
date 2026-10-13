"""Verify archive-first deletion ordering and failure/retry boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import broker_routing
from cloud_agent.logging_utils import JsonLogFormatter
from cloud_agent.sandbox_archive import ArchiveReceipt, ArchiveUpload
from cloud_agent.workers import sandbox_deletion as worker
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

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
        record_admin_sandbox_archive=AsyncMock(side_effect=event("record", True)),
        complete_admin_sandbox_deletion=AsyncMock(side_effect=event("complete", True)),
        fail_admin_sandbox_deletion=AsyncMock(return_value=True),
        renew_admin_sandbox_deletion_claim=AsyncMock(return_value=True),
    )
    broker = SimpleNamespace(
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
    monkeypatch.setattr(worker, "broker_for_host", lambda _: broker)
    return store, broker, archives, events


async def run(store, archives):
    return await worker.delete_one_archived_sandbox(
        store,
        archive_factory=lambda: archives,  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_discard_removes_the_container_without_an_archive(monkeypatch):
    store, broker, archives, events = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    broker.discard_sandbox_deletion = AsyncMock(
        return_value={
            "request_id": "request",
            "container_id": "container-one",
            "deleted": True,
        }
    )
    store.sandbox.complete_admin_sandbox_discard = AsyncMock(return_value=True)
    assert await run(store, archives)
    assert events == []
    broker.prepare_sandbox_deletion.assert_not_awaited()
    broker.upload_sandbox_deletion_archive.assert_not_awaited()
    broker.commit_sandbox_deletion.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_not_awaited()
    archives.prepare_upload.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_deletion.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_discard.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfirmed_discard_retries_without_aborting_or_uploading(monkeypatch):
    store, broker, archives, _events = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    broker.discard_sandbox_deletion = AsyncMock(side_effect=RuntimeError("lost acknowledgement"))
    assert await run(store, archives)
    broker.prepare_sandbox_deletion.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_not_awaited()
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
        (404, "container inventory and workspace broker version"),
        (409, "exact container is stopped and idle"),
        (410, "reviewed container on that host"),
        (422, "workspace broker is up to date"),
        (429, "workspace broker is busy"),
        (503, "Check its broker logs"),
    ],
)
async def test_discard_reports_safe_broker_status_without_exposing_details(
    monkeypatch, caplog, status, reason
):
    store, broker, archives, _ = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_discard"] = True
    broker.discard_sandbox_deletion = AsyncMock(
        side_effect=WorkspaceBrokerError(
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
    assert record.broker_status == status
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
    failure_cause = WorkspaceBrokerError(503, "PRIVATE broker credential")
    failure_cause.__cause__ = transport_error("https://private.invalid/?token=PRIVATE")
    broker.discard_sandbox_deletion = AsyncMock(side_effect=failure_cause)
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert reason in failure["error"]
    assert failure["retain_pending"] is True
    output = failure["error"] + JsonLogFormatter().format(caplog.records[-1])
    assert "PRIVATE" not in output
    assert "private.invalid" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "credential", "reason"),
    [
        (None, "PRIVATE", "host is no longer registered"),
        ({"host": "host-one"}, "PRIVATE", "has not advertised a workspace broker URL"),
        (
            {"host": "host-one", "broker_url": "ftp://PRIVATE@private.invalid"},
            "PRIVATE",
            "URL is invalid or refused",
        ),
        (
            {"host": "host-one", "broker_url": "https://private.invalid"},
            "",
            "control plane has no workspace broker credential",
        ),
    ],
)
async def test_discard_reports_safe_broker_configuration_errors(
    monkeypatch, caplog, host, credential, reason
):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.claim_due_admin_sandbox_deletion.return_value["deletion_discard"] = True
    store.host_pool.get_host.return_value = host
    monkeypatch.setattr(worker, "broker_for_host", lambda _: None)
    monkeypatch.setattr(
        broker_routing,
        "setting_env",
        lambda name: credential if name == broker_routing.ENV_BROKER_TOKEN else None,
    )
    assert await run(store, archives)
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert reason in failure["error"]
    assert failure["retain_pending"] is True
    record = caplog.records[-1]
    assert record.request_phase == "resolve_broker"
    assert record.broker_status is None
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
    broker.upload_sandbox_deletion_archive.side_effect = WorkspaceBrokerError(
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
    assert record.broker_status == 401
    output = failure["error"] + JsonLogFormatter().format(record)
    assert "PRIVATE" not in output
    assert "private.invalid" not in output


@pytest.mark.asyncio
async def test_deletion_waits_for_verified_s3_object_and_committed_receipt(monkeypatch):
    store, broker, archives, events = setup(monkeypatch)
    assert await run(store, archives)
    assert events == ["prepare", "upload", "verify", "record", "delete", "complete"]
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
    broker.abort_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_failure_preserves_container_and_releases_only_accepted_claim(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    broker.upload_sandbox_deletion_archive.side_effect = RuntimeError("private signed URL")
    assert await run(store, archives)
    broker.commit_sandbox_deletion.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_awaited_once()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is False
    assert "private signed URL" not in failure["error"]


@pytest.mark.asyncio
async def test_expired_claim_cannot_abort_a_newer_workers_host_barrier(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    broker.upload_sandbox_deletion_archive.side_effect = RuntimeError("upload failed")
    store.sandbox.renew_admin_sandbox_deletion_claim.return_value = False
    assert await run(store, archives)
    broker.abort_sandbox_deletion.assert_not_awaited()
    broker.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_abort_keeps_request_retryable_and_admission_closed(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    broker.upload_sandbox_deletion_archive.side_effect = RuntimeError("upload failed")
    broker.abort_sandbox_deletion.side_effect = RuntimeError("host unreachable")
    assert await run(store, archives)
    broker.commit_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True
    renewal = store.sandbox.renew_admin_sandbox_deletion_claim.call_args.kwargs
    assert renewal["for_cleanup"] is True
    assert renewal["cleanup_archive_key"] == KEY


@pytest.mark.asyncio
async def test_uncertain_abort_retry_only_cleans_up_original_request(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    claim = store.sandbox.claim_due_admin_sandbox_deletion.return_value
    claim["deletion_cleanup_required"] = True
    claim["deletion_cleanup_archive_key"] = "original-prefix/group/request/archive.tar"
    archives.available = False
    broker.abort_sandbox_deletion.return_value = {
        "request_id": "request",
        "container_id": "container-one",
        "archive_key": claim["deletion_cleanup_archive_key"],
        "aborted": True,
    }
    assert await run(store, archives)
    broker.prepare_sandbox_deletion.assert_not_awaited()
    broker.upload_sandbox_deletion_archive.assert_not_awaited()
    broker.commit_sandbox_deletion.assert_not_awaited()
    archives.verify_archive.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_awaited_once()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is False


@pytest.mark.asyncio
async def test_wrong_container_receipt_never_uploads_or_deletes(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    broker.prepare_sandbox_deletion.side_effect = None
    broker.prepare_sandbox_deletion.return_value = {
        "request_id": "request",
        "container_id": "another-container",
        "archive_key": KEY,
        "sha256": SHA256,
        "size_bytes": 1024,
    }
    assert await run(store, archives)
    archives.prepare_upload.assert_not_awaited()
    broker.upload_sandbox_deletion_archive.assert_not_awaited()
    broker.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_s3_verification_failure_never_permits_deletion(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    archives.verify_archive.side_effect = RuntimeError("wrong checksum")
    assert await run(store, archives)
    broker.commit_sandbox_deletion.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_database_checkpoint_keeps_admission_and_host_barrier_closed(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    store.sandbox.record_admin_sandbox_archive.side_effect = RuntimeError("connection lost")
    assert await run(store, archives)
    broker.commit_sandbox_deletion.assert_not_awaited()
    broker.abort_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True


@pytest.mark.asyncio
async def test_durable_archive_retry_checks_original_object_without_overwriting_it(monkeypatch):
    store, broker, archives, events = setup(monkeypatch, state="archived")
    assert await run(store, archives)
    assert events == ["verify", "delete", "complete"]
    archives.verify_archive.assert_awaited_once_with(URI, SHA256, 1024)
    broker.prepare_sandbox_deletion.assert_not_awaited()
    broker.upload_sandbox_deletion_archive.assert_not_awaited()
    store.sandbox.record_admin_sandbox_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_delete_response_retains_archive_and_retries_without_reopening(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch, state="archived")
    broker.commit_sandbox_deletion.side_effect = RuntimeError("lost acknowledgement")
    assert await run(store, archives)
    broker.abort_sandbox_deletion.assert_not_awaited()
    store.sandbox.complete_admin_sandbox_deletion.assert_not_awaited()
    failure = store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs
    assert failure["retain_pending"] is True
    assert "verified archive is retained" in failure["error"]


@pytest.mark.asyncio
async def test_unconfigured_s3_refuses_all_host_actions(monkeypatch):
    store, broker, archives, _ = setup(monkeypatch)
    archives.available = False
    assert await run(store, archives)
    broker.prepare_sandbox_deletion.assert_not_awaited()
    broker.commit_sandbox_deletion.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_preserves_checkpoint_for_restart(monkeypatch):
    import asyncio

    store, broker, archives, _ = setup(monkeypatch, state="archived")
    broker.commit_sandbox_deletion.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run(store, archives)
    broker.abort_sandbox_deletion.assert_not_awaited()
    assert store.sandbox.fail_admin_sandbox_deletion.call_args.kwargs["retain_pending"] is True
