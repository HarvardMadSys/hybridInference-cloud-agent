"""The seam between the cells that push images and the manifest that names them.

A cell knows one thing the generator cannot: which matrix entry it was, on which
machine, and what the image it pulled back says about itself. The refusals here
are the ones only that point of view can make — a record from another commit, a
cell that ran on the wrong pool, a file copied under another cell's name — and
the one positive property that matters: six honest records assemble into an
input the generator turns into a valid manifest.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from deploy.release import publish_inputs as pi, release_manifest as rm, smoke

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

COMMIT = "4af1b771ac8f736a1be24f20cb307012af114a18"
OTHER_COMMIT = "9" * 40
RUNTIMES = ("claude-code", "codex", "pi", "opencode", "kilo")


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def image(name: str) -> str:
    package = "cloud-agent-sandbox" if name == "sandbox" else "cloud-agent"
    return f"{rm.REGISTRY_PREFIX}{package}@{digest(name)}"


def labels(name: str, **overrides: str) -> dict[str, str]:
    cell = pi.expected_cells()[name]
    result = {
        pi.ROLE_LABEL: cell.role,
        pi.REVISION_LABEL: COMMIT,
        "org.opencontainers.image.source": "https://github.com/HarvardMadSys/hybridInference-cloud-agent",
    }
    if cell.variant is not None:
        result[pi.BASE_PATH_LABEL] = cell.base_path or ""
    if name == "sandbox":
        for index, runtime in enumerate(RUNTIMES):
            version = f"1.{index}.0"
            result[f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_LABEL_SUFFIX}"] = version
            result[f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_MENU_LABEL_SUFFIX}"] = version
    result.update(overrides)
    return result


def record(cell_name: str, **overrides: Any) -> dict[str, Any]:
    """An honest record for `cell_name`, with `overrides` applied on top."""
    cell = pi.expected_cells()[cell_name]
    entry: dict[str, Any] = {
        "name": cell_name,
        "image": image(cell_name),
        "platform": cell.platform,
        "labels": labels(cell_name),
    }
    entry.update(overrides)
    return entry


def records() -> dict[str, dict[str, Any]]:
    return {name: record(name) for name in pi.expected_cells()}


def write_cells(directory: Path, entries: dict[str, dict[str, Any]]) -> Path:
    directory.mkdir(exist_ok=True)
    for name, entry in entries.items():
        (directory / f"{name}.json").write_text(json.dumps(entry))
    return directory


# --------------------------------------------------------------- the cells


def test_the_cells_are_the_roles_the_smoke_starts_once_per_variant() -> None:
    cells = pi.expected_cells()
    assert set(cells) == {
        "control",
        "web-root",
        "web-agents",
        "runner",
        "sandbox",
        "tunnel",
    }
    assert cells["web-agents"] == pi.Cell("web", "agents", "/agents", "linux/arm64")
    assert cells["web-root"] == pi.Cell("web", "root", "", "linux/arm64")
    for name, cell in cells.items():
        assert cell.platform == smoke.ROLES[cell.role].arch, name
        assert cell.platform in rm.EXPECTED_PLATFORMS[cell.role], name


# ----------------------------------------------------------------- record


def test_a_record_carries_what_the_manifest_needs() -> None:
    entry = pi.record("control", image("control"), "linux/arm64", labels("control"), COMMIT)
    assert entry == record("control")


@pytest.mark.parametrize(
    ("name", "overrides", "fragment"),
    [
        ("control", {"name": "console"}, "is not a cell of this release"),
        ("control", {"image": f"{rm.REGISTRY_PREFIX}cloud-agent:control-abc"}, "not a digest"),
        ("control", {"image": f"docker.io/other/cloud-agent@{digest('x')}"}, "outside"),
        ("control", {"platform": "linux/amd64"}, "wrong pool"),
        ("control", {"labels": {}}, "carries no labels"),
        ("control", {"labels": {pi.ROLE_LABEL: "control", pi.REVISION_LABEL: 1}}, "strings"),
        ("runner", {"labels": labels("runner", **{pi.ROLE_LABEL: "sandbox"})}, "declares role"),
        ("web-agents", {"labels": labels("web-agents", **{pi.BASE_PATH_LABEL: ""})}, "base path"),
        ("tunnel", {"labels": labels("tunnel", **{pi.REVISION_LABEL: OTHER_COMMIT})}, "revision"),
    ],
)
def test_a_record_that_contradicts_its_cell_is_refused(
    name: str, overrides: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(pi.PublishError, match=fragment):
        pi.validate_record(record(name, **overrides), COMMIT)


def test_a_record_is_checked_against_a_full_commit() -> None:
    with pytest.raises(pi.PublishError, match="40-hex"):
        pi.validate_record(record("control"), COMMIT[:12])


# --------------------------------------------------------------- assemble


def test_six_honest_records_assemble_into_a_manifest_the_schema_accepts() -> None:
    inputs = pi.assemble(records(), COMMIT)

    assert inputs["source_commit"] == COMMIT
    assert inputs["runtime_labels"] == labels("sandbox")
    assert {entry["role"] for entry in inputs["artifacts"]} == set(rm.EXPECTED_PLATFORMS)
    web = sorted(entry["base_path"] for entry in inputs["artifacts"] if entry["role"] == "web")
    assert web == ["", "/agents"]
    assert all("base_path" not in e for e in inputs["artifacts"] if e["role"] != "web")

    manifest = rm.build_manifest(
        source_commit=inputs["source_commit"],
        artifacts=inputs["artifacts"],
        runtime_labels=inputs["runtime_labels"],
        recipe_url=f"https://github.com/{rm.SOURCE_REPOSITORY}/releases/download/agent-{COMMIT}/recipe.tar.gz",
        recipe_sha256="a" * 64,
        publisher_run_url=f"https://github.com/{rm.SOURCE_REPOSITORY}/actions/runs/1",
        protocol=rm.deploy_protocol(),
        revision="0001_schema_baseline",
    )
    assert len(manifest["artifacts"]) == 6
    assert sorted(manifest["runtime_catalog"]) == sorted(RUNTIMES)


@pytest.mark.parametrize("missing", ["sandbox", "web-root", "tunnel"])
def test_a_missing_cell_is_not_a_smaller_release(missing: str) -> None:
    entries = records()
    del entries[missing]
    with pytest.raises(pi.PublishError, match=f"missing \\['{missing}'\\]"):
        pi.assemble(entries, COMMIT)


def test_a_stray_record_is_refused() -> None:
    entries = records()
    entries["console"] = record("control", name="console")
    with pytest.raises(pi.PublishError, match="stray \\['console'\\]"):
        pi.assemble(entries, COMMIT)


def test_a_record_from_another_commit_is_refused() -> None:
    entries = records()
    entries["tunnel"]["labels"][pi.REVISION_LABEL] = OTHER_COMMIT
    with pytest.raises(pi.PublishError, match="tunnel: the image names revision"):
        pi.assemble(entries, COMMIT)


def test_the_runtime_labels_are_the_sandbox_image_s_own() -> None:
    entries = records()
    entries["sandbox"]["labels"]["org.freeinference.runtime.codex.version"] = "9.9.9"
    entries["sandbox"]["labels"]["org.freeinference.runtime.codex.versions"] = "9.9.9,1.1.0"
    inputs = pi.assemble(entries, COMMIT)
    assert inputs["runtime_labels"]["org.freeinference.runtime.codex.version"] == "9.9.9"


# ------------------------------------------------------------- read_cells


def test_a_file_named_for_another_cell_is_refused(tmp_path: Path) -> None:
    directory = write_cells(tmp_path / "cells", records())
    (directory / "web-root.json").write_text(json.dumps(record("control")))
    with pytest.raises(pi.PublishError, match="named for 'web-root' but records 'control'"):
        pi.read_cells(directory)


def test_an_empty_hand_over_is_refused(tmp_path: Path) -> None:
    (tmp_path / "cells").mkdir()
    with pytest.raises(pi.PublishError, match="no cell handed anything over"):
        pi.read_cells(tmp_path / "cells")


def test_a_record_that_is_not_json_is_refused(tmp_path: Path) -> None:
    directory = write_cells(tmp_path / "cells", records())
    (directory / "runner.json").write_text("{")
    with pytest.raises(pi.PublishError, match="is not JSON"):
        pi.read_cells(directory)


# ------------------------------------------------------------------ notes


def manifest() -> dict[str, Any]:
    inputs = pi.assemble(records(), COMMIT)
    return rm.build_manifest(
        source_commit=COMMIT,
        artifacts=inputs["artifacts"],
        runtime_labels=inputs["runtime_labels"],
        recipe_url=f"https://github.com/{rm.SOURCE_REPOSITORY}/releases/download/agent-{COMMIT}/recipe.tar.gz",
        recipe_sha256="b" * 64,
        publisher_run_url=f"https://github.com/{rm.SOURCE_REPOSITORY}/actions/runs/7",
        protocol=rm.deploy_protocol(),
        revision="0001_schema_baseline",
    )


def test_the_notes_lead_with_the_number_a_distribution_pins() -> None:
    text = pi.notes(manifest(), "c" * 64)
    assert text.index("c" * 64) < text.index("| Artifact |")
    assert f"agent-{COMMIT}" in text
    for artifact in manifest()["artifacts"]:
        assert artifact["image"] in text
    assert "web (agents)" in text and "web (root)" in text
    assert "claude-code 1.0.0" in text
    assert "actions/runs/7" in text


def test_the_notes_refuse_a_hash_that_is_not_one() -> None:
    with pytest.raises(pi.PublishError, match="not a sha256"):
        pi.notes(manifest(), "not-a-hash")


# -------------------------------------------------------------------- CLI


def test_the_cli_records_assembles_and_renders(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cells = tmp_path / "cells"
    for name in pi.expected_cells():
        label_file = tmp_path / f"{name}-labels.json"
        label_file.write_text(json.dumps(labels(name)))
        cell = pi.expected_cells()[name]
        assert (
            pi.main(
                [
                    "record",
                    "--name",
                    name,
                    "--image",
                    image(name),
                    "--platform",
                    cell.platform,
                    "--labels",
                    str(label_file),
                    "--source-commit",
                    COMMIT,
                    "--output",
                    str(cells / f"{name}.json"),
                ]
            )
            == 0
        )
    assert pi.read_cells(cells) == records()

    inputs = tmp_path / "inputs.json"
    assert (
        pi.main(
            ["assemble", "--cells", str(cells), "--source-commit", COMMIT, "--output", str(inputs)]
        )
        == 0
    )
    assert json.loads(inputs.read_text()) == pi.assemble(records(), COMMIT)

    document = tmp_path / "release.json"
    document.write_text(json.dumps(manifest()))
    notes = tmp_path / "notes.md"
    assert (
        pi.main(
            [
                "notes",
                "--manifest",
                str(document),
                "--manifest-sha256",
                "d" * 64,
                "--output",
                str(notes),
            ]
        )
        == 0
    )
    assert "d" * 64 in notes.read_text()


def test_the_cli_refuses_rather_than_writing_a_bad_record(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    label_file = tmp_path / "labels.json"
    label_file.write_text(json.dumps(labels("control", **{pi.REVISION_LABEL: OTHER_COMMIT})))
    output = tmp_path / "cells" / "control.json"
    assert (
        pi.main(
            [
                "record",
                "--name",
                "control",
                "--image",
                image("control"),
                "--platform",
                "linux/arm64",
                "--labels",
                str(label_file),
                "--source-commit",
                COMMIT,
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
    assert "refusing to continue the publication" in capsys.readouterr().err


def test_the_cli_refuses_an_incomplete_hand_over(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    entries = records()
    del entries["control"]
    cells = write_cells(tmp_path / "cells", entries)
    output = tmp_path / "inputs.json"
    assert (
        pi.main(
            ["assemble", "--cells", str(cells), "--source-commit", COMMIT, "--output", str(output)]
        )
        == 1
    )
    assert not output.exists()
    assert "missing ['control']" in capsys.readouterr().err
