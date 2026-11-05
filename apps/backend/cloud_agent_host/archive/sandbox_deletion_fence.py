"""Atomic absence proof and recreation fence for stale sandbox deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox import ChatSandboxSpec

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping

    from cloud_agent_host.chat_sandbox import ChatSandboxManager


def prove_admin_deletion_target_absent(
    *,
    manager: ChatSandboxManager,
    condition: threading.Condition,
    members: Mapping[str, object],
    groups: Mapping[str, object],
    creating: set[str],
    thread_id: str,
    sandbox_id: str,
    container_id: str,
    on_absent: Callable[[str], None] | None,
) -> bool:
    """Prove both Docker identities absent, optionally fencing a callback."""
    container_name = ChatSandboxSpec.container_name_for(thread_id, sandbox_id)
    if on_absent is not None:
        return _run_absent_callback(
            manager=manager,
            condition=condition,
            members=members,
            groups=groups,
            creating=creating,
            thread_id=thread_id,
            sandbox_id=sandbox_id,
            container_id=container_id,
            container_name=container_name,
            on_absent=on_absent,
        )
    with condition:
        if _registered(
            members=members,
            groups=groups,
            creating=creating,
            thread_id=thread_id,
            sandbox_id=sandbox_id,
        ):
            return False
    if manager.inspect(container_name) is not None:
        return False
    absent = manager.inspect(container_id) is None
    with condition:
        return absent and not _registered(
            members=members,
            groups=groups,
            creating=creating,
            thread_id=thread_id,
            sandbox_id=sandbox_id,
        )


def _run_absent_callback(
    *,
    manager: ChatSandboxManager,
    condition: threading.Condition,
    members: Mapping[str, object],
    groups: Mapping[str, object],
    creating: set[str],
    thread_id: str,
    sandbox_id: str,
    container_id: str,
    container_name: str,
    on_absent: Callable[[str], None],
) -> bool:
    with condition:
        if _registered(
            members=members,
            groups=groups,
            creating=creating,
            thread_id=thread_id,
            sandbox_id=sandbox_id,
        ):
            return False
        creating.add(sandbox_id)
    try:
        if manager.inspect(container_name) is not None:
            return False
        if manager.inspect(container_id) is not None:
            return False
        on_absent(container_name)
        return True
    finally:
        with condition:
            creating.discard(sandbox_id)
            condition.notify_all()


def _registered(
    *,
    members: Mapping[str, object],
    groups: Mapping[str, object],
    creating: set[str],
    thread_id: str,
    sandbox_id: str,
) -> bool:
    return thread_id in members or sandbox_id in groups or sandbox_id in creating
