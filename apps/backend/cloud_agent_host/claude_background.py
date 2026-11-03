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

import re
from typing import Any

#: Claude's notification message, and the task it names.
_NOTIFIED_TASK = re.compile(r"<task-notification>\s*<task-id>([^<]+)</task-id>")


def notified_tasks(record: dict[str, Any], text: str) -> frozenset[str] | None:
    """The tasks a replayed message tells Claude finished; ``None`` for any other message.

    An empty set is a notification naming no task the driver can match.
    """
    origin = record.get("origin")
    marked = isinstance(origin, dict) and origin.get("kind") == "task-notification"
    tasks = frozenset(task.strip() for task in _NOTIFIED_TASK.findall(text))
    return tasks if marked or tasks else None


class BackgroundWork:
    """What one turn still waits for: running agents and unread notifications."""

    def __init__(self, known: set[str]) -> None:
        # Every task Claude has listed for the session, shared with it: a shell
        # can outlive the turn that started it, and its end still opens a turn.
        self._known = known
        self.running_agents: set[str] = set()
        # Finished tasks whose notification Claude has not read yet.
        self._unread: set[str] = set()
        # Notifications read in this turn, in case the read is echoed first.
        self._read: set[str] = set()

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
                task_id in self._known
                and record.get("status") != "stopped"
                and task_id not in self._read
            ):
                self._unread.add(task_id)
        elif subtype == "init":
            # The turn Claude opens reads every notification still queued.
            self._unread.clear()

    def observe_read(self, tasks: frozenset[str]) -> None:
        """Claude read these tasks' notification: the next result answers it."""
        if not tasks:
            # A notification the driver cannot match is still read: waiting for
            # a turn Claude will not open would hold the result forever.
            self._unread.clear()
            return
        self._read |= tasks
        self._unread -= tasks
