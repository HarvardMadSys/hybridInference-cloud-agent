#!/usr/bin/env python3
"""Deploy a candidate release the way a distribution will, before it is published.

Every other check in this repository looks at one piece. The smoke starts one
image. `test_artifact_deploy.py` runs the deploy scripts with a recorded Docker
boundary, which proves the *script* without proving the *release*. What neither
can answer is the question the whole artifact mode exists for:

    given only a manifest and a recipe — no checkout, no Dockerfiles, no
    registry credentials of the deployment's own choosing — does the thing that
    comes up run the bytes the release named?

So this runs the real `deploy/control-plane/deploy.sh` out of the extracted
recipe, in artifact mode, against a real Docker daemon, pulling the release's
own digests from the registry. Then it asks each running container what it is,
and compares that with the manifest. The comparison itself lives in
`accept_artifacts.py`, as pure functions; this module is the orchestration.

**The host fence is expected to refuse, and that is not a workaround.**
`deploy_readiness` waits for a heartbeat from an execution host running this
exact commit. CI has no such host, and faking one would mean standing up a
runner, a broker and an enrollment — at which point the check would be proving
something about the fixture. So the deploy is expected to get *to* the fence and
stop there, which is itself worth asserting: reaching it means the images
pulled, Alembic migrated a real database from the control image, the identity
namespace check ran, and both listeners answered `/healthz`. Everything before
the fence is what this acceptance covers. The host end belongs to the staging
handoff, where there is a host.

One service is left over by that stop: `web` starts after the fence, so this
module brings it up itself, from the same generated Compose file and under the
same `--no-build --pull never` restrictions the deploy uses. That is an explicit
step of the acceptance rather than something the deploy did, and it is here
because the web image is the one artifact carrying a variant: checking it is how
"the deployment serves the mount shape the manifest named" stops being a claim.

Run it against a candidate before publishing, or against any published release:

    python deploy/release/acceptance.py \\
        --manifest release.json --recipe recipe.tar.gz --variant agents \\
        --workdir /tmp/acceptance --project acceptance-1234
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__:
    from . import accept_artifacts as aa, smoke
else:  # direct execution from the release directory
    import accept_artifacts as aa
    import smoke

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping, Sequence

#: How long the readiness fence is given before it reports that no host arrived.
#: Short on purpose: there is no host, the refusal is the expected outcome, and
#: the default would hold a runner for fifteen minutes to reach it.
FENCE_TIMEOUT_S = 20

#: How long a listener is given to answer `/healthz` after its container starts.
HEALTH_TIMEOUT_S = 180

#: The throwaway database's password. See `synthetic_environment` for why it is
#: not generated.
ACCEPTANCE_PASSWORD = "acceptance-only-not-a-secret"

#: The mount shape each variant serves, and the browser path that goes with it.
#: Both are asserted by the deployment — the health probe reads the first and
#: the console's bundle was compiled with the second — so a deployment that
#: mixes them fails in a way no image check would catch.
VARIANTS: dict[str, tuple[str, str]] = {"root": ("", "/api"), "agents": ("/agents", "/agents/api")}

#: What the deploy script prints on its way to the fence. Each line stands for a
#: stage that has to have happened for the next one to mean anything, so they are
#: asserted in order rather than as a set: a log that shows the identity check
#: before the migration is a deployment that ran them in the wrong order.
STAGES: tuple[tuple[str, str], ...] = (
    ("pulling release images", "the release's digests were pulled, not built"),
    ("migrating deployment database", "Alembic ran from the control image"),
    ("checking the Agent database identity issuer", "the identity namespace check ran"),
    ("control plane: ok", "the control plane answered /healthz"),
    ("MCP relay: ok", "the MCP relay answered /healthz"),
    ("waiting for target", "the deployment reached the host readiness fence"),
)

#: What the fence says when no host arrives. Asserted so that a deploy which
#: failed *earlier* for an unrelated reason cannot be read as this expected stop.
FENCE_REFUSAL = "[deploy-readiness] ERROR: timed out"


class AcceptanceFailure(Exception):
    """The release did not deploy, or what came up is not what it named."""


def free_port() -> int:
    """A port nothing is listening on, for a stack that shares a machine."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def required_variables(compose: Path) -> set[str]:
    """Every variable the recipe's Compose file refuses to start without.

    Read from the file rather than listed here: a required variable added to
    the stack would otherwise fail this acceptance with an interpolation error
    that says nothing about the release. The image and commit variables are
    excluded deliberately — those are the release's to supply, and a deployment
    that sets them is the negative case below.
    """
    names = set(re.findall(r"\$\{([A-Z_]+)[:?]\?", compose.read_text()))
    return {
        name for name in names if not name.endswith("_IMAGE") and name != "AGENT_DEPLOYMENT_SHA"
    }


def synthetic_environment(compose: Path, variant: str, project: str) -> dict[str, str]:
    """A deployment's `.env`: real where the stack reads it, placeholder elsewhere.

    The secrets have the right *shape* and authenticate nothing. The addresses
    point at `.invalid` so that an artifact needing the internet to start fails
    here rather than on a cold host. `smoke.py` already owns that set for the
    control image; this adds what a Compose deployment needs on top — the
    database the `postgres` service will create, the ports this stack publishes,
    and the variant, which selects the web image and the path the probe uses.
    """
    if variant not in VARIANTS:
        raise AcceptanceFailure(
            f"unknown variant {variant!r}; the release builds {sorted(VARIANTS)}"
        )
    base_path, api_url = VARIANTS[variant]
    # Fixed, not random. This database is reachable only from the stack's own
    # network, holds nothing, and is removed with the project — so a random
    # value protects nothing, and a failure that cannot be reproduced costs
    # exactly what the first control-plane crash here cost: a run to find out.
    password = ACCEPTANCE_PASSWORD

    environment = dict(smoke._control_environment(""))
    # The release supplies it, and setting it here is what the negative case
    # proves a deployment may not do.
    environment.pop("AGENT_DEPLOYMENT_SHA", None)
    environment.update(
        {
            "COMPOSE_PROJECT": project,
            "AGENT_POSTGRES_USER": "cloudagent",
            "AGENT_POSTGRES_PASSWORD": password,
            "AGENT_POSTGRES_DB": "cloudagent",
            # Bare `postgresql://`: the service talks asyncpg and Alembic
            # rewrites this same value for its synchronous engine. The host
            # is the Compose service, which is where the database now lives.
            "AGENT_DATABASE_URL": f"postgresql://cloudagent:{password}@postgres:5432/cloudagent",
            "AGENT_WEB_VARIANT": variant,
            "AGENT_WEB_BASE_PATH": base_path,
            "AGENT_WEB_API_URL": api_url,
            "AGENT_CONTROL_PLANE_PORT": str(free_port()),
            "AGENT_MCP_RELAY_PORT": str(free_port()),
            "AGENT_WEB_PORT": str(free_port()),
        }
    )
    for name in sorted(required_variables(compose) - set(environment)):
        environment[name] = f"acceptance-{name.lower().replace('_', '-')}"
    return environment


def write_env(root: Path, environment: Mapping[str, str]) -> Path:
    """The deployment's own `.env`, the file a deploy host is told to configure."""
    path = root / ".env"
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(environment.items())))
    return path


def extract_recipe(archive: Path, into: Path) -> Path:
    """Unpack the release's recipe, and refuse a root that carries the source."""
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(into, filter="data")
    root = into / "recipe"
    if not root.is_dir():
        raise AcceptanceFailure(f"{archive} does not unpack to a `recipe/` root")
    aa.source_free(root)
    if list(root.rglob("Dockerfile*")):
        raise AcceptanceFailure(
            "the recipe ships a Dockerfile, so a host could build its own image"
        )
    return root


def run_deploy(
    recipe: Path, root: Path, manifest: Path, *, fence_timeout: int
) -> subprocess.CompletedProcess[str]:
    """The real deploy script, in artifact mode, with no source anywhere near it.

    The manifest is passed absolute, and that is not tidiness. `deploy.sh` cds
    into the deployment root before it reads anything, so a relative path is
    resolved against a directory the caller never saw: the first live run of
    this acceptance died on `release manifest does not exist: release/release.json`,
    a file that was right there in the workspace. The recipe's own helper does
    absolutize the value, but only *after* the existence check it had already
    failed, and `deploy/release/README.md` tells an operator to pass an absolute
    path for the same reason.
    """
    environment = {
        **os.environ,
        "AGENT_DEPLOY_MODE": "artifacts",
        "AGENT_RELEASE_MANIFEST": str(manifest.resolve()),
        # The interpreter carrying the recipe's own requirements. Running this
        # module at all proves they are installed.
        "AGENT_DEPLOY_PYTHON": sys.executable,
        "DEPLOY_ROOT": str(root),
        "HEALTH_TIMEOUT": str(HEALTH_TIMEOUT_S),
        "SHARED_READY_TIMEOUT": str(fence_timeout),
    }
    for name in ("DEPLOY_SHA", "SOURCE_REPO", "COMPOSE_PROJECT", "ALLOW_DIRTY"):
        environment.pop(name, None)
    return subprocess.run(
        ["bash", str(recipe / "deploy/control-plane/deploy.sh")],
        env=environment,
        text=True,
        capture_output=True,
        timeout=fence_timeout + HEALTH_TIMEOUT_S * 3,
    )


def check_stages(result: subprocess.CompletedProcess[str]) -> list[str]:
    """The deploy reached the fence, and stopped there rather than earlier."""
    log = result.stdout + result.stderr
    evidence: list[str] = []
    position = -1
    for marker, means in STAGES:
        found = log.find(marker, position + 1)
        if found < 0:
            raise AcceptanceFailure(
                f"the deployment never got as far as {marker!r} ({means}). Exit "
                f"{result.returncode}.\n{log}"
            )
        position, _ = found, evidence.append(means)

    if result.returncode == 0:
        raise AcceptanceFailure(
            "the deployment succeeded, which means the readiness fence passed without an "
            "execution host. That fence is the only thing standing between a control plane "
            "and a pool that has not been replaced with it."
        )
    if FENCE_REFUSAL not in log:
        raise AcceptanceFailure(
            f"the deployment failed after the fence was reached but not *at* it: {FENCE_REFUSAL!r} "
            f"is absent. Exit {result.returncode}.\n{log}"
        )
    evidence.append("the host fence refused, as it must with no execution host")
    return evidence


def docker(*arguments: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=120)
    if check and result.returncode != 0:
        raise AcceptanceFailure(f"`docker {' '.join(arguments)}` failed: {result.stderr.strip()}")
    return result.stdout.strip()


def compose(project: str, file: Path, root: Path, *arguments: str, check: bool = True) -> str:
    return docker(
        "compose",
        "-p",
        project,
        "--env-file",
        str(root / ".env"),
        "-f",
        str(file),
        *arguments,
        check=check,
    )


def generated_compose(root: Path) -> Path:
    """The Compose file the deploy rendered from the manifest, under the deployment root."""
    candidates = sorted((root / "deploy/control-plane").glob(".release-*.yml"))
    if len(candidates) != 1:
        raise AcceptanceFailure(
            f"expected one generated Compose file under {root}, found {candidates}"
        )
    return candidates[0]


def observed_services(project: str, services: Sequence[str]) -> dict[str, dict[str, Any]]:
    """What each named service is actually running, as the registry identifies it."""
    observed: dict[str, dict[str, Any]] = {}
    for service in services:
        containers = [
            line
            for line in docker(
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ).splitlines()
            if line.strip()
        ]
        if not containers:
            continue
        image_id = docker("inspect", containers[0], "--format", "{{.Image}}")
        document = json.loads(docker("image", "inspect", image_id, "--format", "{{json .}}"))
        observed[service] = {
            "repo_digests": document.get("RepoDigests") or [],
            "labels": (document.get("Config") or {}).get("Labels") or {},
        }
    return observed


def database_credentials(project: str) -> list[str]:
    """Whether the stack's two halves agree about the database password.

    Asked because the second live acceptance failed in a way that should be
    impossible: `alembic upgrade head` connected, the identity check connected,
    and then the service was refused with `password authentication failed`. The
    three read one `.env`, but not with one parser — Alembic goes through
    SQLAlchemy and psycopg, the service through asyncpg (`storage/dsn.py`
    explains the split) — so the answer is in what each container was handed.

    Reports agreement, never the values: this runs in a log anyone can read.
    """
    findings: list[str] = []
    for service, variable in (
        ("postgres", "POSTGRES_PASSWORD"),
        ("control-plane", "AGENT_DATABASE_URL"),
    ):
        containers = [
            line
            for line in docker(
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                check=False,
            ).splitlines()
            if line.strip()
        ]
        if not containers:
            findings.append(f"{service}: no container to inspect")
            continue
        document = json.loads(
            docker("inspect", containers[0], "--format", "{{json .Config.Env}}", check=False)
            or "[]"
        )
        values = [entry.split("=", 1)[1] for entry in document if entry.startswith(variable + "=")]
        if not values:
            findings.append(f"{service}: {variable} is not set on the container")
            continue
        value = values[0]
        secret = (
            value.partition("://")[2].partition("@")[0].partition(":")[2]
            if variable.endswith("URL")
            else value
        )
        findings.append(
            f"{service}: {variable} carries a password of {len(secret)} characters, "
            f"sha256 {hashlib.sha256(secret.encode()).hexdigest()[:12]}"
        )
    return findings


def teardown(project: str) -> None:
    """Remove this run's containers and its database volume, whatever happened.

    By label rather than through Compose: the generated file may not exist if
    preparation refused, and a shared runner must not accumulate a Postgres
    volume per acceptance.
    """
    containers = docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}", check=False
    ).split()
    if containers:
        docker("rm", "-f", *containers, check=False)
    for volume in docker("volume", "ls", "-q", "--filter", f"name={project}", check=False).split():
        docker("volume", "rm", "-f", volume, check=False)
    for network in docker(
        "network",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        check=False,
    ).split():
        docker("network", "rm", network, check=False)


def accept(
    manifest_path: Path, recipe: Path, variant: str, workdir: Path, project: str
) -> list[str]:
    """Deploy the release and answer whether what came up is what it named."""
    manifest = json.loads(manifest_path.read_text())
    recipe_root = extract_recipe(recipe, workdir / "recipe")
    evidence = [f"the recipe unpacks to a root with no source, no Dockerfile ({recipe_root.name})"]

    root = workdir / "deployment"
    root.mkdir(parents=True, exist_ok=True)
    environment = synthetic_environment(
        recipe_root / "deploy/control-plane/docker-compose.yml", variant, project
    )
    write_env(root, environment)

    result = run_deploy(recipe_root, root, manifest_path, fence_timeout=FENCE_TIMEOUT_S)
    try:
        evidence += check_stages(result)
    except AcceptanceFailure as failure:
        # Two DSN parsers read one setting, so "which password did each half
        # get" is worth answering here rather than over another run.
        detail = "; ".join(database_credentials(project))
        raise AcceptanceFailure(
            f"{failure}\n\ndatabase credentials as the containers got them: {detail}"
        ) from failure
    if (root / ".deployed-sha").exists():
        raise AcceptanceFailure(
            "the deployment recorded itself as deployed even though the fence refused; "
            "`.deployed-sha` is what an operator reads to answer what is running here"
        )
    evidence.append("nothing was recorded as deployed, because nothing finished deploying")

    # The fence stops the deploy before `web` starts. Bringing it up here is the
    # acceptance's own step, under the deploy's own restrictions: no build, and
    # no implicit pull of anything the release did not pin.
    file = generated_compose(root)
    compose(project, file, root, "up", "-d", "--no-build", "--pull", "never", "web")
    evidence.append(f"the web service started from {file.name} with no build and no implicit pull")

    expected = aa.expected_services(manifest, "control-plane", variant)
    evidence += aa.check_running(expected, observed_services(project, list(expected)))
    return evidence


def refuses_a_host_chosen_image(
    manifest_path: Path, recipe: Path, variant: str, workdir: Path, project: str
) -> list[str]:
    """A deployment that names its own image is refused before anything is pulled.

    The property the exact-SHA fence rests on: if the machine being deployed can
    choose what runs on it, the readiness check compares a value the deployment
    set against a value the deployment set.
    """
    manifest = json.loads(manifest_path.read_text())
    other = next(a["image"] for a in manifest["artifacts"] if a["role"] == "egress")

    root = workdir / "refusal"
    root.mkdir(parents=True, exist_ok=True)
    recipe_root = workdir / "recipe/recipe"
    environment = synthetic_environment(
        recipe_root / "deploy/control-plane/docker-compose.yml", variant, f"{project}-refusal"
    )
    environment["AGENT_CONTROL_IMAGE"] = other
    write_env(root, environment)

    result = run_deploy(recipe_root, root, manifest_path, fence_timeout=FENCE_TIMEOUT_S)
    log = result.stdout + result.stderr
    if result.returncode == 0:
        raise AcceptanceFailure("a deployment naming its own control image was accepted")
    if "A host does not get to choose what runs on it" not in log:
        raise AcceptanceFailure(f"refused, but not for naming its own image:\n{log[-2000:]}")
    if "pulling release images" in log:
        raise AcceptanceFailure("the conflict was found only after images had been pulled")
    return ["a deployment naming its own control image is refused before anything is pulled"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--variant", default="agents", choices=sorted(VARIANTS))
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--project", required=True, help="Compose project, unique to this run.")
    parser.add_argument("--keep", action="store_true", help="Leave the stack up for inspection.")
    arguments = parser.parse_args(argv)
    # Resolved once, here: the deploy script runs from the deployment root and
    # the recipe is extracted from wherever this process happens to be.
    arguments.manifest = arguments.manifest.resolve()
    arguments.recipe = arguments.recipe.resolve()
    arguments.workdir = arguments.workdir.resolve()

    print(f"acceptance: deploying the control-plane stack, {arguments.variant} variant")
    try:
        evidence = accept(
            arguments.manifest,
            arguments.recipe,
            arguments.variant,
            arguments.workdir,
            arguments.project,
        )
        evidence += refuses_a_host_chosen_image(
            arguments.manifest,
            arguments.recipe,
            arguments.variant,
            arguments.workdir,
            arguments.project,
        )
    except (AcceptanceFailure, aa.AcceptanceError, subprocess.TimeoutExpired) as error:
        print(f"::error title=Release does not deploy::{error}", file=sys.stderr)
        return 1
    finally:
        if not arguments.keep:
            teardown(arguments.project)
            teardown(f"{arguments.project}-refusal")
            shutil.rmtree(arguments.workdir / "recipe", ignore_errors=True)

    for line in evidence:
        print(f"  proved: {line}")
    print("acceptance: the release deploys, and runs what it named")
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
