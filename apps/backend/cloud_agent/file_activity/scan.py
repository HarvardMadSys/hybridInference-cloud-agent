"""Reduce a conversation's recorded scan rows to operations, then a view.

The scan reads the conversation's stored events — across every turn and
attempt the owner recorded, not one trajectory page — and this module turns
them into operations with three properties the feature promises:

* **Stable identities.** An operation is keyed by its attempt, its tool call's
  recorded id and (for a multi-file patch) its section. A bridge that re-sends
  the same mutable call counts it once; two calls with different ids are two
  operations even on one file, so a real re-read or retry stays visible.
* **Honest statuses.** A call with no result is ``pending`` while its attempt
  runs and ``not_recorded`` once it ended. Neither is ever shown or counted as
  a completed write.
* **Per-attempt cwd.** A relative path is resolved against the cwd its own
  attempt recorded, so two attempts with different roots cannot collapse into
  one file. An attempt that recorded no cwd leaves its relative paths
  relative, never guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.file_activity.normalize import codex_operations, tool_operations

if TYPE_CHECKING:
    from cloud_agent.file_activity.models import FileOperation


@dataclass
class ScanResult:
    """The operations one bounded read produced, and what it could not place."""

    thread_id: str
    operations: list[FileOperation] = field(default_factory=list)
    turns: int = 0
    attempts: int = 0
    events_scanned: int = 0
    capped: bool = False


def _cwd_rows(rows: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """The cwd each attempt recorded, from its ``started``/``init`` lifecycle row."""
    found: dict[int, str] = {}
    for row in rows:
        if row.get("event_type") != "lifecycle":
            continue
        cwd = row.get("payload", {}).get("cwd") if isinstance(row.get("payload"), Mapping) else None
        if isinstance(cwd, str) and cwd.startswith("/") and "\x00" not in cwd and len(cwd) <= 4096:
            found.setdefault(int(row["attempt_id"]), cwd)
    return found


def _payload_job(row: Mapping[str, Any]) -> str | None:
    """A row's job id when the row itself does not carry one."""
    payload = row.get("payload")
    value = payload.get("job_id") if isinstance(payload, Mapping) else None
    return str(value) if value is not None else None


def _operation_key(operation: FileOperation, index: int) -> tuple[Any, ...]:
    """An operation's stable identity inside its attempt.

    A call id is unique per runtime stream; a Codex ``fileChange`` has none, so
    it is keyed by its event and the change's position. The section index keeps
    two files of one patch apart.
    """
    if operation.tool == "fileChange":
        return ("fileChange", operation.attempt_id, operation.event_id, operation.patch_index)
    return ("call", operation.attempt_id, operation.call_id or f"event:{operation.event_id}", index)


def scan_operations(rows: Iterable[Mapping[str, Any]], *, thread_id: str) -> ScanResult:
    """One pass over the scan rows, deduplicated by operation identity.

    Every row is read once and in ledger order, so the same call recorded twice
    (a mutable bridge update) collapses, while a second call with its own id
    does not.
    """
    materialized = list(rows)
    cwds = _cwd_rows(materialized)
    result = ScanResult(thread_id=thread_id)
    seen: set[tuple[Any, ...]] = set()
    for row in materialized:
        attempt_id = int(row["attempt_id"])
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        payload = {
            **payload,
            "created_at": row.get("created_at") or payload.get("created_at"),
            "result_id": row.get("result_id"),
            "result_error": row.get("result_error"),
            "result_image": row.get("result_image"),
        }
        if row.get("event_type") == "lifecycle":
            continue
        if row.get("event_type") == "raw":
            built = codex_operations(payload, cwds.get(attempt_id))
        else:
            built = tool_operations(payload, cwds.get(attempt_id))
        for index, operation in enumerate(built):
            key = _operation_key(operation, index)
            if key in seen:
                continue
            seen.add(key)
            result.operations.append(operation)
    result.events_scanned = len(materialized)
    result.attempts = len({int(row["attempt_id"]) for row in materialized})
    result.turns = len(
        {str(row["job_id"]) for row in materialized if row.get("job_id") is not None}
        or {_payload_job(row) for row in materialized if _payload_job(row)}
    )
    return result
