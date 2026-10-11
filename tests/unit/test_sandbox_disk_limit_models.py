"""Disk reservations accept bounded whole MiB without coercing worker input."""

import pytest
from pydantic import ValidationError

from cloud_agent.schemas import WorkerSandboxReserveRequest


def _reservation(**overrides):
    return WorkerSandboxReserveRequest(
        sandbox_id="asbx_test",
        container_name="agent-test",
        runtime_id="claude-code",
        execution_profile="untrusted-kata-v1",
        host="runner-one",
        **overrides,
    )


@pytest.mark.parametrize("disk_limit_mib", [0, 1, 16384, 16777216])
def test_worker_disk_reservation_accepts_bounded_integer_mib(disk_limit_mib):
    assert _reservation(disk_limit_mib=disk_limit_mib).disk_limit_mib == disk_limit_mib


def test_a_worker_without_the_new_field_keeps_unlimited_disk():
    assert _reservation().disk_limit_mib == 0


@pytest.mark.parametrize("disk_limit_mib", [-1, 16777217, True, False, 1.0, "16384", None])
def test_worker_disk_reservation_rejects_invalid_or_coerced_limits(disk_limit_mib):
    with pytest.raises(ValidationError):
        _reservation(disk_limit_mib=disk_limit_mib)
