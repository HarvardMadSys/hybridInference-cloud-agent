"""Focused tests for the stopped-sandbox archive primitive."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from cloud_agent_host.sandbox_archive_bundle import build_sandbox_archive_bundle
from cloud_agent_shared.errors import SandboxError

CONTAINER_PATHS = (
    "/workspace",
    "/agent-state",
    "/home/agent/.freeinference-platform",
)
ARCHIVE_PATHS = (
    "paths/workspace.tar",
    "paths/agent-state.tar",
    "paths/freeinference-platform.tar",
)


def _payloads() -> dict[str, bytes]:
    return {
        "/workspace": b"workspace docker tar\0" * 3,
        "/agent-state": b"agent state docker tar\0" * 2,
        "/home/agent/.freeinference-platform": b"platform docker tar\0",
    }


def _build(
    payloads: dict[str, bytes],
    *,
    phase: str = "final",
    metadata: dict[str, object] | None = None,
    max_member_bytes: int = 1024 * 1024,
    max_total_bytes: int = 3 * 1024 * 1024,
) -> tuple[bytes, dict[str, object], list[str]]:
    fetched: list[str] = []

    def fetch(path: str, destination, *, max_bytes: int) -> int:
        fetched.append(path)
        content = payloads[path]
        assert len(content) <= max_bytes
        written = 0
        for offset in range(0, len(content), 7):
            chunk = content[offset : offset + 7]
            written += destination.write(chunk)
        return written

    out = io.BytesIO()
    manifest = build_sandbox_archive_bundle(
        out,
        phase=phase,  # type: ignore[arg-type] -- invalid phases are exercised below.
        fetch_archive=fetch,
        metadata=metadata,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
    )
    return out.getvalue(), manifest, fetched


def _read_bundle(payload: bytes) -> tuple[list[tarfile.TarInfo], dict[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        contents = {}
        for member in members:
            extracted = archive.extractfile(member)
            assert extracted is not None
            contents[member.name] = extracted.read()
    return members, contents


def test_bundle_has_fixed_paths_hashes_sizes_and_normalized_metadata() -> None:
    payloads = _payloads()

    bundle, returned_manifest, fetched = _build(
        payloads,
        phase="final",
        metadata={"runtime": "codex", "members": [{"thread": "thr_1"}]},
    )
    members, contents = _read_bundle(bundle)

    assert fetched == list(CONTAINER_PATHS)
    assert [member.name for member in members] == ["manifest.json", *ARCHIVE_PATHS]
    assert all(member.isfile() for member in members)
    assert all(member.mtime == 0 for member in members)
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "" for member in members)
    assert members[0].mode & 0o777 == 0o444
    assert all(member.mode & 0o777 == 0o400 for member in members[1:])

    manifest = json.loads(contents["manifest.json"])
    assert manifest == returned_manifest
    assert manifest["format"] == "cloud-agent-stopped-sandbox"
    assert manifest["format_version"] == 1
    assert manifest["metadata"] == {
        "members": [{"thread": "thr_1"}],
        "runtime": "codex",
    }
    assert manifest["phase"] == "final"
    assert manifest["payload_count"] == 3
    assert manifest["payload_size_bytes"] == sum(map(len, payloads.values()))
    assert [item["container_path"] for item in manifest["payloads"]] == list(CONTAINER_PATHS)
    assert [item["archive_path"] for item in manifest["payloads"]] == list(ARCHIVE_PATHS)
    for archive_path, container_path, item in zip(
        ARCHIVE_PATHS, CONTAINER_PATHS, manifest["payloads"], strict=True
    ):
        inner = payloads[container_path]
        assert contents[archive_path] == inner
        assert item == {
            "archive_path": archive_path,
            "container_path": container_path,
            "content_type": "application/x-tar",
            "disposition": "opaque",
            "sha256": hashlib.sha256(inner).hexdigest(),
            "size_bytes": len(inner),
        }


def test_initial_bundle_fetches_only_workspace() -> None:
    bundle, returned_manifest, fetched = _build(_payloads(), phase="initial")
    members, _contents = _read_bundle(bundle)

    assert fetched == ["/workspace"]
    assert [member.name for member in members] == ["manifest.json", "paths/workspace.tar"]
    assert returned_manifest["phase"] == "initial"
    assert returned_manifest["payload_count"] == 1
    assert returned_manifest["metadata"] == {}


@pytest.mark.parametrize("phase", ["initial", "final"])
def test_bundle_is_byte_deterministic_for_identical_inputs(phase: str) -> None:
    first, _, _ = _build(_payloads(), phase=phase)
    second, _, _ = _build(_payloads(), phase=phase)

    assert first == second
    # RFC 1952 MTIME is bytes 4..8 of the gzip header.
    assert first[:2] == b"\x1f\x8b"
    assert first[4:8] == b"\0\0\0\0"


def test_inner_tar_names_are_opaque_and_cannot_escape_the_outer_bundle() -> None:
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as archive:
        traversal = tarfile.TarInfo("../../outside")
        traversal.size = 6
        archive.addfile(traversal, io.BytesIO(b"secret"))
        symlink = tarfile.TarInfo("absolute-link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/etc/passwd"
        archive.addfile(symlink)
    payloads = dict.fromkeys(CONTAINER_PATHS, inner.getvalue())

    bundle, _, _ = _build(payloads)
    members, contents = _read_bundle(bundle)

    assert [member.name for member in members] == ["manifest.json", *ARCHIVE_PATHS]
    assert all(member.isfile() for member in members)
    assert contents["paths/workspace.tar"] == inner.getvalue()
    assert "../../outside" not in contents
    assert "absolute-link" not in contents


def test_member_limit_refuses_before_writing_the_outer_bundle() -> None:
    out = io.BytesIO()

    def fetch(_path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes == 4
        return destination.write(b"12345")

    with pytest.raises(SandboxError, match=r"'/workspace'.*4-byte member limit"):
        build_sandbox_archive_bundle(
            out,
            phase="final",
            fetch_archive=fetch,
            max_member_bytes=4,
            max_total_bytes=100,
        )

    assert out.getvalue() == b""


def test_total_limit_is_enforced_across_path_archives() -> None:
    out = io.BytesIO()

    def fetch(_path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes == 4
        return destination.write(b"1234")

    with pytest.raises(SandboxError, match="10-byte total limit"):
        build_sandbox_archive_bundle(
            out,
            phase="final",
            fetch_archive=fetch,
            max_member_bytes=4,
            max_total_bytes=10,
        )

    assert out.getvalue() == b""


def test_fetch_failure_after_a_partial_stream_leaves_output_untouched() -> None:
    def fetch(path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes > 0
        if path == "/agent-state":
            destination.write(b"partial")
            raise OSError("connection closed")
        return destination.write(b"valid")

    out = io.BytesIO()
    with pytest.raises(SandboxError, match=r"'/agent-state'.*connection closed"):
        build_sandbox_archive_bundle(out, phase="final", fetch_archive=fetch)

    assert out.getvalue() == b""


def test_fetch_callback_streams_directly_to_a_bounded_sink() -> None:
    content = b"x" * (2 * 1024 * 1024 + 17)
    limits: list[int] = []

    def fetch(_path: str, destination, *, max_bytes: int) -> int:
        limits.append(max_bytes)
        written = 0
        for offset in range(0, len(content), 64 * 1024):
            written += destination.write(content[offset : offset + 64 * 1024])
        return written

    with io.BytesIO() as out:
        build_sandbox_archive_bundle(
            out,
            phase="final",
            fetch_archive=fetch,
            max_member_bytes=len(content),
            max_total_bytes=3 * len(content),
        )

    assert limits == [len(content)] * 3


@pytest.mark.parametrize("bad_limit", [0, -1, True, 1.5, "100"])
def test_limits_must_be_positive_integers(bad_limit) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="final",
            fetch_archive=lambda _path, _destination, *, max_bytes: max_bytes,
            max_member_bytes=bad_limit,
        )


def test_phase_is_closed_to_initial_and_final() -> None:
    called = False

    def fetch(_path: str, _destination, *, max_bytes: int) -> int:
        nonlocal called
        called = True
        return max_bytes

    with pytest.raises(ValueError, match="'initial' or 'final'"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="during",  # type: ignore[arg-type]
            fetch_archive=fetch,
        )

    assert not called


def test_empty_or_non_byte_fetch_stream_is_refused() -> None:
    with pytest.raises(SandboxError, match=r"'/workspace' is empty"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="final",
            fetch_archive=lambda _path, _destination, *, max_bytes: 0,
        )

    def writes_text(_path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes > 0
        return destination.write("not bytes")

    with pytest.raises(SandboxError, match="wrote non-bytes"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="final",
            fetch_archive=writes_text,
        )


def test_final_bundle_omits_missing_optional_platform_path() -> None:
    payloads = _payloads()
    fetched: list[str] = []

    def fetch(path: str, destination, *, max_bytes: int) -> int:
        fetched.append(path)
        if path == "/home/agent/.freeinference-platform":
            raise FileNotFoundError(path)
        content = payloads[path]
        assert len(content) <= max_bytes
        return destination.write(content)

    out = io.BytesIO()
    manifest = build_sandbox_archive_bundle(out, phase="final", fetch_archive=fetch)
    members, _contents = _read_bundle(out.getvalue())

    assert fetched == list(CONTAINER_PATHS)
    assert [member.name for member in members] == [
        "manifest.json",
        "paths/workspace.tar",
        "paths/agent-state.tar",
    ]
    assert manifest["payload_count"] == 2
    assert [item["container_path"] for item in manifest["payloads"]] == list(CONTAINER_PATHS[:2])


def test_missing_required_path_and_partially_written_optional_path_are_refused() -> None:
    def missing_required(_path: str, _destination, *, max_bytes: int) -> int:
        assert max_bytes > 0
        raise FileNotFoundError

    with pytest.raises(SandboxError, match=r"required.*'/workspace'.*missing"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="initial",
            fetch_archive=missing_required,
        )

    def partial_optional(path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes > 0
        if path == "/home/agent/.freeinference-platform":
            destination.write(b"partial")
            raise FileNotFoundError
        return destination.write(b"tar")

    with pytest.raises(SandboxError, match="disappeared after streaming bytes"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="final",
            fetch_archive=partial_optional,
        )


def test_fetch_callback_must_report_the_exact_written_size() -> None:
    def wrong_size(_path: str, destination, *, max_bytes: int) -> int:
        assert max_bytes > 0
        destination.write(b"tar")
        return 2

    with pytest.raises(SandboxError, match="reported 2 bytes after writing 3"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="initial",
            fetch_archive=wrong_size,
        )


def test_metadata_is_canonical_and_defensively_copied() -> None:
    first_metadata: dict[str, object] = {
        "z": [True, None, 4.5],
        "a": {"runtime": "codex", "generation": 7},
    }
    second_metadata: dict[str, object] = {
        "a": {"generation": 7, "runtime": "codex"},
        "z": [True, None, 4.5],
    }

    first, manifest, _ = _build(_payloads(), phase="initial", metadata=first_metadata)
    second, _, _ = _build(_payloads(), phase="initial", metadata=second_metadata)
    first_metadata["a"] = "changed"

    assert first == second
    assert manifest["metadata"] == {
        "a": {"generation": 7, "runtime": "codex"},
        "z": [True, None, 4.5],
    }


@pytest.mark.parametrize(
    "metadata, message",
    [
        ({"value": b"secret"}, "non-JSON value bytes"),
        ({"value": float("nan")}, "not finite"),
        ({"value": float("inf")}, "not finite"),
        ({1: "value"}, "non-string key"),
        ({"value": (1, 2)}, "non-JSON value tuple"),
    ],
)
def test_metadata_rejects_non_json_values_before_fetch(metadata, message: str) -> None:
    called = False

    def fetch(_path: str, _destination, *, max_bytes: int) -> int:
        nonlocal called
        called = True
        return max_bytes

    with pytest.raises((TypeError, ValueError), match=message):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="initial",
            fetch_archive=fetch,
            metadata=metadata,
        )
    assert not called


def test_metadata_rejects_cycles_before_fetch() -> None:
    cycle: list[object] = []
    cycle.append(cycle)

    with pytest.raises(ValueError, match="contains a cycle"):
        build_sandbox_archive_bundle(
            io.BytesIO(),
            phase="initial",
            fetch_archive=lambda _path, _destination, *, max_bytes: max_bytes,
            metadata={"cycle": cycle},
        )
