"""Between the cells that push images and the manifest that names them.

Six jobs each push one image and come back with a digest and a label map. The
manifest generator (`release_manifest.py`) wants one document — the commit,
every artifact, the sandbox's labels — and refuses an incomplete one. This
module is the seam between the two, in three pieces the workflow calls in turn:

* `record` writes what one cell knows, and refuses to write a record that
  contradicts the cell it came from: an image that is not a digest under this
  organization's registry, a platform other than the one the role is built on,
  a role or revision label that does not match.
* `assemble` turns the records into the generator's `inputs.json`, and refuses a
  set that is not the release: a cell missing, a stray file, a record from
  another commit. The generator checks the shape again on its own terms; the
  checks here are the ones only the cells' point of view can make.
* `notes` renders the manifest for the release page, with the one number a
  distribution pins — the sha256 of the manifest's bytes — at the top.

Records are files rather than job outputs because six cells of one matrix
share one output namespace, and the last writer wins silently.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__:
    from . import release_manifest as rm, smoke
else:  # direct execution from the release directory
    import release_manifest as rm
    import smoke

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping, Sequence

#: The labels a record is checked against. The same three the smoke asserts on
#: the image before it is pushed, and the resolver checks in the registry.
ROLE_LABEL = "io.hybridinference.artifact.role"
BASE_PATH_LABEL = "io.hybridinference.artifact.base_path"
REVISION_LABEL = "org.opencontainers.image.revision"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class PublishError(Exception):
    """Raised when the cells' records do not describe a releasable version."""


@dataclass(frozen=True)
class Cell:
    """One matrix cell: the artifact it publishes, and the platform it builds on."""

    role: str
    variant: str | None
    base_path: str | None
    platform: str


def expected_cells() -> dict[str, Cell]:
    """The cells a release is assembled from, by name.

    Derived from the smoke's role table rather than typed: that table is what
    CI's matrix is pinned against, so the three — CI, the publication matrix
    and this collector — cannot disagree about which images a release has.
    """
    base_paths = {variant: path for path, variant in rm.WEB_VARIANTS.items()}
    cells: dict[str, Cell] = {}
    for role in smoke.ROLES.values():
        if role.variants:
            for variant in role.variants:
                cells[f"{role.name}-{variant}"] = Cell(
                    role.name, variant, base_paths[variant], role.arch
                )
        else:
            cells[role.name] = Cell(role.name, None, None, role.arch)
    return cells


def _image_pattern() -> re.Pattern[str]:
    # The published schema's own rule, so a record this module accepts is one
    # the manifest can carry.
    return re.compile(rm.load_schema()["$defs"]["artifact"]["properties"]["image"]["pattern"])


def validate_record(record: Mapping[str, Any], source_commit: str) -> Cell:
    """Refuse a record that contradicts the cell it claims to come from."""
    name = record.get("name")
    cell = expected_cells().get(str(name))
    if cell is None:
        raise PublishError(
            f"{name!r} is not a cell of this release; {sorted(expected_cells())} are"
        )

    image = str(record.get("image", ""))
    if not _image_pattern().fullmatch(image):
        raise PublishError(
            f"{name}: {image!r} is not a digest reference; a release pins digests, and a tag "
            "can be moved out from under a deployment"
        )
    if not image.startswith(rm.REGISTRY_PREFIX):
        raise PublishError(f"{name}: {image!r} is published outside {rm.REGISTRY_PREFIX}")

    platform = record.get("platform")
    if platform != cell.platform:
        raise PublishError(
            f"{name}: built on {platform!r}, and this release builds {cell.role} on "
            f"{cell.platform!r}. A cell on the wrong pool builds the wrong image and passes."
        )

    labels = record.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise PublishError(f"{name}: the record carries no labels, so the image cannot be verified")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
        raise PublishError(f"{name}: labels must be a map of strings to strings")

    if labels.get(ROLE_LABEL) != cell.role:
        raise PublishError(
            f"{name}: the image declares role {labels.get(ROLE_LABEL)!r}, expected {cell.role!r}"
        )
    if cell.variant is not None and labels.get(BASE_PATH_LABEL) != cell.base_path:
        raise PublishError(
            f"{name}: the image declares base path {labels.get(BASE_PATH_LABEL)!r}, expected "
            f"{cell.base_path!r} for variant {cell.variant!r}"
        )
    if not _COMMIT_RE.match(source_commit or ""):
        raise PublishError(f"source commit is {source_commit!r}, not a full 40-hex commit")
    if labels.get(REVISION_LABEL) != source_commit:
        raise PublishError(
            f"{name}: the image names revision {labels.get(REVISION_LABEL)!r}, and this release "
            f"is {source_commit!r}; an image from another commit is not this release"
        )
    return cell


def record(
    name: str,
    image: str,
    platform: str,
    labels: Mapping[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    """What one cell knows about the image it published, checked before it is written."""
    entry: dict[str, Any] = {
        "name": name,
        "image": image,
        "platform": platform,
        "labels": dict(labels),
    }
    validate_record(entry, source_commit)
    return entry


def read_cells(directory: Path) -> dict[str, dict[str, Any]]:
    """Every record in `directory`, keyed by cell name.

    A file is named for its cell, and the record inside has to agree: a file
    copied under another name would otherwise stand in for a cell that never
    ran.
    """
    files = sorted(directory.glob("*.json"))
    if not files:
        raise PublishError(f"{directory} holds no records; no cell handed anything over")
    records: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            raise PublishError(f"{path} is not JSON: {error}") from error
        if not isinstance(document, dict) or document.get("name") != path.stem:
            raise PublishError(
                f"{path} is named for {path.stem!r} but records "
                f"{document.get('name') if isinstance(document, dict) else document!r}"
            )
        records[path.stem] = document
    return records


def assemble(records: Mapping[str, Mapping[str, Any]], source_commit: str) -> dict[str, Any]:
    """The generator's input: every cell's artifact, and the sandbox's labels."""
    expected = expected_cells()
    missing = sorted(set(expected) - set(records))
    stray = sorted(set(records) - set(expected))
    if missing or stray:
        raise PublishError(
            f"the records are not this release: missing {missing}, stray {stray}. A release "
            "with five images is not a smaller release; it is one that leaves a machine half "
            "updated."
        )

    artifacts: list[dict[str, Any]] = []
    for name in sorted(expected):
        entry = records[name]
        cell = validate_record(entry, source_commit)
        artifact: dict[str, Any] = {
            "role": cell.role,
            "image": entry["image"],
            "platforms": [entry["platform"]],
        }
        if cell.variant is not None:
            artifact["base_path"] = cell.base_path
        artifacts.append(artifact)

    return {
        "source_commit": source_commit,
        "artifacts": artifacts,
        "runtime_labels": dict(records["sandbox"]["labels"]),
    }


def notes(manifest: Mapping[str, Any], manifest_sha256: str) -> str:
    """The release page: the number a distribution pins, then what the release holds."""
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or ""):
        raise PublishError(f"manifest sha256 is not a sha256 digest: {manifest_sha256!r}")
    commit = manifest["source_commit"]
    lines = [
        f"Cloud Agent release `agent-{commit}`, built from `{commit}` by the "
        f"[publication run]({manifest['publisher_run_url']}).",
        "",
        "A distribution pins this release by the sha256 of `release.json`, which is:",
        "",
        f"    {manifest_sha256}",
        "",
        f"Deployment protocol `{manifest['deploy_protocol']}`, database revision "
        f"`{manifest['database_revision']}`, recipe sha256 `{manifest['recipe']['sha256']}`.",
        "",
        "| Artifact | Platforms | Image |",
        "|---|---|---|",
    ]
    for artifact in manifest["artifacts"]:
        label = artifact["role"]
        if artifact["role"] == "web":
            label = f"web ({artifact['variant']})"
        lines.append(f"| {label} | {', '.join(artifact['platforms'])} | `{artifact['image']}` |")
    lines.append("")
    catalog = ", ".join(
        f"{runtime} {entry['default']} (menu: {', '.join(entry['versions'])})"
        for runtime, entry in manifest["runtime_catalog"].items()
    )
    lines.append(f"Runtime catalog: {catalog}.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    write = commands.add_parser("record", help="Write what one cell published.")
    write.add_argument("--name", required=True, help="The matrix cell, e.g. control or web-agents.")
    write.add_argument("--image", required=True, help="The digest reference that was pushed.")
    write.add_argument("--platform", required=True, help="The platform it was built on.")
    write.add_argument("--labels", type=Path, required=True, help="JSON label map of that image.")
    write.add_argument("--source-commit", required=True)
    write.add_argument("--output", type=Path, required=True)

    collect = commands.add_parser("assemble", help="Turn the cells' records into inputs.json.")
    collect.add_argument("--cells", type=Path, required=True, help="Directory of cell records.")
    collect.add_argument("--source-commit", required=True)
    collect.add_argument("--output", type=Path, required=True)

    render = commands.add_parser("notes", help="Render the release notes from the manifest.")
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--manifest-sha256", required=True)
    render.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "record":
            labels = json.loads(arguments.labels.read_text())
            if not isinstance(labels, dict):
                raise PublishError(f"{arguments.labels} does not hold a label map")
            document = record(
                arguments.name,
                arguments.image,
                arguments.platform,
                labels,
                arguments.source_commit,
            )
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
            print(f"recorded {arguments.name}: {arguments.image}")
        elif arguments.command == "assemble":
            document = assemble(read_cells(arguments.cells), arguments.source_commit)
            arguments.output.write_text(json.dumps(document, indent=2) + "\n")
            print(f"wrote {arguments.output}: {len(document['artifacts'])} artifacts")
        else:
            manifest = json.loads(arguments.manifest.read_text())
            arguments.output.write_text(notes(manifest, arguments.manifest_sha256))
            print(f"wrote {arguments.output}")
    except (PublishError, json.JSONDecodeError, OSError) as error:
        print(f"refusing to continue the publication: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
