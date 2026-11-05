"""The sandbox repository's mixins compose the methods the single class had.

``PostgresSandboxRepository`` was one class in ``storage/repositories/sandbox.py``
until it was split into one mixin per aggregate. ``BEFORE_SPLIT`` is the set of
methods that class defined, read from that file at commit ``3e0400ef7``; the
composed class must define exactly these, private ones included, each in one
place.
"""

from __future__ import annotations

import inspect

from cloud_agent.storage.repositories.sandbox.repository import PostgresSandboxRepository

#: Every method ``PostgresSandboxRepository`` defined before the split.
BEFORE_SPLIT = frozenset(
    {
        "__init__",
        "_audit_admin_deletion",
        "_capture_attempt_ledger",
        "_complete_admin_sandbox_discard_in_transaction",
        "_finish_deletion_attempt_in_transaction",
        "_locked_admin_deletion",
        "_reconcile_shared_sandbox_observation",
        "_record_resource_observation",
        "_record_terminal_observation",
        "_record_trajectory_event",
        "_reserve_shared_thread_sandbox",
        "abandon_thread_sandbox",
        "admit_thread_sandbox_turn",
        "claim_due_admin_sandbox_deletion",
        "complete_admin_sandbox_deletion",
        "complete_admin_sandbox_discard",
        "due_thread_sandbox_stop",
        "fail_admin_sandbox_deletion",
        "fence_terminal_workspace",
        "fence_workspace_start",
        "finish_admin_sandbox_deletion_attempt",
        "list_terminal_resumes_pending",
        "mark_terminal_resume_complete",
        "mark_thread_sandbox_running",
        "reconcile_thread_sandbox_observation",
        "reconcile_thread_sandbox_observations",
        "record_admin_sandbox_archive",
        "record_sandbox_checkpoint",
        "record_shared_thread_residency",
        "record_terminal_observation",
        "record_thread_sandbox_process",
        "recorded_sandbox_checkpoints",
        "recover_reclaimed_admin_sandbox_discard",
        "release_moving_out_groups",
        "renew_admin_sandbox_deletion_claim",
        "request_admin_delete",
        "request_admin_stop",
        "request_deleted_sessions_stop",
        "request_move_out_stop",
        "reserve_thread_sandbox",
        "sandboxes_moving_out",
        "sandboxes_of_deleted_sessions",
        "set_admin_archived",
        "settle_thread_sandbox",
        "terminal_control_route",
        "terminal_resume_page",
        "thread_sandbox",
        "thread_workspace_ready",
        "update_admin_sandbox_deletion_attempt_stage",
    }
)


def _defined_methods(cls: type) -> dict[str, list[str]]:
    """Each method name the class and its bases define, with every defining class."""
    found: dict[str, list[str]] = {}
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if isinstance(value, staticmethod | classmethod) or inspect.isfunction(value):
                found.setdefault(name, []).append(klass.__qualname__)
    return found


def test_the_composed_repository_has_exactly_the_methods_of_the_single_class() -> None:
    assert set(_defined_methods(PostgresSandboxRepository)) == BEFORE_SPLIT


def test_no_method_is_defined_by_two_mixins() -> None:
    twice = {
        name: owners
        for name, owners in _defined_methods(PostgresSandboxRepository).items()
        if len(owners) > 1
    }
    assert twice == {}


def test_every_mixin_lives_in_the_sandbox_repository_package() -> None:
    package = "cloud_agent.storage.repositories.sandbox"
    outside = [
        klass.__module__
        for klass in PostgresSandboxRepository.__mro__
        if klass is not object and not klass.__module__.startswith(f"{package}.")
    ]
    assert outside == []
