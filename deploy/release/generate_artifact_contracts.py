#!/usr/bin/env python3
"""Render and freshness-check catalog-derived release contract fragments."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__:
    from . import artifact_catalog as catalog
else:  # direct execution from the release directory
    import artifact_catalog as catalog

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")
PUBLISH_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"

PUBLISH_BEGIN = "          # BEGIN GENERATED ARTIFACT MATRIX"
PUBLISH_END = "          # END GENERATED ARTIFACT MATRIX"


class GenerationError(Exception):
    """A generated projection cannot be rendered safely."""


def _contains(entry: catalog.Artifact) -> dict[str, Any]:
    properties: dict[str, Any] = {"role": {"const": entry.role}}
    required = ["role"]
    if entry.variant is not None:
        properties["variant"] = {"const": entry.variant}
        required.append("variant")
    return {
        "contains": {"properties": properties, "required": required},
        "minContains": 1,
        "maxContains": 1,
    }


def normalized_schema(source: Mapping[str, Any]) -> dict[str, Any]:
    """Replace only catalog-owned vocabulary in the reviewed contract."""
    rendered = deepcopy(dict(source))
    entries = catalog.artifacts()
    declared_version = rendered["properties"]["schema_version"]["const"]
    catalog_version = catalog.manifest_schema_version()
    if declared_version != catalog_version:
        raise GenerationError(
            f"schema version {declared_version} disagrees with the catalog contract "
            f"version {catalog_version}; resolve that compatibility decision explicitly"
        )

    artifacts_schema = rendered["properties"]["artifacts"]
    artifacts_schema["minItems"] = len(entries)
    artifacts_schema["maxItems"] = len(entries)
    # Keep role cardinality first and variant cardinality second for a stable,
    # review-friendly schema independent of build scheduling order.
    ordered = [entry for entry in entries if entry.variant is None] + [
        entry for entry in entries if entry.variant is not None
    ]
    artifacts_schema["allOf"] = [_contains(entry) for entry in ordered]

    artifact_properties = rendered["$defs"]["artifact"]["properties"]
    artifact_properties["role"]["enum"] = list(dict.fromkeys(entry.role for entry in entries))
    web = [entry for entry in entries if entry.role == "web"]
    artifact_properties["variant"]["enum"] = [entry.variant for entry in web]
    artifact_properties["base_path"]["enum"] = [entry.base_path for entry in web]
    artifact_properties["platforms"]["items"]["enum"] = sorted(
        {entry.platform for entry in entries}
    )
    rendered["$defs"]["artifact"]["allOf"][0]["then"]["oneOf"] = [
        {
            "properties": {
                "variant": {"const": entry.variant},
                "base_path": {"const": entry.base_path},
            }
        }
        for entry in web
    ]
    return rendered


def render_schema(source: str) -> str:
    try:
        document = json.loads(source)
    except json.JSONDecodeError as error:
        raise GenerationError(f"published manifest schema is invalid JSON: {error}") from error
    return json.dumps(normalized_schema(document), indent=2, ensure_ascii=True) + "\n"


def render_publish_matrix() -> str:
    lines: list[str] = []
    for entry in catalog.artifacts():
        cell = entry.matrix_cell()
        lines.extend(
            [
                f"          - name: {cell['name']}",
                f"            arch: {cell['arch']}",
                f"            smoke_args: {cell['smoke_args']}",
                f"            dockerfile: {cell['dockerfile']}",
                f"            runner: '{cell['runner']}'",
            ]
        )
        if entry.build_arguments:
            lines.append("            build_args: |")
            lines.extend(f"              {key}={value}" for key, value in entry.build_arguments)
        else:
            lines.append('            build_args: ""')
    return "\n".join(lines)


def replace_publish_matrix(source: str) -> str:
    begin = source.find(PUBLISH_BEGIN)
    end = source.find(PUBLISH_END)
    if begin < 0 or end < 0 or end <= begin:
        raise GenerationError("publish workflow is missing artifact matrix generation markers")
    body_start = source.find("\n", begin) + 1
    if body_start == 0:
        raise GenerationError("publish workflow artifact matrix marker has no newline")
    body = render_publish_matrix() + "\n"
    return source[:body_start] + body + source[end:]


def expected_outputs() -> dict[Path, str]:
    return {
        SCHEMA_PATH: render_schema(SCHEMA_PATH.read_text()),
        PUBLISH_PATH: replace_publish_matrix(PUBLISH_PATH.read_text()),
    }


def stale_outputs() -> list[Path]:
    return [path for path, expected in expected_outputs().items() if path.read_text() != expected]


def accept_contract_change() -> str:
    """Record the reviewed manifest-visible shape; never an implicit side effect."""
    document = catalog.read_document()
    fingerprint = catalog.artifact_shape_fingerprint(document)
    contract = document.get("manifest_contract")
    if not isinstance(contract, dict):
        raise GenerationError("catalog has no manifest_contract object")
    contract["artifact_shape_sha256"] = fingerprint
    catalog.CATALOG_PATH.write_text(json.dumps(document, indent=2) + "\n")
    catalog.artifacts.cache_clear()
    return fingerprint


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if generated outputs are stale")
    mode.add_argument("--write", action="store_true", help="rewrite generated outputs")
    parser.add_argument(
        "--accept-contract-change",
        action="store_true",
        help="record a reviewed manifest artifact-shape change before rendering",
    )
    arguments = parser.parse_args(argv)
    if arguments.accept_contract_change:
        if not arguments.write:
            parser.error("--accept-contract-change requires --write")
        print(f"accepted artifact contract shape {accept_contract_change()}")

    try:
        outputs = expected_outputs()
    except (catalog.CatalogError, GenerationError, KeyError, OSError) as error:
        print(f"artifact contract generation failed: {error}", file=sys.stderr)
        return 1
    if arguments.check:
        stale = [path for path, expected in outputs.items() if path.read_text() != expected]
        if stale:
            for path in stale:
                print(f"stale generated artifact contract: {path.relative_to(REPO_ROOT)}")
            return 1
        print("artifact contract projections are current")
        return 0

    for path, content in outputs.items():
        path.write_text(content)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
