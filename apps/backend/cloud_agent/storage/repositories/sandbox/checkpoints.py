"""Host-reported checkpoint receipts and the restore point they set."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.hosts import (
    MOVING_OUT_HOST_SQL,
    release_covered_sandboxes,
)
from cloud_agent.storage._sql.sandbox_transitions import RECORD_SANDBOX_CHECKPOINT_GROUP_SQL
from cloud_agent.storage.repositories.sandbox._common import SandboxRepositoryBase

if TYPE_CHECKING:
    from collections.abc import Sequence


class CheckpointMixin(SandboxRepositoryBase):
    """Record the checkpoint receipts hosts report for a stopped group."""

    async def recorded_sandbox_checkpoints(self, *, checkpoint_ids: Sequence[str]) -> set[str]:
        """Return which of these checkpoint receipts are already recorded.

        Read without locks. Hosts repeat their latest receipt on every beat
        until the group is gone, and :meth:`record_sandbox_checkpoint` takes
        the compute fence and the group's lock only to find a recorded id
        changes nothing. A receipt is never removed but with its group, so one
        this read finds stays recorded.
        """
        if not checkpoint_ids:
            return set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM agent_sandbox_checkpoints WHERE id = ANY($1::text[])",
                list(checkpoint_ids),
            )
        return {row["id"] for row in rows}

    async def record_sandbox_checkpoint(
        self,
        *,
        host: str,
        thread_id: str,
        checkpoint: dict[str, Any],
    ) -> bool:
        """Record one host-reported checkpoint receipt for its current group.

        The receipt must name the group's current host, generation and exact
        container; anything else is a stale or foreign report and is ignored.
        Hosts repeat the latest receipt on every beat, so an existing id is a
        no-op. Returns whether a new receipt was stored.

        A new receipt becomes the group's restore point only if nothing could
        have started the container since it was captured: the group is still
        stopped and its ``start_seq`` is the one the receipt carries. Every
        path that may start the container advances the sequence and clears the
        restore point first, so a receipt repeated by a later beat, or first
        delivered after such a start, never covers the group again. A receipt
        without a sequence (an older sandbox manager) never covers it. The
        object is not verified here: the restoring host checks size and digest
        against this receipt.
        """
        captured_at = checkpoint["created_at"]
        if isinstance(captured_at, str):
            captured_at = datetime.fromisoformat(captured_at)
        start_seq = checkpoint.get("start_seq")
        if isinstance(start_seq, bool) or not isinstance(start_seq, int) or start_seq < 0:
            start_seq = None
        async with self._pool.acquire() as conn, conn.transaction():
            # The fence a move-out hold changes under, taken before the group
            # row as every compute-aware path takes it.
            await lock_compute_entry(conn)
            group = await conn.fetchrow(
                """
                SELECT state, start_seq FROM agent_sandboxes
                WHERE id = $1 AND origin_thread_id = $2 AND host = $3
                  AND generation = $4 AND container_id = $5
                FOR UPDATE
                """,
                checkpoint["sandbox_id"],
                thread_id,
                host,
                int(checkpoint["generation"]),
                checkpoint["container_id"],
            )
            if group is None:
                return False
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_sandbox_checkpoints (
                    id, sandbox_id, generation, host, container_id, base_image,
                    runtime_id, runtime_version, object_key, object_version_id,
                    sha256, size_bytes, new_layer_bytes, layer_count, members,
                    durations_ms, format_version, captured_at, start_seq
                )
                SELECT $1, g.id, $3, $4, $5, $6, $7, $8, $9, $10,
                       $11, $12, $13, $14, $15::jsonb, $16::jsonb, $17, $18, $20
                FROM agent_sandboxes g
                WHERE g.id = $2 AND g.origin_thread_id = $19 AND g.host = $4
                  AND g.generation = $3 AND g.container_id = $5
                ON CONFLICT (id) DO NOTHING
                RETURNING TRUE
                """,
                checkpoint["checkpoint_id"],
                checkpoint["sandbox_id"],
                int(checkpoint["generation"]),
                host,
                checkpoint["container_id"],
                checkpoint["base_image"],
                checkpoint["runtime_id"],
                checkpoint.get("runtime_version"),
                checkpoint["object_key"],
                checkpoint.get("version_id"),
                checkpoint["sha256"],
                int(checkpoint["size_bytes"]),
                int(checkpoint["new_layer_bytes"]),
                int(checkpoint["layer_count"]),
                json.dumps(checkpoint.get("members") or []),
                json.dumps(checkpoint.get("durations_ms") or {}),
                int(checkpoint.get("format_version") or 1),
                captured_at,
                thread_id,
                start_seq,
            )
            if inserted and start_seq is not None:
                await conn.execute(
                    RECORD_SANDBOX_CHECKPOINT_GROUP_SQL,
                    checkpoint["sandbox_id"],
                    checkpoint["checkpoint_id"],
                    start_seq,
                )
                if await conn.fetchval("SELECT " + MOVING_OUT_HOST_SQL.format(host="$1"), host):
                    # The group's chats are waiting for it on another Host.
                    await release_covered_sandboxes(
                        conn, [host], sandbox_ids=[checkpoint["sandbox_id"]], live_host=True
                    )
        return bool(inserted)
