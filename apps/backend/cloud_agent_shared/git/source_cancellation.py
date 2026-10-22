"""Cooperative cancellation while preparing a trusted checkout for import."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol, TypeVar

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable, Iterator
    from typing import BinaryIO

_T = TypeVar("_T")


class SourcePreparationCancelled(SandboxError):
    """The turn discarded speculative source preparation before it finished."""


class StopSignal(Protocol):
    """A caller-owned cancellation flag; the shared package starts no threads."""

    def is_set(self) -> bool:
        """Whether the caller has abandoned preparation."""
        ...


def check_source_stop(stop: StopSignal | None) -> None:
    """Refuse further source preparation after its owner discards it."""
    if stop is not None and stop.is_set():
        raise SourcePreparationCancelled("source preparation was abandoned before it finished")


def checked_entries(entries: Iterable[_T], stop: StopSignal | None) -> Iterator[_T]:
    """Check before advancing an iterator and before using its next entry."""
    iterator = iter(entries)
    while True:
        check_source_stop(stop)
        try:
            entry = next(iterator)
        except StopIteration:
            return
        check_source_stop(stop)
        yield entry


def _directory_names(
    directory: pathlib.Path, stop: StopSignal | None
) -> tuple[list[str], list[str]]:
    """Match os.walk classification, checking every scandir entry."""
    directories: list[str] = []
    files: list[str] = []
    with os.scandir(directory) as entries:
        for entry in checked_entries(entries, stop):
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            (directories if is_directory else files).append(entry.name)
    return directories, files


def source_entries(root: pathlib.Path, stop: StopSignal | None) -> Iterator[pathlib.Path]:
    """Walk without following symlinks, preserving the existing emission order.

    os.walk collects an entire directory before yielding. A large directory
    must observe cancellation during that collection too. As before, emit
    sorted directory names followed by sorted file names, then recurse in
    the filesystem's directory order. Unreadable directories are skipped,
    matching os.walk's default behavior.
    """
    pending = [root]
    while pending:
        check_source_stop(stop)
        parent = pending.pop()
        try:
            directories, files = _directory_names(parent, stop)
        except OSError:
            continue
        for names in (directories, files):
            for name in checked_entries(sorted(names), stop):
                yield parent / name
        for name in checked_entries(reversed(directories), stop):
            child = parent / name
            if not child.is_symlink():
                pending.append(child)


class CancellableArchiveWriter:
    """Check each tar write, including the chunks copying one large file.

    The caller retains the file and closes it on cancellation. Like the source
    walk, this is cooperative: an individual filesystem syscall must return
    before cancellation can be observed.
    """

    def __init__(self, stream: BinaryIO, stop: StopSignal | None) -> None:
        self._stream = stream
        self._stop = stop

    def write(self, data: bytes) -> int:
        """Write one bounded tar chunk unless preparation was discarded."""
        check_source_stop(self._stop)
        return self._stream.write(data)

    def tell(self) -> int:
        """Expose the caller-owned stream position for tar offsets."""
        return self._stream.tell()

    @property
    def name(self) -> object:
        """Preserve tarfile's exclusion of a named output inside the checkout."""
        return getattr(self._stream, "name", None)

    def seek(self, position: int) -> int:
        """Forward tar positioning without taking ownership of the stream."""
        check_source_stop(self._stop)
        return self._stream.seek(position)

    def read(self, size: int) -> bytes:
        """Forward reads for the file protocol tarfile requires."""
        check_source_stop(self._stop)
        return self._stream.read(size)

    def close(self) -> None:
        """Leave the caller-owned spool open for import or cleanup."""
