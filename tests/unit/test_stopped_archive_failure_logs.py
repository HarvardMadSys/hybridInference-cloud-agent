"""Which step of a clean stop failed, logged when it fails.

After Docker stops a group, the supervisor stores its final archive and then
releases the group bridge that marks the archive unfinished. Either can fail,
and both leave the group pending; these tests check that each failure is
logged, as it happens, with its own phase, in both log formats, without the
provider's or Docker's own text, and that the failure is still raised and
nothing is deleted. The supervisor and deletion receipts are real; Docker and
the archive upload are stand-ins.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.test_sandbox_discard_diagnostics import (
    _CONTAINER,
    _as_json,
    _as_text,
    _Docker,
    _identity,
)
from tests.unit.test_shared_session_supervisor import _members, _spec

from cloud_agent_host.archive_failure_diagnostics import NETWORK_RELEASE_FAILURES
from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_deletion_backup import SandboxDeletionBackupService
from cloud_agent_host.session_lifecycle import SessionBusy
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.logging_utils import JsonLogFormatter

_FINAL = "sandbox_final_archive_failed"
_RELEASE = "sandbox_final_network_release_failed"
_PRIVATE = "synthetic-private-output"
_HOST_SOURCE = Path(__file__).resolve().parents[2] / "apps/backend/cloud_agent_host"


class _ReleasingDocker(_Docker):
    """Docker whose release of a stopped group's bridge fails as scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.release_error: BaseException | None = None
        self.releases = 0

    def stop(self, spec, *, expected_container_id=None, release_networks=True):
        if release_networks and not self.items[spec.container_name].running:
            self.releases += 1
            if self.release_error is not None:
                raise self.release_error
        return super().stop(
            spec, expected_container_id=expected_container_id, release_networks=release_networks
        )


class SyntheticClientError(Exception):
    """An S3 client error: its code and status are safe, its message is not."""

    def __init__(self) -> None:
        super().__init__(f"AccessDenied for {_PRIVATE}")
        self.response = {
            "Error": {"Code": "AccessDenied", "Message": _PRIVATE},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }


def _group(archive) -> tuple[_ReleasingDocker, SharedChatSessionSupervisor]:
    docker = _ReleasingDocker()
    spec = _spec()
    docker.items[spec.container_name] = replace(
        docker._inspection(spec, running=True), container_id=_CONTAINER
    )
    supervisor = SharedChatSessionSupervisor(
        docker, stopped_archive_prepare_hook=lambda *_args: None, stopped_archive_hook=archive
    )
    supervisor.register_manifest(spec, _members(), expected_container_id=_CONTAINER)
    return docker, supervisor


def _events(caplog, event: str):
    return [record for record in caplog.records if getattr(record, "event", None) == event]


def _assert_kept(docker: _ReleasingDocker, supervisor, tmp_path) -> None:
    """The group stays fenced: its removal is refused and nothing is deleted."""
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)
    with pytest.raises(SessionBusy, match="lifecycle archive must finish"):
        service.discard(_identity(), sandbox_id="asbx_shared")
    assert docker.removals == 0
    assert docker.items[_spec().container_name].networks == ("agent-net",)


def test_a_failed_final_archive_names_its_phase_and_provider_code(tmp_path, caplog):
    error = SyntheticClientError()

    def archive(*_args) -> None:
        raise error

    docker, supervisor = _group(archive)

    with pytest.raises(SyntheticClientError) as raised:
        supervisor.stop_if_idle("athr_source", generation=1)

    assert raised.value is error
    (record,) = _events(caplog, _FINAL)
    summary = (
        "phase=final_archive sandbox_id=asbx_shared generation=1 container=0f1e2d3c4b5a "
        "error=SyntheticClientError code=AccessDenied status=403"
    )
    assert record.getMessage() == f"stopped sandbox final archive failed: {summary}"
    assert _as_text(record).endswith(f" - ERROR - stopped sandbox final archive failed: {summary}")
    rendered = _as_json(record)
    assert (rendered["phase"], rendered["code"], rendered["status"]) == (
        "final_archive",
        "AccessDenied",
        403,
    )
    assert rendered["detail"] == (
        "archive failure (phase=fail_stop_final, type=SyntheticClientError, "
        "code=AccessDenied, status=403)"
    )
    for output in (_as_text(record), JsonLogFormatter().format(record)):
        assert _PRIVATE not in output
    assert docker.releases == 0
    assert _events(caplog, _RELEASE) == []
    _assert_kept(docker, supervisor, tmp_path)


def _caused(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "category", "extra"),
    [
        (
            SandboxError(f"inactive group network could not be removed: {_PRIVATE}"),
            "remove",
            {},
        ),
        (
            SandboxError(f"container 0f1e2d3c4b5a could not leave network 'cagent-x': {_PRIVATE}"),
            "disconnect",
            {},
        ),
        (SandboxError(f"Docker network inspect failed: {_PRIVATE}"), "inspect", {}),
        (
            SandboxError("group network 'cagent-x' contains an unexpected peer"),
            "unexpected_peer",
            {},
        ),
        (RetainedChatLost("chat sandbox container identity changed during stop"), "identity", {}),
        (
            _caused(
                SandboxError(f"Docker lifecycle command failed: {_PRIVATE}"),
                subprocess.TimeoutExpired(["docker"], 30),
            ),
            "docker_command",
            {"timeout": True},
        ),
        (
            _caused(
                SandboxError(f"Docker lifecycle command failed: {_PRIVATE}"),
                OSError(2, _PRIVATE),
            ),
            "docker_command",
            {"errno": 2},
        ),
        (SandboxError(f"a sentence no release writes {_PRIVATE}"), "unknown", {}),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_failed_bridge_release_after_the_archive_names_the_docker_step(
    tmp_path, caplog, error, category, extra
):
    archived = []
    docker, supervisor = _group(lambda *args: archived.append(args))
    docker.release_error = error

    with pytest.raises(type(error)) as raised:
        supervisor.stop_if_idle("athr_source", generation=1)

    assert raised.value is error
    assert len(archived) == 1
    assert _events(caplog, _FINAL) == []
    (record,) = _events(caplog, _RELEASE)
    rendered = _as_json(record)
    assert rendered["phase"] == "network_release"
    assert rendered["category"] == category
    assert rendered["error"] == type(error).__name__
    assert {key: rendered[key] for key in ("errno", "timeout") if key in rendered} == extra
    for output in (_as_text(record), JsonLogFormatter().format(record)):
        assert _PRIVATE not in output
        assert "cagent-x" not in output
    assert f"category={category}" in record.getMessage()
    _assert_kept(docker, supervisor, tmp_path)


def test_the_text_format_keeps_a_failed_release_in_full(caplog):
    docker, supervisor = _group(lambda *_args: None)
    docker.release_error = _caused(
        SandboxError(f"Docker lifecycle command failed: {_PRIVATE}"), OSError(13, _PRIVATE)
    )

    with pytest.raises(SandboxError):
        supervisor.stop_if_idle("athr_source", generation=1)

    (record,) = _events(caplog, _RELEASE)
    assert _as_text(record).endswith(
        " - cloud_agent_host.archive_failure_diagnostics - ERROR - "
        "stopped sandbox network release failed: phase=network_release "
        "sandbox_id=asbx_shared generation=1 container=0f1e2d3c4b5a error=SandboxError "
        "category=docker_command errno=13"
    )
    assert json.loads(JsonLogFormatter().format(record))["errno"] == 13


def test_a_clean_stop_logs_neither_failure(caplog):
    docker, supervisor = _group(lambda *_args: None)

    supervisor.stop_if_idle("athr_source", generation=1)

    assert docker.releases == 1
    assert docker.items[_spec().container_name].networks == ()
    assert _events(caplog, _FINAL) == _events(caplog, _RELEASE) == []


def test_every_release_failure_read_is_one_the_host_writes():
    source = "\n".join(
        path.read_text()
        for path in _HOST_SOURCE.glob("*.py")
        if path.name != "archive_failure_diagnostics.py"
    )
    sentences = [prefix for prefix, _category in NETWORK_RELEASE_FAILURES]
    sentences += ["could not leave network ", "contains an unexpected peer"]
    assert [sentence for sentence in sentences if sentence not in source] == []
