"""Group recorded operations into files, then filter, sort and page them.

Grouping is by the normalized path string, so two spellings of one recorded
path (``./a.py`` and ``a.py`` under a recorded cwd) are one file, while two
different roots that end the same way stay apart. Every number on a row is
summed from operations that record it: a successful operation's counts, a
known delta; an unknown delta increments the row's unknown counter instead of
being read as zero.

Ordering is total: each sort has a numeric key and the path as its tie-break,
so the same data always yields the same page, and a cursor names exactly the
row it stopped on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cloud_agent.file_activity.cursor import encode_cursor
from cloud_agent.file_activity.models import (
    FILTERS,
    FileActivityCoverage,
    FileActivityCursor,
    FileActivityRow,
    FileActivityTotals,
    FileActivityView,
    FileOperation,
    FileOperationsPage,
    FilterKey,
    OperationKind,
    OperationsCursor,
    SortKey,
)
from cloud_agent.file_activity.normalize import file_labels

if TYPE_CHECKING:
    from collections.abc import Sequence


def _epoch(value: datetime | None) -> int:
    if value is None:
        return 0
    return int(value.timestamp() * 1000)


#: The column each non-successful status increments.
_STATUS_FIELD = {"failed": "failed", "pending": "pending", "not_recorded": "not_recorded"}


def _matches(row: FileActivityRow, filter_key: FilterKey) -> bool:
    if filter_key == "read":
        return row.reads > 0
    if filter_key == "written":
        return row.writes > 0
    if filter_key == "searched":
        return row.searches > 0
    if filter_key == "images":
        return row.images > 0
    return True


def _sort_number(row: FileActivityRow, sort: SortKey) -> int:
    if sort == "active":
        return row.operations
    if sort == "lines":
        return row.added + row.removed
    if sort == "latest":
        return _epoch(row.last_at)
    return 0


def order_key(row: FileActivityRow, sort: SortKey) -> tuple[int, str, str]:
    """The total order one sort defines: number descending, then text, then path.

    The path is always the final component, so two rows that tie on the sort's
    own key still have one fixed order.
    """
    number = _sort_number(row, sort)
    if sort == "name":
        return (0, row.name.casefold(), row.path)
    if sort == "path":
        return (0, row.path, row.path)
    return (-number, "", row.path)


def _after_cursor(row: FileActivityRow, cursor: FileActivityCursor | None, sort: SortKey) -> bool:
    """Whether ``row`` sorts strictly after the row a cursor names."""
    if cursor is None:
        return True
    return order_key(row, sort) > (cursor.sort_number, cursor.key, cursor.path)


def _cursor_for(row: FileActivityRow, sort: SortKey, scope: str) -> FileActivityCursor:
    number, text, path = order_key(row, sort)
    return FileActivityCursor(
        scope=scope,
        sort_number=number,
        key=text,
        path=path,
        last_at=row.last_at,
    )


def _coverage(
    operations: Sequence[FileOperation],
    *,
    turns: int,
    attempts: int,
    events_scanned: int,
    capped: bool,
) -> FileActivityCoverage:
    times = [operation.at for operation in operations if operation.at is not None]
    return FileActivityCoverage(
        turns=turns,
        attempts=attempts,
        events_scanned=events_scanned,
        events_capped=capped,
        window_first_at=min(times) if times else None,
        window_last_at=max(times) if times else None,
    )


class _Row:
    """One file's running totals, before it becomes a wire row."""

    __slots__ = (
        "added",
        "added_unknown",
        "attempt_set",
        "attempts",
        "events",
        "failed",
        "first_at",
        "images",
        "last_at",
        "not_recorded",
        "operations",
        "path",
        "pending",
        "reads",
        "removed",
        "removed_unknown",
        "searches",
        "subagent",
        "writes",
    )

    def __init__(self, path: str) -> None:
        self.path = path
        self.reads = self.writes = self.searches = self.images = 0
        self.failed = self.pending = self.not_recorded = 0
        self.added = self.removed = 0
        self.added_unknown = self.removed_unknown = 0
        self.operations = self.subagent = self.events = 0
        self.attempts = 0
        self.attempt_set: set[int] = set()
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None

    def add(self, operation: FileOperation) -> None:
        """Fold one operation in, counting only what it proves."""
        self.operations += 1
        self.events += 1 if operation.result_event_id is None else 2
        self.attempt_set.add(operation.attempt_id)
        self.attempts = len(self.attempt_set)
        if operation.subagent:
            self.subagent += 1
        self._times(operation.at)
        if operation.status != "succeeded":
            setattr(
                self,
                _STATUS_FIELD[operation.status],
                getattr(self, _STATUS_FIELD[operation.status]) + 1,
            )
            return
        self._kind_count(operation.kind)
        if operation.kind in {"write", "edit"}:
            self._deltas(operation)

    def _times(self, at: datetime | None) -> None:
        """Widen the recorded window with one operation's time."""
        if at is None:
            return
        if self.first_at is None or at < self.first_at:
            self.first_at = at
        if self.last_at is None or at > self.last_at:
            self.last_at = at

    def _kind_count(self, kind: OperationKind) -> None:
        """Count a successful operation under its kind's own column."""
        column = {"read": "reads", "search": "searches", "image": "images"}.get(kind, "writes")
        setattr(self, column, getattr(self, column) + 1)

    def _deltas(self, operation: FileOperation) -> None:
        """Add the lines the operation proves; a missing side is unknown."""
        if operation.added is None:
            self.added_unknown += 1
        else:
            self.added += operation.added
        if operation.removed is None:
            self.removed_unknown += 1
        else:
            self.removed += operation.removed

    def row(self) -> FileActivityRow:
        label, file_kind = file_labels(self.path)
        return FileActivityRow(
            path=self.path,
            name=self.path.rsplit("/", 1)[-1] or self.path,
            type_label=label,
            file_kind=file_kind,
            reads=self.reads,
            writes=self.writes,
            searches=self.searches,
            images=self.images,
            failed=self.failed,
            pending=self.pending,
            not_recorded=self.not_recorded,
            added=self.added,
            removed=self.removed,
            added_unknown_ops=self.added_unknown,
            removed_unknown_ops=self.removed_unknown,
            operations=self.operations,
            events=self.events,
            subagent_operations=self.subagent,
            attempts=len(self.attempt_set),
            first_at=self.first_at,
            last_at=self.last_at,
        )


def group_files(operations: Sequence[FileOperation]) -> list[FileActivityRow]:
    """One row per distinct normalized path, with nothing invented for the rest."""
    rows: dict[str, _Row] = {}
    for operation in operations:
        if operation.path is None:
            continue
        rows.setdefault(operation.path, _Row(operation.path)).add(operation)
    return [state.row() for state in rows.values()]


def _search_matches(row: FileActivityRow, q: str) -> bool:
    return not q or q.casefold() in row.path.casefold()


def search_files(rows: Sequence[FileActivityRow], q: str) -> list[FileActivityRow]:
    """Rows whose path contains the search, case-insensitively."""
    needle = q.strip().casefold()
    return [row for row in rows if _search_matches(row, needle)]


def filter_counts(rows: Sequence[FileActivityRow]) -> dict[str, int]:
    """How many files each filter would show under the current search."""
    return {key: sum(1 for row in rows if _matches(row, key)) for key in FILTERS}


def totals_for(
    rows: Sequence[FileActivityRow], operations: Sequence[FileOperation]
) -> FileActivityTotals:
    """What the current search's files hold, plus what could not be attributed.

    An operation with no usable path is counted in its status totals and in
    ``unknown_path_operations``: it happened, and hiding its outcome would
    understate failures exactly where the record is weakest.
    """
    unattributed = [op for op in operations if op.path is None]
    unknown_path = sum(1 for op in unattributed if op.kind != "search")
    unknown_search = sum(1 for op in unattributed if op.kind == "search")
    return FileActivityTotals(
        files=len(rows),
        # Every scanned operation, including one no file could be named for.
        operations=sum(row.operations for row in rows) + len(unattributed),
        reads=sum(row.reads for row in rows),
        writes=sum(row.writes for row in rows),
        searches=sum(row.searches for row in rows),
        images=sum(row.images for row in rows),
        added=sum(row.added for row in rows),
        removed=sum(row.removed for row in rows),
        added_unknown_files=sum(1 for row in rows if row.added_unknown_ops),
        removed_unknown_files=sum(1 for row in rows if row.removed_unknown_ops),
        failed=sum(row.failed for row in rows)
        + sum(1 for op in unattributed if op.status == "failed"),
        pending=sum(row.pending for row in rows)
        + sum(1 for op in unattributed if op.status == "pending"),
        not_recorded=sum(row.not_recorded for row in rows)
        + sum(1 for op in unattributed if op.status == "not_recorded"),
        unknown_path_operations=unknown_path,
        search_unknown_scope=unknown_search,
        subagent_operations=sum(row.subagent_operations for row in rows),
    )


def sort_rows(rows: Sequence[FileActivityRow], sort: SortKey) -> list[FileActivityRow]:
    """Sorted by the chosen key, with the path as the deterministic tie-break."""
    return sorted(rows, key=lambda row: order_key(row, sort))


def build_view(
    *,
    thread_id: str,
    operations: Sequence[FileOperation],
    turns: int,
    attempts: int,
    events_scanned: int,
    capped: bool,
    sort: SortKey,
    filter_key: FilterKey,
    q: str,
    cursor: FileActivityCursor | None,
    limit: int,
    scope: str,
    now: datetime | None = None,
) -> FileActivityView:
    """One page of the conversation's File Activity, with honest bounds.

    Totals and filter counts are computed over every scanned operation, not
    over the page; the page itself names the row it ended on so the next one
    continues without repeating or skipping a file.
    """
    rows = group_files(operations)
    searched = search_files(rows, q)
    counts = filter_counts(searched)
    matching = [row for row in searched if _matches(row, filter_key)]
    ordered = sort_rows(matching, sort)
    remaining = [row for row in ordered if _after_cursor(row, cursor, sort)]
    page = remaining[:limit]
    has_more = len(remaining) > len(page)
    return FileActivityView(
        thread_id=thread_id,
        generated_at=now or datetime.now(UTC),
        sort=sort,
        filter=filter_key,
        q=q,
        totals=totals_for(searched, operations),
        filter_counts=counts,
        coverage=_coverage(
            operations,
            turns=turns,
            attempts=attempts,
            events_scanned=events_scanned,
            capped=capped,
        ),
        files=page,
        has_more=has_more,
        next_cursor=encode_cursor(_cursor_for(page[-1], sort, scope))
        if has_more and page
        else None,
    )


def operation_order(operation: FileOperation) -> tuple[datetime, int]:
    """Oldest first within a file, by the recorded time then the event id."""
    return (operation.at or datetime.min.replace(tzinfo=UTC), operation.event_id)


def _after_operation(operation: FileOperation, cursor: OperationsCursor | None) -> bool:
    """Whether ``operation`` comes strictly before the cursor's position.

    The expansion reads newest first, so a page continues with what is older
    than the row it ended on.
    """
    if cursor is None:
        return True
    at = operation.at or datetime.min.replace(tzinfo=UTC)
    marker = cursor.at or datetime.min.replace(tzinfo=UTC)
    return (at, operation.event_id) < (marker, cursor.event_id)


def build_operations_page(
    *,
    thread_id: str,
    path: str,
    operations: Sequence[FileOperation],
    cursor: OperationsCursor | None,
    limit: int,
    scope: str,
) -> FileOperationsPage:
    """One file's operations, newest first, paged by time then event id.

    A repeated call with the same id is already one operation; two calls that
    name the same file are two, so retries and re-reads stay visible.
    """
    matching = sorted(
        (operation for operation in operations if operation.path == path),
        key=operation_order,
        reverse=True,
    )
    remaining = [operation for operation in matching if _after_operation(operation, cursor)]
    page = remaining[:limit]
    has_earlier = len(remaining) > len(page)
    last = page[-1] if page else None
    return FileOperationsPage(
        thread_id=thread_id,
        path=path,
        total=len(matching),
        operations=page,
        has_earlier=has_earlier,
        next_cursor=encode_cursor(
            OperationsCursor(scope=scope, path=path, at=last.at, event_id=last.event_id)
        )
        if has_earlier and last is not None
        else None,
    )
