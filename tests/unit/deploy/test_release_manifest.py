"""The release manifest, and the refusals that decide whether one exists.

FreeInference pins the sha256 of this document's bytes, so it is the whole
contract between the two repositories: what the generator accepts, a deployment
will believe. That makes the refusals the interesting half — a manifest missing
one artifact, or carrying the same image twice under two roles, or naming a
platform nothing was built for, looks perfectly well-formed to everyone
downstream.

Two implementations of the same contract are checked here: the generator
(`deploy/release/release_manifest.py`), which refuses to write such a document,
and the published schema (`deploy/release/manifest.schema.json`), which a
consumer validates against. They are written separately on purpose — the
generator's messages are for the person running the build, the schema's are for
another repository — so a case in this file asserts that a tampered manifest is
rejected by the schema, and the refusals above assert the generator refuses the
inputs that would have produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from deploy.release import release_manifest as rm

from cloud_agent_host import sandbox as host_sandbox

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((REPO_ROOT / "deploy/release/manifest.schema.json").read_text())
# Validate the unchanged schema once, then keep checking every manifest against
# it. jsonschema.validate would repeat the full meta-schema check for each case.
_SCHEMA_VALIDATOR_CLASS = jsonschema.validators.validator_for(SCHEMA)
_SCHEMA_VALIDATOR_CLASS.check_schema(SCHEMA)
SCHEMA_VALIDATOR = _SCHEMA_VALIDATOR_CLASS(SCHEMA)

COMMIT = "4af1b771ac8f736a1be24f20cb307012af114a18"
RUN_URL = "https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/34559400992"

ROLES = ("control", "runner", "sandbox", "tunnel")
RUNTIMES = ("claude-code", "codex", "pi", "opencode", "kilo")


def hexdigest(seed: str) -> str:
    """A bare digest, the way the manifest's `sha256` fields carry one."""
    return hashlib.sha256(seed.encode()).hexdigest()


def digest(seed: str) -> str:
    """An image reference's digest, algorithm included."""
    return f"sha256:{hexdigest(seed)}"


def artifact(role: str, *, base_path: str | None = None, seed: str | None = None, **extra: Any):
    reference = seed or f"{role}:{base_path}"
    entry: dict[str, Any] = {
        "role": role,
        "image": f"{rm.REGISTRY_PREFIX}cloud-agent@{digest(reference)}",
        "platforms": list(rm.EXPECTED_PLATFORMS[role]),
    }
    if base_path is not None:
        entry["base_path"] = base_path
    entry.update(extra)
    return entry


def built_artifacts() -> list[dict[str, Any]]:
    return [
        artifact("control"),
        artifact("web", base_path=""),
        artifact("web", base_path="/agents"),
        *(artifact(role) for role in ROLES[1:]),
    ]


def labels(**overrides: str) -> dict[str, str]:
    """The sandbox image's labels, including one that is not a runtime."""
    catalog: dict[str, str] = {"org.freeinference.helper.guest-observer.version": "1"}
    for index, runtime in enumerate(RUNTIMES):
        version = f"1.{index}.0"
        catalog[f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_LABEL_SUFFIX}"] = version
        catalog[f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_MENU_LABEL_SUFFIX}"] = (
            f"{version},1.{index - 1}.0" if index else version
        )
    catalog.update(overrides)
    return catalog


def web_entry(manifest: dict[str, Any], base_path: str = "") -> dict[str, Any]:
    """The web artifact for one mount shape. Position is not a stable address."""
    return next(
        entry
        for entry in manifest["artifacts"]
        if entry["role"] == "web" and entry.get("base_path") == base_path
    )


def role_entry(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    return next(entry for entry in manifest["artifacts"] if entry["role"] == role)


def build(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source_commit": COMMIT,
        "artifacts": built_artifacts(),
        "runtime_labels": labels(),
        "recipe_url": "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/agent-4af1b77/recipe.tar.gz",
        "recipe_sha256": hexdigest("recipe"),
        "publisher_run_url": RUN_URL,
        "protocol": rm.deploy_protocol(),
        "revision": "0004_residency_on_membership",
    }
    arguments.update(overrides)
    return rm.build_manifest(**arguments)


# ------------------------------------------------------------------ the shape


def test_a_complete_build_produces_a_manifest_the_schema_accepts() -> None:
    manifest = build()
    SCHEMA_VALIDATOR.validate(manifest)
    assert [entry["role"] for entry in manifest["artifacts"]] == sorted(
        ["control", "web", "web", "runner", "sandbox", "tunnel"]
    )
    assert {entry["variant"] for entry in manifest["artifacts"] if entry["role"] == "web"} == {
        "root",
        "agents",
    }


@pytest.mark.parametrize("missing", sorted(rm.EXPECTED_PLATFORMS))
def test_a_missing_role_is_refused_by_name(missing: str) -> None:
    artifacts = [entry for entry in built_artifacts() if entry["role"] != missing]
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=artifacts)
    assert missing in str(raised.value)


@pytest.mark.parametrize("duplicated", sorted(rm.EXPECTED_PLATFORMS))
def test_a_duplicated_role_is_refused(duplicated: str) -> None:
    artifacts = built_artifacts()
    extra = artifact(duplicated, base_path="" if duplicated == "web" else None, seed="duplicate")
    artifacts.append(extra)
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=artifacts)
    assert duplicated in str(raised.value)


def test_web_is_built_once_per_mount_shape() -> None:
    only_root = [entry for entry in built_artifacts() if entry.get("base_path") != "/agents"]
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=only_root)
    assert "mount shape" in str(raised.value)

    two_roots = [*built_artifacts(), artifact("web", base_path="", seed="web-root-again")]
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=two_roots)
    assert "mount shape" in str(raised.value)

    unknown_path = [
        entry if entry.get("base_path") != "/agents" else artifact("web", base_path="/agent")
        for entry in built_artifacts()
    ]
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=unknown_path)
    assert "base path" in str(raised.value)


def test_an_unknown_role_is_refused() -> None:
    stray = {
        "role": "migrate",
        "image": f"{rm.REGISTRY_PREFIX}cloud-agent@{digest('migrate')}",
        "platforms": ["linux/arm64"],
    }
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=[*built_artifacts(), stray])
    assert "unknown artifact role" in str(raised.value)


def test_one_image_cannot_stand_in_for_two_artifacts() -> None:
    shared = f"{rm.REGISTRY_PREFIX}cloud-agent@{digest('shared')}"
    artifacts = built_artifacts()
    for entry in artifacts:
        if entry["role"] in {"control", "runner"}:
            entry["image"] = shared
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=artifacts)
    assert "same image" in str(raised.value)


def test_the_two_web_variants_cannot_share_a_digest() -> None:
    artifacts = built_artifacts()
    first = next(entry for entry in artifacts if entry.get("base_path") == "")
    second = next(entry for entry in artifacts if entry.get("base_path") == "/agents")
    second["image"] = first["image"]
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=artifacts)
    assert "never built" in str(raised.value)


@pytest.mark.parametrize(
    ("role", "platforms"),
    [
        ("runner", ["linux/arm64"]),
        ("sandbox", ["linux/amd64", "linux/arm64"]),
        ("control", ["linux/amd64"]),
        ("web", []),
    ],
)
def test_platforms_must_be_what_this_release_builds(role: str, platforms: list[str]) -> None:
    artifacts = built_artifacts()
    for entry in artifacts:
        if entry["role"] == role and (role != "web" or entry.get("base_path") == ""):
            entry["platforms"] = platforms
    with pytest.raises(rm.ManifestError) as raised:
        build(artifacts=artifacts)
    assert "platforms" in str(raised.value)


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/harvardmadsys/cloud-agent:4af1b77",  # a tag, not a digest
        f"ghcr.io/harvardmadsys/cloud-agent@sha256:{'a' * 63}",  # short digest
        f"ghcr.io/harvardmadsys/cloud-agent@sha256:{'A' * 64}",  # not lowercase hex
        f"docker.io/harvardmadsys/cloud-agent@{digest('elsewhere')}",  # wrong registry
    ],
)
def test_image_references_must_be_our_digests(image: str) -> None:
    artifacts = built_artifacts()
    artifacts[0]["image"] = image
    with pytest.raises(rm.ManifestError):
        build(artifacts=artifacts)


@pytest.mark.parametrize("commit", ["4af1b77", "4AF1B771AC8F736A1BE24F20CB307012AF114A18", ""])
def test_source_commit_must_be_the_full_commit(commit: str) -> None:
    with pytest.raises(rm.ManifestError) as raised:
        build(source_commit=commit)
    assert "source_commit" in str(raised.value)


@pytest.mark.parametrize(
    ("recipe_url", "recipe_sha256"),
    [("http://example.test/recipe.tar.gz", "0" * 64), ("https://example.test/r.tgz", "nothex")],
)
def test_the_recipe_is_pinned(recipe_url: str, recipe_sha256: str) -> None:
    with pytest.raises(rm.ManifestError):
        build(recipe_url=recipe_url, recipe_sha256=recipe_sha256)


def test_the_publisher_run_is_recorded() -> None:
    with pytest.raises(rm.ManifestError) as raised:
        build(publisher_run_url="https://example.test/somewhere")
    assert "publisher_run_url" in str(raised.value)


# ---------------------------------------------------------- the runtime catalog


def test_the_runtime_catalog_is_the_images_own_labels() -> None:
    manifest = build()
    catalog = manifest["runtime_catalog"]
    assert sorted(catalog) == sorted(RUNTIMES)
    assert catalog["claude-code"] == {"default": "1.0.0", "versions": ["1.0.0"]}
    assert catalog["codex"] == {"default": "1.1.0", "versions": ["1.1.0", "1.0.0"]}
    # The menu keeps the image's order, default first, rather than being sorted.
    assert catalog["codex"]["versions"][0] == catalog["codex"]["default"]
    # A helper label describes a helper, not a runtime a session may run.
    assert "guest-observer" not in catalog


def test_a_runtime_default_must_be_in_its_own_menu() -> None:
    with pytest.raises(rm.ManifestError) as raised:
        build(
            runtime_labels=labels(
                **{f"{rm.RUNTIME_LABEL_PREFIX}codex{rm.RUNTIME_MENU_LABEL_SUFFIX}": "9.9.9"}
            )
        )
    assert "menu" in str(raised.value)


def test_a_half_written_runtime_label_pair_is_refused() -> None:
    without_menu = labels()
    del without_menu[f"{rm.RUNTIME_LABEL_PREFIX}kilo{rm.RUNTIME_MENU_LABEL_SUFFIX}"]
    with pytest.raises(rm.ManifestError) as raised:
        build(runtime_labels=without_menu)
    assert "kilo" in str(raised.value)


def test_an_unlabelled_sandbox_cannot_be_released() -> None:
    with pytest.raises(rm.ManifestError) as raised:
        build(runtime_labels={"org.opencontainers.image.revision": COMMIT})
    assert "labels" in str(raised.value)


def test_the_label_names_are_the_ones_the_host_reads() -> None:
    """Two modules read these labels; a rename in one must not pass silently.

    `cloud_agent_host.sandbox` reads them at claim time to stamp an attempt, and
    this generator reads them to describe a release. The names are duplicated
    rather than imported — a publication job has no business loading the host
    package — so the agreement is asserted here.
    """
    assert rm.RUNTIME_LABEL_PREFIX == host_sandbox.RUNTIME_VERSION_LABEL_PREFIX
    assert rm.RUNTIME_LABEL_SUFFIX == host_sandbox.RUNTIME_VERSION_LABEL_SUFFIX
    assert rm.RUNTIME_MENU_LABEL_SUFFIX == host_sandbox.RUNTIME_VERSION_MENU_LABEL_SUFFIX


# --------------------------------------------------- versions read from sources


def test_the_protocol_comes_from_the_file_and_matches_the_schema() -> None:
    assert rm.deploy_protocol() == rm.DEPLOY_PROTOCOL_FILE.read_text().strip()
    assert rm.deploy_protocol() == SCHEMA["properties"]["deploy_protocol"]["const"]


def test_the_database_revision_is_alembics_head() -> None:
    """A property, not the current value: a head is whatever the chain ends at."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (REPO_ROOT / "migrations/versions").glob("*.py"):
        text = path.read_text()
        revision = next(
            (line for line in text.splitlines() if line.startswith("revision = ")), None
        )
        parent = next(
            (line for line in text.splitlines() if line.startswith("down_revision = ")), None
        )
        assert revision is not None and parent is not None, f"{path} declares no chain"
        revisions.add(revision.split("=", 1)[1].strip().strip('"'))
        parent_value = parent.split("=", 1)[1].strip()
        if parent_value != "None":
            parents.add(parent_value.strip('"'))

    head = rm.database_revision()
    assert head in revisions
    assert head not in parents


@pytest.mark.parametrize(
    ("document", "sentence"),
    [
        ("ENVIRONMENT.md", r"Run `alembic upgrade head` \(through `([^`]+)`\)"),
        ("docs/developer/upgrade-notes.md", r"run `alembic upgrade head` through\s+`([^`]+)`"),
    ],
)
def test_the_upgrade_instructions_name_the_revision_this_release_migrates_to(
    document: str, sentence: str
) -> None:
    """An operator comparing ``alembic current`` with the docs sees the real head.

    Both documents name the revision ``alembic upgrade head`` ends at, and
    both kept naming an older one while later revisions were added, so a
    database missing those revisions read as current.
    """
    named = re.findall(sentence, (REPO_ROOT / document).read_text())
    assert named == [rm.database_revision()]


# ------------------------------------------------- the schema refuses tampering


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda m: m.update(schema_version=2), id="unknown schema version"),
        pytest.param(lambda m: m.update(source_commit="4af1b77"), id="short commit"),
        pytest.param(lambda m: m.update(deploy_protocol="whatever"), id="unknown protocol"),
        pytest.param(lambda m: m.update(artifacts=[]), id="no artifacts"),
        pytest.param(lambda m: m["artifacts"][0].update(role="migrate"), id="unknown role"),
        pytest.param(
            lambda m: web_entry(m).update(image="ghcr.io/harvardmadsys/cloud-agent:tag"),
            id="tag instead of digest",
        ),
        pytest.param(lambda m: web_entry(m).pop("variant"), id="web without a variant"),
        pytest.param(
            lambda m: web_entry(m).update(base_path="/agents"), id="variant that disagrees"
        ),
        pytest.param(
            lambda m: role_entry(m, "runner").update(platforms=["linux/win32"]),
            id="unknown platform",
        ),
        pytest.param(lambda m: role_entry(m, "sandbox").update(platforms=[]), id="no platforms"),
        pytest.param(lambda m: m["runtime_catalog"]["codex"].update(versions=[]), id="empty menu"),
        pytest.param(lambda m: m.update(database_revision=""), id="no database revision"),
        pytest.param(lambda m: m["recipe"].update(sha256="nothex"), id="unpinned recipe"),
        pytest.param(
            lambda m: m["compatibility"].update(verified_rollback_to=["nope"]),
            id="unverified rollback claim",
        ),
        pytest.param(
            lambda m: m.update(publisher_run_url="https://example.test/run"),
            id="publisher outside the repository",
        ),
        pytest.param(lambda m: m.update(extra="field"), id="unknown field"),
    ],
)
def test_the_schema_rejects_a_tampered_manifest(tamper) -> None:
    manifest = build()
    tamper(manifest)
    with pytest.raises(jsonschema.ValidationError):
        SCHEMA_VALIDATOR.validate(manifest)


def test_membership_is_the_invariant_the_schema_cannot_express() -> None:
    """A JSON Schema cannot say "this string is one of that array".

    The runtime catalog's `default` must appear in its own `versions`, and the
    generator refuses a catalog where it does not — but the published schema
    accepts it, because expressing membership is outside what the format can
    check. Written down here because "the schema validated it" is exactly the
    assumption that would let a session ask for a version the image does not
    have, and the resolver that consumes this document has to check it too.
    """
    manifest = build()
    manifest["runtime_catalog"]["codex"]["default"] = "9.9.9"
    SCHEMA_VALIDATOR.validate(manifest)

    with pytest.raises(rm.ManifestError) as raised:
        build(
            runtime_labels=labels(
                **{f"{rm.RUNTIME_LABEL_PREFIX}codex{rm.RUNTIME_MENU_LABEL_SUFFIX}": "1.0.0"}
            )
        )
    assert "menu" in str(raised.value)


def test_the_schema_and_the_generator_refuse_the_same_thing() -> None:
    """Completeness is a contract both halves have to carry.

    A role dropped from the build is the failure a schema cannot see by
    counting: the list is still a list, and one of the right length holding the
    wrong roles is the same broken release wearing a valid-looking shape.
    Review found exactly that hole — `minItems: 7` accepted a build that lost
    its sandbox and gained a second control plane — so the schema now states the
    cardinality per role and per web variant, and both halves are checked
    against the case that found it.
    """
    impostor = build()
    impostor["artifacts"] = [
        entry for entry in impostor["artifacts"] if entry["role"] != "sandbox"
    ] + [dict(next(e for e in impostor["artifacts"] if e["role"] == "control"))]
    with pytest.raises(jsonschema.ValidationError):
        SCHEMA_VALIDATOR.validate(impostor)
    with pytest.raises(rm.ManifestError):
        build(
            artifacts=[
                *[e for e in built_artifacts() if e["role"] != "sandbox"],
                artifact("control", seed="impostor"),
            ]
        )

    # An eighth artifact of a role that is already present, same story.
    eighth = build()
    eighth["artifacts"].append(dict(eighth["artifacts"][0]))
    with pytest.raises(jsonschema.ValidationError):
        SCHEMA_VALIDATOR.validate(eighth)
    with pytest.raises(rm.ManifestError):
        build(artifacts=[*built_artifacts(), artifact("control", seed="eighth")])

    # And a second web build of a variant the release already carries.
    duplicated_variant = build()
    duplicated_variant["artifacts"].append(
        dict(next(e for e in duplicated_variant["artifacts"] if e.get("variant") == "root"))
    )
    with pytest.raises(jsonschema.ValidationError):
        SCHEMA_VALIDATOR.validate(duplicated_variant)
    with pytest.raises(rm.ManifestError):
        build(artifacts=[*built_artifacts(), artifact("web", base_path="", seed="again")])


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"verified_upgrade_from": ["dev"]}, id="a branch name, not a commit"),
        pytest.param({"verified_rollback_to": [COMMIT, COMMIT]}, id="a repeat"),
        pytest.param(
            {
                "runtime_labels": {
                    **labels(),
                    f"{rm.RUNTIME_LABEL_PREFIX}codex{rm.RUNTIME_MENU_LABEL_SUFFIX}": "1.1.0,1.1.0",
                }
            },
            id="a runtime version listed twice",
        ),
    ],
)
def test_the_generator_refuses_what_only_the_schema_catches(overrides: dict[str, Any]) -> None:
    """The generator's own checks are not the whole contract, and must not be.

    These three are expressible in the schema and nowhere in the code above: a
    compatibility range that is not a commit, a list with a repeat, a runtime
    menu naming one version twice. Review reproduced the first through the real
    CLI — exit code 0, file written, and the release rejected by this PR's own
    schema — which is why the generator now validates its output against the
    published document before writing anything.
    """
    with pytest.raises(rm.ManifestError) as raised:
        build(**overrides)
    assert "does not satisfy" in str(raised.value)


# ------------------------------------------------------------------- the CLI


def write_inputs(path: Path, **overrides: Any) -> Path:
    document = {
        "source_commit": COMMIT,
        "artifacts": built_artifacts(),
        "runtime_labels": labels(),
    }
    document.update(overrides)
    path.write_text(json.dumps(document))
    return path


def test_the_cli_writes_a_manifest(tmp_path: Path) -> None:
    inputs = write_inputs(tmp_path / "inputs.json")
    output = tmp_path / "release.json"
    code = rm.main(
        [
            "--inputs",
            str(inputs),
            "--recipe-url",
            "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/x/r.tgz",
            "--recipe-sha256",
            hexdigest("recipe"),
            "--publisher-run-url",
            RUN_URL,
            "--output",
            str(output),
        ]
    )
    assert code == 0
    written = json.loads(output.read_text())
    SCHEMA_VALIDATOR.validate(written)
    # Defaults come from the repository, not from the caller.
    assert written["deploy_protocol"] == rm.deploy_protocol()
    assert written["database_revision"] == rm.database_revision()
    assert written["compatibility"]["minimum_deployer"] == rm.deploy_protocol()


def test_a_refused_run_writes_nothing(tmp_path: Path) -> None:
    """The output file is a consumable document with a hash.

    A partially written or stale-but-valid-looking manifest is worse than none,
    so a refusal must leave no file behind — including the case where the
    caller points `--output` at a path that already exists.
    """
    inputs = write_inputs(tmp_path / "inputs.json", artifacts=built_artifacts()[:-1])
    output = tmp_path / "release.json"
    assert (
        rm.main(
            [
                "--inputs",
                str(inputs),
                "--recipe-url",
                "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/x/r.tgz",
                "--recipe-sha256",
                hexdigest("recipe"),
                "--publisher-run-url",
                RUN_URL,
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def cli_arguments(inputs: Path, output: Path) -> list[str]:
    return [
        "--inputs",
        str(inputs),
        "--recipe-url",
        "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/x/r.tgz",
        "--recipe-sha256",
        hexdigest("recipe"),
        "--publisher-run-url",
        RUN_URL,
        "--output",
        str(output),
    ]


def test_a_schema_invalid_manifest_is_never_written(tmp_path: Path) -> None:
    """The case review reproduced against the real CLI.

    `--verified-upgrade-from dev` reached the writer: exit code 0, file
    written, and this PR's own schema rejecting the document a moment later. A
    publication job that reports success while producing an unconsumable
    release is the worst place for this to be discovered, so the CLI is checked
    for it here rather than trusted to the library.
    """
    inputs = write_inputs(tmp_path / "inputs.json")
    output = tmp_path / "release.json"
    code = rm.main(
        [
            *cli_arguments(inputs, output),
            "--verified-upgrade-from",
            "dev",
        ]
    )
    assert code == 1
    assert not output.exists()
