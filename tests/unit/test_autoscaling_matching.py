"""Exact compatibility matching behind CloudLab autoscaling pressure."""

from cloud_agent.execution_profiles import KATA_RUNTIME
from cloud_agent.storage.repositories.host_pool import _maximum_compatible_jobs


def shape(*capabilities: str, runtime: str | None = None) -> tuple[frozenset[str], str | None]:
    return (frozenset(capabilities), runtime)


def job(*requires: str, profile: str = "trusted-v1") -> tuple[frozenset[str], str]:
    return (frozenset(requires), profile)


def test_maximum_matching_does_not_waste_the_flexible_slot() -> None:
    jobs = [job("common"), job("common", "special")]
    hosts = [
        (shape("common", "special"), 1),
        (shape("common"), 1),
    ]

    assert _maximum_compatible_jobs(jobs, hosts) == 2


def test_matching_respects_capacity_and_execution_profile() -> None:
    jobs = [
        job("common", profile="untrusted-kata-v1"),
        job("common", profile="untrusted-kata-v1"),
    ]

    assert _maximum_compatible_jobs(jobs, [(shape("common", runtime="runc"), 2)]) == 0
    assert _maximum_compatible_jobs(jobs, [(shape("common", runtime=KATA_RUNTIME), 1)]) == 1
