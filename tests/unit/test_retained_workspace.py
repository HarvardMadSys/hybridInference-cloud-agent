"""Contract tests for fixed in-container retained workspace programs."""

from __future__ import annotations

import base64
import json
import os
import resource
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import CHAT_WORKDIR
from cloud_agent_host.retained_workspace import (
    _READ_PROGRAM,
    _WRITE_PROGRAM,
    _error_from_result,
    read_retained_file,
)


def _read(root, relative: str, *, max_bytes: int = 1024, entries: int = 100):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _READ_PROGRAM,
            str(root),
            relative,
            str(max_bytes),
            str(entries),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(result.stdout) if result.stdout else None


def test_retained_reader_matches_file_and_directory_response_contract(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello\n")
    os.symlink("README.md", tmp_path / "linked")

    root_result, root = _read(tmp_path, "")
    file_result, file = _read(tmp_path, "README.md")

    assert root_result.returncode == file_result.returncode == 0
    assert [entry["name"] for entry in root["entries"]] == ["src", "linked", "README.md"]
    assert all(entry["name"] != ".git" for entry in root["entries"])
    assert (
        next(entry for entry in root["entries"] if entry["name"] == "linked")["omitted_reason"]
        == "symlink"
    )
    assert file["content"] == "hello\n"
    assert file["source"] == "workspace"
    assert file["writable"] is True


def test_retained_reader_never_follows_a_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not visible")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.symlink(outside, workspace / "escape")

    result, payload = _read(workspace, "escape/secret.txt")

    assert result.returncode == 40
    assert payload["error"] == "workspace path crosses an invalid directory"


@pytest.mark.parametrize(
    ("raw", "media_type"),
    [
        (b"\x89PNG\r\n\x1a\n\x00image", "image/png"),
        (b"\xff\xd8\xff\xe0image", "image/jpeg"),
        (b"GIF89a\x00image", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPimage", "image/webp"),
        (b"\x00\xffbinary", None),
    ],
)
def test_retained_reader_exposes_exact_bounded_binary_bytes(tmp_path, raw, media_type):
    # Extension is deliberately unrelated: preview types come from bytes.
    (tmp_path / "artifact.dat").write_bytes(raw)

    result, payload = _read(tmp_path, "artifact.dat", max_bytes=len(raw))

    assert result.returncode == 0
    assert payload["content"] is None
    assert payload["binary"] is True
    assert payload["media_type"] == media_type
    assert base64.b64decode(payload["content_base64"], validate=True) == raw
    assert payload["size"] == len(raw)
    assert payload["truncated"] is False
    assert payload["writable"] is False


@pytest.mark.parametrize("content", ['<svg onload="alert(1)"/>', "<script>alert(1)</script>"])
def test_retained_reader_does_not_trust_image_extensions_for_active_content(tmp_path, content):
    (tmp_path / "fake.png").write_text(content)

    result, payload = _read(tmp_path, "fake.png")

    assert result.returncode == 0
    assert payload["content"] == content
    assert payload["media_type"] is None
    assert payload["content_base64"] is None


def test_retained_reader_does_not_return_partial_downloads(tmp_path):
    (tmp_path / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    result, payload = _read(tmp_path, "large.png", max_bytes=10)

    assert result.returncode == 0
    assert payload["truncated"] is True
    assert payload["omitted_reason"] == "too_large"
    assert payload["content"] is None
    assert payload.get("content_base64") is None


def test_retained_reader_never_returns_download_bytes_for_symlinks(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00image")
    os.symlink("image.png", tmp_path / "linked.png")

    result, payload = _read(tmp_path, "linked.png")

    assert result.returncode == 0
    assert payload["kind"] == "symlink"
    assert payload.get("content_base64") is None


def _backend(root, *, lang: str | None = None):
    """A retained backend whose exec runs the fixed argv against ``root``.

    The program sees the container's environment, which names no locale.
    """

    def exec_capture(_spec, argv, *, workdir, timeout_s, observer=None):
        command = [sys.executable, *argv[1:]]
        command[command.index(CHAT_WORKDIR)] = str(root)
        return subprocess.run(
            command,
            cwd=workdir,
            env={"PATH": "/usr/bin:/bin", **({"LANG": lang} if lang else {})},
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )

    return SimpleNamespace(spec=None, manager=SimpleNamespace(exec_capture=exec_capture))


@pytest.mark.skipif(sys.platform != "linux", reason="other filesystems refuse non-UTF-8 names")
@pytest.mark.parametrize("lang", [None, "C.UTF-8"])
def test_a_name_that_is_not_utf8_leaves_the_rest_of_its_directory_listed(tmp_path, lang):
    (tmp_path / "README.md").write_text("hello\n")
    (tmp_path / "café.txt").write_text("x")
    # Latin-1: a reply cannot name it, so no request could name it back.
    with open(os.path.join(os.fsencode(tmp_path), b"caf\xe9.dat"), "wb") as handle:
        handle.write(b"x")

    listing = read_retained_file(
        _backend(tmp_path, lang=lang), "", max_preview_bytes=1024, max_directory_entries=100
    )

    assert [entry["name"] for entry in listing["entries"]] == ["café.txt", "README.md"]
    assert listing["truncated"] is True


def test_a_directory_listed_up_to_its_entry_limit_says_so(tmp_path):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name)

    capped = read_retained_file(
        _backend(tmp_path), "", max_preview_bytes=1024, max_directory_entries=2
    )
    whole = read_retained_file(
        _backend(tmp_path), "", max_preview_bytes=1024, max_directory_entries=3
    )

    assert [entry["name"] for entry in capped["entries"]] == ["a.txt", "b.txt"]
    assert capped["truncated"] is True
    assert len(whole["entries"]) == 3
    assert whole["truncated"] is False


@pytest.mark.parametrize("path", ["../image.png", "./image.png", "nested/../image.png"])
def test_retained_reader_refuses_download_path_traversal(tmp_path, path):
    result, payload = _read(tmp_path, path)

    assert result.returncode == 40
    assert payload["error"] == "workspace path is invalid"


def test_retained_writer_uses_stdin_and_refuses_symlink_targets(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before")
    command = [
        sys.executable,
        "-c",
        _WRITE_PROGRAM,
        str(tmp_path),
        "note.txt",
        "100",
    ]

    written = subprocess.run(command, input=b"after\n", capture_output=True, check=False)
    assert written.returncode == 0
    assert target.read_text() == "after\n"

    os.symlink(target, tmp_path / "linked.txt")
    refused = subprocess.run(
        [*command[:-2], "linked.txt", "100"],
        input=b"overwrite",
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 49
    assert target.read_text() == "after\n"


def test_retained_writer_keeps_files_group_writable_for_fork_members(tmp_path):
    command = [
        sys.executable,
        "-c",
        _WRITE_PROGRAM,
        str(tmp_path),
        "shared.txt",
        "100",
    ]

    written = subprocess.run(command, input=b"shared\n", capture_output=True, check=False)

    assert written.returncode == 0
    assert stat.S_IMODE((tmp_path / "shared.txt").stat().st_mode) == 0o664


def _write_with_file_size_limit(root, relative: str, content: bytes, *, limit: int):
    """Run the writer where no file may grow past ``limit`` bytes.

    A file size limit is the one way to make a write run out of room without
    filling a real disk: the kernel refuses past it as it would on a full disk
    or quota, and Python already ignores SIGXFSZ, so the writer sees the error.
    """

    def limit_file_size() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return subprocess.run(
        [sys.executable, "-c", _WRITE_PROGRAM, str(root), relative, "1000"],
        input=content,
        capture_output=True,
        check=False,
        preexec_fn=limit_file_size,
    )


def test_retained_writer_leaves_the_file_unchanged_when_the_new_content_has_no_room(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("original content\n")

    refused = _write_with_file_size_limit(tmp_path, "note.txt", b"replacement " * 10, limit=32)
    refused_new = _write_with_file_size_limit(tmp_path, "new.txt", b"replacement " * 10, limit=32)

    assert refused.returncode == 47
    assert target.read_text() == "original content\n"
    assert refused_new.returncode == 47
    assert not (tmp_path / "new.txt").exists()
    error = _error_from_result(refused.returncode, refused.stdout, refused.stderr)
    assert (error.status_code, error.message) == (
        507,
        "workspace has no room for this file; it was not changed",
    )


#: One system call made to fail as a full disk does, where a real one cannot be
#: arranged: creating a file, or copying it up out of an image layer, can run
#: out of room at its open; overwriting blocks that are shared (copy-on-write,
#: reflinked copies) can run out after the reservation succeeded.
_NO_ROOM_AT = {
    "open": (
        "import errno, os\n"
        "_open = os.open\n"
        "def _no_room_to_open(path, flags, *args, **kwargs):\n"
        "    if flags & os.O_WRONLY:\n"
        "        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))\n"
        "    return _open(path, flags, *args, **kwargs)\n"
        "os.open = _no_room_to_open\n"
    ),
    "write": (
        "import errno, os\n"
        "def _no_room_to_write(*_args):\n"
        "    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))\n"
        "os.pwrite = _no_room_to_write\n"
    ),
}


@pytest.mark.parametrize(
    ("stage", "returncode", "message"),
    [
        ("open", 47, "workspace has no room for this file; it was not changed"),
        ("write", 48, "workspace ran out of room while writing this file; it may be incomplete"),
    ],
)
def test_retained_writer_names_a_full_workspace_wherever_room_runs_out(
    tmp_path, stage, returncode, message
):
    target = tmp_path / "note.txt"
    target.write_text("original content\n")

    refused = subprocess.run(
        [
            sys.executable,
            "-c",
            _NO_ROOM_AT[stage] + _WRITE_PROGRAM,
            str(tmp_path),
            "note.txt",
            "1000",
        ],
        input=b"replacement\n",
        capture_output=True,
        check=False,
    )

    assert refused.returncode == returncode, refused.stderr
    error = _error_from_result(refused.returncode, refused.stdout, refused.stderr)
    assert (error.status_code, error.message) == (507, message)
    if stage == "open":
        assert target.read_text() == "original content\n"


def test_retained_writer_replaces_in_place_and_keeps_the_files_inode_and_mode(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\necho a much longer original script\n")
    target.chmod(0o755)
    before = target.stat()

    written = subprocess.run(
        [sys.executable, "-c", _WRITE_PROGRAM, str(tmp_path), "run.sh", "100"],
        input=b"#!/bin/sh\necho short\n",
        capture_output=True,
        check=False,
    )

    after = target.stat()
    assert written.returncode == 0
    assert target.read_text() == "#!/bin/sh\necho short\n"
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o755)


#: What the type check saw before a sibling swapped a FIFO in at the path: the
#: regular file that was there a moment earlier.
_FIFO_SWAPPED_IN_AFTER_THE_CHECK = (
    "import os, stat\n"
    "_stat = os.stat\n"
    "def _stat_before_the_swap(path, *args, **kwargs):\n"
    "    found = _stat(path, *args, **kwargs)\n"
    "    if stat.S_ISFIFO(found.st_mode):\n"
    "        return os.stat_result((stat.S_IFREG | 0o664,) + tuple(found)[1:])\n"
    "    return found\n"
    "os.stat = _stat_before_the_swap\n"
)


@pytest.mark.parametrize("swapped_in", [False, True], ids=["already_there", "swapped_in"])
def test_retained_writer_refuses_a_fifo_without_blocking_on_it(tmp_path, swapped_in):
    """A FIFO with no reader would block the open, and the save, for good."""
    os.mkfifo(tmp_path / "pipe")
    prefix = _FIFO_SWAPPED_IN_AFTER_THE_CHECK if swapped_in else ""

    refused = subprocess.run(
        [sys.executable, "-c", prefix + _WRITE_PROGRAM, str(tmp_path), "pipe", "100"],
        input=b"data",
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert refused.returncode == 49, refused.stderr
