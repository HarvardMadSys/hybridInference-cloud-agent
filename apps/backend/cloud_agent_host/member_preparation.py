"""One exec for the commands a member's own UID runs before its CLI starts.

Before a member's CLI starts, its UID reaps what earlier launches left running
in its slot, removes their pidfiles and the owner credentials the last launch
left for its bash, prepares its provider home and, for Claude, merges the
platform's tool hooks into its settings. Each was a ``docker exec`` of its
own, about 100 ms on runc and 100-300 ms through kata-agent.

:class:`MemberPreparation` queues them in the order they ran and runs the
queue in one exec of :data:`RUNNER`. The runner is sent with the exec like
every guest script, so a sandbox image of any age runs it. It runs each
command as a process of its own, with the argv, input, timeout and working
directory it had, and with the environment ``docker exec`` gave the runner,
which is the one each command's own exec had. Each command keeps its own
error, and a failure stops the queue there, as a failed exec stopped the
launch; only a command whose failure must not cost the launch (the hook
merge) is reported, and the queue goes on.

Three kinds of work never join a queue. Root's: the reaper's installation
runs before the exec, and the runtime and adapter checks after it. A plugin
installer: the queue is run before it starts, so a slot is reaped and its
home prepared first, and the hook merge waits for a later queue. And the
owner's inference credentials, which an exec of their own writes after the
reap, as before: a process an earlier launch left running as this UID lives
until the reap kills it, and could read the runner's input through ``/proc``
meanwhile.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Sequence

    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

#: Reads its steps as JSON on stdin and writes each one's exit status (``null``
#: for a step that outlived its timeout) and stderr as a line of JSON on stdout
#: when it ends. It stops after the first failure of a step that is not soft.
RUNNER = r"""
import base64
import json
import os
import subprocess
import sys

# What docker exec gave this process, and so each step's own exec: Python's
# start may have added to os.environ (PEP 538's LC_CTYPE, in an image that
# sets no locale).
with open("/proc/self/environ", "rb") as handle:
    environment = dict(
        entry.split(b"=", 1) for entry in handle.read().split(b"\0") if b"=" in entry
    )
# This process as the reaper names one, so that a reap run from here spares
# the process running it, as the reaper spares itself.
with open("/proc/self/stat", encoding="ascii") as handle:
    started = handle.read().rsplit(") ", 1)[1].split()[19]
runner = f"{os.getpid()} {started}\n".encode("ascii")

for step in json.loads(sys.stdin.buffer.read()):
    data = None if step["input"] is None else base64.b64decode(step["input"])
    if step["reaps"]:
        data = (data or b"") + runner
    try:
        done = subprocess.run(
            step["argv"],
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=step["timeout"],
            check=False,
        )
        status, stderr = done.returncode, done.stderr
    except subprocess.TimeoutExpired as exc:
        status, stderr = None, exc.stderr or b""
    except OSError as exc:
        status, stderr = 127, str(exc).encode()
    if len(stderr) > 8192:
        stderr = stderr[:4096] + stderr[-4096:]
    report = {"status": status, "stderr": base64.b64encode(stderr).decode("ascii")}
    print(json.dumps(report), flush=True)
    if status != 0 and not step["soft"]:
        sys.exit(1)
"""

#: What the runner may take beyond its steps' own timeouts: its own start.
_RUNNER_SLACK_S = 10.0


@dataclass(frozen=True)
class MemberStep:
    """One command the member's own UID runs before its CLI starts."""

    argv: tuple[str, ...]
    timeout_s: float
    #: The error its failure raises, built from its stderr.
    failure: Callable[[bytes], Exception]
    input_bytes: bytes | None = None
    #: It runs the member-process reaper: the reaper is installed before the
    #: exec, and a runner adds itself to the processes the reap spares.
    reaps: bool = False
    #: How a failure that must not cost the launch is reported; empty for one
    #: that stops the queue. Only a step queued behind others is soft: run
    #: alone, its failure is its caller's to judge.
    soft: str = ""
    #: An exit status after which ``before_retry`` runs, and the queue runs
    #: again from this step, once.
    retry_status: int | None = None
    before_retry: Callable[[], object] | None = None


@dataclass(frozen=True)
class _Outcome:
    #: ``None`` for a step that outlived its timeout.
    status: int | None
    stderr: bytes


class MemberPreparation:
    """The steps queued for one member before its CLI starts, run in one exec."""

    def __init__(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        user_gid: int,
    ) -> None:
        self._manager = manager
        self._spec = spec
        self._uid = user_uid
        self._gid = user_gid
        self._steps: list[MemberStep] = []

    @property
    def pending(self) -> bool:
        """Whether steps wait for :meth:`run`."""
        return bool(self._steps)

    def add(self, *steps: MemberStep, user_uid: int, user_gid: int) -> None:
        """Queue ``steps``, which must run as this preparation's member."""
        if (user_uid, user_gid) != (self._uid, self._gid):
            raise ValueError("a member step must run as the member it is queued for")
        self._steps.extend(steps)

    def run(self) -> None:
        """Run the queue in order, in one exec, and raise what its first failure raises."""
        steps, self._steps = self._steps, []
        retried = False
        while steps:
            outcomes = self._execute(steps)
            rest = steps[len(outcomes) :]
            for index, (step, outcome) in enumerate(zip(steps, outcomes, strict=False)):
                if outcome.status == 0:
                    continue
                if step.soft:
                    print(f"{step.soft}: {_failure(step, outcome)}", file=sys.stderr)
                    continue
                if (
                    not retried
                    and step.before_retry is not None
                    and outcome.status == step.retry_status
                ):
                    step.before_retry()
                    retried = True
                    rest = steps[index:]
                    break
                raise _failure(step, outcome)
            steps = rest

    def _execute(self, steps: Sequence[MemberStep]) -> list[_Outcome]:
        """Each step's outcome, up to the one that stopped the exec."""
        if any(step.reaps for step in steps):
            self._manager.ensure_member_process_reaper(self._spec)
        if len(steps) == 1:
            # Alone, a step is the exec it always was.
            (step,) = steps
            result = self._exec(list(step.argv), step.timeout_s, step.input_bytes)
            return [_Outcome(result.returncode, result.stderr)]
        result = self._exec(
            ["python3", "-I", "-c", RUNNER],
            sum(step.timeout_s for step in steps) + _RUNNER_SLACK_S,
            _request(steps),
        )
        outcomes = _outcomes(result.stdout, len(steps))
        stopped = bool(outcomes) and outcomes[-1].status != 0 and not steps[len(outcomes) - 1].soft
        if len(outcomes) < len(steps) and not stopped:
            # The runner ended inside the next step: that step failed as the exec did.
            outcomes.append(_Outcome(result.returncode or 1, result.stderr))
        return outcomes

    def _exec(
        self, argv: list[str], timeout_s: float, input_bytes: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        return self._manager.exec_capture(
            self._spec,
            argv,
            workdir="/",
            timeout_s=timeout_s,
            input_bytes=input_bytes,
            user_uid=self._uid,
            user_gid=self._gid,
        )


def run_member_steps(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *steps: MemberStep,
    user_uid: int,
    user_gid: int,
    batch: MemberPreparation | None = None,
) -> None:
    """Queue ``steps`` on ``batch``, or run them now, in one exec, when there is none."""
    if batch is None:
        batch = MemberPreparation(manager, spec, user_uid=user_uid, user_gid=user_gid)
        batch.add(*steps, user_uid=user_uid, user_gid=user_gid)
        batch.run()
    else:
        batch.add(*steps, user_uid=user_uid, user_gid=user_gid)


def _failure(step: MemberStep, outcome: _Outcome) -> Exception:
    if outcome.status is None:
        # What an exec of its own that ran out of time raised.
        return SandboxError(
            f"chat sandbox exec failed: {step.argv[0]} timed out after {step.timeout_s:g} seconds"
        )
    return step.failure(outcome.stderr)


def _request(steps: Sequence[MemberStep]) -> bytes:
    return json.dumps(
        [
            {
                "argv": list(step.argv),
                "input": (
                    None
                    if step.input_bytes is None
                    else base64.b64encode(step.input_bytes).decode("ascii")
                ),
                "timeout": step.timeout_s,
                "reaps": step.reaps,
                "soft": bool(step.soft),
            }
            for step in steps
        ]
    ).encode("utf-8")


def _outcomes(stdout: bytes, limit: int) -> list[_Outcome]:
    """The runner's reports, up to the first line that is not one."""
    outcomes: list[_Outcome] = []
    for line in stdout.splitlines()[:limit]:
        try:
            report = json.loads(line)
            status = report["status"]
            stderr = base64.b64decode(report["stderr"], validate=True)
        except (ValueError, KeyError, TypeError, binascii.Error):
            break
        if status is not None and type(status) is not int:
            break
        outcomes.append(_Outcome(status, stderr))
    return outcomes


#: Claude reads its hooks from the member's private settings, which only the
#: member's UID can write: a host-side write would configure the sandbox
#: manager, not the CLI.
TOOL_HOOKS_SCRIPT = (
    "import contextlib, fcntl, json, os, pathlib, stat, sys, tempfile\n"
    "config = pathlib.Path(sys.argv[1])\n"
    "marks = pathlib.Path(sys.argv[2])\n"
    "raw = sys.stdin.buffer.read(65537)\n"
    "if len(raw) > 65536:\n"
    "    raise RuntimeError('tool hook settings are too large')\n"
    "update = json.loads(raw)\n"
    "if not isinstance(update, dict):\n"
    "    raise RuntimeError('tool hook settings are not an object')\n"
    "os.umask(0o077)\n"
    "info = config.lstat()\n"
    "if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():\n"
    "    raise RuntimeError('Claude config is not member-owned')\n"
    "config.chmod(0o700)\n"
    "mark_fd = os.open(marks, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o660)\n"
    "with contextlib.suppress(PermissionError):\n"
    "    os.fchmod(mark_fd, 0o660)\n"
    "os.close(mark_fd)\n"
    "lock_path = config / '.freeinference-hooks.lock'\n"
    "lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)\n"
    "os.fchmod(lock_fd, 0o600)\n"
    "with os.fdopen(lock_fd, 'r+') as lock:\n"
    "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
    "    settings_path = config / 'settings.json'\n"
    "    settings = {}\n"
    "    try:\n"
    "        fd = os.open(settings_path, os.O_RDONLY | os.O_NOFOLLOW)\n"
    "        with os.fdopen(fd, 'r') as handle:\n"
    "            candidate = json.load(handle)\n"
    "        if isinstance(candidate, dict):\n"
    "            settings = candidate\n"
    "    except (OSError, ValueError, TypeError):\n"
    "        pass\n"
    "    settings.update(update)\n"
    "    fd, filename = tempfile.mkstemp(prefix='.settings.', suffix='.tmp', dir=config)\n"
    "    temporary = pathlib.Path(filename)\n"
    "    try:\n"
    "        with os.fdopen(fd, 'w') as handle:\n"
    "            json.dump(settings, handle, indent=2)\n"
    "        os.replace(temporary, settings_path)\n"
    "    finally:\n"
    "        with contextlib.suppress(FileNotFoundError):\n"
    "            temporary.unlink()\n"
)


__all__ = ["RUNNER", "TOOL_HOOKS_SCRIPT", "MemberPreparation", "MemberStep", "run_member_steps"]
