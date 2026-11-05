"""Bounded file and byte inputs for sandbox subprocesses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import BinaryIO


def interactive_input(
    input_bytes: bytes | None, stdin: BinaryIO | None, kata_runtime: bool
) -> bool:
    """Validate one stdin source and preserve Kata's empty-input EOF behavior."""
    if input_bytes is not None and stdin is not None:
        raise ValueError("exec input must be bytes or a stream, not both")
    interactive = input_bytes is not None or stdin is not None
    # Attaching and immediately closing an empty stdin can wedge the Kata shim.
    # The command builder supplies /dev/null when no interactive source exists.
    return interactive and not (kata_runtime and stdin is None and not input_bytes)


def require_nonempty_rewindable(stream: BinaryIO) -> None:
    """Rewind a seekable binary stream, refusing an empty one."""
    stream.seek(0, 2)
    if stream.tell() == 0:
        raise ValueError("base bundle must be non-empty")
    stream.seek(0)


__all__ = ["interactive_input", "require_nonempty_rewindable"]
