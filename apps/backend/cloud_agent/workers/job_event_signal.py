"""Wake an owner's event stream when its job stores an event or changes state.

Triggers notify ``agent_job_events`` with the job's id (migration ``0030``), and
the process's shared listener (``notifications``) hands it here. Only streams
of that job are woken; each then reads the events table from its own cursor.
"""

from __future__ import annotations

from cloud_agent.workers.notifications import KeyedSignal

CHANNEL = "agent_job_events"


class JobEventSignal(KeyedSignal):
    """Per-job wake-ups for the event streams of this process."""

    channel = CHANNEL
