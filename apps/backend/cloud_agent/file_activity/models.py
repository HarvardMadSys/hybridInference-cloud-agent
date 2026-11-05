"""The wire shapes of a conversation's File Activity.

Every field says which kind of number it carries, and the module keeps the
distinctions the feature promises: a count of successfully recorded
operations, cumulative recorded line churn, a bounded scan window, and a
delta the record did not carry (``None``, never zero).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves the model fields.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OperationKind = Literal["read", "write", "edit", "search", "image"]
#: ``pending``: the attempt is still running and no result is stored.
#: ``not_recorded``: the attempt ended without storing a result.
OperationStatus = Literal["succeeded", "failed", "pending", "not_recorded"]
PathBasis = Literal["absolute", "cwd_relative", "relative", "unknown"]
FileKind = Literal["code", "data", "markup", "image", "shell", "text", "other"]
SortKey = Literal["active", "lines", "latest", "path", "name"]
FilterKey = Literal["all", "read", "written", "searched", "images"]

#: Rows one files page returns, and the most it may be asked for.
DEFAULT_FILE_PAGE = 25
MAX_FILE_PAGE = 100
#: Operations one expansion returns, and the most it may be asked for.
DEFAULT_OPERATION_PAGE = 40
MAX_OPERATION_PAGE = 200
#: Characters of a recorded input text this read asks Postgres for. A longer
#: write, edit or patch is marked clipped, and its delta stays unknown.
MAX_RECORDED_TEXT = 60_000
#: Stored event rows one read scans before reporting that it stopped at a bound.
MAX_SCAN_EVENTS = 20_000
#: Events one page of that scan moves.
EVENT_PAGE = 500
#: Characters a path search may name.
MAX_SEARCH_CHARS = 200

SORTS: tuple[SortKey, ...] = ("active", "lines", "latest", "path", "name")
FILTERS: tuple[FilterKey, ...] = ("all", "read", "written", "searched", "images")


class FileOperation(BaseModel):
    """One recorded operation on one file, or one search of unknown scope."""

    model_config = ConfigDict(extra="forbid")

    #: Stable identity: the call's own recorded id where it has one.
    call_id: str | None = None
    event_id: int
    result_event_id: int | None = None
    attempt_id: int
    attempt_no: int
    job_id: str
    turn_no: int
    seq: int
    at: datetime | None = None
    tool: str
    action: str
    kind: OperationKind
    status: OperationStatus
    path: str | None = None
    name: str | None = None
    path_basis: PathBasis = "unknown"
    #: Lines the recorded content or patch adds and removes; None when the
    #: record did not carry enough to compute it.
    added: int | None = None
    removed: int | None = None
    #: One section of a multi-file patch: its position and the patch's size.
    patch_index: int | None = None
    patch_files: int | None = None
    renamed_to: str | None = None
    #: A delegated agent produced it, not the conversation's own agent.
    subagent: bool = False
    #: The attempt a retry replaced: listed, never silently dropped or counted.
    superseded: bool = False
    #: The recorded content was clipped, so its delta is unknown.
    clipped: bool = False


class FileActivityRow(BaseModel):
    """One file's recorded activity over the conversation's scanned attempts."""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    #: A badge from the extension: a label, never evidence of activity.
    type_label: str
    file_kind: FileKind
    reads: int = Field(ge=0)
    writes: int = Field(ge=0)
    searches: int = Field(ge=0)
    images: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(ge=0)
    not_recorded: int = Field(ge=0)
    #: Cumulative recorded churn: a write's content, an edit's own strings, a
    #: patch's hunk lines, added up across events. Not the current Git diff.
    added: int = Field(ge=0)
    removed: int = Field(ge=0)
    #: Successful write/edit operations whose line count the record did not carry.
    added_unknown_ops: int = Field(ge=0)
    removed_unknown_ops: int = Field(ge=0)
    operations: int = Field(ge=0)
    events: int = Field(ge=0)
    subagent_operations: int = Field(ge=0)
    attempts: int = Field(ge=0)
    first_at: datetime | None = None
    last_at: datetime | None = None


class FileActivityTotals(BaseModel):
    """What the scanned window holds — a window, not a claim about all history."""

    model_config = ConfigDict(extra="forbid")

    files: int = Field(ge=0)
    operations: int = Field(ge=0)
    reads: int = Field(ge=0)
    writes: int = Field(ge=0)
    searches: int = Field(ge=0)
    images: int = Field(ge=0)
    added: int = Field(ge=0)
    removed: int = Field(ge=0)
    #: Files carrying at least one successful write/edit with an unknown side.
    added_unknown_files: int = Field(ge=0)
    removed_unknown_files: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(ge=0)
    not_recorded: int = Field(ge=0)
    #: File-shaped calls (and searches) with no usable path: counted, unnamed.
    unknown_path_operations: int = Field(ge=0)
    #: Recorded searches whose scope names no file: counted, attached to none.
    search_unknown_scope: int = Field(ge=0)
    subagent_operations: int = Field(ge=0)


class FileActivityCoverage(BaseModel):
    """What the read covered, so a bounded window is never called the whole."""

    model_config = ConfigDict(extra="forbid")

    turns: int = Field(ge=0)
    attempts: int = Field(ge=0)
    events_scanned: int = Field(ge=0)
    #: The scan stopped at its bound: older operations are not in this view.
    events_capped: bool = False
    window_first_at: datetime | None = None
    window_last_at: datetime | None = None


class FileActivitySemantics(BaseModel):
    """What the numbers are — and are not — so nothing implies the rest."""

    model_config = ConfigDict(extra="forbid")

    line_changes: Literal["cumulative_recorded"] = "cumulative_recorded"
    net_git_diff: Literal["not_read"] = "not_read"
    search_scope: Literal["explicit_path_only"] = "explicit_path_only"
    image_evidence: Literal["explicit_only"] = "explicit_only"
    counts: Literal["successful_operations"] = "successful_operations"


class FileActivityView(BaseModel):
    """One page of the conversation's File Activity, with its own bounds."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    generated_at: datetime
    sort: SortKey
    filter: FilterKey
    q: str
    totals: FileActivityTotals
    #: Files each filter would show under the current search.
    filter_counts: dict[str, int]
    coverage: FileActivityCoverage
    semantics: FileActivitySemantics = Field(default_factory=FileActivitySemantics)
    files: list[FileActivityRow] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class FileOperationsPage(BaseModel):
    """One page of one file's recorded operations, newest first."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    path: str
    total: int = Field(ge=0)
    operations: list[FileOperation] = Field(default_factory=list)
    has_earlier: bool = False
    next_cursor: str | None = None


class FileActivityCursor(BaseModel):
    """Where a page ended, so the next one continues exactly.

    The tuple is the page's own total order: its numeric key, its text key and
    the path that always breaks a tie.
    """

    model_config = ConfigDict(extra="forbid")

    scope: str
    sort_number: int = 0
    key: str = ""
    path: str
    last_at: datetime | None = None


class OperationsCursor(BaseModel):
    """Where an expansion page ended, by the operation's own order."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    path: str
    at: datetime | None = None
    event_id: int
