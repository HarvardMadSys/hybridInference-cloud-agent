"""What "the deployment is what the release named" means, as a pure function.

Review killed the first plan for this job on two counts — a locally loaded image
cannot prove that a digest is pullable, and an image ID is not a manifest digest
— so the comparison lives here, where both are explicit and both can be tested
without a daemon.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

from deploy.release import accept_artifacts as aa

COMMIT = "3b69711" + "0" * 33


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def reference(role: str, variant: str | None = None) -> str:
    return f"localhost:5000/cloud-agent@{digest(f'{role}:{variant}')}"


def manifest() -> dict[str, Any]:
    return {
        "source_commit": COMMIT,
        "artifacts": [
            {"role": "control", "image": reference("control")},
            {"role": "web", "variant": "root", "base_path": "", "image": reference("web", "root")},
            {
                "role": "web",
                "variant": "agents",
                "base_path": "/agents",
                "image": reference("web", "agents"),
            },
        ],
    }


def observation(expected: dict[str, str], **overrides: Any) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "repo_digests": [f"localhost:5000/cloud-agent@{expected['digest']}"],
        "labels": {
            "org.opencontainers.image.revision": expected["revision"],
            "io.hybridinference.artifact.role": expected["role"],
        },
    }
    if "base_path" in expected:
        observed["labels"]["io.hybridinference.artifact.base_path"] = expected["base_path"]
    observed.update(overrides)
    return observed


def test_a_digest_reference_is_the_only_thing_that_names_an_artifact() -> None:
    assert aa.registry_digest(reference("control")) == digest("control:None")
    for bad in ("localhost:5000/cloud-agent:latest", "cloud-agent@sha256:short", ""):
        with pytest.raises(aa.AcceptanceError):
            aa.registry_digest(bad)


def test_every_service_gets_the_role_that_supplies_it() -> None:
    expected = aa.expected_services(manifest(), "control-plane", "agents")
    assert expected["control-plane"]["digest"] == digest("control:None")
    assert expected["mcp-relay"]["digest"] == digest("control:None")
    assert expected["web"]["digest"] == digest("web:agents")
    assert expected["web"]["base_path"] == "/agents"
    assert all(entry["revision"] == COMMIT for entry in expected.values())


def test_a_service_whose_role_is_missing_is_refused() -> None:
    document = manifest()
    document["artifacts"] = [a for a in document["artifacts"] if a["role"] != "control"]
    with pytest.raises(aa.AcceptanceError) as raised:
        aa.expected_services(document, "control-plane", "agents")
    assert "control" in str(raised.value)


def test_the_stack_needs_to_know_which_console_it_serves() -> None:
    with pytest.raises(aa.AcceptanceError):
        aa.expected_services(manifest(), "control-plane", None)


def test_a_correctly_deployed_service_passes() -> None:
    expected = aa.expected_services(manifest(), "control-plane", "agents")
    observed = {name: observation(want) for name, want in expected.items()}
    evidence = aa.check_running(expected, observed)
    assert len(evidence) == len(expected)
    assert all("runs sha256:" in line for line in evidence)


@pytest.mark.parametrize(
    ("mutation", "expected_text"),
    [
        pytest.param(
            lambda o: o.update(repo_digests=[]), "built here rather than pulled", id="locally built"
        ),
        pytest.param(
            lambda o: o.update(repo_digests=[f"localhost:5000/cloud-agent@{digest('other')}"]),
            "not sha256:",
            id="another image",
        ),
        pytest.param(
            lambda o: o["labels"].update(**{"org.opencontainers.image.revision": "b" * 40}),
            "names revision",
            id="another commit",
        ),
        pytest.param(
            lambda o: o["labels"].update(**{"io.hybridinference.artifact.role": "runner"}),
            "declares role",
            id="another role",
        ),
        pytest.param(
            lambda o: o["labels"].update(**{"io.hybridinference.artifact.base_path": ""}),
            "base path",
            id="the other mount shape",
        ),
    ],
)
def test_what_is_running_must_be_what_the_release_named(mutation, expected_text: str) -> None:
    """The image ID is not the answer, and a missing RepoDigests is not a skip.

    Each case below is a deployment that would work and be wrong: a locally built
    image standing in for a registry digest, another commit's bytes, a runner
    image answering for the control plane, and the other console variant.
    """
    expected = aa.expected_services(manifest(), "control-plane", "agents")
    observed = {name: observation(want) for name, want in expected.items()}
    mutation(observed["web"])
    with pytest.raises(aa.AcceptanceError) as raised:
        aa.check_running(expected, observed)
    assert expected_text in str(raised.value)


def test_a_service_that_is_not_running_is_refused() -> None:
    expected = aa.expected_services(manifest(), "control-plane", "agents")
    observed = {name: observation(want) for name, want in expected.items()}
    del observed["mcp-relay"]
    with pytest.raises(aa.AcceptanceError) as raised:
        aa.check_running(expected, observed)
    assert "mcp-relay" in str(raised.value)


def test_a_recipe_root_may_not_carry_the_source_it_replaces(tmp_path: Path) -> None:
    """The claim is "deploys without source", so the check is on the directory."""
    aa.source_free(tmp_path)
    # Three names until the consolidation -- `web`, `cloud_agent` and
    # `cloud_agent_host` -- are one directory now, so one case covers them.
    for name in (".git", "apps"):
        (tmp_path / name).mkdir()
        with pytest.raises(aa.AcceptanceError) as raised:
            aa.source_free(tmp_path)
        assert name in str(raised.value)
        (tmp_path / name).rmdir()
