"""Claude Code's background work, as a resident turn must follow it to end.

A resident turn ends where the one-shot ``claude -p`` run would: not while a
background agent is still running, and not before Claude has answered every
background task that finished. Claude reports both on stdout:

- ``system``/``background_tasks_changed`` lists what is still running;
- ``system``/``task_notification`` says a task finished;
- the notification is a message Claude reads into the conversation, and with
  ``--replay-user-messages`` it echoes that read as a ``user`` replay whose
  text is ``<task-notification>``, then ``<task-id>ID</task-id>``. Claude Code 2.1.300
  marks it ``origin: {"kind": "task-notification"}`` as well; the pinned
  2.1.220 echoes the text alone.

Claude reads it in the turn it is running, between tool calls, before the
model's next request, so that turn's result answers it; or, when no request
follows, in a turn of its own that it opens after its result with a
``system``/``init``. Only the second kind needs waiting for.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Claude's notification message, and the task it names.
_NOTIFIED_TASK = re.compile(r"<task-notification>\s*<task-id>([^<]+)</task-id>")

#: A stdout line's record, and for a replay in the main conversation its text
#: and the tasks it tells Claude finished (``None``: not a notification).
ReadLine = tuple[dict[str, Any] | None, str | None, frozenset[str] | None]


def record_text(record: dict[str, Any]) -> str:
    """The text blocks of one stream-json message record, joined."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def is_task_notification(record: dict[str, Any]) -> bool:
    """Whether Claude marked this record as coming from a task notification."""
    origin = record.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "task-notification"


def notified_tasks(record: dict[str, Any], text: str, known: set[str]) -> frozenset[str] | None:
    """The tasks a replayed message tells Claude finished; ``None`` for any other message.

    Without the ``origin`` mark (the pinned 2.1.220) only the text tells, so it
    must name a task Claude listed for this session: an owner's message that
    quotes a notification, say pasted from a log, names none of them. An empty
    set is a marked notification naming no task the driver can match.
    """
    tasks = frozenset(task.strip() for task in _NOTIFIED_TASK.findall(text))
    if is_task_notification(record):
        return tasks
    listed = tasks & known
    return listed or None


class SessionTasks:
    """Claude's background tasks as one resident session saw them, across its turns."""

    def __init__(self) -> None:
        # Every task Claude has listed: a shell can outlive the turn that
        # started it, and its end still opens a turn.
        self.known: set[str] = set()
        # Every notification Claude has read: the read can be echoed before its
        # ``task_notification``, and that record may come only after the
        # result, in the next turn's slice.
        self.read: set[str] = set()


class BackgroundWork:
    """What one turn still waits for: running agents and unread notifications."""

    def __init__(self, session: SessionTasks) -> None:
        self._known = session.known
        self._read = session.read
        self.running_agents: set[str] = set()
        # Finished tasks whose notification Claude has not read yet.
        self._unread: set[str] = set()
        # Notifications read but matched to no task, whose ``task_notification``
        # has not arrived yet: that many are already answered when they do.
        self._unmatched_reads = 0
        # The last line read, and what ``read`` made of it.
        self._last: tuple[str, ReadLine] | None = None

    def read(self, line: str) -> ReadLine:
        """One stdout line, parsed and, if it is a replay, matched once.

        The turn settles on a line, then turns it into events: both read the
        same line object, so the second read reuses the first.
        """
        if self._last is not None and self._last[0] is line:
            return self._last[1]
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            record = None
        if not isinstance(record, dict):
            record = None
        text = notice = None
        # A subagent's replays are its sidechain, not this conversation's.
        if (
            record is not None
            and record.get("type") == "user"
            and record.get("isReplay")
            and not record.get("parent_tool_use_id")
        ):
            text = record_text(record)
            notice = notified_tasks(record, text, self._known)
        self._last = (line, (record, text, notice))
        return self._last[1]

    @property
    def unanswered(self) -> bool:
        """Whether a notification turn Claude has yet to open is still owed."""
        return bool(self._unread)

    def observe_system(self, record: dict[str, Any]) -> None:
        """Follow one top-level ``system`` record."""
        subtype = record.get("subtype")
        if subtype == "background_tasks_changed":
            tasks = [task for task in record.get("tasks") or [] if isinstance(task, dict)]
            listed = {str(task["task_id"]): task for task in tasks if task.get("task_id")}
            self._known |= set(listed)
            self.running_agents = {
                task_id
                for task_id, task in listed.items()
                if task.get("task_type") == "local_agent"
            }
        elif subtype == "task_notification":
            task_id = str(record.get("task_id") or "")
            # A stopped task gets no turn of its own; a foreground agent's
            # notification is answered by its tool result.
            if (
                task_id not in self._known
                or record.get("status") == "stopped"
                or task_id in self._read
            ):
                return
            if self._unmatched_reads:
                self._unmatched_reads -= 1
                return
            self._unread.add(task_id)
        elif subtype == "init":
            # The turn Claude opens reads every notification still queued.
            self._unread.clear()

    def observe_read(self, tasks: frozenset[str]) -> None:
        """Claude read these tasks' notification: the next result answers it."""
        if not tasks:
            # A notification the driver cannot match is still read: waiting for
            # a turn Claude will not open would hold the result forever. Its
            # task is one already waited for, or one whose notice is to come.
            if self._unread:
                self._unread.clear()
            else:
                self._unmatched_reads += 1
            return
        self._read |= tasks
        self._unread -= tasks
