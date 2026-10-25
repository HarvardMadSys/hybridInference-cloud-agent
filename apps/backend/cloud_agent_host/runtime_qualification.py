"""Prove a host's Kata runtime once, and again only when something changes.

The full probe boots a throwaway Kata guest (``ContainerBackend.preflight``),
so the long-lived host service runs it rather than every turn's child: when it
starts, whenever the installer's runtime epoch or the sandbox image changes,
and at least every half hour. Each child receives the verdict inside the turn
payload the host already writes to its standard input. A claim's own
underscore keys are stripped before delivery, so only this host can supply it.
Mutable network rules stay outside the verdict: they travel as a verification of
their own, at most a minute old (``network_guard.VERIFICATION_KEY``), and every
child checks the network before it accepts its turn.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import time
import uuid
from typing import Any

from cloud_agent_host.host_runtime import LAUNCH_BACKOFF_CAP_S, LAUNCH_BACKOFF_S
from cloud_agent_host.network_guard import VERIFICATION_KEY
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxBackend
from cloud_agent_shared.errors import SandboxError

RUNTIME_STATE_PATH = "/run/agent-kata-runtime/epoch.json"
#: The turn-payload key a verdict travels under. The parent adds it after the
#: claim's underscore keys are stripped, so a claim can never supply one.
VERDICT_KEY = "_runtime"
#: How long a verdict stands when nothing it can see has changed. It bounds how
#: long a change made outside the installer, which no marker records, can go
#: unnoticed.
RECHECK_S = 1800.0


def _file_identity(observed: os.stat_result) -> list[int]:
    return [
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    ]


def _require_safe_mode(observed: os.stat_result, *, directory: bool, root: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(observed.st_mode)
        or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (root and observed.st_uid != 0)
    ):
        raise SandboxError("runtime qualification marker has unsafe ownership or permissions")


def _read_marker(path: str, *, root: bool) -> tuple[str, list[int]] | None:
    """Read a bounded regular marker without following its file or directory symlink."""
    target = pathlib.Path(path)
    try:
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SandboxError("runtime qualification marker directory cannot be trusted") from exc
    try:
        if root:
            _require_safe_mode(os.fstat(directory), directory=True, root=True)
        try:
            descriptor = os.open(
                target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SandboxError("runtime qualification marker cannot be trusted") from exc
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            _require_safe_mode(before, directory=False, root=root)
            raw = source.read(4097)
            after = os.fstat(source.fileno())
            if len(raw) > 4096 or _file_identity(before) != _file_identity(after):
                raise SandboxError("runtime qualification marker changed while being read")
            try:
                return raw.decode("utf-8").strip(), _file_identity(after)
            except UnicodeDecodeError as exc:
                raise SandboxError("runtime qualification marker is not UTF-8") from exc
    except OSError as exc:
        raise SandboxError("runtime qualification marker could not be read") from exc
    finally:
        os.close(directory)


def check_runtime_state(path: str = RUNTIME_STATE_PATH) -> dict[str, Any] | None:
    """Refuse an unsafe or changing runtime; absence permits only a full probe."""
    marker = _read_marker(path, root=True)
    if marker is None:
        return None
    raw, identity = marker
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or type(state.get("version")) is not int:
            raise ValueError("invalid version")
        if state["version"] != 1 or state.get("state") not in {"ready", "updating"}:
            raise ValueError("invalid state")
        if not isinstance(state.get("generation"), str):
            raise ValueError("invalid generation")
        generation = str(uuid.UUID(state["generation"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxError("runtime qualification epoch is malformed") from exc
    if state["state"] == "updating":
        raise SandboxError("Kata runtime is being updated; refusing to accept a turn")
    return {"generation": generation, "identity": identity}


def _image_id(backend: ContainerBackend) -> str:
    """The image a new sandbox would start from, as Docker resolves it now."""
    try:
        result = subprocess.run(
            [backend.docker_binary, "image", "inspect", "--format", "{{.Id}}", backend.image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"the sandbox image could not be inspected: {exc}") from exc
    image_id = result.stdout.strip()
    if result.returncode != 0 or not image_id.startswith("sha256:"):
        raise SandboxError(
            "the sandbox image could not be inspected: "
            + (result.stderr.strip()[:200] or "no image id")
        )
    return image_id


class HostQualification:
    """The host service's proof that its Kata runtime is sound, renewed on change."""

    def __init__(
        self, backend: ContainerBackend, *, runtime_state_path: str = RUNTIME_STATE_PATH
    ) -> None:
        self.backend = backend
        self.runtime_state_path = runtime_state_path
        self._verdict: dict[str, Any] | None = None
        self._image_id = ""
        self._checked_at = 0.0
        # A failed probe stops claims and is retried on a backoff, so a broken
        # runtime neither takes work nor boots a probe guest on every poll.
        self._failure = ""
        self._failures = 0
        self._retry_at = 0.0

    def refresh(self) -> bool:
        """Keep the proof current before a claim.

        ``False`` leaves every child to its own full probe: the backend is not
        Kata, or there is no runtime epoch by which an update could be noticed.
        Raises while the runtime is being updated or its probe is failing, and
        the host claims nothing meanwhile.
        """
        if self.backend.runtime != KATA_RUNTIME:
            return False
        epoch = check_runtime_state(self.runtime_state_path)
        if epoch is None:
            self._verdict = None
            return False
        now = time.monotonic()
        if (
            self._verdict is not None
            and self._verdict["epoch"] == epoch
            and now < self._checked_at + RECHECK_S
        ):
            return True
        self._verdict = None
        if self._failure and now < self._retry_at:
            raise SandboxError(self._failure)
        try:
            self._prove(epoch)
        except SandboxError as exc:
            self._failures += 1
            self._failure = str(exc) or "runtime qualification failed"
            self._retry_at = now + min(
                LAUNCH_BACKOFF_S * 2 ** (self._failures - 1), LAUNCH_BACKOFF_CAP_S
            )
            raise
        self._failure, self._failures = "", 0
        return True

    def _prove(self, epoch: dict[str, Any]) -> None:
        image = _image_id(self.backend)
        self.backend.preflight(check_network=False)
        # An update or a rebuilt image during the probe would leave the verdict
        # describing the runtime as it was rather than as it is.
        if (
            check_runtime_state(self.runtime_state_path) != epoch
            or _image_id(self.backend) != image
        ):
            raise SandboxError("runtime changed while it was being checked")
        guest_kernel = self.backend.validate_guest_kernel(
            self.backend.version_stamp().get("guest_kernel")
        )
        self.backend._version_stamp = None
        self._verdict = {"guest_kernel": guest_kernel, "epoch": epoch}
        self._image_id = image
        self._checked_at = time.monotonic()

    def verdict(self) -> dict[str, Any] | None:
        """The verdict for a turn handed over now, or ``None`` for a full child probe."""
        if self._verdict is None:
            return None
        try:
            current = (
                check_runtime_state(self.runtime_state_path) == self._verdict["epoch"]
                and _image_id(self.backend) == self._image_id
            )
        except SandboxError:
            current = False
        if not current:
            # The next refresh proves the runtime as it is now.
            self._verdict = None
            return None
        return dict(self._verdict)

    def handover(self) -> dict[str, Any]:
        """What a turn handed over now carries of this host's proofs.

        The runtime verdict while it stands (:meth:`verdict`), and the
        verification of the network rules from the guard this service's
        startup check verified them with, renewed first once it is no longer
        recent (``HostNetworkGuard.handover``). With the verification the
        child starts no helper container of its own; with one that aged out
        before a slow child could adopt it, the child installs and reads back
        the rules itself; with a failed one it declines the turn. The caller
        adds both after the claim's own underscore keys are stripped, so only
        this host can supply them.
        """
        handed: dict[str, Any] = {}
        verdict = self.verdict()
        if verdict is not None:
            handed[VERDICT_KEY] = verdict
        if self.backend.sandbox_network is not None:
            handed[VERIFICATION_KEY] = self.backend.network_guard().handover()
        return handed


def adopt_verdict(
    backend: SandboxBackend,
    verdict: object,
    *,
    runtime_state_path: str = RUNTIME_STATE_PATH,
) -> bool:
    """Take the host's verdict for this turn, or return ``False`` for a full probe.

    The epoch is read once more: an update that began after the host's check
    stops this turn (``check_runtime_state`` raises), and one that finished
    means the verdict describes a runtime that is no longer installed.
    """
    if not isinstance(backend, ContainerBackend) or backend.runtime != KATA_RUNTIME:
        return False
    if not isinstance(verdict, dict):
        return False
    epoch = check_runtime_state(runtime_state_path)
    if epoch is None or verdict.get("epoch") != epoch:
        return False
    try:
        guest_kernel = backend.validate_guest_kernel(verdict.get("guest_kernel"))
    except SandboxError:
        return False
    backend._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": guest_kernel}
    backend._version_stamp = None
    return True
