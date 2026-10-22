"""Dependency-free access to the release artifact catalog.

The JSON document is data rather than Python so the frontend guard can consume
the same source. This module validates the relationships consumers rely on and
provides small projections; behavior-specific smoke checks and the published
manifest fixture deliberately remain independent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Mapping

CATALOG_PATH = Path(__file__).with_name("artifact_catalog.json")


class CatalogError(Exception):
    """The declarative artifact catalog is incomplete or contradictory."""


@dataclass(frozen=True)
class Artifact:
    """One build, smoke, publication, and manifest cell."""

    name: str
    role: str
    variant: str | None
    base_path: str | None
    api_url: str | None
    platform: str
    architecture: str
    dockerfile: str
    runner_labels: tuple[str, ...]
    smoke_key: str
    ci_scopes: tuple[str, ...]

    @property
    def smoke_args(self) -> str:
        suffix = f" --variant {self.variant}" if self.variant is not None else ""
        return f"--role {self.role}{suffix}"

    @property
    def build_arguments(self) -> tuple[tuple[str, str], ...]:
        if self.role != "web":
            return ()
        return (
            ("NEXT_PUBLIC_BASE_PATH", self.base_path or ""),
            ("NEXT_PUBLIC_AGENT_API_URL", self.api_url or ""),
        )

    def matrix_cell(self) -> dict[str, str]:
        build_args = "".join(f"{key}={value}\n" for key, value in self.build_arguments)
        return {
            "name": self.name,
            "arch": self.architecture,
            "smoke_args": self.smoke_args,
            "dockerfile": self.dockerfile,
            "runner": json.dumps(self.runner_labels, separators=(",", ":")),
            "build_args": build_args,
        }


def read_document(path: Path = CATALOG_PATH) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read artifact catalog {path}: {error}") from error
    if not isinstance(document, dict):
        raise CatalogError("artifact catalog must be a JSON object")
    return document


def artifact_shape_fingerprint(document: Mapping[str, Any]) -> str:
    """Hash only fields exposed by the published manifest contract."""
    raw = document.get("artifacts")
    if not isinstance(raw, list):
        raise CatalogError("artifact catalog must contain an artifacts array")
    contract = document.get("manifest_contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("schema_version"), int):
        raise CatalogError("artifact catalog must declare manifest_contract.schema_version")
    entries = [
        {
            "role": item.get("role"),
            "variant": item.get("variant"),
            "base_path": item.get("base_path"),
            "platform": item.get("platform"),
        }
        for item in raw
        if isinstance(item, dict)
    ]
    shape = {
        "schema_version": contract["schema_version"],
        # Scheduling order and workflow-only cell names are not published
        # manifest shape, so neither should force a compatibility decision.
        "artifacts": sorted(entries, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    payload = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _required_string(item: Mapping[str, Any], key: str, name: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise CatalogError(f"artifact {name!r} needs a non-empty {key}")
    return value


def _optional_string(item: Mapping[str, Any], key: str, name: str) -> str | None:
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise CatalogError(f"artifact {name!r} has a non-string {key}")
    return value


def load_catalog(path: Path = CATALOG_PATH) -> tuple[Artifact, ...]:
    document = read_document(path)
    contract = document.get("manifest_contract")
    expected = artifact_shape_fingerprint(document)
    recorded = contract.get("artifact_shape_sha256") if isinstance(contract, dict) else None
    if recorded != expected:
        raise CatalogError(
            "artifact shape changed without a manifest-contract decision: "
            f"record {expected} only after reviewing schema compatibility/versioning"
        )

    raw = document.get("artifacts")
    if not isinstance(raw, list) or not raw:
        raise CatalogError("artifact catalog must contain at least one artifact")
    entries: list[Artifact] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CatalogError("every artifact catalog entry must be an object")
        name = _required_string(item, "name", "<unnamed>")
        role = _required_string(item, "role", name)
        variant = _optional_string(item, "variant", name)
        base_path = _optional_string(item, "base_path", name)
        api_url = _optional_string(item, "api_url", name)
        platform = _required_string(item, "platform", name)
        architecture = _required_string(item, "architecture", name)
        dockerfile = _required_string(item, "dockerfile", name)
        smoke_key = _required_string(item, "smoke_key", name)
        runner_labels = item.get("runner_labels")
        scopes = item.get("ci_scopes")
        if (
            not isinstance(runner_labels, list)
            or not runner_labels
            or not all(isinstance(value, str) and value for value in runner_labels)
        ):
            raise CatalogError(f"artifact {name!r} needs non-empty runner_labels")
        if not isinstance(scopes, list) or not all(isinstance(value, str) for value in scopes):
            raise CatalogError(f"artifact {name!r} has invalid ci_scopes")
        if platform != f"linux/{architecture}":
            raise CatalogError(
                f"artifact {name!r} platform {platform!r} disagrees with {architecture!r}"
            )
        if role == "web":
            if not variant or base_path is None or not api_url:
                raise CatalogError(f"web artifact {name!r} needs variant, base_path, and api_url")
        elif any(value is not None for value in (variant, base_path, api_url)):
            raise CatalogError(f"non-web artifact {name!r} declares web metadata")
        entries.append(
            Artifact(
                name=name,
                role=role,
                variant=variant,
                base_path=base_path,
                api_url=api_url,
                platform=platform,
                architecture=architecture,
                dockerfile=dockerfile,
                runner_labels=tuple(runner_labels),
                smoke_key=smoke_key,
                ci_scopes=tuple(scopes),
            )
        )

    for field, values in (
        ("name", [entry.name for entry in entries]),
        ("smoke_key", [entry.smoke_key for entry in entries]),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise CatalogError(f"duplicate artifact {field}: {duplicates}")
    identities = [(entry.role, entry.variant) for entry in entries]
    if len(identities) != len(set(identities)):
        raise CatalogError("artifact role/variant identities must be unique")
    web_paths = [entry.base_path for entry in entries if entry.role == "web"]
    if len(web_paths) != len(set(web_paths)):
        raise CatalogError("web base paths must be unique")
    return tuple(entries)


@lru_cache
def artifacts() -> tuple[Artifact, ...]:
    return load_catalog()


def manifest_schema_version() -> int:
    contract = read_document().get("manifest_contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("schema_version"), int):
        raise CatalogError("artifact catalog must declare manifest_contract.schema_version")
    return contract["schema_version"]


def expected_platforms() -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for entry in artifacts():
        result.setdefault(entry.role, [])
        if entry.platform not in result[entry.role]:
            result[entry.role].append(entry.platform)
    return {role: tuple(platforms) for role, platforms in result.items()}


def web_variants() -> dict[str, str]:
    return {
        entry.base_path or "": entry.variant or "" for entry in artifacts() if entry.role == "web"
    }


def site_variants() -> dict[str, tuple[str, str]]:
    return {
        entry.variant or "": (entry.base_path or "", entry.api_url or "")
        for entry in artifacts()
        if entry.role == "web"
    }


def artifact_for(role: str, variant: str | None = None) -> Artifact:
    matches = [entry for entry in artifacts() if entry.role == role and entry.variant == variant]
    if len(matches) != 1:
        raise CatalogError(
            f"expected one catalog artifact for role {role!r}, variant {variant!r}; "
            f"found {len(matches)}"
        )
    return matches[0]
