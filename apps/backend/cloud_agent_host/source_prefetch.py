"""A new chat's source, prepared on a thread of its own while its container starts.

The runner's subclass says how the checkout is made. This module owns when
each part of it is ready, and what a failure or a discard leaves behind.
"""

from __future__ import annotations

import abc
import tempfile
import threading
from typing import TYPE_CHECKING, Any, BinaryIO

from cloud_agent_host.chat_sandbox import CHAT_WORKDIR
from cloud_agent_host.source_preparation import SOURCE_PREFETCH_JOIN_TIMEOUT_S
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.git.source_cancellation import check_source_stop

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


def _spool() -> BinaryIO:
    """An unnamed file outside the checkout, gone once it is closed.

    The archive is spooled to disk, not held in this process, and outlives the
    thread that writes it: ``import_into`` or ``discard`` closes it.
    """
    return tempfile.TemporaryFile(prefix="freeinference-source-", suffix=".tar")


class SourcePrefetch(abc.ABC):
    """A new chat's source, cloned and archived while its container starts.

    Cloning outside the chat, scrubbing credentials and building the
    normalized tar all happen on the host and need nothing from the container,
    so they run on a thread of their own beside the activation, which creates
    and boots the guest, instead of after it. Only the import waits for both.
    A failure is raised where the checkout always failed, in the checkout
    stage; a turn that stops before then discards whatever was prepared.

    A group handed an installation snapshot chooses it by the checkout's
    dependency manifests before it is created, so they are published as soon
    as Git has listed them, and the archive is built while that group starts.

    The thread is not a daemon: a turn that ends while the clone is still
    running waits for it at exit, so its temporary directory is always removed.
    Discarding cancels Git and archive preparation before joining its cleanup.

    A subclass makes the checkout. It sets what its steps read before calling
    this constructor, which starts the thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listed = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._discarded = False
        self._base_sha = ""
        self._lockfiles: dict[str, str] | None = None
        self._archive: Any = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._prepare, name="source-prefetch")
        self._thread.start()

    @abc.abstractmethod
    def _check_out(self, workdir: str, stop: threading.Event) -> str:
        """Make the source in the empty ``workdir`` and return its commit."""

    @abc.abstractmethod
    def _list_lockfiles(self, workdir: str, stop: threading.Event) -> dict[str, str] | None:
        """Each tracked dependency manifest's object id, or ``None`` when Git cannot say."""

    @abc.abstractmethod
    def _build_archive(self, workdir: str, out: BinaryIO, stop: threading.Event) -> None:
        """Write the checkout's normalized tar to ``out``."""

    def _prepare(self) -> None:
        archive: Any = None
        try:
            with tempfile.TemporaryDirectory(prefix="freeinference-source-") as source:
                check_source_stop(self._stop)
                base_sha = self._check_out(source, self._stop)
                check_source_stop(self._stop)
                self._lockfiles = self._list_lockfiles(source, self._stop)
                self._listed.set()
                check_source_stop(self._stop)
                archive = _spool()
                self._build_archive(source, archive, self._stop)
                archive.flush()
                archive.seek(0)
        except BaseException as exc:
            if archive is not None:
                archive.close()
            self._error = exc
        else:
            with self._lock:
                if self._discarded:
                    archive.close()
                else:
                    self._base_sha, self._archive = base_sha, archive
        finally:
            # A preparation that failed or was discarded before Git listed the
            # manifests releases their waiter too, which then sees the error.
            self._listed.set()
            self._done.set()

    def lockfiles(self) -> dict[str, str] | None:
        """The checkout's dependency manifests once Git has listed them; ``None`` if it failed.

        It does not wait for the archive. A failure after the listing is
        raised by :meth:`import_into`, in the checkout stage, as any other is.
        """
        self._listed.wait()
        return None if self._error is not None else self._lockfiles

    def base_sha(self) -> str | None:
        """The commit the checkout resolved, once it is prepared."""
        self._done.wait()
        return self._base_sha or None

    def import_into(
        self,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        *,
        observer: operation_profiling.StageObserver | None = None,
    ) -> str:
        """Import the prepared archive into the chat and return its base commit."""
        with operation_profiling.substage(observer, "source_wait"):
            self._done.wait()
        if self._error is not None:
            raise self._error
        with self._lock:
            archive, self._archive = self._archive, None
        if archive is None:
            raise SandboxError("the prepared source was already discarded")
        try:
            with operation_profiling.substage(observer, "source_import"):
                manager.import_archive(spec, CHAT_WORKDIR, archive)
        finally:
            archive.close()
        return self._base_sha

    def discard(self) -> None:
        """Cancel preparation and give its owner a bounded wait to clean up."""
        self._stop.set()
        with self._lock:
            self._discarded = True
            archive, self._archive = self._archive, None
        if archive is not None:
            archive.close()
        self._thread.join(timeout=SOURCE_PREFETCH_JOIN_TIMEOUT_S)


__all__ = ["SourcePrefetch"]
