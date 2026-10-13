"""What a host may report about itself is bounded, and real reports fit.

A host's capabilities, version stamps, labels and worker id are stored as
JSONB that the admin pool view and the owner's job view read back, so none of
them may be unbounded. The caps are generous on purpose: a refused heartbeat
takes a machine out of the pool, so every assertion that something is refused
sits next to one proving that what a real runner sends is accepted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cloud_agent.schemas import (
    HOST_CAPABILITY_MAX_CHARS,
    HOST_LABEL_VALUE_MAX_CHARS,
    HOST_LABELS_MAX_ITEMS,
    HOST_REPORT_VERSION_MAX_CHARS,
    HOST_VERSION_MENU_MAX_ITEMS,
    WORKER_FINISH_DETAIL_MAX_CHARS,
    WORKER_ID_MAX_CHARS,
    HostHeartbeatRequest,
    HostRegisterRequest,
    WorkerClaimAttestation,
    WorkerClaimRequest,
    WorkerFinishRequest,
)
from cloud_agent_host.runner import default_worker_id
from cloud_agent_shared.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)

_REAL_CAPABILITIES = [
    SHARED_SANDBOX_CAPABILITY,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    shared_deployment_capability("0123456789abcdef0123456789abcdef01234567"),
]


def _register(**overrides) -> HostRegisterRequest:
    body = {
        "enroll_token": "token",
        "name": "runner-a",
        "slots_total": 4,
        "sandbox_manager_url": "http://runner-a:8787",
        "capabilities": _REAL_CAPABILITIES,
        "labels": {"physical_host": "rack-1-node-7"},
    }
    body.update(overrides)
    return HostRegisterRequest.model_validate(body)


def test_a_real_runners_reports_are_accepted() -> None:
    """The shared protocol's own constants and version pins fit every cap."""
    menu = {runtime: [version] for runtime, version in SHARED_SANDBOX_RUNTIME_PINS.items()}
    HostHeartbeatRequest(
        capabilities=_REAL_CAPABILITIES,
        runtime_versions=dict(SHARED_SANDBOX_RUNTIME_PINS),
        runtime_versions_available=menu,
    )
    WorkerClaimAttestation(
        runner_capabilities=_REAL_CAPABILITIES,
        runtime_versions=dict(SHARED_SANDBOX_RUNTIME_PINS),
        runtime_versions_available=menu,
        helper_versions=dict(SHARED_SANDBOX_HELPER_PINS),
    )
    _register()
    WorkerClaimRequest(worker_id=f"{default_worker_id()}-12")


def test_an_oversized_capability_is_refused_everywhere_a_host_declares_one() -> None:
    too_long = "x" * (HOST_CAPABILITY_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        HostHeartbeatRequest(capabilities=[too_long])
    with pytest.raises(ValidationError):
        WorkerClaimAttestation(runner_capabilities=[too_long])
    with pytest.raises(ValidationError):
        _register(capabilities=[too_long])


def test_version_stamps_are_bounded_in_value_and_in_menu_length() -> None:
    long_version = "1." + "0" * HOST_REPORT_VERSION_MAX_CHARS
    with pytest.raises(ValidationError):
        HostHeartbeatRequest(runtime_versions={"claude-code": long_version})
    with pytest.raises(ValidationError):
        HostHeartbeatRequest(runtime_versions_available={"claude-code": [long_version]})
    with pytest.raises(ValidationError):
        HostHeartbeatRequest(
            runtime_versions_available={
                "claude-code": [f"1.0.{n}" for n in range(HOST_VERSION_MENU_MAX_ITEMS + 1)]
            }
        )
    with pytest.raises(ValidationError):
        WorkerClaimAttestation(helper_versions={"x" * 65: "1.0.0"})


def test_registration_labels_are_bounded() -> None:
    _register(labels={"physical_host": "h" * HOST_LABEL_VALUE_MAX_CHARS})
    with pytest.raises(ValidationError):
        _register(labels={"physical_host": "h" * (HOST_LABEL_VALUE_MAX_CHARS + 1)})
    with pytest.raises(ValidationError):
        _register(labels={f"label-{n}": "v" for n in range(HOST_LABELS_MAX_ITEMS + 1)})


def test_a_worker_id_is_bounded() -> None:
    WorkerClaimRequest(worker_id="w" * WORKER_ID_MAX_CHARS)
    with pytest.raises(ValidationError):
        WorkerClaimRequest(worker_id="w" * (WORKER_ID_MAX_CHARS + 1))


def test_a_long_finish_detail_is_trimmed_not_refused() -> None:
    """A finish the schema refused would strand the turn on its lease."""
    assert WorkerFinishRequest(state="failed").detail is None
    assert WorkerFinishRequest(state="failed", detail="agent exited 1").detail == "agent exited 1"
    exact = "d" * WORKER_FINISH_DETAIL_MAX_CHARS
    assert WorkerFinishRequest(state="failed", detail=exact).detail == exact

    trimmed = WorkerFinishRequest(state="failed", detail="setup failed: " + "x" * 100_000).detail

    assert trimmed is not None
    assert len(trimmed) == WORKER_FINISH_DETAIL_MAX_CHARS
    assert trimmed.startswith("setup failed: ")
    assert trimmed.endswith("…")
