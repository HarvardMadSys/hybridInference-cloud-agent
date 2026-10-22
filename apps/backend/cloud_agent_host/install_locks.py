"""One lock per container for installing helpers and runtimes into it.

Two installs into one container must never interleave, and an install into one
container need not wait for any other: a runtime copy into one chat can take
minutes, and every turn's admission and end take the same lock for the reaper
probe. A container is the same container whichever manager installs into it,
so the locks are kept per process, by container name, and only while someone
holds or waits on one.
"""

from __future__ import annotations

import threading
import weakref

_guard = threading.Lock()
_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


def install_lock(container_name: str) -> threading.Lock:
    """The lock for installing into ``container_name``, the same one while anyone uses it."""
    with _guard:
        lock = _locks.get(container_name)
        if lock is None:
            lock = threading.Lock()
            _locks[container_name] = lock
        return lock
