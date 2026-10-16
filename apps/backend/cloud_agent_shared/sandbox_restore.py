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

#: How long a cold group waits after its first, second and third failed restore
#: before a host may claim it again. A restore fails on a registry or S3
#: outage as readily as on a broken checkpoint. Spaced only by the queue's short
#: retry delay, every claim fell inside an outage of a minute or two and the
#: chat was lost for good. These waits spread the attempts over more than half
#: an hour, which a passing outage does not outlast; a checkpoint that is really
#: broken still ends lost, only later.
RESTORE_RETRY_DELAYS_S: Final = (30.0, 300.0, 1800.0)

#: Claims a cold group gets before it is marked lost: the first, and one after
#: each wait above. A restore that fails before the turn is admitted returns
#: the turn to the queue, so without a bound one unreadable checkpoint would
#: be retried forever.
MAX_RESTORE_ATTEMPTS: Final = len(RESTORE_RETRY_DELAYS_S) + 1

#: Why a host hands a turn back when its sandbox manager could not recreate the
#: group. It counts as a failed restore. A claim handed back for any other reason
#: before its host reserved the group never reached the checkpoint, and counts
#: as none.
RESTORE_FAILED_RELEASE_REASON: Final = "sandbox_restore_failed"

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
    "RESTORE_FAILED_RELEASE_REASON",
    "RESTORE_RETRY_DELAYS_S",
    "SANDBOX_RESTORE_CAPABILITY",
    "pullable_image",
]
