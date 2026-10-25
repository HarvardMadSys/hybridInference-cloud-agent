"""What a Host logs when it refuses to discard a stopped sandbox.

Each refusal is produced by the real supervisor lifecycle and the real
deletion receipts; only Docker is a stand-in. The record is checked as both
log formats render it: the text format keeps only the message, so the
diagnostics are in it too, and the JSON format carries them as fields.
"""

from __future__ import annotations

import errno
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.unit.test_sandbox_deletion_backup import _Manager as _ArchivingDocker
from tests.unit.test_shared_session_supervisor import _Manager, _members, _spec

from cloud_agent_host import sandbox_deletion_diagnostics
from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.logging_utils import _FORMAT
from cloud_agent_host.sandbox import ProcessBackend, SandboxError
from cloud_agent_host.sandbox_deletion_backup import ArchiveIdentity, SandboxDeletionBackupService
from cloud_agent_host.sandbox_deletion_diagnostics import KNOWN_REASONS
from cloud_agent_host.sandbox_manager import create_app
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)
from cloud_agent_shared.logging_utils import JsonLogFormatter

_CONTAINER = "0f1e2d3c4b5a" + "0" * 52
_REQUEST = "superseded-synthetic-request"
_EVENT = "sandbox_discard_refused"
_HOST_SOURCE = Path(__file__).resolve().parents[2] / "apps/backend/cloud_agent_host"


class _Docker(_Manager):
    """The supervisor tests' Docker, with the exact-container deletion checks."""

    def __init__(self) -> None:
        super().__init__()
        self.removals = 0
        self.refusal: str | None = None

    def require_stopped_archive(self, spec: ChatSandboxSpec, *, container_id: str):
        current = self.items[spec.container_name]
        if current.container_id != container_id:
            raise SessionLeaseLost("chat sandbox container identity changed")
        if self.refusal is not None:
            raise SandboxError(self.refusal)
        if current.running:
            raise SandboxError("sandbox archive requires a stopped or dead container")
        return current

    def remove_stopped_archived(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        self.require_stopped_archive(spec, container_id=container_id)
        self.removals += 1
        del self.items[spec.container_name]


def _running_group(**hooks: Any) -> tuple[_Docker, SharedChatSessionSupervisor]:
    """One running group of two members on its bridge, with a Docker-length id."""
    docker = _Docker()
    spec = _spec()
    docker.items[spec.container_name] = replace(
        docker._inspection(spec, running=True), container_id=_CONTAINER
    )
    supervisor = SharedChatSessionSupervisor(docker, **hooks)
    supervisor.register_manifest(spec, _members(), expected_container_id=_CONTAINER)
    return docker, supervisor


def _identity(*, generation: int = 1, thread_id: str = "athr_source") -> ArchiveIdentity:
    return ArchiveIdentity(thread_id, _REQUEST, generation, _CONTAINER, "")


def _refusal(caplog) -> logging.LogRecord:
    (record,) = [item for item in caplog.records if getattr(item, "event", None) == _EVENT]
    return record


def _as_text(record: logging.LogRecord) -> str:
    return logging.Formatter(_FORMAT).format(record)


def _as_json(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonLogFormatter().format(record))


def test_a_pending_final_archive_is_named_in_both_log_formats(tmp_path, caplog):
    def fail_upload(*_args) -> None:
        raise SandboxError("synthetic upload failure")

    docker, supervisor = _running_group(
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=fail_upload,
    )
    with pytest.raises(SandboxError, match="synthetic upload failure"):
        supervisor.stop_if_idle("athr_source", generation=1)
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)

    with pytest.raises(SessionBusy, match="lifecycle archive must finish"):
        service.discard(_identity(), sandbox_id="asbx_shared")

    assert docker.removals == 0
    record = _refusal(caplog)
    assert record.levelno == logging.WARNING
    summary = (
        'reason="sandbox lifecycle archive must finish before deletion" error=SessionBusy '
        "status=409 thread_id=athr_source sandbox_id=asbx_shared "
        f"request_digest={sandbox_deletion_diagnostics._digest(_REQUEST)} generation=1 "
        "container=0f1e2d3c4b5a receipts=none member_known=true group_generation=1 "
        "container_matches=true container_running=false bridge_attached=true "
        "archive_request=none lifecycle_check=false barriers=stop_archive_pending "
        "operations=0 terminals=0 members=2 leased_members=0 active_turns=0 "
        "state=after_refusal"
    )
    assert record.getMessage() == f"sandbox discard refused: {summary}"
    assert _as_text(record).endswith(
        " - cloud_agent_host.sandbox_deletion_diagnostics - WARNING - "
        f"sandbox discard refused: {summary}"
    )
    rendered = _as_json(record)
    assert rendered["event"] == _EVENT
    assert rendered["reason"] == "sandbox lifecycle archive must finish before deletion"
    assert rendered["status"] == 409
    assert rendered["barriers"] == ["stop_archive_pending"]
    assert rendered["bridge_attached"] is True
    assert rendered["receipts"] == []
    assert "synthetic upload failure" not in json.dumps(rendered)


def test_an_open_workspace_operation_is_counted(tmp_path, caplog):
    docker, supervisor = _running_group()
    token, _ = supervisor.operation_opened("athr_source")
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)

    with pytest.raises(SessionBusy, match="requires an idle group"):
        service.discard(_identity(), sandbox_id="asbx_shared")

    rendered = _as_json(_refusal(caplog))
    assert rendered["reason"] == "sandbox archive requires an idle group"
    assert (rendered["operations"], rendered["container_running"]) == (1, True)
    assert rendered["barriers"] == []
    supervisor.operation_closed("athr_source", token)
    assert docker.removals == 0


def test_a_stale_generation_names_both_generations(tmp_path, caplog):
    docker, supervisor = _running_group()
    supervisor.stop_if_idle("athr_source", generation=1)
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)

    with pytest.raises(SessionLeaseLost, match="generation is stale"):
        service.discard(_identity(generation=2), sandbox_id="asbx_shared")

    rendered = _as_json(_refusal(caplog))
    assert (rendered["error"], rendered["status"]) == ("SessionLeaseLost", 409)
    assert (rendered["generation"], rendered["group_generation"]) == (2, 1)
    assert rendered["barriers"] == []
    assert docker.removals == 0


def test_a_request_that_prepared_an_archive_shows_its_receipt(tmp_path, caplog):
    spec = ChatSandboxSpec(
        thread_id="athr_origin", sandbox_id="asbx_one", generation=2, runtime_id="claude-code"
    )
    docker = _ArchivingDocker(spec)
    supervisor = SharedChatSessionSupervisor(docker)
    supervisor.register_manifest(
        spec,
        [SharedMemberManifest(thread_id="athr_origin", native_session_id="n", resident_slot_id=0)],
        expected_container_id="container-1",
    )
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)
    prepared = ArchiveIdentity("athr_origin", "delete-1", 2, "container-1", "archives/one.tar")
    service.prepare(prepared)

    with pytest.raises(SessionLeaseLost, match="cannot be discarded"):
        service.discard(replace(prepared, archive_key=""), sandbox_id="asbx_one")

    rendered = _as_json(_refusal(caplog))
    assert rendered["receipts"] == ["prepare"]
    assert rendered["archive_request"] == "this"
    # Not the shape of a Docker id: left out rather than logged as given.
    assert rendered["container"] == "invalid"
    assert docker.removals == 0


def test_text_the_host_did_not_write_is_never_logged(tmp_path, caplog):
    docker, supervisor = _running_group()
    supervisor.stop_if_idle("athr_source", generation=1)
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)
    docker.refusal = "token=fake at /var/lib/synthetic-private-path"

    with pytest.raises(SandboxError, match="token=fake"):
        service.discard(_identity(), sandbox_id="asbx_shared")
    with pytest.raises(SandboxError, match="no shared sandbox membership"):
        service.discard(_identity(thread_id="athr_x\nforged=1"), sandbox_id="asbx_shared")

    unknown, forged = [item for item in caplog.records if getattr(item, "event", None) == _EVENT]
    assert (_as_json(unknown)["reason"], _as_json(unknown)["status"]) == ("unrecognized", 503)
    assert _as_json(forged)["reason"] == "chat has no shared sandbox membership"
    assert _as_json(forged)["thread_id"] == "invalid"
    assert _as_json(forged)["member_known"] is False
    for record in (unknown, forged):
        for output in (_as_text(record), JsonLogFormatter().format(record)):
            assert "token=fake" not in output
            assert "synthetic-private-path" not in output
            assert "forged" not in output
            assert "\n" not in output
    assert docker.removals == 0


def test_a_removal_logs_no_refusal(tmp_path, caplog):
    docker, supervisor = _running_group()
    supervisor.stop_if_idle("athr_source", generation=1)
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)

    assert service.discard(_identity(), sandbox_id="asbx_shared")["deleted"] is True

    assert docker.removals == 1
    assert not [item for item in caplog.records if getattr(item, "event", None) == _EVENT]


def test_a_state_read_that_fails_still_raises_the_refusal(tmp_path, caplog, monkeypatch):
    def unreadable(*_args) -> None:
        raise RuntimeError("synthetic state failure")

    _docker, supervisor = _running_group()
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)
    monkeypatch.setattr(sandbox_deletion_diagnostics, "_state", unreadable)

    # Idle, but still running: Docker's stopped-container check refuses it.
    with pytest.raises(SandboxError, match="stopped or dead container"):
        service.discard(_identity(), sandbox_id="asbx_shared")

    rendered = _as_json(_refusal(caplog))
    assert (rendered["state"], rendered["state_error"]) == ("unavailable", "other")
    assert "synthetic state failure" not in json.dumps(rendered)


@pytest.mark.asyncio
async def test_a_receipt_the_filesystem_refuses_is_logged_and_still_answered_500(tmp_path, caplog):
    docker, supervisor = _running_group()
    supervisor.stop_if_idle("athr_source", generation=1)
    # The receipt directory's place is taken by a file: making it fails.
    state_dir = tmp_path / "archives"
    state_dir.write_text("not a directory")
    app = create_app(
        token="host-secret", backend=ProcessBackend(), shared_session_supervisor=supervisor
    )
    app.state.sandbox_deletion_archive_service = SandboxDeletionBackupService(
        supervisor, state_dir=state_dir
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox-manager") as http:
        response = await http.post(
            "/shared-sandboxes/athr_source/admin-delete/discard",
            headers={"X-Agent-Workspace-Token": "host-secret"},
            json={
                "request_id": _REQUEST,
                "generation": 1,
                "expected_container_id": _CONTAINER,
                "sandbox_id": "asbx_shared",
            },
        )

    assert response.status_code == 500
    assert "FileExistsError" in response.json()["detail"]
    assert docker.removals == 0
    assert supervisor.owns("athr_source")
    record = _refusal(caplog)
    rendered = _as_json(record)
    assert (rendered["error"], rendered["status"], rendered["errno"]) == (
        "OSError",
        500,
        errno.EEXIST,
    )
    assert rendered["reason"] == "unrecognized"
    assert rendered["barriers"] == []
    assert f"status=500 errno={errno.EEXIST} thread_id=athr_source" in _as_text(record)
    assert str(tmp_path) not in _as_text(record)


def test_every_known_reason_is_a_sentence_the_host_raises():
    source = "\n".join(
        path.read_text()
        for path in _HOST_SOURCE.glob("*.py")
        if path.name != "sandbox_deletion_diagnostics.py"
    )
    missing = sorted(reason for reason in KNOWN_REASONS if f'"{reason}"' not in source)
    assert missing == []
