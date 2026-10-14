"""Workspace files, Git status, terminals, and operation profiling responses."""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentWorkspaceEntry(BaseModel):
    """One safe child in an agent job's read-only workspace browser."""

    name: str
    path: str
    kind: Literal["file", "directory", "symlink"]
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None


class AgentOperationProfilingFragment(BaseModel):
    """Which selected measurement produced this response, and how complete it is.

    Present only while an experiment has selected this operation. A client that
    does not know the field sees no change, and a client that does can carry
    ``interaction_id`` into the next request of the same interaction and report
    its own endpoint separately -- the browser's clock is never compared with
    the server's, the two are correlated through that id.

    Two completeness questions are kept apart on purpose. The ``fragment_*``
    pair describes the downstream call this reply came from; ``collection_complete``
    describes this whole local record. A reader must not have to infer either
    from the other, and a fragment can be answered and still be a floor.
    """

    #: One logical interaction. Every request of it shares this id.
    interaction_id: str = Field(..., min_length=1, max_length=64)
    #: This request's own record id. Never reused across requests.
    measurement_id: str = Field(..., min_length=1, max_length=64)
    #: The downstream call this reply answers, when one answered.
    call_id: str | None = Field(None, min_length=1, max_length=64)
    #: The operation the request was filed under, once its result was known.
    operation: str = Field("", max_length=128)
    #: Which local request this was, so a distribution does not mix a listing
    #: with a file preview or a terminal attach.
    request_kind: str = Field("", max_length=128)
    #: Whether the sandbox manager finished collecting for that call. Distinct from
    #: having timed something: a stage that legitimately did no timed work --
    #: a reuse, an empty directory -- answers without stages and is complete.
    fragment_recorded: bool = False
    #: Whether that collection was cut short by a deadline. An empty stage list
    #: means "collected and nothing to time" only when this is false.
    fragment_truncated: bool = False
    #: Whether this local record closed every boundary, owed no unanswered
    #: fragment and lost no span. A failed operation can still be complete.
    collection_complete: bool = False


class AgentWorkspaceResponse(BaseModel):
    """A directory listing or bounded file payload from a job workspace."""

    path: str
    kind: Literal["file", "directory", "symlink"]
    entries: list[AgentWorkspaceEntry] | None = None
    content: str | None = None
    # Preserve directory/text response shapes when there are no binary bytes.
    content_base64: str | None = Field(None, exclude_if=lambda value: value is None)
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"] | None = Field(
        None, exclude_if=lambda value: value is None
    )
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None
    writable: bool = False
    source: Literal["workspace"] = "workspace"
    profiling: AgentOperationProfilingFragment | None = None


class AgentWorkspaceWriteRequest(BaseModel):
    """Replace one UTF-8 file in a retained workspace."""

    content: str = Field(..., max_length=2 * 1024 * 1024)


class AgentTerminalRequest(BaseModel):
    """Execute one user-entered shell command in the workspace sandbox."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(60.0, gt=0, le=120)


class AgentTerminalResponse(BaseModel):
    """Bounded output and resulting working directory for one command."""

    output: str
    stderr: str
    exit_code: int
    cwd: str


_MAX_TERMINAL_INPUT_BYTES = 64 * 1024
_MAX_TERMINAL_INPUT_BASE64_CHARS = 4 * ((_MAX_TERMINAL_INPUT_BYTES + 2) // 3)


class AgentTerminalSessionCreateRequest(BaseModel):
    """Open one interactive PTY after an agent run has settled."""

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class AgentTerminalSessionInputRequest(BaseModel):
    """Base64-encoded bytes to write to an interactive PTY."""

    data: str = Field(..., min_length=1, max_length=_MAX_TERMINAL_INPUT_BASE64_CHARS)

    @field_validator("data")
    @classmethod
    def valid_bounded_base64(cls, value: str) -> str:
        """Reject malformed or oversized input before it reaches the sandbox manager."""
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be valid base64") from exc
        if len(decoded) > _MAX_TERMINAL_INPUT_BYTES:
            raise ValueError("decoded terminal input must be at most 65536 bytes")
        return value


class AgentTerminalSessionResizeRequest(BaseModel):
    """Resize an interactive PTY."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


class AgentTerminalSessionResponse(BaseModel):
    """Owner-safe metadata for one interactive terminal session."""

    id: str = Field(..., pattern=r"^term_[A-Za-z0-9_-]{1,80}$")
    shell: str = Field(..., min_length=1, max_length=4096)
    state: str = Field(..., min_length=1, max_length=32)
    cwd: str = Field(..., min_length=1, max_length=4096)
    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)
    last_seq: int = Field(..., ge=0)


# Deliberately no profiling field here. This model is the terminal *item*
# inside a list as well as the reply to a create, and it is reused by
# routes that are not instrumented (input, resize, delete). A field on it
# would put ``profiling: null`` on every nested item and on those replies,
# changing the contract for requests that were never measured. The measured
# routes add their block to the serialized body instead.


class AgentTerminalSessionListResponse(BaseModel):
    """Interactive terminal sessions retained by one live workspace."""

    terminals: list[AgentTerminalSessionResponse] = Field(default_factory=list)
    #: The owning sandbox manager's per-workspace terminal limit. Absent when the sandbox manager
    #: predates advertising it; the browser then assumes its own default.
    max_sessions: int | None = None
    #: The most input bytes the owning sandbox manager accepts in one request.
    max_input_bytes: int | None = None
    #: Present only on a measured list request; the instrumented handler adds
    #: it after serializing, so an unmeasured reply has no key at all.
    profiling: AgentOperationProfilingFragment | None = None


class AgentGitChange(BaseModel):
    """One worktree status row."""

    code: str
    path: str


class AgentGitCommit(BaseModel):
    """One recent commit visible from the worktree."""

    sha: str
    short_sha: str
    subject: str
    author: str
    authored_at: str


class AgentGitWorkspaceResponse(BaseModel):
    """Live diff, status, and commit data from the retained workspace."""

    available: bool
    branch: str = ""
    changes: list[AgentGitChange] = Field(default_factory=list)
    patch: str = ""
    commits: list[AgentGitCommit] = Field(default_factory=list)
