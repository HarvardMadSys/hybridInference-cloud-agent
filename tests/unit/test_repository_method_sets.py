"""A repository split into mixins exposes exactly the methods it had as one class.

Each class below was one module before it became a package of mixins, one per
concern. The expected names are the methods its class body defined then, read
from that module at commit ``3e0400ef7``, private ones included: a method lost
in the split, or a helper that leaked into the class, changes the set.
"""

from __future__ import annotations

from types import FunctionType

from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository


def _methods(cls: type) -> set[str]:
    """Every method the class and its mixins define, as the class resolves them."""
    return {
        name
        for klass in cls.__mro__
        if klass is not object
        for name, value in vars(klass).items()
        if isinstance(value, FunctionType | staticmethod | classmethod)
    }


def test_the_host_pool_repository_keeps_its_methods() -> None:
    assert _methods(PostgresHostPoolRepository) == {
        "__init__",
        "_host_row_to_dict",
        "_keep_capacity_history_isolated",
        "_record_host_capacity",
        "_record_host_capacity_isolated",
        "acquire_host_drain_fence",
        "activate_ready_runtime_upgrades",
        "active_runner_host",
        "authenticate_host",
        "autoscaling_observation",
        "consume_enroll_token",
        "deployment_runner_pool",
        "finish_host_removals",
        "forget_runner_host",
        "get_host",
        "host_for_attempt",
        "host_for_job",
        "host_settings",
        "issue_enroll_token",
        "list_runner_hosts",
        "queue_health",
        "reap_offline_hosts",
        "record_host_heartbeat",
        "register_host",
        "release_host_drain_fence",
        "remove_host",
        "request_runtime_upgrade",
        "runner_pool",
        "runtime_download_targets",
        "runtime_upgrade_snapshot",
        "sandbox_archive_settings",
        "set_active_runner_host",
        "set_host_settings",
        "set_host_slots_desired",
        "set_host_status",
        "set_runner_host_settings",
        "set_runtime_version",
        "set_sandbox_archive_settings",
    }
