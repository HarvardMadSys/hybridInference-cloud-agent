"""Download one object version as parallel byte ranges, each into its place in a file.

A restore waits for its checkpoint, and one S3 stream reads roughly 50 to
100 MB/s: 20 to 40 s for a GiB. A few workers, each fetching a range of the
same exact version into its own place in the output file, take a fraction of
that, and a range whose stream breaks is fetched again instead of failing the
restore. The file is hashed in order as its ranges complete, so the digest
still covers every byte before anything reads the file, as one stream's did.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, BinaryIO, Final

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: Smaller objects are read in one stream: a range's request costs more than
#: it would save.
RANGED_DOWNLOAD_MIN_BYTES: Final = 64 * 1024 * 1024
RANGE_BYTES: Final = 16 * 1024 * 1024
RANGE_WORKERS: Final = 8
#: Each range is fetched at most this many times. The SDK retries a request
#: that fails on its own, but not a stream that breaks after its headers.
RANGE_ATTEMPTS: Final = 3
_CHUNK_BYTES: Final = 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class RangeMismatch(SandboxError):
    """The object is not the one the receipt names: fetching again cannot help."""


class RangeTruncated(SandboxError):
    """A range's stream ended early; it is fetched again until its attempts run out."""


class _Stopped(SandboxError):
    """Another range failed first. That failure, not this, is the one reported."""


def download_ranges(
    get_range: Callable[..., Mapping[str, Any]],
    *,
    fd: int,
    size: int,
    sha256: str,
    pinned: bool,
    check_deadline: Callable[[], None],
    what: str,
) -> None:
    """Write the object's ``size`` bytes into ``fd`` from offset 0, then check ``sha256``.

    ``get_range(Range=...)`` reads one range of the object, of its exact
    version when ``pinned``. Without a version, the ranges after the first
    are read only while the object keeps the first one's ETag, so a replaced
    object can never be mixed into this one. Raises ``RangeMismatch`` for an
    object that is not the receipt's, and a range's last error once it has
    failed ``RANGE_ATTEMPTS`` times.
    """
    starts = list(range(0, size, RANGE_BYTES))
    condition: dict[str, str] = {}
    if not pinned:
        etag = _fetch(get_range, fd, 0, size, condition, check_deadline, what)
        condition = {"IfMatch": etag} if etag else {}
    failed = threading.Event()
    #: What stopped the download, recorded before ``failed`` is set.
    causes: list[BaseException] = []

    def check() -> None:
        if failed.is_set():
            raise _Stopped(f"{what} download stopped")
        check_deadline()

    def fetch(start: int) -> None:
        try:
            _fetch(get_range, fd, start, size, condition, check, what)
        except _Stopped:
            raise
        except BaseException as exc:
            causes.append(exc)
            failed.set()
            raise

    hasher = hashlib.sha256()
    with ThreadPoolExecutor(max_workers=RANGE_WORKERS, thread_name_prefix="s3-range") as pool:
        pending = [(start, pool.submit(fetch, start)) for start in starts[0 if pinned else 1 :]]
        try:
            if not pinned:
                _hash(fd, hasher, 0, size)
            for start, future in pending:
                future.result()
                _hash(fd, hasher, start, size)
        except BaseException as exc:
            failed.set()
            for _start, future in pending:
                future.cancel()
            # The range waited on may only have been stopped by a later one:
            # an object that is not the receipt's must still read as one.
            if isinstance(exc, _Stopped) and causes:
                raise causes[0] from None
            raise
    if hasher.hexdigest() != sha256:
        raise RangeMismatch(f"{what} object does not match its digest")


def _fetch(
    get_range: Callable[..., Mapping[str, Any]],
    fd: int,
    start: int,
    size: int,
    condition: dict[str, str],
    check: Callable[[], None],
    what: str,
) -> str:
    """Write the range at ``start`` in its place, again after a broken stream; its ETag."""
    attempt = 1
    while True:
        check()
        try:
            return _write_range(get_range, fd, start, size, condition, check, what)
        except (RangeMismatch, _Stopped):
            raise
        except Exception:
            if attempt == RANGE_ATTEMPTS:
                raise
            attempt += 1


def _write_range(
    get_range: Callable[..., Mapping[str, Any]],
    fd: int,
    start: int,
    size: int,
    condition: dict[str, str],
    check: Callable[[], None],
    what: str,
) -> str:
    end = min(start + RANGE_BYTES, size) - 1
    response = get_range(Range=f"bytes={start}-{end}", **condition)
    body = response["Body"]
    offset = start
    try:
        matched = _CONTENT_RANGE.fullmatch(str(response.get("ContentRange") or ""))
        if matched is None or tuple(map(int, matched.groups())) != (start, end, size):
            raise RangeMismatch(f"{what} object is not the size its receipt names")
        if response.get("ContentLength") not in (None, end - start + 1):
            raise RangeMismatch(f"{what} object returned a range of the wrong size")
        while offset <= end:
            check()
            chunk = memoryview(body.read(min(_CHUNK_BYTES, end - offset + 1)))
            if not chunk:
                raise RangeTruncated(f"{what} range ended early")
            while chunk:
                written = os.pwrite(fd, chunk, offset)
                offset += written
                chunk = chunk[written:]
        if body.read(1):
            raise RangeMismatch(f"{what} object returned more than the range asked for")
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return str(response.get("ETag") or "")


def _hash(fd: int, hasher: Any, start: int, size: int) -> None:
    """Fold the range at ``start``, as written, into ``hasher``."""
    end = min(start + RANGE_BYTES, size)
    while start < end:
        chunk = os.pread(fd, min(_CHUNK_BYTES, end - start), start)
        if not chunk:
            raise RangeTruncated("downloaded range is shorter than was written")
        hasher.update(chunk)
        start += len(chunk)


def empty_file(output: BinaryIO) -> int | None:
    """The descriptor of ``output`` when it is an empty regular file open to read and write.

    Ranges are written at their offsets and read back to be hashed. A file
    open to append would put every write at its end, and one open only to
    write could not be read back, so either keeps the one stream.
    """
    try:
        fd = output.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    observed = os.fstat(fd)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_size
        or output.tell()
        or flags & os.O_ACCMODE != os.O_RDWR
        or flags & os.O_APPEND
    ):
        return None
    return fd
