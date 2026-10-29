"""Argon2id password hashing and the password policy.

Adapted from HybridInference ``apps/backend/serving/utils/password.py`` at
``321e9a6c457dcc80d35977aaac472d54041204a8`` (MIT License, Copyright (c) 2026
Harvard SEAS). The parameters (Argon2id, 64 MiB, three passes, four lanes) and
the strength rules are unchanged. Two adaptations:

- ``argon2-cffi`` is called directly instead of through ``passlib``, which
  wraps the same library and is unmaintained. Both produce the same PHC
  ``$argon2id$v=19$m=65536,t=3,p=4$...`` strings.
- Hashing and verifying run on a small dedicated thread pool, never on the
  event loop: one hash costs 64 MiB and tens of milliseconds, and the pool's
  size bounds both the memory and the CPU a burst of sign-ins can take.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import TYPE_CHECKING, TypeVar

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: At most this many hashes run at once in one process; the rest wait their turn.
HASH_CONCURRENCY = 4
_EXECUTOR = ThreadPoolExecutor(max_workers=HASH_CONCURRENCY, thread_name_prefix="agent-password")

#: At most this many wait or run at once; beyond it a request is refused at
#: once, so a burst queues no unbounded number of plaintext passwords.
MAX_PENDING_HASHES = 64
_pending = 0
_pending_lock = threading.Lock()


class HasherBusy(RuntimeError):
    """Too many password checks are already waiting in this process."""


def _release_slot(_future: object = None) -> None:
    global _pending
    with _pending_lock:
        _pending -= 1


async def _run(function: Callable[..., T], *args: str) -> T:
    """Run one hash on the pool; its slot is held until the hash itself ends.

    The slot is released by the pool future's completion, not by this
    coroutine: a caller cancelled while its hash is already running (a client
    that disconnected, a timeout) leaves that hash running, holding its
    memory and its plaintext, so it must keep counting against
    :data:`MAX_PENDING_HASHES`. A hash still waiting in the queue is
    cancelled with its caller and frees its slot at once.
    """
    global _pending
    with _pending_lock:
        if _pending >= MAX_PENDING_HASHES:
            raise HasherBusy
        _pending += 1
    try:
        future = _EXECUTOR.submit(function, *args)
    except BaseException:
        _release_slot()
        raise
    future.add_done_callback(_release_slot)
    return await asyncio.wrap_future(future)


MIN_PASSWORD_CHARS = 8
#: Bounds the work one request can ask the hasher to do.
MAX_PASSWORD_CHARS = 1024


def validate_password_strength(password: str) -> tuple[bool, str | None]:
    """Validate the password rules HybridInference applies.

    Requirements: at least 8 characters, an uppercase letter, a lowercase
    letter and a digit; and, added here, at most 1,024 characters.

    Returns:
        ``(is_valid, error_message)``; the message is ``None`` when valid.
    """
    if len(password) < MIN_PASSWORD_CHARS:
        return False, "Password must be at least 8 characters long"
    if len(password) > MAX_PASSWORD_CHARS:
        return False, "Password must be at most 1024 characters long"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number"
    return True, None


def _hash(password: str) -> str:
    return _HASHER.hash(password)


def _verify(password: str, hashed: str) -> bool:
    try:
        return _HASHER.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    # Verified against when no account matches, so an unknown email costs the
    # same time as a wrong password.
    return _hash(secrets.token_urlsafe(24))


async def hash_password(password: str) -> str:
    """Hash a password with Argon2id off the event loop.

    Raises:
        HasherBusy: :data:`MAX_PENDING_HASHES` are already waiting.
    """
    return await _run(_hash, password)


async def verify_password(password: str, hashed: str | None) -> bool:
    """Check a password in constant time off the event loop.

    Args:
        password: What the caller typed.
        hashed: The stored hash, or ``None`` when no account matched; a decoy
            hash is then checked so both cases take the same time.
    """
    if hashed is None:
        await _run(_verify, password, await _run(_decoy_hash))
        return False
    return await _run(_verify, password, hashed)


def needs_rehash(hashed: str) -> bool:
    """Whether a stored hash predates the current parameters."""
    try:
        return _HASHER.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


__all__ = [
    "HASH_CONCURRENCY",
    "MAX_PASSWORD_CHARS",
    "MAX_PENDING_HASHES",
    "MIN_PASSWORD_CHARS",
    "HasherBusy",
    "hash_password",
    "needs_rehash",
    "validate_password_strength",
    "verify_password",
]
