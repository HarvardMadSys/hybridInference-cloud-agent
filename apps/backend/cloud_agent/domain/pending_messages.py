"""The owner's pending messages: the values a queue row and its thread can carry.

A pending message is one the owner wrote while a turn was running and that the
platform has not turned into a turn yet. It is ``queued`` until the turn in
front of it ends, ``held`` while the owner edits it or after the platform could
not send it, and ``sending`` once the owner has chosen to send it into the
running turn. Once sent it no longer exists here: the job and the transcript
row it became are the record.

The thread says why its queue stopped moving, if it did. The reason stays until
the owner continues, however many times the queue is looked at in between.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cloud_agent_shared.shared_protocol import USER_MESSAGE_EVENT as USER_MESSAGE_EVENT

#: Waiting for the turn in front of it to end.
PENDING_QUEUED = "queued"
#: Being edited, or refused by the checks a turn has to pass. Nothing is sent
#: past a held message; the queue waits for it.
PENDING_HELD = "held"
#: Chosen to go into the live turn ("send now"): its host puts it in front of
#: the model before the model's next tool call, and the turn's stream records
#: where (``USER_MESSAGE_EVENT``), which is when it leaves the queue. A turn
#: that ends without taking it leaves it here, and the settlement that ending
#: runs puts it back in line: it goes as the next turn.
PENDING_SENDING = "sending"
PENDING_STATES = (PENDING_QUEUED, PENDING_HELD, PENDING_SENDING)


#: The owner stopped the turn this queue was waiting behind.
QUEUE_PAUSED_STOPPED = "stopped"
#: The turn this queue was waiting behind failed.
QUEUE_PAUSED_FAILED = "failed"
#: The conversation was archived with messages still waiting.
QUEUE_PAUSED_ARCHIVED = "archived"
QUEUE_PAUSE_REASONS = (QUEUE_PAUSED_STOPPED, QUEUE_PAUSED_FAILED, QUEUE_PAUSED_ARCHIVED)

#: The longest refusal kept on a held message. The column enforces the same.
MAX_PENDING_ERROR_CHARS = 2000


@dataclass(frozen=True)
class PreparedHead:
    """The queue's first message, checked by the rules every turn passes.

    Made outside the settling transaction, because the checks can reach the
    gateway and the source-control provider, and handed back into it. It applies
    only to the exact message it was made for: the same id and revision, and
    the same turn it would follow. Anything else and the settlement asks again.

    ``error`` is a refusal: the message is held with that sentence instead of
    being sent. Otherwise the remaining fields are the resolved choices the
    store writes, with the meanings :func:`insert_follow_up` gives them.
    """

    message_id: str
    revision: int
    parent_job_id: str
    model: str | None = None
    reasoning_effort: str | None = None
    reset_reasoning_effort: bool = False
    skills: list[dict[str, Any]] | None = None
    attachments: list[dict[str, Any]] | None = None
    error: str | None = None


@dataclass(frozen=True)
class SettleOutcome:
    """What one settlement did.

    ``job`` and ``message_id`` are set together when it turned the queue's first
    message into the next turn. ``head`` and ``parent_job_id`` are set together
    when it would have, but the message has not been checked for that turn yet:
    the caller prepares it and settles again. All four are ``None`` when there
    was nothing to do — a turn still running, a paused or empty queue, or a
    first message that is held.
    """

    job: dict[str, Any] | None = None
    message_id: str | None = None
    head: dict[str, Any] | None = None
    parent_job_id: str | None = None
