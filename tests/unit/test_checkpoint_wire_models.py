"""Checkpoint records stay one compatible wire contract across both services."""

from __future__ import annotations

import json

import pytest

from cloud_agent.schemas.hosts import SandboxCheckpointBeat
from cloud_agent.schemas.workers import WorkerSandboxRestore
from cloud_agent_host.sandbox_manager import SharedSandboxRestoreRequest
from cloud_agent_shared.sandbox_restore import (
    CheckpointRestore,
    SandboxCheckpointReceipt,
)

_RECEIPT = {
    "checkpoint_id": "c" * 32,
    "sandbox_id": "asbx_one",
    "generation": 3,
    "container_id": "container-1234567890",
    "base_image": "sha256:" + "b" * 64,
    "runtime_id": "claude-code",
    "runtime_version": "2.1.0",
    "object_key": "cloud-agent/sandbox/v2/" + "d" * 64 + "/g3/" + "c" * 32 + ".oci.tar",
    "version_id": "version-1",
    "sha256": "e" * 64,
    "size_bytes": 4096,
    "new_layer_bytes": 2048,
    "layer_count": 3,
    "created_at": "2026-10-13T12:00:00+00:00",
    "members": [
        {
            "thread_id": "athr_one",
            "native_session_id": "native-1",
            "last_settled_job_id": "ajob_1",
            "context_status": "live",
        }
    ],
    "durations_ms": {"commit": 10},
    "format_version": 1,
    "start_seq": 7,
}

_RESTORE = {
    "checkpoint_id": "c" * 32,
    "object_key": _RECEIPT["object_key"],
    "version_id": "version-1",
    "sha256": "e" * 64,
    "size_bytes": 4096,
    "base_image": "registry.example.com/team/image@sha256:" + "b" * 64,
    "format_version": 1,
}


def test_host_receipt_bytes_are_accepted_by_the_heartbeat_model() -> None:
    receipt = SandboxCheckpointReceipt.from_dict(_RECEIPT)

    assert json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":")) == json.dumps(
        _RECEIPT, sort_keys=True, separators=(",", ":")
    )
    heartbeat = SandboxCheckpointBeat.model_validate(receipt.as_dict())
    assert heartbeat.model_dump(mode="json") == {
        **_RECEIPT,
        "created_at": "2026-10-13T12:00:00Z",
    }


def test_restore_models_keep_the_published_version_compatibility() -> None:
    future = {**_RESTORE, "format_version": 2}

    claim = WorkerSandboxRestore.model_validate(future)
    request = SharedSandboxRestoreRequest.model_validate(future)

    assert isinstance(claim, CheckpointRestore)
    assert isinstance(request, CheckpointRestore)
    assert claim.model_dump() == future
    assert request.model_dump() == future
    with pytest.raises(ValueError, match="not supported"):
        request.require_supported()


def test_restore_schema_requiredness_does_not_change_during_extraction() -> None:
    claim_schema = WorkerSandboxRestore.model_json_schema()
    manager_schema = SharedSandboxRestoreRequest.model_json_schema()

    assert claim_schema["properties"]["format_version"] == {
        "minimum": 1,
        "title": "Format Version",
        "type": "integer",
    }
    assert "format_version" in claim_schema["required"]
    assert manager_schema["properties"]["format_version"] == {
        "default": 1,
        "minimum": 1,
        "title": "Format Version",
        "type": "integer",
    }
    assert "format_version" not in manager_schema["required"]


def test_heartbeat_schema_still_allows_only_the_current_format() -> None:
    version = SandboxCheckpointBeat.model_json_schema()["properties"]["format_version"]

    assert version == {
        "const": 1,
        "default": 1,
        "title": "Format Version",
        "type": "integer",
    }
