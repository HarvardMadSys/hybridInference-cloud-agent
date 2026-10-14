"""What the control plane and a host agree on to move a stopped group.

A group whose host is gone can continue on another host from its latest
checkpoint. The control plane decides which checkpoint covers a group and
which host may restore it; the host's sandbox manager downloads, checks and
loads it. Both read the facts below, so they live here rather than in either
implementation package.
"""

from __future__ import annotations

import re
from typing import Final

#: A host's sandbox manager can recreate a group from a checkpoint. A claim
#: hands a cold group only to a replica that attests it.
SANDBOX_RESTORE_CAPABILITY: Final = "sandbox-restore-v1"

#: Claims a cold group gets before it is marked lost. A restore that fails
#: before the turn is admitted returns the turn to the queue, so without a
#: bound one unreadable checkpoint would be retried forever.
MAX_RESTORE_ATTEMPTS: Final = 3

#: A base image another host can fetch by itself: a registry host with a dot,
#: a repository, and a digest. A locally built image has no such name, and a
#: group created from one cannot leave its host. The same text is used as a
#: PostgreSQL regular expression.
PULLABLE_IMAGE_PATTERN: Final = (
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?"
    r"/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_PULLABLE_IMAGE_RE: Final = re.compile(PULLABLE_IMAGE_PATTERN)


def pullable_image(reference: str | None) -> bool:
    """Whether another host can pull ``reference`` without this host's daemon."""
    return bool(reference) and _PULLABLE_IMAGE_RE.fullmatch(reference or "") is not None


__all__ = [
    "MAX_RESTORE_ATTEMPTS",
    "PULLABLE_IMAGE_PATTERN",
    "SANDBOX_RESTORE_CAPABILITY",
    "pullable_image",
]
