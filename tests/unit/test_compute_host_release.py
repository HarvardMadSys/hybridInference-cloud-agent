"""The control-plane host release, loaded once per file state.

The reconciler's schedule pass runs every ``compute.poll_seconds`` (30 s
default) and used to re-read, re-hash, and re-validate the whole 2 MB host
archive on every pass — synchronous event-loop work in a process serving
requests (issue #770). The bundle never changes while a release is running;
what can change is the file on disk, so the memo is keyed by the stats of
``release.json`` and ``source.tar.gz`` and validation re-runs whenever
either moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloud_agent.application import compute_setup
from cloud_agent.application.compute_setup import load_control_plane_host_release


def _write_release(directory: Path, *, sha: str = "c" * 40) -> None:
    """Write a minimal bundle the loader accepts, the way a build would."""
    import hashlib
    import io
    import tarfile

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name in sorted(compute_setup._HOST_BUNDLE_FILES):
            member = tarfile.TarInfo(name)
            data = f"# {name}\n".encode()
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    bundle = payload.getvalue()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.tar.gz").write_bytes(bundle)
    (directory / "release.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "bundle": "source.tar.gz",
                "source_commit": sha,
                "source_sha256": hashlib.sha256(bundle).hexdigest(),
            }
        )
    )


@pytest.fixture()
def release_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "compute-host-release"
    _write_release(directory)
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "c" * 40)
    return directory


def _counting_load(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = compute_setup.load_host_release

    def counting(directory: Path, *, expected_sha: str):
        calls.append(1)
        return original(directory, expected_sha=expected_sha)

    monkeypatch.setattr(compute_setup, "load_host_release", counting)
    return calls


def test_the_bundle_is_read_once_per_file_state(release_dir: Path, monkeypatch):
    """Two passes over unchanged files cost one full load (issue #770)."""
    calls = _counting_load(monkeypatch)

    first = load_control_plane_host_release(release_dir)
    second = load_control_plane_host_release(release_dir)

    assert first is not None and second is not None
    assert first.bundle == second.bundle
    assert len(calls) == 1  # baseline red: len(calls) == 2 — both passes reload


def test_a_changed_bundle_stat_reloads_and_revalidates(release_dir: Path, monkeypatch):
    """Validation still runs whenever the archive on disk moves."""
    calls = _counting_load(monkeypatch)
    assert load_control_plane_host_release(release_dir) is not None

    import os

    path = release_dir / "source.tar.gz"
    os.utime(path, ns=(0, 0))  # stat change, same bytes
    assert load_control_plane_host_release(release_dir) is not None
    assert len(calls) == 2


def test_a_failed_load_stays_none_and_retries_after_a_change(
    release_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """The None result keeps its current behavior: same conditions, and a
    repaired release is picked up on the next pass."""
    (release_dir / "release.json").write_text("{not json")
    assert load_control_plane_host_release(release_dir) is None

    _write_release(release_dir)
    assert load_control_plane_host_release(release_dir) is not None


def test_a_missing_directory_returns_none(release_dir: Path):
    """No embedded release directory: not deployable, as before."""
    assert load_control_plane_host_release(Path("/nonexistent-release")) is None


def test_a_deployment_sha_change_reloads(release_dir: Path, monkeypatch):
    """The expected commit is part of the key: a new deployment re-checks."""
    calls = _counting_load(monkeypatch)
    assert load_control_plane_host_release(release_dir) is not None
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "d" * 40)
    assert load_control_plane_host_release(release_dir) is None
    assert len(calls) == 2
