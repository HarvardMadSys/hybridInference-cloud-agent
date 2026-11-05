"""SQL for sandbox incidents that more than one module runs.

The cursor's advance runs inside the attempt's own event transaction
(``cloud_agent.storage._sql.event_batches``), when its ``started`` event is
stored: from then on the attempt's prompt has been used, so what it told is
told. It only moves forward.
"""

from __future__ import annotations

#: Move the thread's cursor to what attempt ``$1``'s prompt told, if anything.
ADVANCE_INCIDENT_CURSOR_SQL = """
INSERT INTO agent_thread_incident_cursors (thread_id, told_through)
SELECT thread_id, through_id FROM agent_attempt_incident_marks WHERE attempt_id = $1
ON CONFLICT (thread_id) DO UPDATE
SET told_through = GREATEST(agent_thread_incident_cursors.told_through, EXCLUDED.told_through),
    updated_at = NOW()
"""
