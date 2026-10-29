"""Cooperative cancellation while preparing a trusted checkout for import."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol, TypeVar

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
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


def source_entries(root: str, stop: StopSignal | None) -> Iterator[str]:
    """Stream a tree's paths without following symlinks or retaining directory listings.

    Each path is ``root`` joined with the entry's name below it. They stay
    strings: on a large tree, a ``pathlib`` path per entry cost the archive
    builder more than all of its system calls.
    """
    pending = [os.scandir(root)]
    try:
        while pending:
            check_source_stop(stop)
            try:
                entry = next(pending[-1])
            except StopIteration:
                pending.pop().close()
                continue
            check_source_stop(stop)
            yield entry.path
            check_source_stop(stop)
            if entry.is_dir(follow_symlinks=False):
                pending.append(os.scandir(entry.path))
    finally:
        for directory in pending:
            directory.close()


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
