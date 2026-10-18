"""Which acquisitions an administrator may close as having created nothing."""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.domain.compute import acquisition_absence_confirmable


def operation(**over: Any) -> dict[str, Any]:
    return {"kind": "acquire", "state": "unknown", "result": {"external_pending": True}, **over}


def allocation(**over: Any) -> dict[str, Any]:
    return {
        "mode": "provision",
        "autoscaling_owned": False,
        "external_id": None,
        "nodes": [],
        "hosts": [],
        "release_committed_at": None,
        "observed_state": "unknown",
        **over,
    }


@pytest.mark.parametrize("state", ["unknown", "failed", "blocked"])
def test_a_stopped_acquisition_that_named_nothing_can_be_closed(state):
    assert acquisition_absence_confirmable(operation(state=state), allocation())


@pytest.mark.parametrize(
    ("operation_over", "allocation_over"),
    [
        # Still acting, or never stopped: its own result is still to come.
        ({"state": "running"}, {}),
        ({"state": "pending"}, {}),
        ({"state": "waiting"}, {}),
        ({"state": "cancelled"}, {}),
        ({"state": "succeeded"}, {}),
        # Only acquisitions create provider resources.
        ({"kind": "retire"}, {}),
        ({"kind": "inspect"}, {}),
        # Something recorded names a resource.
        ({"result": {"resource_ref": {"external_id": "experiment"}}}, {}),
        ({"result": {"external_id": "experiment"}}, {}),
        ({"result": {"resolved_external_id": "experiment"}}, {}),
        ({}, {"external_id": "experiment"}),
        ({}, {"nodes": [{"id": "node"}]}),
        ({}, {"hosts": [{"host": "host-a"}]}),
        # Already settled another way, or not a request of this kind.
        ({}, {"release_committed_at": "2026-10-18T00:00:00Z"}),
        ({}, {"observed_state": "terminated"}),
        ({}, {"observed_state": "absent"}),
        ({}, {"mode": "attach"}),
        # Autoscaled members keep their own lifecycle and reservations.
        ({}, {"autoscaling_owned": True}),
    ],
)
def test_anything_that_may_have_reached_a_resource_cannot_be_closed(
    operation_over, allocation_over
):
    assert not acquisition_absence_confirmable(
        operation(**operation_over), allocation(**allocation_over)
    )


def test_an_operation_without_an_allocation_cannot_be_closed():
    assert not acquisition_absence_confirmable(operation(), None)
