"""Fork receipt SQL shared by prepare, finalize and recovery."""

from __future__ import annotations

import json
from typing import Any

import cloud_agent.storage._sql.rows as _rows
from cloud_agent_host.shared_protocol import NATIVE_FORK_AT_ANCHOR_CAPABILITY

_NATIVE_FORK_CAPABILITY_JSON = json.dumps([NATIVE_FORK_AT_ANCHOR_CAPABILITY], separators=(",", ":"))


async def session_fork_details(conn: Any, operation_id: str) -> dict[str, Any] | None:
    """Return one native-fork saga with its immutable host launch inputs."""
    operation = await conn.fetchrow(
        "SELECT * FROM agent_session_forks WHERE id = $1",
        operation_id,
    )
    if operation is None:
        return None
    sandbox = await conn.fetchrow(
        f"SELECT s.* FROM {_rows._THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
        operation["source_thread_id"],
    )
    source = await conn.fetchrow(
        f"SELECT {_rows._JOB_COLUMNS} FROM agent_jobs WHERE id = $1",
        operation["source_job_id"],
    )
    result = dict(operation)
    result["host_receipt"] = _rows.load_json(result.get("host_receipt"))
    if sandbox is not None:
        sandbox_dict = _rows.sandbox_row_to_dict(sandbox)
        result["thread_sandbox"] = sandbox_dict
        for key, value in sandbox_dict.items():
            result.setdefault(key, value)
    if source is not None:
        result["source_job"] = _rows.job_row_to_dict(source)
        # Assigned at the native settlement boundary, not inferred from
        # the final job state. A lease may expire after context settlement
        # but before job finalization; subsequent successful turns must
        # still resolve to the provider's correct ordinal.
        result["source_native_turn_no"] = int(source["native_turn_no"] or 0)
    return result
