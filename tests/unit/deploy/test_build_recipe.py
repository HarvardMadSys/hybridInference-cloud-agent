"""The recipe archive is explicit, reproducible, and enough on its own.

Three properties, each of which fails silently if it is wrong: a file list that
is not what shipped (a host breaks later), bytes that change when the tree did
not (the hash in a manifest stops meaning anything), and an archive that turns
out to need the source it was meant to replace.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploy.release import build_recipe as br

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest() -> dict[str, Any]:
    """The four fields the renderer reads; the schema is another test's subject."""

    def ref(seed: str) -> str:
        return (
            f"ghcr.io/harvardmadsys/cloud-agent@sha256:{hashlib.sha256(seed.encode()).hexdigest()}"
        )

    artifacts = [{"role": "control", "image": ref("control")}]
    for variant, base in (("root", ""), ("agents", "/agents")):
        artifacts.append(
            {"role": "web", "variant": variant, "base_path": base, "image": ref(f"web{variant}")}
        )
    for role in ("runner", "sandbox", "tunnel", "egress"):
        artifacts.append({"role": role, "image": ref(role)})
    return {"source_commit": "a" * 40, "artifacts": artifacts}


def test_the_archive_carries_exactly_the_listed_files() -> None:
    payload = br.build(REPO_ROOT)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = sorted(member.name for member in archive.getmembers())
    assert names == sorted(f"recipe/{name}" for name in br.RECIPE_FILES)


def test_the_archive_has_no_source_and_no_credentials() -> None:
    payload = br.build(REPO_ROOT)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = [member.name for member in archive.getmembers()]
    assert not [name for name in names if "Dockerfile" in name], (
        "an image that can be built on the host can disagree with the digest the manifest pinned"
    )
    assert not [name for name in names if br.SECRET_SHAPED.search(name)]
    assert not [name for name in names if "/.git" in name or "/local/" in name]


def test_the_same_tree_hashes_the_same_way() -> None:
    """The digest goes into a release manifest, so it has to mean the content."""
    first = hashlib.sha256(br.build(REPO_ROOT)).hexdigest()
    second = hashlib.sha256(br.build(REPO_ROOT)).hexdigest()
    assert first == second


def test_a_listed_file_that_is_missing_is_refused(tmp_path: Path) -> None:
    for name in br.RECIPE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / name).read_bytes())
    (tmp_path / "deploy/host/sandbox_host.sh").unlink()
    with pytest.raises(br.RecipeError) as raised:
        br.build(tmp_path)
    assert "sandbox_host.sh" in str(raised.value)


def test_a_credential_shaped_entry_is_refused_whatever_the_list_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in br.RECIPE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / name).read_bytes())
    (tmp_path / "deploy/host/.env").write_text("AGENT_SESSION_SECRET=oops\n")
    monkeypatch.setattr(br, "RECIPE_FILES", (*br.RECIPE_FILES, "deploy/host/.env"))
    with pytest.raises(br.RecipeError) as raised:
        br.build(tmp_path)
    assert "credentials" in str(raised.value)


def test_the_extracted_recipe_renders_a_release_compose_without_a_checkout(
    tmp_path: Path,
) -> None:
    """The property the archive exists for, executed rather than asserted.

    The archive is extracted into a directory with no repository in it, the
    manifest is rendered by the helper the archive carries, and the result has no
    `build` left. The two-end deployment still needs hosts and images — that is
    the acceptance run — but a recipe that cannot produce its own Compose file is
    not a recipe.
    """
    with tempfile.TemporaryDirectory() as workspace:
        extracted = Path(workspace) / "recipe"
        extracted.mkdir()
        with tarfile.open(fileobj=io.BytesIO(br.build(REPO_ROOT)), mode="r:gz") as archive:
            archive.extractall(extracted, filter="data")

        root = extracted / "recipe"
        assert not (root / ".git").exists()
        manifest_path = Path(workspace) / "release.json"
        manifest_path.write_text(json.dumps(manifest()))
        output = Path(workspace) / "compose.yml"

        completed = subprocess.run(
            [
                sys.executable,
                str(root / "deploy/release/artifact_compose.py"),
                "--manifest",
                str(manifest_path),
                "--compose",
                str(root / "deploy/control-plane/docker-compose.yml"),
                "--stack",
                "control-plane",
                "--variant",
                "agents",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            cwd=root,
        )
        assert completed.returncode == 0, completed.stderr
        rendered = yaml.safe_load(output.read_text())
        assert "build" not in yaml.safe_dump(rendered)
        assert rendered["services"]["web"]["image"] == next(
            a["image"] for a in manifest()["artifacts"] if a.get("variant") == "agents"
        )
