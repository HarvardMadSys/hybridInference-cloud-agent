"""Build one release manifest from what a build actually produced.

A release is one document. FreeInference pins the sha256 of this document's
bytes and nothing else, so everything a deployment needs to know about a
version lives here: the commit, every artifact pinned by digest, the platforms
each was built on, the Agent CLIs the sandbox contains, the deployment recipe,
and what the release has actually been exercised against.

What this module checks, and what it deliberately does not:

* **It checks the shape of a release.** Every role present exactly once, web
  present once per mount shape, no digest reused across roles, each artifact
  built on exactly the platforms it is expected on, image references that are
  digest references under this organization's registry. A missing artifact is
  not a smaller release — it is a release that would leave a machine half
  updated — so these are refusals, not warnings.
* **It validates its own output against the published schema.** That schema is
  the document a consumer holds, so the generator checks its own work against it
  before writing anything: a publication job must not be able to produce a
  manifest that a distribution refuses later, on a machine nobody is watching.
* **It reads versions from their real sources.** `deploy_protocol` comes from
  `deploy/DEPLOY_PROTOCOL`, `database_revision` from Alembic's own script
  directory, and the runtime catalog from the sandbox image's labels, which the
  build derives from the effective build arguments. None of them is typed here,
  because a typed version is a version that can disagree with the artifact it
  describes.
* **It does not verify the images.** This runs where the images were built, on
  their way out. That a digest exists, that its index really carries the
  declared platforms, and that each platform image's
  `org.opencontainers.image.revision` equals `source_commit` are facts about
  the registry, and they are checked where the release is *consumed* — by the
  distribution's resolver, which must treat this document as a claim rather
  than as evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE_REPOSITORY = "HarvardMadSys/hybridInference-cloud-agent"
SCHEMA_VERSION = 1
RECIPE_FORMAT_VERSION = 1

#: Every image this release carries is published under this registry path.
#: Which package or tag it lands in is storage layout — a deployment addresses
#: artifacts by digest, so that part is free to change without a code change.
REGISTRY_PREFIX = "ghcr.io/harvardmadsys/"

#: The value, read from that file rather than typed here: the file is what the
#: deploy scripts compare against, and a manifest that named a different
#: protocol would describe a deployment that cannot install it.
DEPLOY_PROTOCOL_FILE = REPO_ROOT / "deploy" / "DEPLOY_PROTOCOL"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"

#: The published contract, and the document this module validates its own output
#: against before writing it.
SCHEMA_PATH = REPO_ROOT / "deploy" / "release" / "manifest.schema.json"

#: The roles a release must carry, and the platforms each is built on.
#:
#: Exact rather than a minimum: this is the set the publication flow builds and
#: starts, and "the control plane is arm64 while the hosts are amd64" is a fact
#: about the deployment, not a preference. A release that wants a second
#: platform has to add it to the build first, which is the point — declaring a
#: platform nobody built is how a single-platform image gets called multi-arch.
EXPECTED_PLATFORMS: dict[str, tuple[str, ...]] = {
    "control": ("linux/arm64",),
    "web": ("linux/arm64",),
    "runner": ("linux/amd64",),
    "sandbox": ("linux/amd64",),
    "tunnel": ("linux/amd64",),
    "egress": ("linux/amd64",),
}

#: Web is built per mount shape, because `basePath` is resolved while the
#: bundle is compiled. One artifact per entry here, and the manifest names the
#: variant so a deployment pins the one it serves rather than guessing.
WEB_VARIANTS: dict[str, str] = {"": "root", "/agents": "agents"}

#: The sandbox image's runtime labels. Duplicated from
#: `cloud_agent_host.sandbox` on purpose — this module is imported by a
#: publication job that has no business loading the host package — and
#: `test_release_manifest.py` fails if the two ever disagree.
RUNTIME_LABEL_PREFIX = "org.freeinference.runtime."
RUNTIME_LABEL_SUFFIX = ".version"
RUNTIME_MENU_LABEL_SUFFIX = ".versions"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(Exception):
    """Raised when the inputs cannot describe a releasable version."""


def deploy_protocol(path: Path = DEPLOY_PROTOCOL_FILE) -> str:
    """The deployment protocol these artifacts require."""
    value = path.read_text().strip()
    if not value:
        raise ManifestError(f"{path} is empty: a release must name its deployment protocol")
    return value


def database_revision(alembic_ini: Path = ALEMBIC_INI) -> str:
    """The Alembic revision this release migrates to.

    Asked of Alembic rather than parsed or typed. A release that named a
    revision the migration scripts do not head at would have a preflight
    compare two different things and call it agreement; two heads is a
    repository state that cannot be released at all, so it raises.
    """
    config = Config(str(alembic_ini))
    # Explicit, so the answer does not depend on the working directory: a
    # deploy job runs this from wherever it happens to be.
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    try:
        head = ScriptDirectory.from_config(config).get_current_head()
    except CommandError as error:
        raise ManifestError(f"migrations do not have a single head: {error}") from error
    if not head:
        raise ManifestError("migrations declare no revision to migrate to")
    return head


def read_runtime_catalog(labels: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The Agent CLIs inside the sandbox image, from that image's own labels.

    Two labels per runtime, and both are needed: `.version` is what a session
    runs when it asks for nothing in particular, `.versions` is the menu the
    image installed. The build writes them from the same arguments it installs
    from, and refuses to finish if a binary reports a version its label does
    not, so this is the image describing itself rather than this module
    guessing from a Dockerfile default that CI may have overridden.
    """
    defaults: dict[str, str] = {}
    menus: dict[str, list[str]] = {}

    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not key.startswith(RUNTIME_LABEL_PREFIX):
            continue
        if key.endswith(RUNTIME_MENU_LABEL_SUFFIX):
            runtime_id = key[len(RUNTIME_LABEL_PREFIX) : -len(RUNTIME_MENU_LABEL_SUFFIX)]
            menus[runtime_id] = [item.strip() for item in value.split(",") if item.strip()]
        elif key.endswith(RUNTIME_LABEL_SUFFIX):
            runtime_id = key[len(RUNTIME_LABEL_PREFIX) : -len(RUNTIME_LABEL_SUFFIX)]
            defaults[runtime_id] = value.strip()

    if not defaults:
        raise ManifestError(
            "the sandbox image carries no "
            f"{RUNTIME_LABEL_PREFIX}*{RUNTIME_LABEL_SUFFIX} labels, so the release cannot say "
            "which Agent CLIs it contains. An unlabelled image is ineligible to claim work, "
            "and it must not be published as a release either."
        )

    unpaired = sorted(set(defaults) ^ set(menus))
    if unpaired:
        raise ManifestError(
            "these runtimes have a default version label without a menu label, or the "
            f"reverse, so the image disagrees with itself: {unpaired}"
        )

    catalog: dict[str, dict[str, Any]] = {}
    for runtime_id in sorted(defaults):
        default = defaults[runtime_id]
        versions = menus[runtime_id]
        if not default:
            raise ManifestError(f"{runtime_id} has an empty default version label")
        if not versions:
            raise ManifestError(f"{runtime_id} has an empty version menu label")
        if default not in versions:
            raise ManifestError(
                f"{runtime_id} defaults to {default!r}, which its own menu {versions!r} "
                "does not contain"
            )
        # Order is the image's, default first, and is kept rather than sorted:
        # the admin surface shows the menu in this order.
        catalog[runtime_id] = {"default": default, "versions": versions}
    return catalog


def _image_digest(image: str, role: str) -> str:
    if "@" not in image:
        raise ManifestError(
            f"artifact {role!r} is {image!r}: a release pins digests, and a tag can be "
            "moved out from under a deployment"
        )
    repository, _, digest = image.partition("@")
    if not _DIGEST_RE.match(digest):
        raise ManifestError(f"artifact {role!r} has a malformed digest: {digest!r}")
    if not repository.startswith(REGISTRY_PREFIX):
        raise ManifestError(
            f"artifact {role!r} is published outside {REGISTRY_PREFIX}: {repository!r}"
        )
    return digest


def validate_artifacts(raw: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Check the artifact set, and return it in the manifest's own shape.

    Four ways a build can produce something that looks complete and is not:
    a role missing, a role duplicated (two control planes, one of which the
    manifest silently drops), a variant missing (a deployment that serves
    `/agents` and gets the root build), and one image published under two
    roles. Each is refused by name.
    """
    by_role: dict[str, list[dict[str, Any]]] = {}
    for entry in raw:
        role = entry.get("role")
        if role not in EXPECTED_PLATFORMS:
            raise ManifestError(
                f"unknown artifact role {role!r}; this release knows {sorted(EXPECTED_PLATFORMS)}"
            )
        artifact: dict[str, Any] = {
            "role": role,
            "image": entry.get("image"),
            "platforms": list(entry.get("platforms") or ()),
        }
        if role == "web":
            base_path = entry.get("base_path", "")
            if base_path not in WEB_VARIANTS:
                raise ManifestError(
                    f"web artifact has base path {base_path!r}; the built variants are "
                    f"{sorted(WEB_VARIANTS)}"
                )
            artifact["variant"] = WEB_VARIANTS[base_path]
            artifact["base_path"] = base_path
        elif "base_path" in entry or "variant" in entry:
            raise ManifestError(f"artifact {role!r} declares a web variant")
        by_role.setdefault(role, []).append(artifact)

    expected_roles = set(EXPECTED_PLATFORMS)
    seen_roles = set(by_role)
    if seen_roles != expected_roles:
        missing = sorted(expected_roles - seen_roles)
        extra = sorted(seen_roles - expected_roles)
        raise ManifestError(f"artifact set is not this release: missing {missing}, extra {extra}")

    artifacts: list[dict[str, Any]] = []
    for role in sorted(by_role):
        entries = by_role[role]
        if role == "web":
            variants = sorted(entry["variant"] for entry in entries)
            if variants != sorted(WEB_VARIANTS.values()):
                raise ManifestError(
                    f"web is built once per mount shape, and this release has {variants}; "
                    f"expected {sorted(WEB_VARIANTS.values())}"
                )
        elif len(entries) != 1:
            raise ManifestError(f"role {role!r} appears {len(entries)} times; once is the release")

        for artifact in entries:
            digest = _image_digest(artifact["image"], role)
            platforms = artifact["platforms"]
            expected = list(EXPECTED_PLATFORMS[role])
            if sorted(set(platforms)) != sorted(expected) or len(set(platforms)) != len(platforms):
                raise ManifestError(
                    f"artifact {role!r} declares platforms {platforms}; this release builds it "
                    f"on {expected}. Declaring a platform nobody built, or dropping one that "
                    "was, is how a single-platform image gets deployed as a multi-architecture "
                    "one."
                )
            artifact["_digest"] = digest
        artifacts.extend(entries)

    digests: dict[str, str] = {}
    for artifact in artifacts:
        digest = artifact.pop("_digest")
        label = artifact["role"] if artifact["role"] != "web" else f"web/{artifact['variant']}"
        owner = digests.setdefault(digest, label)
        if owner != label:
            raise ManifestError(
                f"artifacts {owner!r} and {label!r} are the same image ({digest}); two "
                "entries sharing bytes means one of them was never built, and a release "
                "must not publish that as if it were checked"
            )
    return artifacts


def build_manifest(
    *,
    source_commit: str,
    artifacts: Sequence[Mapping[str, Any]],
    runtime_labels: Mapping[str, Any],
    recipe_url: str,
    recipe_sha256: str,
    publisher_run_url: str,
    minimum_deployer: str | None = None,
    verified_upgrade_from: Sequence[str] = (),
    verified_rollback_to: Sequence[str] = (),
    protocol: str | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Assemble the manifest. Raises `ManifestError` rather than writing one.

    `protocol` and `revision` are injectable for tests; production passes
    neither and gets the file and the migration scripts.
    """
    if not _COMMIT_RE.match(source_commit or ""):
        raise ManifestError(
            f"source_commit is {source_commit!r}; a release names the full 40-hex commit it "
            "was built from, because that is what every image's revision label is compared "
            "against"
        )
    if not recipe_url.startswith("https://"):
        raise ManifestError(f"recipe url must be an https URL, got {recipe_url!r}")
    if not re.match(r"^[0-9a-f]{64}$", recipe_sha256 or ""):
        raise ManifestError(f"recipe sha256 is not a sha256 digest: {recipe_sha256!r}")
    run_prefix = f"https://github.com/{SOURCE_REPOSITORY}/actions/runs/"
    if (
        not publisher_run_url.startswith(run_prefix)
        or not publisher_run_url[len(run_prefix) :].isdigit()
    ):
        raise ManifestError(
            f"publisher_run_url is not a {SOURCE_REPOSITORY} workflow run URL: "
            f"{publisher_run_url!r}. A release points at the trusted run that produced it."
        )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": source_commit,
        "deploy_protocol": protocol or deploy_protocol(),
        "artifacts": validate_artifacts(artifacts),
        "runtime_catalog": read_runtime_catalog(runtime_labels),
        "recipe": {
            "url": recipe_url,
            "sha256": recipe_sha256,
            "format_version": RECIPE_FORMAT_VERSION,
        },
        "database_revision": revision or database_revision(),
        "compatibility": {
            "minimum_deployer": minimum_deployer or protocol or deploy_protocol(),
            "verified_upgrade_from": list(verified_upgrade_from),
            "verified_rollback_to": list(verified_rollback_to),
        },
        "publisher_run_url": publisher_run_url,
    }
    validate_against_schema(manifest)
    return manifest


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """The published contract, read from the file a consumer validates against."""
    return json.loads(path.read_text())


def validate_against_schema(manifest: Mapping[str, Any], path: Path = SCHEMA_PATH) -> None:
    """Refuse a document the published schema would reject.

    The checks above and the schema are two implementations of one contract,
    and the schema is the one a consumer holds. Validating here means a
    publication job cannot write a document that a distribution refuses later:
    the failure lands on the machine that produced it, while the build is still
    in front of the person who can fix it.

    The two do not overlap by accident. Anything expressible in the schema —
    field types, digest and URL shapes, `uniqueItems` on the compatibility and
    runtime-version lists, exactly one artifact per role — is checked there as
    well, and this call is what keeps the generator from drifting away from it.
    What the schema cannot express (a runtime default appearing in its own
    menu) stays in the functions above.

    `jsonschema` is a dev dependency, not a runtime one: it is needed here and
    in the test suite, while the shipped images install only `pip install .`.
    An environment that cannot import it fails rather than skipping the check —
    an unvalidated manifest is exactly what this function exists to prevent.
    """
    try:
        import jsonschema
    except ImportError as error:  # pragma: no cover - the publication env has dev deps
        raise ManifestError(
            f"jsonschema is not installed, so this manifest cannot be validated against "
            f"{path.name}. Install the dev dependencies in the environment that publishes "
            "releases; a manifest that was never validated must not be written."
        ) from error

    try:
        jsonschema.validate(instance=dict(manifest), schema=load_schema(path))
    except jsonschema.ValidationError as error:
        where = "/".join(str(part) for part in error.absolute_path) or "<document>"
        raise ManifestError(
            f"the manifest does not satisfy {path.name} at {where}: {error.message}"
        ) from error


def _inputs(path: Path) -> tuple[str, Sequence[Mapping[str, Any]], Mapping[str, Any]]:
    """Read what the publication job wrote: what it built, and what it saw."""
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ManifestError(f"{path} is not JSON: {error}") from error
    for key in ("source_commit", "artifacts", "runtime_labels"):
        if key not in document:
            raise ManifestError(f"{path} has no {key!r}: the publication job writes all three")
    return document["source_commit"], document["artifacts"], document["runtime_labels"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build one Cloud Agent release manifest from a build's own output."
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        required=True,
        help="JSON written by the publication job: source_commit, artifacts, runtime_labels.",
    )
    parser.add_argument("--recipe-url", required=True)
    parser.add_argument("--recipe-sha256", required=True)
    parser.add_argument("--publisher-run-url", required=True)
    parser.add_argument(
        "--minimum-deployer",
        default=None,
        help="Deployment protocol a deployer must implement. Defaults to deploy/DEPLOY_PROTOCOL.",
    )
    parser.add_argument("--verified-upgrade-from", action="append", default=[])
    parser.add_argument("--verified-rollback-to", action="append", default=[])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("release.json"),
        help="Where to write the manifest. Written only if every check passes.",
    )
    arguments = parser.parse_args(argv)

    try:
        source_commit, artifacts, runtime_labels = _inputs(arguments.inputs)
        manifest = build_manifest(
            source_commit=source_commit,
            artifacts=artifacts,
            runtime_labels=runtime_labels,
            recipe_url=arguments.recipe_url,
            recipe_sha256=arguments.recipe_sha256,
            publisher_run_url=arguments.publisher_run_url,
            minimum_deployer=arguments.minimum_deployer,
            verified_upgrade_from=arguments.verified_upgrade_from,
            verified_rollback_to=arguments.verified_rollback_to,
        )
    except ManifestError as error:
        print(f"refusing to write a release manifest: {error}", file=sys.stderr)
        return 1

    # Written last and in one piece: a half-written manifest is a consumable
    # document with a correct-looking hash, which is worse than none.
    arguments.output.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(
        f"wrote {arguments.output}: {len(manifest['artifacts'])} artifacts, "
        f"runtime catalog {sorted(manifest['runtime_catalog'])}, "
        f"database revision {manifest['database_revision']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
