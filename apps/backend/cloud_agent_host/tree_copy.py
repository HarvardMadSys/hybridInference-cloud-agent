"""Copy a root-owned tree from a trusted image into a retained chat.

A downloaded agent CLI and the patched Kilo build reach a retained chat the
same way: out of a stopped, networkless container of a trusted image, into a
staging directory in the chat, after which the caller's root-only script
checks the files and activates them. Nothing passes through the host's own
filesystem, whose ownership may differ in development.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, BinaryIO, cast

from cloud_agent_host.runtime_images import SOURCE_LABEL, WORK_LABEL
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


def has_root_marker(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, path: str, content: str
) -> bool:
    """Whether ``path`` in the chat is root's, mode 0644, and holds exactly ``content``."""
    probe = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'test "$(stat -c %u:%a "$1")" = 0:644 && test "$(cat "$1")" = "$2"',
            "sh",
            path,
            content,
        ],
        workdir="/",
        timeout_s=20,
        user_uid=0,
        user_gid=0,
    )
    return probe.returncode == 0


@contextmanager
def staged_tree(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    image: str,
    source: str,
    stage: str,
    what: str,
    copy_timeout_s: float,
) -> Iterator[None]:
    """Copy ``source`` out of ``image`` into the new directory ``stage`` in the chat.

    Run the activation inside the block. ``stage`` and the source container
    are removed when it ends, whether or not anything failed, and a removal
    that fails never hides a failure before it. ``what`` names the tree in
    errors.
    """
    docker = manager.backend.docker_binary
    current = manager._require(spec)
    created = manager._run(
        [
            docker,
            "create",
            "--network",
            "none",
            # Not install work, whatever the image says. A version kept by an
            # earlier release carries the work label of the container it was
            # committed from, and a runner restarting during this copy removes
            # every container that has it (``RuntimeDownloads``).
            "--label",
            f"{WORK_LABEL}=",
            # A copy source, so one this process dies before removing is
            # removed once it is too old to be a copy still running.
            "--label",
            f"{SOURCE_LABEL}={manager.backend.sandbox_namespace}",
            "--entrypoint",
            "/bin/true",
            image,
        ],
        timeout=60,
    )
    if created.returncode != 0:
        raise SandboxError(f"could not read {what}")
    source_id = created.stdout.strip()
    try:
        made = manager.exec_capture(
            spec,
            ["mkdir", "-m", "0755", stage],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if made.returncode != 0:
            raise SandboxError(f"could not prepare {what} in this chat")
        # Streamed, not buffered: a CLI is a few hundred megabytes. Root
        # ownership stays in Docker's archive the whole way.
        reader = subprocess.Popen(
            [docker, "cp", f"{source_id}:{source}", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            copied = manager._run_bytes(
                [docker, "cp", "-", f"{current.container_id}:{stage}"],
                timeout=copy_timeout_s,
                stdin=cast("BinaryIO", reader.stdout),
            )
        finally:
            if reader.stdout is not None:
                reader.stdout.close()
            try:
                read = reader.wait(timeout=30)
            except subprocess.TimeoutExpired:
                reader.kill()
                read = -1
        if read != 0 or copied.returncode != 0:
            raise SandboxError(f"could not copy {what} into this chat")
        yield
    except BaseException:
        with suppress(Exception):
            _remove(manager, spec, stage, source_id)
        raise
    _remove(manager, spec, stage, source_id)


def _remove(manager: ChatSandboxManager, spec: ChatSandboxSpec, stage: str, source_id: str) -> None:
    """Remove ``stage`` from the chat, then the source container even if that failed."""
    try:
        manager.exec_capture(
            spec, ["rm", "-rf", stage], workdir="/", timeout_s=60, user_uid=0, user_gid=0
        )
    finally:
        manager._run([manager.backend.docker_binary, "rm", source_id], timeout=30)
