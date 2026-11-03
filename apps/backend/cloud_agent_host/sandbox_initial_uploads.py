"""Durable hand-off of a captured initial archive to a background S3 upload.

The pre-prompt baseline is fixed when its bundle is written while the
supervisor holds every writer frozen: from then on its bytes are a file the
Sandbox Manager owns, and nothing the resident CLI or the prompt does inside
the container can change them. Only that capture has to precede the first
prompt. The upload of those exact bytes may follow it, provided the bundle
cannot be lost while the container's initial marker says it was prepared:
once the CLI has run, the workspace no longer rebuilds it.

Each hand-off is one directory under the Sandbox Manager-only archive state
directory, named by the one S3 key it writes (namespace, sandbox, generation):

* ``bundle.tar.gz``: the exact bundle the marker names, fsynced;
* ``marker.json``: that marker's canonical bytes;
* ``committed``: present once the marker is published inside the container.

An entry is renamed into place before the container's marker is written and
committed only after that write returned. A committed entry therefore always
has its marker, and is uploaded in the background, again after a restart,
until S3 holds the object. An entry that never committed belongs to a capture
that returned no receipt: the runner admitted no prompt, the frozen workspace
still holds those bytes, and the probe's rebuild covers it exactly as it did
before uploads were deferred. Start removes it.

The directory holds no credentials, and no sandbox mounts it.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final, cast

from cloud_agent_host.sandbox_archives import safe_archive_error_type
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

GIB: Final = 1024 * 1024 * 1024
#: The directory under ``AGENT_SANDBOX_ARCHIVE_STATE_DIR``; each deployment
#: namespace has its own child, so sibling Sandbox Managers never share one.
INITIAL_UPLOAD_DIRNAME: Final = "initial-uploads"
#: Published bundles waiting for S3, plus the worst case of each one being
#: written. Past it a capture stores its bundle before returning, as before.
DEFAULT_INITIAL_UPLOAD_BUDGET_BYTES: Final = 8 * GIB
#: Seconds before the next background attempt at an entry, by failure count;
#: the last one repeats until S3 holds the object.
INITIAL_UPLOAD_RETRY_DELAYS_S: Final = (5.0, 30.0, 120.0, 600.0)
#: How often the worker looks for an entry no capture woke it for, such as
#: one a restart left or one waiting for a policy.
INITIAL_UPLOAD_POLL_S: Final = 60.0
_BUNDLE: Final = "bundle.tar.gz"
_MARKER: Final = "marker.json"
_COMMITTED: Final = "committed"
_STAGING_PREFIX: Final = ".staging-"
_ENTRY_NAME = re.compile(r"[0-9a-f]{64}")
_HASH_CHUNK_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class SpooledInitialUpload:
    """One published hand-off and the marker it was published under."""

    entry_id: str
    directory: Path
    marker: bytes


@dataclass
class InitialUploadReservation:
    """Scratch space for one bundle being written; not yet published."""

    entry_id: str
    directory: Path
    stream: BinaryIO
    reserved_bytes: int


class InitialUploadSpool:
    """The hand-off directory of one deployment namespace."""

    def __init__(self, root: Path, *, budget_bytes: int = DEFAULT_INITIAL_UPLOAD_BUDGET_BYTES):
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int):
            raise ValueError("initial upload spool budget must be a positive integer")
        if budget_bytes <= 0:
            raise ValueError("initial upload spool budget must be a positive integer")
        self._root = root
        self._budget = budget_bytes
        self._lock = threading.Lock()
        self._held: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._claims: dict[str, threading.Lock] = {}
        self._started = False

    @staticmethod
    def entry_id(*, namespace_sha256: str, sandbox_id: str, generation: int) -> str:
        """Name the one entry the initial S3 key of this sandbox generation can have."""
        identity = f"{namespace_sha256}\0{sandbox_id}\0{generation}".encode()
        return hashlib.sha256(identity).hexdigest()

    def start(self) -> None:
        """Create the directory, drop entries no marker can name, and count the rest."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        held: dict[str, int] = {}
        for child in self._root.iterdir():
            if child.name.startswith(_STAGING_PREFIX):
                _remove_tree(child)
            elif _ENTRY_NAME.fullmatch(child.name):
                if not (child / _COMMITTED).exists():
                    _remove_tree(child)
                    continue
                with contextlib.suppress(OSError):
                    held[child.name] = (child / _BUNDLE).stat().st_size
        with self._lock:
            self._held = held
            self._started = True

    def reserve(self, entry_id: str, max_bytes: int) -> InitialUploadReservation | None:
        """Open scratch space for one bundle, or ``None`` to store it synchronously."""
        with self._lock:
            if (
                not self._started
                or entry_id in self._held
                or entry_id in self._reserved
                or sum(self._held.values()) + sum(self._reserved.values()) + max_bytes
                > self._budget
            ):
                return None
            self._reserved[entry_id] = max_bytes
        try:
            directory = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self._root))
            try:
                stream = cast("BinaryIO", (directory / _BUNDLE).open("xb+"))
            except BaseException:
                _remove_tree(directory)
                raise
        except OSError as exc:
            with self._lock:
                self._reserved.pop(entry_id, None)
            logger.warning(
                "initial archive hand-off unavailable; storing synchronously",
                extra={
                    "event": "sandbox_initial_archive_handoff_unavailable",
                    "error_type": safe_archive_error_type(exc),
                },
            )
            return None
        return InitialUploadReservation(entry_id, directory, stream, max_bytes)

    def publish(
        self, reservation: InitialUploadReservation, *, marker: bytes, size: int
    ) -> SpooledInitialUpload:
        """Make the written bundle durable under ``marker``, before that marker is written."""
        reservation.stream.flush()
        os.fsync(reservation.stream.fileno())
        with (reservation.directory / _MARKER).open("xb") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(reservation.directory)
        target = self._root / reservation.entry_id
        # Never over an existing entry: ``reserve`` refused a held one, and
        # this fails rather than replace one that appeared since. Renaming
        # onto the empty directory just made replaces it atomically.
        os.mkdir(target, 0o700)
        try:
            os.rename(reservation.directory, target)
            _sync_directory(self._root)
        except BaseException:
            _remove_tree(target)
            raise
        with self._lock:
            self._reserved.pop(reservation.entry_id, None)
            self._held[reservation.entry_id] = size
        return SpooledInitialUpload(reservation.entry_id, target, marker)

    def commit(self, entry: SpooledInitialUpload) -> None:
        """Record that the container's marker names ``entry``: it is uploaded from now on."""
        descriptor = os.open(
            entry.directory / _COMMITTED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(entry.directory)

    def discard(self, handoff: InitialUploadReservation | SpooledInitialUpload) -> None:
        """Drop a hand-off no committed marker names; the old synchronous rules apply."""
        if isinstance(handoff, InitialUploadReservation):
            handoff.stream.close()
        _remove_tree(handoff.directory)
        with self._lock:
            self._reserved.pop(handoff.entry_id, None)
            self._held.pop(handoff.entry_id, None)

    def entries(self) -> list[SpooledInitialUpload]:
        """Every committed entry, in name order."""
        found: list[SpooledInitialUpload] = []
        for child in sorted(self._root.iterdir()):
            if not _ENTRY_NAME.fullmatch(child.name):
                continue
            entry = self._committed(child)
            if entry is not None:
                found.append(entry)
        return found

    @contextlib.contextmanager
    def claim(self, entry_id: str, *, timeout: float) -> Iterator[None]:
        """Let one uploader in this process at a time hold ``entry_id``.

        A probe and the background worker may both reach one entry; S3's
        conditional write keeps either order correct, and this keeps the
        second from uploading again once the first removed it.
        """
        with self._lock:
            claim = self._claims.setdefault(entry_id, threading.Lock())
        if not claim.acquire(timeout=max(0.0, timeout)):
            raise SandboxError("initial archive hand-off is still being uploaded")
        try:
            yield
        finally:
            claim.release()

    def find(self, entry_id: str, marker: bytes) -> SpooledInitialUpload | None:
        """The committed entry published under exactly ``marker``, if this host holds it."""
        entry = self._committed(self._root / entry_id)
        return entry if entry is not None and entry.marker == marker else None

    def open_bundle(
        self, entry: SpooledInitialUpload, *, size: int, sha256: str
    ) -> BinaryIO | None:
        """Open the bundle for upload, or ``None`` once it lost the marked bytes.

        A bundle without the marked size and SHA-256 can never become the
        marked object: it is removed, and a probe rebuilds from the workspace
        instead, which fails closed if that has changed too.
        """
        try:
            stream = cast("BinaryIO", (entry.directory / _BUNDLE).open("rb"))
        except FileNotFoundError:
            stream = None
        if stream is not None:
            try:
                digest = hashlib.sha256()
                total = 0
                while chunk := stream.read(_HASH_CHUNK_BYTES):
                    total += len(chunk)
                    digest.update(chunk)
                if total == size and digest.hexdigest() == sha256:
                    stream.seek(0)
                    return stream
            except BaseException:
                stream.close()
                raise
            stream.close()
        logger.error(
            "initial archive hand-off lost its marked bytes",
            extra={"event": "sandbox_initial_archive_handoff_corrupt"},
        )
        self.remove(entry)
        return None

    def remove(self, entry: SpooledInitialUpload) -> None:
        """Forget ``entry``: S3 holds its object, or it can never be uploaded."""
        # Uncommit first, so a crash part-way leaves an entry start discards.
        with contextlib.suppress(FileNotFoundError):
            (entry.directory / _COMMITTED).unlink()
        _remove_tree(entry.directory)
        with self._lock:
            self._held.pop(entry.entry_id, None)
            self._claims.pop(entry.entry_id, None)

    @staticmethod
    def _committed(directory: Path) -> SpooledInitialUpload | None:
        try:
            if not (directory / _COMMITTED).exists():
                return None
            marker = (directory / _MARKER).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        return SpooledInitialUpload(directory.name, directory, marker)


class InitialUploadWorker:
    """Upload committed entries in the background until S3 holds each one.

    ``upload`` stores one entry and removes it, returns ``False`` when it
    cannot try yet (no policy delivered), and raises when an attempt failed.
    """

    def __init__(
        self,
        spool: InitialUploadSpool,
        upload: Callable[[SpooledInitialUpload], bool],
        *,
        retry_delays_s: tuple[float, ...] = INITIAL_UPLOAD_RETRY_DELAYS_S,
        poll_s: float = INITIAL_UPLOAD_POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not retry_delays_s:
            raise ValueError("initial upload retry delays must not be empty")
        self._spool = spool
        self._upload = upload
        self._retry_delays_s = retry_delays_s
        self._poll_s = poll_s
        self._clock = clock
        self._condition = threading.Condition()
        self._failures: dict[str, int] = {}
        self._due: dict[str, float] = {}
        self._woken = False
        self._closing = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin uploading what the spool holds, including what a restart left."""
        with self._condition:
            if self._thread is not None or self._closing:
                return
            self._thread = threading.Thread(
                target=self._run, name="sandbox-initial-uploads", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        """Look at the spool now: a capture committed an entry or a policy arrived."""
        with self._condition:
            self._woken = True
            self._condition.notify_all()

    def close(self) -> None:
        """Stop after the attempt in flight; what is left is uploaded after a restart."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closing:
                    return
                self._woken = False
            next_due = self._attempt_due()
            with self._condition:
                if self._closing:
                    return
                if not self._woken:
                    timeout = self._poll_s
                    if next_due is not None:
                        timeout = max(0.0, min(timeout, next_due - self._clock()))
                    self._condition.wait(timeout)

    def _attempt_due(self) -> float | None:
        """Try every entry that is due; return when the next one falls due."""
        try:
            entries = self._spool.entries()
        except OSError as exc:
            logger.error(
                "initial archive hand-off directory is unreadable",
                extra={
                    "event": "sandbox_initial_archive_handoff_unreadable",
                    "error_type": safe_archive_error_type(exc),
                },
            )
            return None
        next_due: float | None = None
        for entry in entries:
            with self._condition:
                if self._closing:
                    break
            due = self._due.get(entry.entry_id, 0.0)
            if due > self._clock():
                next_due = due if next_due is None else min(next_due, due)
                continue
            try:
                attempted = self._upload(entry)
            except Exception as exc:
                failures = self._failures.get(entry.entry_id, 0) + 1
                self._failures[entry.entry_id] = failures
                delay = self._retry_delays_s[min(failures, len(self._retry_delays_s)) - 1]
                due = self._clock() + delay
                self._due[entry.entry_id] = due
                next_due = due if next_due is None else min(next_due, due)
                logger.warning(
                    "deferred initial sandbox archive upload failed; retrying",
                    extra={
                        "event": "sandbox_initial_archive_deferred_retry",
                        "attempts": failures,
                        "retry_in_s": delay,
                        "error_type": safe_archive_error_type(exc),
                    },
                )
            else:
                if attempted:
                    self._failures.pop(entry.entry_id, None)
                    self._due.pop(entry.entry_id, None)
        live = {entry.entry_id for entry in entries}
        for entry_id in [each for each in self._due if each not in live]:
            self._due.pop(entry_id, None)
            self._failures.pop(entry_id, None)
        return next_due


def _remove_tree(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _sync_directory(directory: Path) -> None:
    """Make the entries just made in ``directory`` durable."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DEFAULT_INITIAL_UPLOAD_BUDGET_BYTES",
    "INITIAL_UPLOAD_DIRNAME",
    "InitialUploadReservation",
    "InitialUploadSpool",
    "InitialUploadWorker",
    "SpooledInitialUpload",
]
