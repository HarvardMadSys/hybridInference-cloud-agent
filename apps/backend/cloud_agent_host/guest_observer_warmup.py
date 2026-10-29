"""Start a freshly booted Kata guest's observer before its first prompt asks for it.

The observer starts lazily: the first prompt after each boot finds no helper
and starts it inside its own fence (``GuestObserverController.begin``), so
that prompt waits for a cleanup exec, the detached start, the eBPF load and
the polls until the helper is ready, about 0.8-3 s. Nothing about that start
depends on the turn. Once an activation has started or created a guest and
proved its network, the helper is started here on a daemon thread instead,
beside the member's preparation and CLI start. The first prompt then finds it
running, or waits on the same container lock while this start finishes.

This is the lazy path's own start, unchanged and only earlier: the same lock,
the same ``ensure``, which starts a helper only when none answers. It acts
only while the exact container it was asked for is still the one proven for
its spec, and never while a cursor is open on that guest, since a restart
would invalidate it. A start that fails here is only logged; the first
prompt's fence then starts the helper itself, as before.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import KATA_RUNTIME

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec

logger = logging.getLogger(__name__)


def warm_guest_observer(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, container_id: str
) -> threading.Thread | None:
    """Start the observer of the just-proven guest ``container_id`` on a daemon thread.

    Returns the thread, or ``None`` when the sandbox is not a Kata guest or
    the host has no thread to spare; the first prompt then starts it.
    """
    if manager.backend.runtime != KATA_RUNTIME:
        return None
    thread = threading.Thread(
        target=_warm,
        args=(manager, spec, container_id),
        name="guest-observer-warmup",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError as exc:
        _left_to_the_prompt(spec, exc)
        return None
    return thread


def _warm(manager: ChatSandboxManager, spec: ChatSandboxSpec, container_id: str) -> None:
    """Ensure the helper under its container's lock, if that container is still this one."""
    try:
        with manager._guest_observers.container(spec.container_name) as observers:
            proven = manager._proven.get(spec)
            if observers.active or proven is None or proven.container_id != container_id:
                # A turn is already observed here, or the name now holds
                # another container: whatever runs there next starts its own
                # helper when it needs one.
                return
            manager._guest_observer_controller(spec, lock=observers.lock).ensure()
    except Exception as exc:
        _left_to_the_prompt(spec, exc)


def _left_to_the_prompt(spec: ChatSandboxSpec, exc: Exception) -> None:
    """Record a start this left undone; the first prompt's own fence makes it."""
    logger.info(
        "Kata guest observer did not start ahead of the first prompt; that prompt starts it",
        extra={
            "event": "guest_observer_warmup_failed",
            "sandbox_id": spec.sandbox_id,
            "detail": str(exc)[:300],
        },
    )


__all__ = ["warm_guest_observer"]
