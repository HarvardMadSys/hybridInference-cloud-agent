"""The per-container install locks: one per container, kept only while in use."""

from __future__ import annotations

import gc

from cloud_agent_host import install_locks
from cloud_agent_host.install_locks import install_lock


def test_one_container_has_one_lock_and_another_container_its_own() -> None:
    held = install_lock("agent-chat-a")
    with held:
        assert install_lock("agent-chat-a") is held
        assert not install_lock("agent-chat-a").acquire(blocking=False)
        other = install_lock("agent-chat-b")
        assert other is not held
        assert other.acquire(blocking=False)
        other.release()


def test_a_lock_nobody_holds_or_waits_on_is_not_kept() -> None:
    lock = install_lock("agent-chat-gone")
    assert "agent-chat-gone" in install_locks._locks
    del lock
    gc.collect()
    assert "agent-chat-gone" not in install_locks._locks
