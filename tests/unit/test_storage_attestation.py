"""The deployment helper must identify the actual read-only mount and fail closed."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_agent_host import storage_attestation as probe


def test_mount_id_selects_the_mount_even_when_targets_are_duplicated(tmp_path: Path) -> None:
    (tmp_path / "fdinfo").mkdir()
    (tmp_path / "fdinfo/7").write_text("pos:\t0\nmnt_id:\t23\n")
    (tmp_path / "mountinfo").write_text(
        "22 1 8:1 / /host/disk ro - ext4 /dev/a rw\n"
        "23 1 8:2 / /host/disk\\040with\\040space ro,nosuid - xfs /dev/b rw\n"
        "24 1 8:3 / /host/disk\\040with\\040space ro - ext4 /dev/c rw\n"
    )

    assert probe.mount_for_fd(7, proc=tmp_path) == ("8:2", "/", "xfs")


def test_writable_host_mount_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "fdinfo").mkdir()
    (tmp_path / "fdinfo/7").write_text("mnt_id:\t23\n")
    (tmp_path / "mountinfo").write_text("23 1 8:2 / /host rw - xfs /dev/b rw\n")

    with pytest.raises(ValueError, match="not read-only"):
        probe.mount_for_fd(7, proc=tmp_path)


@pytest.mark.parametrize("wrong_mount", [True, False])
def test_actual_bind_identity_is_checked_before_comparing_capacity(
    monkeypatch: pytest.MonkeyPatch, wrong_mount: bool
) -> None:
    disk = SimpleNamespace(f_frsize=4096, f_blocks=100, f_bfree=40, f_bavail=35)
    quota = SimpleNamespace(f_frsize=4096, f_blocks=10)
    closed = []
    monkeypatch.setattr(probe.os, "O_PATH", 0, raising=False)  # macOS test hosts
    monkeypatch.setattr(probe.os, "open", lambda path, flags: 7 if path == "/disk" else 8)
    monkeypatch.setattr(probe.os, "close", closed.append)
    monkeypatch.setattr(probe.os, "fstatvfs", lambda fd: disk if fd == 7 else quota)
    monkeypatch.setattr(
        probe,
        "mount_for_fd",
        lambda fd: ("8:2", "/" if fd == 7 else "/wrong" if wrong_mount else "/upper", "xfs"),
    )
    if wrong_mount:
        with pytest.raises(ValueError, match="does not match"):
            probe.measure("/.disk", "8:2", "/", "/upper", "xfs")
    else:
        assert probe.measure("/.disk", "8:2", "/", "/upper", "xfs") == {
            "mount_root": "/.disk",
            "fstype": "xfs",
            "total_bytes": 409600,
            "used_bytes": 245760,
            "available_bytes": 143360,
            "upper_total_bytes": 40960,
        }
    assert closed == [7, 8]


def test_helper_has_an_independent_deadline() -> None:
    import subprocess
    import sys
    import time

    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,time; from cloud_agent_host import storage_attestation as p; "
            "p.measure=lambda *args:time.sleep(30); "
            "sys.argv=['probe','/','8:2','/','/upper','xfs','1']; p.main()",
        ],
        timeout=5,
        check=False,
    )
    assert result.returncode == 124
    assert time.monotonic() - started < 4
