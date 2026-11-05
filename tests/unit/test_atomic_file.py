"""Durability and failure semantics of host-private atomic replacement."""

from __future__ import annotations

import os
from typing import Any

import pytest

from cloud_agent_host import atomic_file


def _temporary_files(root: Any) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.endswith(".tmp"))


def _raise_oserror(message: str) -> None:
    raise OSError(message)


def test_atomic_replace_fully_writes_private_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "receipt.json"
    real_write = os.write

    def partial_write(descriptor: int, payload: bytes) -> int:
        return real_write(descriptor, payload[:1])

    monkeypatch.setattr(atomic_file.os, "write", partial_write)

    atomic_file.atomic_replace_bytes(target, b'{"ready":true}', directory_sync="strict")

    assert target.read_bytes() == b'{"ready":true}'
    assert target.stat().st_mode & 0o777 == 0o600
    assert _temporary_files(tmp_path) == []


@pytest.mark.parametrize("failure", ["write", "file_fsync", "replace"])
def test_failure_before_replace_removes_the_temporary_file(tmp_path, monkeypatch, failure) -> None:
    target = tmp_path / "receipt.json"

    if failure == "write":
        monkeypatch.setattr(atomic_file.os, "write", lambda *_args: _raise_oserror("write"))
    elif failure == "file_fsync":
        monkeypatch.setattr(atomic_file.os, "fsync", lambda _fd: _raise_oserror("fsync"))
    else:
        monkeypatch.setattr(
            atomic_file.os,
            "replace",
            lambda *_args: _raise_oserror("replace"),
        )

    with pytest.raises(OSError):
        atomic_file.atomic_replace_bytes(target, b"payload", directory_sync="strict")

    assert not target.exists()
    assert _temporary_files(tmp_path) == []


def test_cleanup_failure_does_not_hide_the_write_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "receipt.json"
    monkeypatch.setattr(
        atomic_file.os,
        "write",
        lambda *_args: _raise_oserror("write failed"),
    )
    monkeypatch.setattr(
        atomic_file.os,
        "unlink",
        lambda *_args: _raise_oserror("cleanup failed"),
    )

    with pytest.raises(OSError, match="write failed"):
        atomic_file.atomic_replace_bytes(target, b"payload", directory_sync="strict")


@pytest.mark.parametrize(
    ("policy", "raises"),
    [("strict", True), ("best_effort", False)],
)
def test_directory_fsync_failure_keeps_the_published_target(
    tmp_path, monkeypatch, policy, raises
) -> None:
    target = tmp_path / "receipt.json"
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(atomic_file.os, "fsync", fail_directory_fsync)

    if raises:
        with pytest.raises(OSError, match="directory fsync failed"):
            atomic_file.atomic_replace_bytes(target, b"published", directory_sync=policy)
    else:
        atomic_file.atomic_replace_bytes(target, b"published", directory_sync=policy)

    assert target.read_bytes() == b"published"
    assert _temporary_files(tmp_path) == []
