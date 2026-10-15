"""Check that a downloaded agent CLI works with this release's driver.

A version an admin can switch new chats to must be one the production driver
can actually hold a conversation with. Parsing ``--version`` proves only that
a binary exists; the failure that matters — an event shape the driver does not
understand — shows up only in a real turn.

So the probe runs one: a container from the candidate image, with **no
network**, whose main process is :mod:`cloud_agent_host.runtime_probe_model`
on loopback. The CLI is started exactly as a member is started in a retained
chat — the same driver, ``docker exec -i`` as the member uid, the version's
directory first on ``PATH`` — and asked one question. The version passes when
the turn completes with the model's answer, and nothing it printed was a
shape the driver had to give up on.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE
from cloud_agent_host.drivers import LaunchContext, TurnInput, get_driver
from cloud_agent_host.runtimes.events import MESSAGE, RAW
from cloud_agent_host.sandbox import (
    SANDBOX_GID,
    SANDBOX_PATH,
    _PopenProcess,
    runtime_bin_dir,
)
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import reports_version

logger = logging.getLogger(__name__)

#: Loopback port of the probe model inside the probe container.
PROBE_MODEL_PORT = 18765
#: Long enough for a cold CLI start under load; a hung CLI fails at this.
PROBE_TIMEOUT_S = 240.0
#: How long the model server may take to start listening.
_MODEL_READY_S = 20.0

_MEMBER_UID = MEMBER_UID_BASE
_MEMBER_HOME = "/home/agent-member-0"
_WORKDIR = "/workspace"
_STATE_DIR = "/agent-state/runtime-probe"
_ACTIVATION_DIR = f"{_MEMBER_HOME}/activation"
_PROMPT = "Reply with one word."

#: Raw-row reasons that mean the driver could not read a line at all. Matched
#: as prefixes because some carry the offending value. A line of an unknown
#: *type* is deliberately not here: every live dialect passes its protocol
#: acknowledgements and progress notices through as raw rows (pi's
#: ``{"type": "response"}`` to each command is one), so an unfamiliar type is
#: ordinary. What a user would notice is the answer missing from the chat, and
#: :func:`_turn` requires exactly that answer as a message event.
_STRUCTURAL = ("not json", "not an object")


@dataclass(frozen=True)
class ProbeResult:
    """Whether the version passed, and in a sentence why not."""

    ok: bool
    detail: str = ""


def _model_source() -> str:
    return (Path(__file__).with_name("runtime_probe_model.py")).read_text(encoding="utf-8")


class _Container:
    """One networkless probe container, removed on close."""

    def __init__(self, docker: str, image: str, label: str) -> None:
        self.docker = docker
        created = subprocess.run(
            [
                docker,
                "create",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "1024",
                "--memory",
                "4g",
                "--label",
                label,
                "--entrypoint",
                "python3",
                image,
                "-I",
                "-c",
                _model_source(),
                str(PROBE_MODEL_PORT),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if created.returncode != 0:
            raise RuntimeError(f"could not create the probe container: {created.stderr.strip()}")
        self.id = created.stdout.strip()

    def run(self, *args: str, user: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker, "exec", "--user", user, self.id, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def close(self) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [self.docker, "rm", "-f", self.id], capture_output=True, timeout=60, check=False
            )


def _prepare(box: _Container, runtime_id: str, version: str) -> str | None:
    """Start the model, lay out a member, and find the binary; an error, or None."""
    started = subprocess.run(
        [box.docker, "start", box.id], capture_output=True, text=True, timeout=60, check=False
    )
    if started.returncode != 0:
        return f"the probe container did not start: {started.stderr.strip()}"
    # As the member, like a retained chat's own setup: the image gives the
    # member group the state and workspace roots, and root in a container
    # with every capability dropped could not enter them anyway.
    member = f"{_MEMBER_UID}:{SANDBOX_GID}"
    made = box.run("install", "-d", "-m", "0700", _STATE_DIR, _ACTIVATION_DIR, user=member)
    if made.returncode != 0:
        return f"could not prepare the probe member: {made.stderr.strip()}"
    git = box.run(
        "/bin/sh",
        "-c",
        f"git init -q {_WORKDIR} && git -C {_WORKDIR} -c safe.directory={_WORKDIR} "
        "-c user.name=probe -c user.email=probe@localhost commit -q --allow-empty -m probe",
        user=member,
    )
    if git.returncode != 0:
        return f"could not prepare the probe workspace: {git.stderr.strip()}"
    # The version's own directory leads PATH, but a missing one would let the
    # image's default answer instead and pass a version that is not there.
    binary = f"{runtime_bin_dir(runtime_id, version)}/{downloadable_runtimes()[runtime_id].binary}"
    reported = box.run(binary, "--version", user=member)
    # Either stream: some CLIs print the version to stdout and a warning
    # about their environment to stderr after it.
    if reported.returncode != 0 or not (
        reports_version(reported.stdout, version) or reports_version(reported.stderr, version)
    ):
        output = (reported.stdout + reported.stderr).strip()[-200:]
        return f"{binary} does not report version {version} ({output or 'no output'})"
    deadline = time.monotonic() + _MODEL_READY_S
    check = (
        f"import socket,sys;socket.create_connection(('127.0.0.1',{PROBE_MODEL_PORT}),1).close()"
    )
    while time.monotonic() < deadline:
        if box.run("python3", "-I", "-c", check, user=member, timeout=10).returncode == 0:
            return None
        time.sleep(0.25)
    return "the probe model did not start"


def _context(box: _Container, runtime_id: str, version: str) -> LaunchContext:
    path = f"{runtime_bin_dir(runtime_id, version)}:{SANDBOX_PATH}"

    def spawn(argv: list[str], env: dict[str, str]) -> _PopenProcess:
        values = {
            **env,
            "HOME": _MEMBER_HOME,
            "PATH": path,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": _WORKDIR,
        }
        command = [
            box.docker,
            "exec",
            "--interactive",
            "--user",
            f"{_MEMBER_UID}:{SANDBOX_GID}",
            "--workdir",
            _WORKDIR,
        ]
        # Named here, valued only in the client's environment, so no value —
        # the probe credential included — is ever a process argument.
        for key in sorted(values):
            command += ["--env", key]
        process = subprocess.Popen(
            [*command, box.id, *argv],
            env={**os.environ, **values},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return _PopenProcess(process, container=box.id)

    return LaunchContext(
        workdir=_WORKDIR,
        gateway_base_url=f"http://127.0.0.1:{PROBE_MODEL_PORT}",
        credential="runtime-probe",
        provides_isolation=True,
        state_dir=_STATE_DIR,
        session_state_dir=_STATE_DIR,
        activation_dir=_ACTIVATION_DIR,
        spawn_live=spawn,
    )


def _turn(box: _Container, runtime_id: str, version: str) -> ProbeResult:
    from cloud_agent_host.runtime_probe_model import MODEL, REPLY

    # Opened and started as the sandbox manager opens and starts a member
    # (``ResidentTransportFactory``), then given one turn.
    opener = getattr(get_driver(runtime_id), "launch_live", None)
    if opener is None:
        return ProbeResult(False, f"{runtime_id} has no live session transport")
    session = opener(_context(box, runtime_id, version))
    try:
        session.ensure_started(model=MODEL, reasoning_effort=None)
        if not session.process_alive():
            detail = (session.stderr_text() or "").strip()[-300:]
            return ProbeResult(
                False, f"{runtime_id} {version} did not start: {detail or 'process exited'}"
            )
        turn = session.start_turn(TurnInput(prompt=_PROMPT, model=MODEL))
        unreadable: list[str] = []
        answered = False
        for event in turn.iter_events():
            if event.event_type == MESSAGE and REPLY in str(event.payload.get("text", "")):
                answered = True
            elif event.event_type == RAW:
                reason = str(event.payload.get("reason", ""))
                if reason.startswith(_STRUCTURAL):
                    unreadable.append(reason)
        result = turn.wait()
    finally:
        with contextlib.suppress(Exception):
            session.close()
    if unreadable:
        return ProbeResult(
            False,
            f"{runtime_id} {version} prints output this release cannot read "
            f"({'; '.join(unreadable[:3])})",
        )
    if result.status != "completed":
        detail = (result.final_output or session.stderr_text() or "").strip()[-300:]
        return ProbeResult(
            False, f"{runtime_id} {version} did not complete a turn: {detail or result.status}"
        )
    if not answered or REPLY not in result.final_output:
        # The chat would show the turn ending without what the model said.
        return ProbeResult(
            False,
            f"{runtime_id} {version} completed a turn without the model's answer",
        )
    return ProbeResult(True)


def probe_runtime(
    *,
    docker: str,
    image: str,
    runtime_id: str,
    version: str,
    label: str,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Run one turn of ``runtime_id`` ``version`` from ``image``; never raises.

    ``label`` (``key=value``) marks the probe container so a runner restarted
    mid-probe can find and remove what it left behind.
    """
    try:
        box = _Container(docker, image, label)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return ProbeResult(False, str(exc))
    outcome: list[ProbeResult] = []

    def run() -> None:
        try:
            problem = _prepare(box, runtime_id, version)
            outcome.append(
                ProbeResult(False, problem) if problem else _turn(box, runtime_id, version)
            )
        except Exception as exc:  # the probe reports; it does not crash the runner
            logger.warning(
                "runtime probe failed",
                extra={"event": "runtime_probe_error", "runtime": runtime_id, "version": version},
                exc_info=True,
            )
            outcome.append(ProbeResult(False, f"{runtime_id} {version} probe error: {exc}"))

    worker = threading.Thread(target=run, daemon=True, name=f"runtime-probe-{runtime_id}")
    try:
        worker.start()
        worker.join(timeout_s)
    finally:
        # Removing the container also ends every exec in it, which is what
        # unblocks a turn that would otherwise wait on a hung CLI forever.
        box.close()
        worker.join(30)
    if not outcome:
        return ProbeResult(False, f"{runtime_id} {version} did not finish a turn in time")
    return outcome[0]
