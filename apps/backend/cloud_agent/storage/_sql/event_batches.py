"""An attempt's events, appended in order inside the caller's transaction.

What a single append and a runner's batch both run, a single event being a
batch of one. One lock on the live attempt fences the whole batch. Each event
then has the side effects of its own append, in order: the terminal readiness
its lifecycle phase sets, then its row and transcript mirror. A model call's
usage asks for one ledger read after the rows, so the ledger row stays the last
row the transaction takes, as it was for a single append.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.attempts import lock_live_attempt
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.ledger import NOTIFY_JOB_USAGE_SQL, carries_model_usage

if TYPE_CHECKING:
    from collections.abc import Sequence


async def append_attempt_events(
    conn: Any,
    *,
    attempt_id: int,
    lease_generation: int,
    events: Sequence[tuple[str, dict[str, Any] | None, int | None]],
) -> list[int] | None:
    """Append ``(event_type, payload, event_seq)`` events in order, fenced by the live lease.

    Returns their ids in the order given, or ``None`` when the attempt no
    longer holds its lease, and then writes nothing. A number the attempt
    already stored answers with that row's id, its side effects run again as a
    replayed single append's do.
    """
    job_id = await lock_live_attempt(conn, attempt_id, lease_generation)
    if job_id is None:
        return None
    event_ids: list[int] = []
    for event_type, payload, event_seq in events:
        phase = (payload or {}).get("phase") if event_type == "lifecycle" else None
        if phase == "workspace_ready":
            await conn.execute(
                "UPDATE agent_attempts SET terminal_ready = TRUE WHERE id = $1", attempt_id
            )
        elif phase in {"workspace_preparing", "workspace_finalizing"}:
            await conn.execute(
                "UPDATE agent_attempts SET terminal_ready = FALSE WHERE id = $1", attempt_id
            )
        event_ids.append(
            await insert_event(
                conn,
                job_id=job_id,
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
                client_seq=event_seq,
            )
        )
    if any(carries_model_usage(event_type, payload) for event_type, payload, _ in events):
        # A model call's usage just arrived: push it to the owner's open tabs
        # when this commits, rather than leave them to their next read.
        await conn.execute(NOTIFY_JOB_USAGE_SQL, job_id)
    return event_ids
