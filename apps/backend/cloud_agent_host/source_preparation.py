"""Cancellable subprocesses owned by a source-prefetch thread."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING

from cloud_agent_shared.environment_snapshots import MAX_LOCKFILES, is_lockfile
from cloud_agent_shared.git.source_cancellation import check_source_stop, checked_entries

if TYPE_CHECKING:
    import threading

SOURCE_PREFETCH_JOIN_TIMEOUT_S = 2.0
_CANCEL_POLL_S = 0.1


def read_tracked_lockfiles(
    workdir: str, *, env: dict[str, str], timeout: float, stop: threading.Event | None
) -> dict[str, str] | None:
    """Fingerprint tracked manifests without losing Git's non-UTF-8 names.

    ``None`` means Git cannot say, or there are too many manifests to record.
    Cancellation is a failure of source preparation, not a missing match key.
    """
    listed = run_source_git(
        ["ls-files", "-s", "-z"], cwd=workdir, env=env, timeout=timeout, stop=stop
    )
    if listed.returncode != 0:
        return None
    found: dict[str, str] = {}
    for entry in checked_entries(listed.stdout.split(b"\0"), stop):
        meta, _, raw = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) != 3:
            continue
        try:
            path = raw.decode()
        except UnicodeDecodeError:
            if is_lockfile(raw.rpartition(b"/")[2].decode(errors="replace")):
                return None
            continue
        if not is_lockfile(path):
            continue
        found[path] = fields[1].decode()
        if len(found) > MAX_LOCKFILES:
            return None
    return found


def run_source_git(
    args: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    timeout: float,
    stop: threading.Event | None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Drain Git while checking cancellation; kill and reap its whole group.

    Bytes preserve non-UTF-8 tracked names. Credentials stay in the supplied
    environment, never in the process arguments. An already-discarded source
    does not launch another command.
    """
    check_source_stop(stop)
    with subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_source_stop(stop)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=input_bytes, timeout=min(_CANCEL_POLL_S, remaining)
                    )
                    check_source_stop(stop)
                    return subprocess.CompletedProcess(
                        process.args, process.returncode, stdout, stderr
                    )
                except subprocess.TimeoutExpired:
                    input_bytes = None
        except BaseException:
            # The leader may have exited while a child still holds its pipes.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
