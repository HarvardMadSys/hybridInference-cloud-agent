"""The embedded Host release is complete, reproducible and credential-free."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import tarfile
from pathlib import Path

import pytest
import yaml
from deploy.release import build_host_bundle as hb

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMIT = "a" * 40


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """A clean build input tree with a source file in each approved package."""
    root = tmp_path / "source"
    for name in hb.HOST_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / name).read_bytes())
    for package in hb.PACKAGE_ROOTS:
        target = root / package
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").write_text("")
        (target / "worker.py").write_text("VALUE = 1\n")
    return root


def members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        result = {}
        for member in archive.getmembers():
            assert member.isfile()
            stream = archive.extractfile(member)
            assert stream is not None
            result[member.name] = stream.read()
        return result


def test_every_host_docker_build_input_is_available_at_the_source_root() -> None:
    """Check COPY inputs rather than discovering omissions on a remote machine."""
    content = members(hb.build(REPO_ROOT))
    for dockerfile in (
        "deploy/host/Dockerfile.agent-runner",
        "deploy/host/Dockerfile.agent-gateway-tunnel",
        "deploy/sandbox/Dockerfile.agent-sandbox",
    ):
        lines = content[dockerfile].decode().replace("\\\n", " ").splitlines()
        for line in lines:
            if not line.startswith("COPY ") or "--from=" in line:
                continue
            for source in shlex.split(line)[1:-1]:
                assert source in content or any(name.startswith(source) for name in content), (
                    f"{dockerfile} needs {source}, but SSH bootstrap could not build it"
                )
    assert "deploy/host/kata_runtime_epoch.py" in content
    assert "pyproject.toml" in content
    assert not any(name.startswith("recipe/") for name in content)


def test_archive_metadata_does_not_depend_on_the_builder(source_tree: Path) -> None:
    first = hb.build(source_tree)
    for path in hb.source_files(source_tree):
        os.utime(path, (1_750_000_000, 1_750_000_000))
        path.chmod(0o777)
    second = hb.build(source_tree)
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        for member in archive.getmembers():
            assert (member.uid, member.gid, member.mtime) == (0, 0, 0)
            assert (member.uname, member.gname) == ("", "")
            assert member.mode in {0o644, 0o755}


def test_default_root_builds_the_current_checkout() -> None:
    """Keep the repository-local entry point usable without an explicit root."""
    assert hb.build() == hb.build(REPO_ROOT)


@pytest.mark.parametrize("commit", [COMMIT, "unknown"])
def test_relocated_builder_accepts_an_explicit_source_root(
    source_tree: Path, tmp_path: Path, commit: str
) -> None:
    """A shallow Docker copy must reach argument parsing and build the supplied tree."""
    script = Path(hb.__file__).read_text()
    relocated_path = str(Path(tmp_path.anchor) / "builder" / "build_host_bundle.py")
    namespace: dict[str, object] = {
        "__file__": relocated_path,
        "__name__": "relocated_host_bundle",
    }
    exec(compile(script, relocated_path, "exec"), namespace)
    output = tmp_path / "relocated-release"
    main = namespace["main"]
    assert callable(main)
    assert (
        main(
            [
                "--root",
                str(source_tree),
                "--output",
                str(output),
                "--source-commit",
                commit,
                "--allow-unknown",
            ]
        )
        == 0
    )
    if commit == "unknown":
        assert output.is_dir()
        assert not (output / "release.json").exists()
        assert not (output / "source.tar.gz").exists()
        return
    bundle = hb.build(source_tree)
    assert (output / "source.tar.gz").read_bytes() == bundle
    manifest = json.loads((output / "release.json").read_text())
    assert manifest == {
        "format_version": 1,
        "source_commit": commit,
        "source_sha256": hashlib.sha256(bundle).hexdigest(),
        "bundle": "source.tar.gz",
    }


def test_untracked_deployment_secrets_and_caches_do_not_enter_the_archive(
    source_tree: Path,
) -> None:
    secret = b"SECRET MUST NOT BE EMBEDDED"
    for name in (
        ".env",
        "deploy/host/.env.production",
        "deploy/host/private.json",
        "deploy/sandbox/custom.key",
        "apps/backend/cloud_agent_host/private.json",
        "apps/backend/cloud_agent_host/credentials.py",
        "apps/backend/cloud_agent_host/__pycache__/cached.py",
        "apps/backend/cloud_agent_host/.compute-secrets/key.py",
    ):
        target = source_tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(secret)
    content = members(hb.build(source_tree))
    assert not any(secret in data for data in content.values())


@pytest.mark.parametrize(
    "name", ["deploy/host/sandbox_host.sh", "apps/backend/cloud_agent_host/link.py"]
)
def test_symlink_build_inputs_are_refused(source_tree: Path, name: str) -> None:
    target = source_tree / name
    target.unlink(missing_ok=True)
    target.symlink_to(source_tree / "README.md")
    with pytest.raises(hb.HostBundleError, match="symlink"):
        hb.build(source_tree)


def test_missing_host_asset_is_refused(source_tree: Path) -> None:
    (source_tree / "deploy/sandbox/kilo-project-config.patch").unlink()
    with pytest.raises(hb.HostBundleError, match=r"kilo-project-config\.patch"):
        hb.build(source_tree)


def test_release_manifest_binds_the_exact_archive_and_source_revision(
    source_tree: Path, tmp_path: Path
) -> None:
    output = tmp_path / "release"
    assert hb.write_release(source_tree, output, COMMIT)
    manifest = json.loads((output / "release.json").read_text())
    assert manifest == {
        "format_version": 1,
        "source_commit": COMMIT,
        "source_sha256": hashlib.sha256((output / "source.tar.gz").read_bytes()).hexdigest(),
        "bundle": "source.tar.gz",
    }


@pytest.mark.parametrize("commit", ["unknown", "a" * 39, "A" * 40, "a" * 41, "main"])
def test_non_revision_identity_cannot_publish_a_release(
    source_tree: Path, tmp_path: Path, commit: str
) -> None:
    with pytest.raises(hb.HostBundleError, match="40-character"):
        hb.write_release(source_tree, tmp_path / "release", commit)


def test_manual_build_with_unknown_revision_has_no_selectable_release(
    source_tree: Path, tmp_path: Path
) -> None:
    output = tmp_path / "release"
    hb.write_release(source_tree, output, COMMIT)
    assert not hb.write_release(source_tree, output, "unknown", allow_unknown=True)
    assert output.is_dir()
    assert not (output / "release.json").exists()
    assert not (output / "source.tar.gz").exists()


def test_control_build_and_discovery_both_name_the_deployed_revision() -> None:
    compose = yaml.safe_load((REPO_ROOT / "deploy/control-plane/docker-compose.yml").read_text())
    for service in ("migrate", "control-plane", "mcp-relay"):
        assert compose["services"][service]["build"]["args"]["SOURCE_COMMIT"] == (
            "${AGENT_DEPLOYMENT_SHA:-unknown}"
        )
    assert compose["services"]["control-plane"]["labels"]["io.hybridinference.deployment.sha"] == (
        "${AGENT_DEPLOYMENT_SHA:?exact commit this control plane serves}"
    )
