"""Command executions: what an agent ran, parsed from recorded events only.

A command execution is one command-bearing tool call and the result that
answers it, matched by the call identity the runtime recorded
(``attempt_id`` + the call's id), never by the command text: the same text run
twice is two executions. A repeated tool event carrying an identity already
seen is the same execution reported again and is merged, not counted twice.
A ``write_stdin``/poll event has no command of its own; when it names the
session of a running execution its result updates that execution instead of
becoming a new one.

Nothing here executes, expands or shells out. The command text is tokenized
just enough to name the program that a reader would call the command's
subject: launchers (``env``, ``sudo``, ``timeout``, ``nohup`` …), leading
assignments and ``cd``/``pushd`` navigation are skipped, pipelines contribute
their first program, and a construct the parser cannot resolve is grouped
under ``shell`` rather than guessed. A program is never invented.

Ownership, bounds and pagination live in the query and the API route; this
module decides what a recorded row means. Statuses keep the tool's and the
command's outcomes apart:

- ``failed``: the runtime marked the call an error, or the command's own exit
  code was recorded and is not zero;
- ``completed``: a result was recorded and neither of those happened, even
  when the exit code was not recorded (missing is not zero);
- ``running``: the attempt is still running and no result was recorded;
- ``unknown``: no result was recorded and the attempt is no longer running
  (a crash, a cancellation, a superseded attempt).

Durations are the tool-call duration the platform measured for that call when
one was recorded (``recorded``); otherwise the interval between the call and
its result events as the platform received them (``events``), which includes
harness lag and is labelled as such. With neither, the duration is absent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import decode_cursor, encode_cursor

#: A command at or over this many milliseconds is "slow".
#:
#: Ten seconds is the convention the Context dashboard's reference
#: implementation (bowenliang123/dsh-context, Apache-2.0) already uses for the
#: same filter, and it is published in the response as ``slow_cutoff_ms`` and
#: in the UI's filter tooltip, so the cutoff is discoverable rather than magic.
SLOW_COMMAND_MS = 10_000

#: Characters of a command this read keeps. A longer recorded command is
#: parsed and searched up to this bound and reported as truncated.
MAX_COMMAND_CHARS = 8_000
#: Characters of a result shown without asking for the event's own window.
MAX_OUTPUT_PREVIEW = 2_000
#: Command executions one read assembles. Beyond it the newest are covered and
#: the response says how many there are in total; a capped subset is never
#: presented as the whole conversation.
MAX_COMMAND_EXECUTIONS = 2_000
#: Results and polls fetched beside the covered calls.
MAX_RELATED_EVENTS = 20_000
#: Programs listed in the histogram.
MAX_PROGRAMS = 200
#: Characters of a program name retained.
MAX_PROGRAM_CHARS = 120
#: Rows one page returns.
MAX_COMMAND_PAGE = 200
DEFAULT_COMMAND_PAGE = 50
#: Characters of a command search accepts.
MAX_COMMAND_SEARCH = 200

CommandStatus = Literal["running", "completed", "failed", "unknown"]
CommandFilter = Literal["all", "failed", "slow", "background"]
CommandSort = Literal["latest", "slowest"]
DurationBasis = Literal["recorded", "events"]
ParseBasis = Literal["executable", "shell"]
CUMULATIVE_BASIS = Literal["recorded", "mixed", "events", "none"]

#: Runtimes whose recorded command tool the platform recognizes.
SUPPORTED_RUNTIMES = ("claude-code", "codex", "pi", "opencode", "kilo")

#: Recorded command tool names, per runtime, lower-cased. A name outside the
#: runtime's set is not assumed to be a command: an unsupported format stays
#: unavailable instead of being rendered as one.
_COMMAND_TOOLS: dict[str, frozenset[str]] = {
    "claude-code": frozenset({"bash"}),
    "codex": frozenset({"commandexecution", "exec_command", "shell", "local_shell"}),
    "pi": frozenset({"bash"}),
    "opencode": frozenset({"bash"}),
    "kilo": frozenset({"bash"}),
}
#: Names treated as commands for a runtime this module does not know.
_GENERIC_COMMAND_TOOLS = frozenset({"bash", "shell", "commandexecution", "exec_command"})
#: Tool names that continue or read a command session rather than start one.
_POLL_TOOLS = frozenset(
    {"bashoutput", "writestdin", "write_stdin", "commandoutput", "poll", "shell_output"}
)
#: Input keys a recorded command may sit under, in the order tried.
_COMMAND_KEYS = ("command", "cmd")
CONTEXT_COMMANDS_CURSOR_VERSION = 1


def command_tool_names() -> tuple[str, ...]:
    """Every recorded tool name the query should fetch, for all runtimes."""
    names = set(_GENERIC_COMMAND_TOOLS)
    for tools in _COMMAND_TOOLS.values():
        names.update(tools)
    return tuple(sorted(names))


def poll_tool_names() -> tuple[str, ...]:
    """Recorded tool names that continue an existing command session."""
    return tuple(sorted(_POLL_TOOLS))


class CommandExecution(BaseModel):
    """One command execution, with only what its records carried."""

    model_config = ConfigDict(extra="forbid")

    #: Stable row identity: the attempt and the call's recorded id.
    id: str
    job_id: str
    turn_no: int = Field(ge=0)
    attempt_id: int
    attempt_no: int = Field(ge=0)
    runtime: str
    tool: str
    #: The command text the record carried, bounded and marked truncated.
    command: str
    command_chars: int = Field(ge=0)
    command_truncated: bool
    #: The recorded working directory, when the record carried one.
    cwd: str | None = None
    status: CommandStatus
    #: The command's own exit code; absent is unknown, never zero.
    exit_code: int | None = None
    #: The runtime marked this call an error (a tool error, not an exit code).
    tool_error: bool = False
    #: Detached/background only from recorded evidence, never from shell text.
    background: bool = False
    #: From an attempt a retry replaced; listed, still a command that ran.
    superseded: bool = False
    duration_ms: int | None = Field(None, ge=0)
    duration_basis: DurationBasis | None = None
    at: datetime | None = None
    result_at: datetime | None = None
    event_id: int | None = None
    result_event_id: int | None = None
    #: A bounded preview; the full result is the result event's own window.
    output_preview: str = ""
    output_chars: int = Field(0, ge=0)
    output_truncated: bool = False
    program: str
    parse_basis: ParseBasis


class CommandProgram(BaseModel):
    """One parsed program and its share of the covered executions."""

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int = Field(ge=0)
    share: float = Field(ge=0, le=1)
    #: The group unresolvable commands fall under, not a program that ran.
    fallback: bool = False


class CommandSummary(BaseModel):
    """Cumulative totals over every covered execution, independent of filters."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    #: Executions with a settled outcome: completed + failed.
    settled: int = Field(ge=0)
    failed: int = Field(ge=0)
    #: failed / settled; null when nothing settled (the denominator is empty).
    failure_rate: float | None = Field(None, ge=0, le=1)
    running: int = Field(ge=0)
    unknown: int = Field(ge=0)
    #: Sum of the durations actually available, not the session's wall time.
    cumulative_ms: int = Field(ge=0)
    durations_known: int = Field(ge=0)
    durations_missing: int = Field(ge=0)
    duration_basis: CUMULATIVE_BASIS
    background: int = Field(ge=0)


class CommandCounts(BaseModel):
    """Filter counts over the searched/program-narrowed executions."""

    model_config = ConfigDict(extra="forbid")

    all: int = Field(ge=0)
    failed: int = Field(ge=0)
    slow: int = Field(ge=0)
    background: int = Field(ge=0)


class CommandCoverage(BaseModel):
    """What this read covered, so a bounded subset is never the whole story."""

    model_config = ConfigDict(extra="forbid")

    #: Command calls the conversation records in total (counted in the database).
    executions_total: int = Field(ge=0)
    executions_covered: int = Field(ge=0)
    #: Covered fewer than recorded: the newest ``executions_covered`` were read.
    truncated: bool
    turns: int = Field(ge=0)
    attempts: int = Field(ge=0)
    #: Fork-copied turns are another conversation's commands; not counted here.
    copied_jobs_excluded: int = Field(ge=0)
    #: Runtimes whose command format this build does not recognize.
    unsupported_runtimes: list[str] = Field(default_factory=list)


class CommandAttention(BaseModel):
    """The executions worth a second look, over the same narrowed set."""

    model_config = ConfigDict(extra="forbid")

    slowest: CommandExecution | None = None
    failing: CommandProgram | None = None
    repeats: dict[str, Any] | None = None
    background: int = Field(ge=0)


class CommandPage(BaseModel):
    """The rows this page carries and how many the narrowed set has."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    limit: int = Field(ge=1)
    next_cursor: str | None = None


class CommandExecutions(BaseModel):
    """One conversation's command executions and their cumulative totals."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    generated_at: datetime
    slow_cutoff_ms: int = SLOW_COMMAND_MS
    summary: CommandSummary
    coverage: CommandCoverage
    counts: CommandCounts
    attention: CommandAttention
    programs: list[CommandProgram]
    executions: list[CommandExecution]
    page: CommandPage
    filter: CommandFilter
    sort: CommandSort
    q: str = ""
    program: str | None = None


class CommandCursor(BaseModel):
    """The keyset position of the last row a page returned."""

    model_config = ConfigDict(extra="forbid")

    v: int = CONTEXT_COMMANDS_CURSOR_VERSION
    scope: str
    event_id: int
    duration_ms: int | None = None


def command_cursor_scope(
    thread_id: str, *, filter: str, sort: str, q: str, program: str | None
) -> str:
    """A cursor is bound to the list it was issued for."""
    return f"{thread_id}|{filter}|{sort}|{q}|{program or ''}"


# ---------------------------------------------------------------------------
# Shell text → program (pure tokenization; nothing is executed or expanded)
# ---------------------------------------------------------------------------

#: Launchers in front of the real program, and the options that take a value.
_LAUNCHERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset(
        {
            "-u",
            "--user",
            "-g",
            "--group",
            "-p",
            "--prompt",
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-h",
            "--host",
            "-r",
            "--role",
            "-t",
            "--type",
            "-U",
            "--other-user",
            "-R",
            "--chroot",
        }
    ),
    "doas": frozenset({"-u", "-C"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "noglob": frozenset(),
    "nocorrect": frozenset(),
    "builtin": frozenset(),
}
#: Shell builtins that run no program of their own; a segment made of one is
#: navigation/setup, and the next segment is the command's subject.
_SETUP_BUILTINS = frozenset(
    {
        "cd",
        "pushd",
        "popd",
        "export",
        "local",
        "declare",
        "readonly",
        "typeset",
        "unset",
        "set",
        "shopt",
        "umask",
        "alias",
        "unalias",
        "trap",
        "source",
        ".",
    }
)
#: Shell control keywords: a construct rather than one program.
_CONTROL = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
        "fi",
    }
)
#: Tokens that end a command segment.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&", "\n"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*$")
#: A ``timeout`` duration: bare seconds or a GNU time spec.
_DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")
#: A program name made of an expansion cannot be named without running it.
_EXPANSION = re.compile(r"[$`]")


@dataclass(frozen=True)
class _Token:
    text: str
    #: True for a separator token.
    separator: bool = False


def _quoted(command: str, index: int, quote: str, word: list[str]) -> int:
    """Consume a quoted stretch into ``word``; return the index after it."""
    length = len(command)
    index += 1
    while index < length:
        char = command[index]
        if char == quote:
            return index + 1
        if char == "\\" and quote == '"' and index + 1 < length:
            index += 1
            word.append(command[index])
        else:
            word.append(char)
        index += 1
    return index


def _separator(command: str, index: int) -> tuple[str, int] | None:
    """A separator token at ``index`` and the index after it, if there is one."""
    char = command[index]
    if char == "\n":
        return "\n", index + 1
    if char not in "&|;":
        return None
    if command.startswith("&&", index) or command.startswith("||", index):
        return command[index : index + 2], index + 2
    if command.startswith("|&", index):
        return "|&", index + 2
    return char, index + 1


def _tokenize(command: str) -> tuple[list[_Token], bool]:
    """Split a shell command into words and separators, respecting quotes.

    Returns the tokens and whether the command contains a heredoc redirection.
    No substitution is performed; a quoted word keeps its literal text.
    """
    tokens: list[_Token] = []
    word: list[str] = []
    heredoc = False
    index = 0
    length = len(command)

    def flush() -> None:
        if word:
            tokens.append(_Token("".join(word)))
            word.clear()

    while index < length:
        char = command[index]
        if char in {"'", '"'}:
            index = _quoted(command, index, char, word)
            continue
        if char == "\\" and index + 1 < length:
            if command[index + 1] == "\n":
                # A line continuation joins the words either side of it.
                index += 2
                continue
            word.append(command[index + 1])
            index += 2
            continue
        if char in {" ", "\t", "\r"}:
            flush()
            index += 1
            continue
        separator = _separator(command, index)
        if separator is not None:
            flush()
            text, index = separator
            tokens.append(_Token(text, separator=True))
            continue
        if char == "<" and command.startswith("<<", index):
            heredoc = True
        if char in {"(", ")", "{", "}"}:
            # Grouping punctuation belongs to the construct around the program.
            flush()
            tokens.append(_Token(char))
            index += 1
            continue
        word.append(char)
        index += 1
    flush()
    return tokens, heredoc


def _skip_launcher(tokens: Sequence[_Token], start: int) -> int | None:
    """Where the program after a launcher prefix starts, or None if malformed."""
    index = start + 1
    options = _LAUNCHERS[tokens[start].text.lower()]
    while index < len(tokens):
        token = tokens[index]
        if token.separator:
            return None
        text = token.text
        if text == "--":
            return index + 1
        if text.startswith("-") and text != "-":
            if text in options:
                index += 2
            else:
                index += 1
            continue
        if tokens[start].text.lower() == "env" and _ASSIGNMENT.match(text):
            index += 1
            continue
        if tokens[start].text.lower() == "timeout" and _DURATION.match(text):
            # ``timeout 30 npm``: the duration is the launcher's, not a program.
            index += 1
            continue
        return index
    return None


def _resolve_program(tokens: Sequence[_Token], index: int) -> tuple[str, ParseBasis]:
    """Walk from ``index`` to the program a reader would name, or the shell group."""
    while index < len(tokens):
        token = tokens[index]
        text = token.text
        if token.separator or text in {")", "}"}:
            index += 1
            continue
        if text in _CONTROL:
            return "shell", "shell"
        lowered = text.lower()
        # Navigation and assignment are setup, never the program.
        if _ASSIGNMENT.match(text):
            index += 1
            continue
        if lowered in _SETUP_BUILTINS:
            while index < len(tokens) and not tokens[index].separator:
                index += 1
            continue
        if lowered in _LAUNCHERS:
            next_index = _skip_launcher(tokens, index)
            if next_index is None:
                return "shell", "shell"
            # What follows a launcher can itself be another launcher.
            index = next_index
            continue
        if text in {"[", "[[", "test", "eval"}:
            return "shell", "shell"
        if _EXPANSION.search(text) or text.startswith("-") or not text:
            return "shell", "shell"
        # A path names its executable by its last component.
        name = text.rstrip("/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > MAX_PROGRAM_CHARS or not _PROGRAM.match(name):
            return "shell", "shell"
        return name, "executable"
    return "shell", "shell"


def parse_program(command: str) -> tuple[str, ParseBasis]:
    """The program a reader would call the subject of ``command``.

    Returns the program name and whether it is an executable or the ``shell``
    group an unresolvable construct falls under. Navigation, assignments and
    launchers are skipped only when the parser has evidence of a following
    command; a command whose head is only ``cd`` reports ``shell``, not ``cd``.
    """
    text = command.strip()
    if not text:
        return "shell", "shell"
    tokens, _heredoc = _tokenize(text)
    index = 0
    # Optional leading grouping or negation before the subject.
    while (
        index < len(tokens)
        and not tokens[index].separator
        and tokens[index].text in {"!", "(", "{"}
    ):
        index += 1
    return _resolve_program(tokens, index)


# ---------------------------------------------------------------------------
# Recorded rows → executions
# ---------------------------------------------------------------------------


@dataclass
class _Execution:
    """Mutable working state of one execution while its records are read."""

    id: str
    job_id: str
    turn_no: int
    attempt_id: int
    attempt_no: int
    runtime: str
    tool: str
    command: str = ""
    command_chars: int = 0
    cwd: str | None = None
    event_id: int | None = None
    result_event_id: int | None = None
    at: datetime | None = None
    result_at: datetime | None = None
    is_error: bool = False
    exit_code: int | None = None
    background: bool = False
    status: CommandStatus = "unknown"
    duration_ms: int | None = None
    duration_basis: DurationBasis | None = None
    output: str = ""
    output_chars: int = 0
    session_id: str | None = None
    result_seen: bool = False
    superseded: bool = False
    program: str = "shell"
    parse_basis: ParseBasis = "shell"


@dataclass
class CommandAssembly:
    """A conversation's covered executions, before any filter or page."""

    thread_id: str
    generated_at: datetime
    records: list[_Execution]
    executions_total: int
    turns: int
    attempts: int
    copied_jobs_excluded: int
    unsupported_runtimes: list[str] = field(default_factory=list)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,9}", value):
        return int(value)
    return None


def _truth(value: Any) -> bool:
    return value is True or value == "true"


def runtime_command_tools(runtime: str | None) -> frozenset[str] | None:
    """The command tool names recorded for ``runtime``, or None if unknown."""
    if runtime is None:
        return None
    return _COMMAND_TOOLS.get(runtime)


def _is_command_call(runtime: str | None, name: str, payload: Mapping[str, Any]) -> bool:
    """Whether a recorded tool call is a command this module can name.

    A runtime this build does not know has no verified command shape, so its
    calls are left out and the runtime is reported unsupported rather than
    guessed at from a tool name.
    """
    lowered = name.lower()
    tools = runtime_command_tools(runtime)
    if tools is None or lowered not in tools:
        return False
    return _command_text(payload) is not None


def _command_text(payload: Mapping[str, Any]) -> str | None:
    """The command text a call's input carried, without expanding anything.

    Accepts a projected row (``command_text``) as well as a stored payload, so
    the assembler and a caller testing it see the same text.
    """
    projected = payload.get("command_text")
    if isinstance(projected, str):
        return projected or None
    value = payload.get("command")
    if isinstance(value, str):
        return value
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        return raw_input or None
    if isinstance(raw_input, Mapping):
        for key in _COMMAND_KEYS:
            candidate = raw_input.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _recorded_duration(row: Mapping[str, Any]) -> tuple[int, str] | None:
    """The platform's own measured duration for a call, when it recorded one."""
    duration = row.get("duration_ms")
    if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
        return duration, str(row.get("timing_source") or "message")
    return None


def _event_duration(call: Mapping[str, Any], result: Mapping[str, Any]) -> int | None:
    """Call-to-result interval as the platform received the events."""
    started, ended = call.get("at"), result.get("at")
    if isinstance(started, datetime) and isinstance(ended, datetime) and ended >= started:
        return max(0, int((ended - started).total_seconds() * 1000))
    return None


def _merge_call(execution: _Execution, row: Mapping[str, Any]) -> None:
    """A repeated tool event of the same identity: fill gaps, never duplicate."""
    command = _command_text(row)
    if command and not execution.command:
        execution.command = command[:MAX_COMMAND_CHARS]
        execution.command_chars = int(row.get("command_chars") or len(command))
        execution.program, execution.parse_basis = parse_program(execution.command)
    if not execution.cwd and _text(row.get("cwd")):
        execution.cwd = str(row["cwd"])
    if not execution.session_id and _text(row.get("session_id")):
        execution.session_id = str(row["session_id"])
    if _truth(row.get("background")):
        execution.background = True


def _apply_result(execution: _Execution, row: Mapping[str, Any]) -> None:
    """A result, or a session poll's result, updates its execution in place."""
    if not execution.session_id and _text(row.get("session_id")):
        # The result that opened a session names it; later polls join on it.
        execution.session_id = str(row["session_id"])
    execution.result_seen = True
    execution.result_event_id = int(row["event_id"])
    execution.result_at = row.get("at")
    if _truth(row.get("is_error")):
        execution.is_error = True
    exit_code = _integer(row.get("exit_code"))
    if exit_code is not None:
        execution.exit_code = exit_code
    if _truth(row.get("background")):
        execution.background = True
    content = row.get("content")
    if isinstance(content, str) and content:
        execution.output = content
        execution.output_chars = int(row.get("content_chars") or len(content))
    # A poll that reports the session still running has not settled it.
    if row.get("status") == "running":
        execution.result_seen = False


def _final_status(execution: _Execution, job_state: str, attempt_status: str) -> CommandStatus:
    if execution.is_error or (execution.exit_code is not None and execution.exit_code != 0):
        return "failed"
    if execution.result_seen:
        return "completed"
    if attempt_status == "running" or job_state in {"queued", "running", "publishing"}:
        return "running"
    return "unknown"


def _is_poll(row: Mapping[str, Any]) -> bool:
    """A recorded continuation of a command session, not a new command."""
    if row.get("event_type") != "tool_use":
        return False
    name = str(row.get("tool_name") or "").lower()
    if name in _POLL_TOOLS:
        return True
    return not _text(row.get("command_text")) and bool(_text(row.get("session_id")))


def _call_execution(row: Mapping[str, Any], job: Mapping[str, Any], command: str) -> _Execution:
    """A new execution from the first recorded event of one call identity."""
    execution = _Execution(
        id=f"{int(row['attempt_id'])}:{_text(row.get('call_id')) or f'seq{row.get("seq")}'}",
        job_id=str(row["job_id"]),
        turn_no=int(row.get("turn_no") or 0),
        attempt_id=int(row["attempt_id"]),
        attempt_no=int(row.get("attempt_no") or 0),
        runtime=str(job.get("runtime") or "unknown"),
        tool=_text(row.get("tool_name")) or "",
        command=command[:MAX_COMMAND_CHARS],
        command_chars=int(row.get("command_chars") or len(command)),
        cwd=_text(row.get("cwd")),
        event_id=int(row["event_id"]),
        at=row.get("at"),
        background=_truth(row.get("background")),
        superseded=str(row.get("attempt_status") or "") == "superseded",
        session_id=_text(row.get("session_id")),
    )
    execution.program, execution.parse_basis = parse_program(execution.command)
    recorded = _recorded_duration(row)
    if recorded is not None:
        execution.duration_ms, execution.duration_basis = recorded[0], "recorded"
    return execution


@dataclass
class _Index:
    """The executions a projection's rows have produced so far."""

    records: list[_Execution] = field(default_factory=list)
    calls: dict[tuple[int, str], _Execution] = field(default_factory=dict)
    call_rows: dict[tuple[int, str], Mapping[str, Any]] = field(default_factory=dict)
    sessions: dict[tuple[int, str], _Execution] = field(default_factory=dict)
    poll_calls: dict[tuple[int, str], _Execution] = field(default_factory=dict)


def _index_calls(
    rows: Sequence[Mapping[str, Any]], jobs: Mapping[str, Mapping[str, Any]], index: _Index
) -> None:
    """Read every command call once; a repeated identity only fills gaps."""
    for row in rows:
        if _is_poll(row) or row.get("event_type") != "tool_use":
            continue
        job = jobs.get(str(row["job_id"]))
        if job is None:
            continue
        name = _text(row.get("tool_name")) or ""
        command = _text(row.get("command_text"))
        if command is None or not _is_command_call(str(job.get("runtime") or ""), name, row):
            continue
        identity = (int(row["attempt_id"]), _text(row.get("call_id")) or f"seq{row.get('seq')}")
        existing = index.calls.get(identity)
        if existing is not None:
            _merge_call(existing, row)
            index.call_rows.setdefault(identity, row)
            continue
        execution = _call_execution(row, job, command)
        index.calls[identity] = execution
        index.call_rows[identity] = row
        if execution.session_id:
            index.sessions.setdefault((execution.attempt_id, execution.session_id), execution)
        index.records.append(execution)


def _index_results(rows: Sequence[Mapping[str, Any]], index: _Index) -> None:
    """Fold results and session polls into the calls just indexed."""
    for row in rows:
        attempt_id = int(row["attempt_id"])
        if _is_poll(row):
            session_id = _text(row.get("session_id"))
            poll_id = _text(row.get("call_id"))
            if session_id is not None and poll_id is not None:
                execution = index.sessions.get((attempt_id, session_id))
                if execution is not None:
                    index.poll_calls.setdefault((attempt_id, poll_id), execution)
            continue
        if row.get("event_type") != "tool_result":
            continue
        call_id = _text(row.get("tool_use_id"))
        execution = index.calls.get((attempt_id, call_id)) if call_id else None
        if execution is None and call_id:
            execution = index.poll_calls.get((attempt_id, call_id))
        if execution is None:
            session_id = _text(row.get("session_id"))
            execution = index.sessions.get((attempt_id, session_id)) if session_id else None
        if execution is None:
            continue
        _apply_result(execution, row)
        if execution.session_id:
            index.sessions.setdefault((attempt_id, execution.session_id), execution)
        if execution.duration_ms is None:
            call_row = index.call_rows.get((attempt_id, execution.id.split(":", 1)[1]))
            if call_row is not None:
                execution.duration_ms = _event_duration(call_row, row)
                if execution.duration_ms is not None:
                    execution.duration_basis = "events"


def _settle(
    records: Sequence[_Execution],
    rows: Sequence[Mapping[str, Any]],
    jobs: Mapping[str, Mapping[str, Any]],
) -> None:
    """Apply each execution's recorded outcome, from its attempt and its turn."""
    attempt_status: dict[int, str] = {}
    for row in rows:
        attempt_status.setdefault(int(row["attempt_id"]), str(row.get("attempt_status") or ""))
    for execution in records:
        job = jobs.get(execution.job_id) or {}
        execution.status = _final_status(
            execution,
            str(job.get("state") or ""),
            attempt_status.get(execution.attempt_id, "running"),
        )


def assemble_command_executions(
    *,
    thread_id: str,
    generated_at: datetime,
    jobs: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    executions_total: int | None = None,
) -> CommandAssembly:
    """Match recorded calls to their results and assemble the covered executions.

    ``jobs`` are the conversation's turns (``job_id``, ``turn_no``, ``state``,
    ``runtime``, ``copied``). ``rows`` are the projected command calls, their
    results and the session polls, each with ``event_id``, ``attempt_id``,
    ``event_type``, ``at``, ``tool_name``, ``call_id``, ``tool_use_id``,
    ``session_id``, ``command_text`` and the outcome fields the query reads.

    A call and its result are matched by the recorded call identity, or by the
    session a poll continues; never by the command text. A result with no
    covered call adds nothing: a bounded read does not invent a command.
    """
    jobs_by_id = {str(job["job_id"]): job for job in jobs if not job.get("copied")}
    copied = sum(1 for job in jobs if job.get("copied"))
    unsupported = sorted(
        {
            str(job.get("runtime") or "unknown")
            for job in jobs
            if not job.get("copied") and job.get("runtime") not in SUPPORTED_RUNTIMES
        }
    )
    ordered = sorted(rows, key=lambda row: (int(row["event_id"]), int(row.get("seq") or 0)))
    index = _Index()
    # Calls first, so a result that arrived before its call in this projection
    # still finds it; then the results and polls that answer them.
    _index_calls(ordered, jobs_by_id, index)
    _index_results(ordered, index)
    _settle(index.records, ordered, jobs_by_id)
    turns = len({str(job["job_id"]) for job in jobs_by_id.values()})
    attempts = len({int(row["attempt_id"]) for row in rows if str(row["job_id"]) in jobs_by_id})
    return CommandAssembly(
        thread_id=thread_id,
        generated_at=generated_at,
        records=index.records,
        executions_total=executions_total if executions_total is not None else len(index.records),
        turns=turns,
        attempts=attempts,
        copied_jobs_excluded=copied,
        unsupported_runtimes=unsupported,
    )


def _public(record: _Execution) -> CommandExecution:
    command = record.command[:MAX_COMMAND_CHARS]
    output = record.output[:MAX_OUTPUT_PREVIEW]
    return CommandExecution(
        id=record.id,
        job_id=record.job_id,
        turn_no=record.turn_no,
        attempt_id=record.attempt_id,
        attempt_no=record.attempt_no,
        runtime=record.runtime,
        tool=record.tool,
        command=command,
        command_chars=record.command_chars,
        command_truncated=record.command_chars > len(command),
        cwd=record.cwd,
        status=record.status,
        exit_code=record.exit_code,
        tool_error=record.is_error,
        background=record.background,
        superseded=record.superseded,
        duration_ms=record.duration_ms,
        duration_basis=record.duration_basis,
        at=record.at,
        result_at=record.result_at,
        event_id=record.event_id,
        result_event_id=record.result_event_id,
        output_preview=output,
        output_chars=record.output_chars,
        output_truncated=record.output_chars > len(output),
        program=record.program,
        parse_basis=record.parse_basis,
    )


def _settled(record: _Execution) -> bool:
    return record.status in {"completed", "failed"}


def _slow(record: _Execution) -> bool:
    return record.duration_ms is not None and record.duration_ms >= SLOW_COMMAND_MS


def _matches_filter(record: _Execution, filter: CommandFilter) -> bool:
    if filter == "failed":
        return record.status == "failed"
    if filter == "slow":
        return _slow(record)
    if filter == "background":
        return record.background
    return True


def _summary(records: Sequence[_Execution]) -> CommandSummary:
    settled = sum(1 for record in records if _settled(record))
    failed = sum(1 for record in records if record.status == "failed")
    durations = [record.duration_ms for record in records if record.duration_ms is not None]
    bases = {record.duration_basis for record in records if record.duration_basis is not None}
    basis: CUMULATIVE_BASIS
    if not bases:
        basis = "none"
    elif len(bases) == 1:
        basis = next(iter(bases))
    else:
        basis = "mixed"
    return CommandSummary(
        total=len(records),
        settled=settled,
        failed=failed,
        failure_rate=(failed / settled) if settled else None,
        running=sum(1 for record in records if record.status == "running"),
        unknown=sum(1 for record in records if record.status == "unknown"),
        cumulative_ms=sum(durations),
        durations_known=len(durations),
        durations_missing=len(records) - len(durations),
        duration_basis=basis,
        background=sum(1 for record in records if record.background),
    )


def _programs(records: Sequence[_Execution]) -> list[CommandProgram]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.program] = counts.get(record.program, 0) + 1
    total = len(records)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_PROGRAMS]
    return [
        CommandProgram(
            name=name,
            count=count,
            share=(count / total) if total else 0.0,
            fallback=name == "shell",
        )
        for name, count in ranked
    ]


def _narrowed(assembly: CommandAssembly, q: str, program: str | None) -> list[_Execution]:
    query = q.strip().lower()
    found = assembly.records
    if query:
        found = [
            record
            for record in found
            if query in record.command.lower()
            or (record.cwd is not None and query in record.cwd.lower())
            or query in record.program.lower()
        ]
    if program is not None:
        found = [record for record in found if record.program == program]
    return found


def _sorted(records: Iterable[_Execution], sort: CommandSort) -> list[_Execution]:
    if sort == "slowest":
        return sorted(
            records,
            key=lambda record: (
                record.duration_ms is None,
                -(record.duration_ms or 0),
                -(record.event_id or 0),
            ),
        )
    return sorted(records, key=lambda record: -(record.event_id or 0))


def _after_cursor(record: _Execution, cursor: CommandCursor, sort: CommandSort) -> bool:
    """Whether ``record`` sorts strictly after the cursor's row."""
    if sort == "latest":
        return (record.event_id or 0) < cursor.event_id
    key = (record.duration_ms is None, -(record.duration_ms or 0), -(record.event_id or 0))
    marker = (
        cursor.duration_ms is None,
        -(cursor.duration_ms or 0),
        -cursor.event_id,
    )
    return key > marker


def _attention(records: Sequence[_Execution]) -> CommandAttention:
    slowest: _Execution | None = None
    for record in records:
        if record.duration_ms is None or record.duration_ms < SLOW_COMMAND_MS:
            continue
        if slowest is None or record.duration_ms > (slowest.duration_ms or 0):
            slowest = record
    failing_counts: dict[str, int] = {}
    for record in records:
        if record.status == "failed":
            failing_counts[record.program] = failing_counts.get(record.program, 0) + 1
    failing = None
    if failing_counts:
        name, count = max(failing_counts.items(), key=lambda item: (item[1], item[0]))
        failing = CommandProgram(
            name=name,
            count=count,
            share=(count / max(1, sum(failing_counts.values()))),
            fallback=name == "shell",
        )
    repeat_counts: dict[str, int] = {}
    for record in records:
        if record.command:
            repeat_counts[record.command] = repeat_counts.get(record.command, 0) + 1
    repeats = None
    repeated = [(command, count) for command, count in repeat_counts.items() if count >= 2]
    if repeated:
        command, count = max(repeated, key=lambda item: (item[1], item[0]))
        repeats = {"command": command[:500], "count": count}
    return CommandAttention(
        slowest=_public(slowest) if slowest is not None else None,
        failing=failing,
        repeats=repeats,
        background=sum(1 for record in records if record.background),
    )


def command_executions_page(
    assembly: CommandAssembly,
    *,
    filter: CommandFilter = "all",
    sort: CommandSort = "latest",
    q: str = "",
    program: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_COMMAND_PAGE,
) -> CommandExecutions:
    """Apply search, program and state filters, then return one keyset page.

    Summary and programs always describe every covered execution; the counts
    and attention describe the searched/program-narrowed set, so the chips and
    the list agree with each other.

    Raises:
        ValueError: the cursor is malformed or belongs to another list.
    """
    if not 1 <= limit <= MAX_COMMAND_PAGE:
        raise ValueError(f"limit must be 1..{MAX_COMMAND_PAGE}")
    scope = command_cursor_scope(
        assembly.thread_id, filter=filter, sort=sort, q=q.strip(), program=program
    )
    position = decode_cursor(cursor, CommandCursor, scope)
    narrowed = _narrowed(assembly, q, program)
    counts = CommandCounts(
        all=len(narrowed),
        failed=sum(1 for record in narrowed if _matches_filter(record, "failed")),
        slow=sum(1 for record in narrowed if _matches_filter(record, "slow")),
        background=sum(1 for record in narrowed if _matches_filter(record, "background")),
    )
    filtered = [record for record in narrowed if _matches_filter(record, filter)]
    attention = _attention(filtered)
    ordered = _sorted(filtered, sort)
    if position is not None:
        ordered = [record for record in ordered if _after_cursor(record, position, sort)]
    page_rows = ordered[:limit]
    next_cursor = None
    if len(ordered) > len(page_rows) and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            CommandCursor(
                scope=scope,
                event_id=last.event_id or 0,
                duration_ms=last.duration_ms,
            )
        )
    return CommandExecutions(
        thread_id=assembly.thread_id,
        generated_at=assembly.generated_at,
        summary=_summary(assembly.records),
        coverage=CommandCoverage(
            executions_total=assembly.executions_total,
            executions_covered=len(assembly.records),
            truncated=assembly.executions_total > len(assembly.records),
            turns=assembly.turns,
            attempts=assembly.attempts,
            copied_jobs_excluded=assembly.copied_jobs_excluded,
            unsupported_runtimes=list(assembly.unsupported_runtimes),
        ),
        counts=counts,
        attention=attention,
        programs=_programs(assembly.records),
        executions=[_public(record) for record in page_rows],
        page=CommandPage(
            total=len(filtered),
            returned=len(page_rows),
            limit=limit,
            next_cursor=next_cursor,
        ),
        filter=filter,
        sort=sort,
        q=q.strip(),
        program=program,
    )
