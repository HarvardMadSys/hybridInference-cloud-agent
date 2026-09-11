"""Attempt-window SQL shared between the ledger and the queue."""

from __future__ import annotations

# Bound a shared grant to one attempt's execution interval. A subsequent
# attempt may start before a delayed predecessor is marked finished; clamp
# that predecessor rather than counting the same requests twice. PostgreSQL
# LEAST ignores the NULL produced when there is no following attempt.
#
# The successor lookup is named separately because a second reader asks a
# different question of the same definition: whether an attempt's window is
# still open (no end of its own and no successor) or already bounded.
# Duplicating the subquery to answer that would be a second algorithm free to
# drift from this one. ``a`` and ``j`` are the attempt and job aliases.
NEXT_ATTEMPT_START_SQL = """
(
    SELECT MIN(next_attempt.started_at)
    FROM agent_attempts next_attempt
    JOIN agent_jobs next_job ON next_job.id = next_attempt.job_id
    WHERE next_job.thread_id = j.thread_id
      AND (next_attempt.started_at, next_attempt.id) > (a.started_at, a.id)
)
"""
