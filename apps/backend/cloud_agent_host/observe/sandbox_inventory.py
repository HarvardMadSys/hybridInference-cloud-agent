"""The runner's bounded, read-only count of its Docker host's retained chat containers.

One ``docker container ls`` with a JSON Go template and no inspection, run as
a bounded child: killed at its timeout, its stdout capped in bytes and lines
while it is read, and a capped or timed-out run reported unavailable rather
than as the smaller count it managed to read. The host capacity collector
(:mod:`cloud_agent_host.host_capacity`) samples it on its own thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL,
)
from cloud_agent_shared.host_capacity import OTHER_STATES

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from _typeshed import FileDescriptorLike


#: Docker's own state vocabulary, partitioned exactly as the design requires.
RUNNING_STATE = "running"
STOPPED_STATE = "exited"

#: Inventory outcomes.
INVENTORY_AVAILABLE = "available"
INVENTORY_PARTIAL = "partial"
INVENTORY_UNAVAILABLE = "unavailable"

#: Why an inventory is partial or unavailable. Bounded identifiers rather than
#: prose: they are stored, compared and counted.
REASON_NAMESPACE_UNSET = "namespace_unset"
REASON_DOCKER_UNAVAILABLE = "docker_unavailable"
REASON_DOCKER_TIMEOUT = "docker_timeout"
REASON_DOCKER_EXIT_ERROR = "docker_exit_error"
REASON_DOCKER_OUTPUT_LIMIT = "docker_output_limit"
REASON_DOCKER_LINE_LIMIT = "docker_line_limit"
REASON_DOCKER_MALFORMED_OUTPUT = "docker_malformed_output"
REASON_UNKNOWN_STATE = "unknown_state"
REASON_UNSCOPED_LEGACY_CONTAINERS = "unscoped_legacy_containers"


class CommandStatus(StrEnum):
    """How a bounded child process ended."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    LINE_LIMIT = "line_limit"


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one bounded child process.

    Attributes:
        status: Why it ended.
        stdout: Captured output, empty unless ``status`` is ``OK``. A capped
            run returns nothing rather than its prefix.
        stderr: A bounded prefix of stderr, for the log line.
    """

    status: CommandStatus
    stdout: str = ""
    stderr: str = ""


def run_bounded_command(
    argv: Sequence[str],
    *,
    timeout_s: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    max_lines: int,
    binary_check: Callable[[str], str | None] | None = None,
) -> CommandResult:
    """Run one child process with output read under a hard ceiling.

    The ceiling is enforced while reading, not after. ``communicate()`` would
    buffer whatever the child writes and only then let the caller decide it was
    too much, which is not a bound at all — a Docker daemon answering with a
    gigabyte of diagnostics would be fully resident before the first check.

    Args:
        argv: The exact argument vector. Never a shell string.
        timeout_s: Wall-clock budget for the whole child.
        max_stdout_bytes: Ceiling on captured stdout.
        max_stderr_bytes: Ceiling on captured stderr.
        max_lines: Ceiling on newline count in stdout.
        binary_check: Optional resolver; ``None`` means the binary is missing.

    Returns:
        The outcome. Over-budget and timed-out runs have had their process
        group killed and reaped before this returns.
    """
    if not argv:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    resolver = binary_check or shutil.which
    if resolver(argv[0]) is None:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    return _drain_child(
        child,
        timeout_s=timeout_s,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        max_lines=max_lines,
    )


def _drain_child(  # noqa: C901, PLR0912 complexity baseline, #771
    child: subprocess.Popen[bytes],
    *,
    timeout_s: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    max_lines: int,
) -> CommandResult:
    """Read a running child's pipes until EOF, a ceiling, or the deadline."""
    deadline = time.monotonic() + timeout_s
    stdout = bytearray()
    stderr = bytearray()
    status: CommandStatus | None = None
    lines = 0
    selector = selectors.DefaultSelector()
    # The selector reports each registered stream back as whatever
    # ``register`` accepted (``FileDescriptorLike``), so the table it is read
    # through is keyed by that type rather than by the concrete pipes.
    streams: dict[FileDescriptorLike | None, bytearray] = {
        child.stdout: stdout,
        child.stderr: stderr,
    }
    try:
        for stream in streams:
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = CommandStatus.TIMEOUT
                break
            for key, _ in selector.select(timeout=remaining):
                sink = streams[key.fileobj]
                limit = max_stdout_bytes if sink is stdout else max_stderr_bytes
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(sink) + len(chunk) > limit:
                    status = (
                        CommandStatus.OUTPUT_LIMIT if sink is stdout else CommandStatus.UNAVAILABLE
                    )
                    break
                sink.extend(chunk)
                if sink is stdout:
                    lines += chunk.count(b"\n")
                    if lines > max_lines:
                        status = CommandStatus.LINE_LIMIT
                        break
            if status is not None:
                break
    finally:
        selector.close()
    if status is None:
        try:
            code = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            status = CommandStatus.TIMEOUT
        else:
            status = CommandStatus.OK if code == 0 else CommandStatus.UNAVAILABLE
    if status is not CommandStatus.OK or child.poll() is None:
        _kill_child(child)
    if status is not CommandStatus.OK:
        return CommandResult(
            status=status,
            stderr=(bytes(stderr[:max_stderr_bytes]).decode("utf-8", "replace")).strip(),
        )
    return CommandResult(
        status=CommandStatus.OK,
        stdout=bytes(stdout).decode("utf-8", "replace"),
        stderr=bytes(stderr[:max_stderr_bytes]).decode("utf-8", "replace").strip(),
    )


def _kill_child(child: subprocess.Popen[bytes]) -> None:
    """Kill a child and everything it started, then reap it."""
    try:
        os.killpg(os.getpgid(child.pid), 9)
    except OSError:
        # The group is gone, or the pid was reaped between the two calls. Fall
        # back to the child alone rather than leaving it running.
        with contextlib.suppress(OSError):
            child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5.0)
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


@dataclass(frozen=True)
class SandboxInventory:
    """Entity counts of retained chat containers on one Docker host.

    Counts are of **containers**, not of memberships: a Fork adds a member to a
    group that already has one container, so counting memberships would report
    a machine as busier every time a user branched a chat.

    Attributes:
        namespace: The deployment namespace the counts are scoped to.
        status: ``available``, ``partial`` or ``unavailable``.
        running: Containers in ``running``.
        stopped: Containers in ``exited``.
        other_states: Fixed-key breakdown of every other Docker state, with
            ``None`` where the count could not be established.
        reason: Bounded identifier explaining a non-available status.
        unscoped_legacy_containers: Chat containers carrying no namespace
            label. They cannot be attributed safely and are therefore not in
            any count above.
        duplicate_sandbox_id: A sandbox id observed on more than one
            container. Both containers are counted, because both hold
            resources, but the condition is worth surfacing.
    """

    namespace: str | None
    status: str
    running: int | None
    stopped: int | None
    other_states: dict[str, int | None] = field(default_factory=dict)
    reason: str | None = None
    unscoped_legacy_containers: int | None = None
    duplicate_sandbox_id: bool = False

    def as_payload(self) -> dict[str, Any]:
        """Return the fixed wire shape of this inventory."""
        return {
            "namespace": self.namespace,
            "status": self.status,
            "running": self.running,
            "stopped": self.stopped,
            "other_states": {name: self.other_states.get(name) for name in OTHER_STATES},
            "reason": self.reason,
            "unscoped_legacy_containers": self.unscoped_legacy_containers,
            "duplicate_sandbox_id": self.duplicate_sandbox_id,
        }


def unavailable_inventory(*, namespace: str | None, reason: str) -> SandboxInventory:
    """Return an inventory that could not be established at all.

    Args:
        namespace: The namespace the attempt was scoped to, if any.
        reason: Bounded identifier for the failure.

    Returns:
        An inventory whose counts are ``None``. Never zero: a failed listing
        and an empty host are different facts, and the page says so.
    """
    return SandboxInventory(
        namespace=namespace,
        status=INVENTORY_UNAVAILABLE,
        running=None,
        stopped=None,
        other_states=dict.fromkeys(OTHER_STATES),
        reason=reason,
    )


def docker_list_argv(*, docker_binary: str) -> list[str]:
    """Return the exact read-only listing command this module runs.

    Args:
        docker_binary: The Docker CLI to invoke.

    Returns:
        An argument vector. No shell, no ``--size``, no inspection, no state
        change: the four fields are read straight off the list, because a
        per-container inspect is what makes inventory collection expensive
        enough to be a lifecycle hazard.
    """
    template = (
        '{"id":{{json .ID}}'
        f',"namespace":{{{{json (.Label "{CHAT_NAMESPACE_LABEL}")}}}}'
        f',"sandbox_id":{{{{json (.Label "{CHAT_SANDBOX_ID_LABEL}")}}}}'
        ',"state":{{json .State}}}'
    )
    return [
        docker_binary,
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        f"label={CHAT_SANDBOX_LABEL}=true",
        "--format",
        template,
    ]


def parse_docker_rows(stdout: str) -> list[dict[str, Any]]:
    """Parse the Go-template JSON lines the listing emits.

    Args:
        stdout: Raw child output.

    Returns:
        One mapping per listed container.

    Raises:
        ValueError: Any line is blank or not a JSON object with the four
            expected keys. A malformed listing is reported as unavailable
            rather than as the smaller count that happened to parse.
    """
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("listing line is not JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("listing line is not an object")
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("listing line has no container id")
        if row.get("namespace") is not None and not isinstance(row["namespace"], str):
            raise ValueError("listing line has a non-string namespace")
        rows.append(row)
    return rows


def build_inventory(rows: Sequence[Mapping[str, Any]], *, namespace: str) -> SandboxInventory:
    """Count retained containers from already-parsed listing rows.

    Args:
        rows: Parsed listing rows.
        namespace: This deployment's namespace. Only exact matches count.

    Returns:
        The inventory. Foreign namespaces are excluded silently — they belong
        to a sibling stack on the same daemon and are not this deployment's
        business — while containers carrying *no* namespace cannot be
        attributed at all and mark the result partial.
    """
    if not namespace:
        return unavailable_inventory(namespace=None, reason=REASON_NAMESPACE_UNSET)
    counts: dict[str, int] = {RUNNING_STATE: 0, STOPPED_STATE: 0}
    counts.update(dict.fromkeys(OTHER_STATES, 0))
    unscoped = 0
    unknown = False
    seen_sandbox_ids: set[str] = set()
    duplicate = False
    for row in rows:
        reported = row.get("namespace")
        if isinstance(reported, str) and reported:
            if reported != namespace:
                continue
        elif reported == "" or reported is None:
            # No namespace label: either a container from before the label
            # existed, or one a sibling stack created. Counting it into this
            # deployment would be a guess, and deleting it to remove the
            # ambiguity is not monitoring's call to make.
            unscoped += 1
            continue
        else:  # pragma: no cover - ``parse_docker_rows`` rejects these
            continue
        sandbox_id = row.get("sandbox_id")
        if isinstance(sandbox_id, str) and sandbox_id:
            if sandbox_id in seen_sandbox_ids:
                duplicate = True
            seen_sandbox_ids.add(sandbox_id)
        state = row.get("state")
        if isinstance(state, str) and state in counts:
            counts[state] += 1
        else:
            unknown = True
    if unknown:
        return SandboxInventory(
            namespace=namespace,
            status=INVENTORY_PARTIAL,
            running=counts[RUNNING_STATE],
            stopped=counts[STOPPED_STATE],
            other_states={name: counts[name] for name in OTHER_STATES},
            reason=REASON_UNKNOWN_STATE,
            unscoped_legacy_containers=unscoped,
            duplicate_sandbox_id=duplicate,
        )
    if unscoped:
        return SandboxInventory(
            namespace=namespace,
            status=INVENTORY_PARTIAL,
            running=counts[RUNNING_STATE],
            stopped=counts[STOPPED_STATE],
            other_states={name: counts[name] for name in OTHER_STATES},
            reason=REASON_UNSCOPED_LEGACY_CONTAINERS,
            unscoped_legacy_containers=unscoped,
            duplicate_sandbox_id=duplicate,
        )
    return SandboxInventory(
        namespace=namespace,
        status=INVENTORY_AVAILABLE,
        running=counts[RUNNING_STATE],
        stopped=counts[STOPPED_STATE],
        other_states={name: counts[name] for name in OTHER_STATES},
        unscoped_legacy_containers=0,
        duplicate_sandbox_id=duplicate,
    )
