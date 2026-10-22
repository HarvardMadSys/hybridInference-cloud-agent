"""The artifact catalog's projections and explicit compatibility boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from deploy.release import artifact_catalog as catalog, generate_artifact_contracts as generate

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_catalog_pins_the_existing_six_release_cells() -> None:
    """Independent release expectations stay visible beside the shared data."""
    entries = catalog.artifacts()
    assert [entry.name for entry in entries] == [
        "control",
        "web-root",
        "web-agents",
        "runner",
        "sandbox",
        "tunnel",
    ]
    assert catalog.site_variants() == {
        "root": ("", "/api"),
        "agents": ("/agents", "/agents/api"),
    }
    assert {entry.name: entry.platform for entry in entries} == {
        "control": "linux/arm64",
        "web-root": "linux/arm64",
        "web-agents": "linux/arm64",
        "runner": "linux/amd64",
        "sandbox": "linux/amd64",
        "tunnel": "linux/amd64",
    }
    for entry in entries:
        assert (REPO_ROOT / entry.dockerfile).is_file(), entry.name


def test_manifest_visible_shape_requires_an_explicit_contract_decision(tmp_path: Path) -> None:
    document = catalog.read_document()
    document["artifacts"][0]["role"] = "renamed-control"
    changed = tmp_path / "artifact_catalog.json"
    changed.write_text(json.dumps(document))

    with pytest.raises(catalog.CatalogError, match="manifest-contract decision"):
        catalog.load_catalog(changed)


def test_generated_schema_and_publish_matrix_are_current() -> None:
    assert generate.stale_outputs() == []


def test_schema_version_is_not_rewritten_as_a_catalog_side_effect() -> None:
    schema = json.loads(generate.SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"]["const"] = 2
    with pytest.raises(generate.GenerationError, match="compatibility decision"):
        generate.normalized_schema(schema)


def test_generated_schema_requires_every_catalog_cell_once() -> None:
    schema = generate.normalized_schema(json.loads(generate.SCHEMA_PATH.read_text()))
    artifacts = schema["properties"]["artifacts"]
    assert artifacts["minItems"] == artifacts["maxItems"] == len(catalog.artifacts())
    required = {
        (
            rule["contains"]["properties"]["role"]["const"],
            rule["contains"]["properties"].get("variant", {}).get("const"),
        )
        for rule in artifacts["allOf"]
    }
    assert required == {(entry.role, entry.variant) for entry in catalog.artifacts()}
