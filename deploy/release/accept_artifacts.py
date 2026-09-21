"""The comparison a control-plane acceptance makes, without a Docker daemon.

The acceptance job deploys the control-plane stack from a release manifest — in
a directory with no source, pulling images from a registry, through the real
deploy script and the real readiness code — and then has to answer one question:
**is what is running what the release named?** That question is answered here, as
pure functions, so it can be tested without a daemon and so the job itself is
only orchestration.

Two things this deliberately gets right, both of which review caught:

* **A digest is not an image ID.** `docker inspect` on a container reports the
  image ID — the local config digest — while a manifest names the *manifest*
  digest a registry serves. They are different objects, and comparing them
  either always fails or, worse, is quietly skipped. The comparison below takes
  the digest from `RepoDigests`, which is the registry-side identity, and it
  refuses when a container has none (a locally built image, which is exactly the
  case a build-and-load job would have produced).
* **Labels are the only way to verify a role.** The registry does not know what
  an artifact is *for*, so `role` and the web `base_path` are checked against
  the image's own labels, the same ones the smoke check reads and a resolver
  will read in P4.

What this module is not: it does not start anything, and it does not know how
the host protocol is satisfied in CI. That is the job's business — see
`deploy/release/README.md`, "Control-plane integration acceptance".
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping

#: The images the acceptance builds and pushes. Not all seven: the host stack's
#: artifacts are started on their own architectures by the smoke check in CI
#: already, and building them again here would measure the same thing twice.
ACCEPTANCE_ARTIFACTS = ("control", "web-root", "web-agents")

#: Which compose service runs which manifest role, per stack. The web service
#: appears once per variant because a deployment serves one mount shape.
STACK_SERVICES: dict[str, dict[str, str]] = {
    "control-plane": {
        "control-plane": "control",
        "mcp-relay": "control",
        "web": "web",
    },
}

#: A repository path may carry a registry port — `localhost:5000/…` is what the
#: acceptance itself pushes to, and a pattern that refused it would have been
#: tested against nothing but Docker Hub.
_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@(sha256:[0-9a-f]{64})$")


class AcceptanceError(Exception):
    """Raised when what is running is not what the release named."""


def registry_digest(reference: str) -> str:
    """The `sha256:…` a container must carry to be this artifact.

    A container with no `RepoDigests` entry was built locally rather than
    pulled, and there is nothing to compare — so its absence is a failure rather
    than a skip. That is the difference between this acceptance and the
    build-and-load shortcut it replaces.
    """
    match = _DIGEST_RE.match(reference or "")
    if not match:
        raise AcceptanceError(f"{reference!r} is not an image digest reference")
    return match.group(1)


def expected_services(
    manifest: Mapping[str, Any], stack: str, variant: str | None
) -> dict[str, dict[str, str]]:
    """What each service of `stack` must be running, from the manifest alone."""
    if stack not in STACK_SERVICES:
        raise AcceptanceError(f"unknown stack {stack!r}")
    if stack == "control-plane" and variant is None:
        raise AcceptanceError("the control-plane stack needs the variant this deployment serves")

    by_role: dict[str, dict[str, Any]] = {}
    for artifact in manifest.get("artifacts") or ():
        role = artifact.get("role")
        if role == "web" and artifact.get("variant") != variant:
            continue
        if role in by_role:
            raise AcceptanceError(f"the manifest carries two {role!r} artifacts")
        by_role[role] = artifact

    expected: dict[str, dict[str, str]] = {}
    for service, role in STACK_SERVICES[stack].items():
        artifact = by_role.get(role)
        if artifact is None:
            raise AcceptanceError(f"the manifest carries no {role!r} artifact for this stack")
        expected[service] = {
            "digest": registry_digest(str(artifact.get("image", ""))),
            "revision": str(manifest.get("source_commit", "")),
            "role": role,
        }
        if role == "web":
            expected[service]["base_path"] = str(artifact.get("base_path", ""))
    return expected


def check_service(
    service: str, expected: Mapping[str, str], observed: Mapping[str, Any]
) -> list[str]:
    """Compare one running container with what the manifest asked for."""
    problems: list[str] = []

    digests = observed.get("repo_digests") or []
    if not digests:
        problems.append(
            f"{service} has no RepoDigests, so it was built here rather than pulled — the "
            "release names a registry digest and nothing local can stand in for it"
        )
    elif not any(str(entry).endswith("@" + expected["digest"]) for entry in digests):
        problems.append(f"{service} is running {sorted(digests)}, not {expected['digest']}")

    labels = observed.get("labels") or {}
    if labels.get("org.opencontainers.image.revision") != expected["revision"]:
        problems.append(
            f"{service} names revision {labels.get('org.opencontainers.image.revision')!r}, "
            f"and the release is {expected['revision']!r}"
        )
    if labels.get("io.hybridinference.artifact.role") != expected["role"]:
        problems.append(
            f"{service} declares role {labels.get('io.hybridinference.artifact.role')!r}, "
            f"expected {expected['role']!r}"
        )
    if (
        "base_path" in expected
        and labels.get("io.hybridinference.artifact.base_path") != expected["base_path"]
    ):
        problems.append(
            f"{service} declares base path "
            f"{labels.get('io.hybridinference.artifact.base_path')!r}, expected "
            f"{expected['base_path']!r}"
        )

    if problems:
        raise AcceptanceError("; ".join(problems))
    return [
        f"{service} runs {expected['digest'][:19]}… at revision {expected['revision'][:12]} "
        f"as role {expected['role']}"
    ]


def check_running(
    expected: Mapping[str, Mapping[str, str]], observed: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Every expected service, and no service left unaccounted for."""
    missing = sorted(set(expected) - set(observed))
    if missing:
        raise AcceptanceError(f"the deployment is not running these services: {missing}")

    evidence: list[str] = []
    for service, want in sorted(expected.items()):
        evidence += check_service(service, want, observed[service])
    return evidence


def source_free(directory: str | Any) -> None:
    """Refuse to call a directory a recipe root if it carries the source it replaces."""
    from pathlib import Path

    root = Path(directory)
    for name in (".git", "apps", "deploy/local"):
        if (root / name).exists():
            raise AcceptanceError(
                f"{root} contains {name}, so a deployment that works there says nothing about "
                "a deployment that has a recipe"
            )


if __name__ == "__main__":  # pragma: no cover - the job drives the functions
    raise SystemExit("this module is imported by the acceptance job, not run directly")
