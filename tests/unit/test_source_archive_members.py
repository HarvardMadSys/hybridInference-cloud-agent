"""What each source archive member carries, now that the builder walks strings.

The builder used to parse a ``pathlib`` path for every entry, twice, and wrote
each member's fractional mtime as a 1 KiB pax header. It now keeps paths as
strings and writes whole-second mtimes. Everything else a member carries, and
every check that refuses one, must be what it was.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys
import tarfile

import pytest

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.git.source_archive import _normalize_source_member, build_source_archive
from cloud_agent_shared.git.source_cancellation import source_entries

_UID = 10001
_GID = 10002
#: A fractional mtime, as every real stat returns one.
_MTIME_NS = 1_700_000_000_987_654_321


def _archive(root: pathlib.Path) -> bytes:
    out = io.BytesIO()
    build_source_archive(str(root), out, uid=_UID, gid=_GID)
    return out.getvalue()


def _members(payload: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        return {member.name: member for member in archive.getmembers()}


def _checkout(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "src").mkdir()
    for index in range(5):
        (root / "src" / f"module_{index}.py").write_bytes(b"x" * (100 * index))
    os.symlink("src/module_1.py", root / "link.py")
    return root


def _pathlib_reference(root: pathlib.Path) -> bytes:
    """The archive as the pathlib builder wrote it, with whole-second mtimes.

    The same walk in the same order, a ``pathlib`` path per entry, its name
    taken with ``relative_to`` and checked with ``PurePosixPath``.
    """

    def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
        name = pathlib.PurePosixPath(member.name)
        assert not name.is_absolute() and ".." not in name.parts
        assert member.isfile() or member.isdir() or member.issym()
        member.uid, member.gid, member.uname, member.gname = _UID, _GID, "", ""
        member.mode &= 0o1777
        member.mtime = int(member.mtime)
        return member

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", dereference=False) as archive:
        for entry in source_entries(str(root), None):
            path = pathlib.Path(entry)
            relative = path.relative_to(root).as_posix()
            archive.add(path, arcname=relative, recursive=False, filter=normalize)
    return out.getvalue()


def test_each_member_carries_its_mtime_in_whole_seconds_without_a_pax_header(tmp_path):
    root = _checkout(tmp_path)
    for path in [root, *root.rglob("*")]:
        os.utime(path, ns=(_MTIME_NS, _MTIME_NS), follow_symlinks=False)

    members = _members(_archive(root))

    assert {".git", ".git/HEAD", "src", "src/module_4.py", "link.py"} <= members.keys()
    for member in members.values():
        assert member.mtime == _MTIME_NS // 1_000_000_000, member.name
        assert member.pax_headers == {}, member.name


def test_a_member_costs_one_header_block_besides_its_content(tmp_path):
    """A fractional mtime cost every member a pax header too: two more blocks."""
    root = _checkout(tmp_path)
    blocks = 0
    for path in root.rglob("*"):
        blocks += 1
        if path.is_file() and not path.is_symlink():
            blocks += -(-path.stat().st_size // tarfile.BLOCKSIZE)

    with tarfile.open(fileobj=io.BytesIO(_archive(root)), mode="r:") as archive:
        last = archive.getmembers()[-1]
    end = last.offset_data + -(-last.size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE

    assert end == blocks * tarfile.BLOCKSIZE


def test_a_name_the_ustar_header_cannot_hold_still_gets_a_pax_header(tmp_path):
    root = _checkout(tmp_path)
    deep = root.joinpath(*["a-directory-with-a-long-name"] * 4)
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("deep\n")
    (root / "unicodé.txt").write_text("not ascii\n")

    members = _members(_archive(root))

    long_name = deep.relative_to(root).as_posix() + "/file.txt"
    assert len(long_name) > 100
    assert members[long_name].pax_headers == {"path": long_name}
    assert members["unicodé.txt"].pax_headers == {"path": "unicodé.txt"}


def test_the_archive_is_the_pathlib_builders_but_for_its_mtimes(tmp_path):
    """Byte for byte, across every kind of entry and name the walk can meet."""
    root = _checkout(tmp_path)
    deep = root.joinpath(*["nested-directory"] * 7)
    deep.mkdir(parents=True)
    (deep / "a-file-name-long-enough-to-need-a-pax-path.txt").write_text("deep\n")
    (root / "empty").touch()
    (root / "tool").write_text("#!/bin/sh\n")
    (root / "tool").chmod(0o6755)
    (root / "shared").mkdir()
    (root / "shared").chmod(0o3777)
    (root / "unicodé.txt").write_text("utf-8\n")
    os.symlink("/etc/passwd", root / "absolute-link")
    os.symlink("t" * 150, root / "long-link")
    if sys.platform == "linux":
        with open(os.path.join(os.fsencode(root), b"r\xe9sum\xe9.txt"), "wb") as handle:
            handle.write(b"latin-1\n")

    assert _archive(root) == _pathlib_reference(root)


def test_setuid_and_setgid_never_cross_but_permissions_and_sticky_do(tmp_path):
    root = _checkout(tmp_path)
    (root / "tool").write_text("#!/bin/sh\n")
    (root / "tool").chmod(0o6755)
    (root / "shared").mkdir()
    (root / "shared").chmod(0o3777)
    (root / "private").write_text("secret\n")
    (root / "private").chmod(0o600)

    members = _members(_archive(root))

    assert members["tool"].mode == 0o755
    assert members["shared"].mode == 0o1777
    assert members["private"].mode == 0o600


def test_a_second_name_for_a_file_is_refused(tmp_path):
    """Tar would store it as a hard link, which the import refuses as before."""
    root = _checkout(tmp_path)
    os.link(root / "src" / "module_1.py", root / "src" / "module_1_again.py")

    with pytest.raises(SandboxError, match="refused member type"):
        _archive(root)


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "//etc/passwd",
        "..",
        "../outside",
        "a/../../outside",
        "a/..",
        "a//../b",
        "..a",
        "a..",
        "a/..b/c",
        "a/b../c",
        "...",
        "a/./b",
        "./a",
        "a/b/",
    ],
)
def test_the_escape_check_refuses_exactly_what_purepath_found(name):
    path = pathlib.PurePosixPath(name)
    escapes = path.is_absolute() or ".." in path.parts
    member = tarfile.TarInfo(name)
    member.mtime = 1.5

    if escapes:
        with pytest.raises(SandboxError, match="escapes workspace"):
            _normalize_source_member(member, uid=_UID, gid=_GID)
    else:
        assert _normalize_source_member(member, uid=_UID, gid=_GID).mtime == 1
