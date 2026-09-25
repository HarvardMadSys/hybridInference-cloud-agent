#!/usr/bin/env python3
"""Start an artifact the way its deployment does, and assert what only starting shows.

Building an image proves the Dockerfile parses. It does not prove the thing runs,
and this repository has already paid for that difference once: the since-removed
egress proxy built fine, passed `squid -k parse`, and then exited on every start —
2814 times on staging — because squid opened its logs after dropping to an
unprivileged user. No agent job ran for two days.

So every artifact a release carries has an entry here, and a release is not
consumable until each one has been started *by the workflow that will push it*.
That is why this is a script rather than a workflow step: the same check has to
run over the images CI built on a pull request and over the images the
publication workflow is about to push, and two implementations of "it starts"
would drift.

Each role says what starting means for it and, just as deliberately, what it does
not prove — a check that quietly proves less than its name is worse than no
check:

* **control** — migrates a real database and answers `/healthz`. Does not prove
  the gateway contract works; that needs a gateway.
* **web** (both variants) — serves its sign-in page and renders the identity it
  was configured with at *runtime*. Does not prove sign-in; that needs the
  control plane and the gateway.
* **runner** — imports its modules and finds the Docker CLI it shells out to,
  and the `nft` its host-rule helper runs. Does not prove it can claim work, or
  that the rules hold: those need a host, a socket, an enrolled credential and
  a real kernel.
* **sandbox** — every Agent CLI reports the version its own label claims. Does
  not prove isolation: that needs Kata and a real job.
* **tunnel** — the entrypoint runs and fails loudly on an unreachable peer. Does
  not prove a hop: that needs the far end.

Run it on a machine with a Docker daemon and this repository checked out:

    uv run --with-editable . python deploy/release/smoke.py \\
        --role web --variant agents --image cloud-agent-web:ci-123-1 --suffix 123-1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The runtime label namespace the sandbox image writes and the host reads.
SMOKE_RUN_LABEL = "io.hybridinference.smoke.run"

#: The runtime label namespace the sandbox image writes and the host reads.
RUNTIME_LABEL_PREFIX = "org.freeinference.runtime."
RUNTIME_LABEL_SUFFIX = ".version"

#: Every runtime in the sandbox image, and the command that reports its version.
#: The bare binaries are the ones `install-runtimes.sh` installs; the reviewed
#: `*-freeinference` wrappers are what a job actually invokes, and they exec
#: these.
RUNTIME_COMMANDS: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "pi": "pi",
    "opencode": "opencode",
    "kilo": "kilo",
}

STARTUP_TIMEOUT_S = 90
POLL_INTERVAL_S = 1.0


class SmokeFailure(Exception):
    """The artifact did not do what starting it has to prove."""


@dataclass(frozen=True)
class Role:
    """One artifact's start check, and the limits of what it proves."""

    name: str
    arch: str
    proves: str
    does_not_prove: str
    variants: tuple[str, ...] = ()


ROLES: dict[str, Role] = {
    "control": Role(
        name="control",
        arch="linux/arm64",
        proves="it migrates a real database and answers /healthz",
        does_not_prove="that the gateway contract works; that needs a gateway",
    ),
    "web": Role(
        name="web",
        arch="linux/arm64",
        proves="it serves its sign-in page under its own mount prefix, with the identity "
        "read at runtime",
        does_not_prove="sign-in end to end; that needs the control plane and the gateway",
        variants=("root", "agents"),
    ),
    "runner": Role(
        name="runner",
        arch="linux/amd64",
        proves="its modules import and the Docker CLI it shells out to is present",
        does_not_prove="that it can claim work; that needs a host, a socket and a credential",
    ),
    "sandbox": Role(
        name="sandbox",
        arch="linux/amd64",
        proves="every Agent CLI reports the version its own image label claims",
        does_not_prove="isolation; that needs Kata and a real job",
    ),
    "tunnel": Role(
        name="tunnel",
        arch="linux/amd64",
        proves="the entrypoint runs and refuses an unreachable peer loudly",
        does_not_prove="a working hop; that needs the far end",
    ),
}


def run(
    *args: str,
    check: bool = True,
    timeout: int = 300,
    stdin: str | None = None,
) -> str:
    """Run a command, capture it, and turn a non-zero exit into a failure."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
    )
    if check and result.returncode != 0:
        raise SmokeFailure(
            f"`{' '.join(args)}` exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def docker(*args: str, **kwargs: object) -> str:
    return run("docker", *args, **kwargs)  # type: ignore[arg-type]


def docker_json(*args: str) -> object:
    return json.loads(docker(*args))


@dataclass
class Container:
    """A container this run owns, removed whatever happens.

    `docker run --rm` is not enough: a failure below exits non-zero before any
    inline cleanup, and the runner is a shared machine that would accumulate one
    of these per attempt until the disk fills — the symptom then landing on
    whatever job happens to be running at the time.

    Every container carries the run it belongs to in a label, because cleanup
    keyed on the name prefix reaches across runs: a shared runner hosts two jobs
    at once, and a job that failed a moment ago is running its own
    `docker rm -f` over every container whose name contains `smoke-`. That
    happened — this check's control cell lost its application container ninety
    seconds into a poll — and the label is the fix.
    """

    name: str
    run: str
    published: dict[int, int] = field(default_factory=dict)

    @classmethod
    def for_run(cls, kind: str, run: str) -> Container:
        return cls(name=f"smoke-{kind}-{run}", run=run)

    def start(
        self,
        image: str,
        *,
        env: dict[str, str] | None = None,
        publish: Sequence[int] = (),
        network: str | None = None,
        volumes: dict[Path, str] | None = None,
        entrypoint: str | None = None,
        command: Sequence[str] = (),
    ) -> None:
        args = [
            "run",
            "-d",
            "--name",
            self.name,
            "--label",
            f"{SMOKE_RUN_LABEL}={self.run}",
        ]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        for port in publish:
            args += ["-p", f"127.0.0.1::{port}"]
        if network:
            args += ["--network", network]
        for host_path, container_path in (volumes or {}).items():
            args += ["-v", f"{host_path}:{container_path}:ro"]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args.append(image)
        args += list(command)
        docker(*args)
        for port in publish:
            mapping = docker("port", self.name, f"{port}/tcp").strip()
            # `0.0.0.0:49153` or `127.0.0.1:49153`, one per line.
            self.published[port] = int(mapping.rsplit(":", 1)[1])

    def url(self, port: int, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.published[port]}{path}"

    def state(self) -> str:
        return docker("inspect", self.name, "--format", "{{.State.Status}}").strip()

    def exit_code(self) -> int:
        value = docker("inspect", self.name, "--format", "{{.State.ExitCode}}").strip()
        return int(value)

    def logs(self) -> str:
        """Everything the container wrote, in order, stderr included.

        `docker logs` sends the container's stderr to *this* process's stderr, so
        a plain capture loses half of what a container says — and the half it
        loses is where postgres announces that it is ready (and where the
        since-removed squid announced it was accepting connections). Both were
        read as silence on this check's first real run.
        """
        result = subprocess.run(
            ["docker", "logs", self.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        return result.stdout

    def wait(self, timeout: int = STARTUP_TIMEOUT_S) -> None:
        """Block until the container exits, and fail if it does not in time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state() in {"exited", "dead"}:
                return
            time.sleep(POLL_INTERVAL_S)
        raise SmokeFailure(
            f"{self.name} neither ran nor exited within {timeout}s; "
            f"state={self.state()}\n{self.logs()}"
        )

    def stop(self) -> None:
        run("docker", "rm", "-f", self.name, check=False)


@dataclass
class Network:
    """A network this check creates, and removes after its containers are gone.

    Explicit rather than a context manager: the ordering that matters is
    containers-then-network, and a `with` block invites the opposite — a
    `return` inside it removes the network while the containers are still
    attached, which fails, and the failure is swallowed because there is nothing
    useful to do about it. That leaves the network on the runner for good.
    """

    name: str

    def create(self) -> None:
        docker("network", "create", self.name)

    def remove(self) -> None:
        run("docker", "network", "rm", self.name, check=False)


def http_status(url: str) -> tuple[int | None, str]:
    """The status and body, or (None, reason) while nothing is listening yet."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except OSError as error:
        return None, str(error)


def wait_for_http(
    url: str, timeout: int = STARTUP_TIMEOUT_S, container: Container | None = None
) -> tuple[int, str]:
    """Poll until something answers, then say why nothing did.

    A connection refused for ninety seconds is not a diagnosis. The container's
    state and its own log are, so a failure carries them rather than sending the
    reader to the Actions log to find out that the process died on a missing
    variable.
    """
    deadline = time.monotonic() + timeout
    last = "no attempt made"
    while time.monotonic() < deadline:
        status, body = http_status(url)
        if status is not None:
            return status, body
        last = body
        time.sleep(POLL_INTERVAL_S)

    detail = ""
    if container is not None:
        detail = f"\nstate={container.state()}\n{container.logs()[-4000:]}"
    raise SmokeFailure(f"{url} did not answer within {timeout}s: {last}{detail}")


def image_labels(image: str) -> dict[str, str]:
    labels = docker("inspect", image, "--format", "{{json .Config.Labels}}")
    return json.loads(labels) if labels.strip() not in {"", "null"} else {}


# ---------------------------------------------------------------- the artifacts


def _drop_image(image: str) -> None:
    """Remove this cell's image tag, and never at another cell's expense.

    Not `-f`. Forced removal untags an image that another cell or run may still
    be starting containers from, and a tag pulled out from under a concurrent
    build is a failure nobody can explain afterwards. A plain `rm` refuses while
    any container references the image, so the worst case is a leftover image.

    To be exact about what this is *not*: removing an image does not remove the
    containers using it. The containers this check lost went missing because
    cleanup was keyed on a name prefix and a label that several cells shared,
    and those removals are the ones that had to be scoped.
    """
    run("docker", "image", "rm", image, check=False)


def verify_web(image: str, *, variant: str, suffix: str, keep: bool) -> list[str]:
    """The sign-in page, under this variant's own mount prefix.

    Two properties, and the second is the one a build cannot show: the page is
    served at the prefix the image was compiled for, and the identity on it comes
    from the *runtime* environment rather than from whatever was baked in. #158
    moved that read to the server, and this is the container-level proof that it
    stayed moved.
    """
    base_path = {"root": "", "agents": "/agents"}[variant]
    container = Container.for_run(f"web-{variant}", suffix)
    try:
        container.start(
            image,
            env={
                "AGENT_WEB_APP_NAME": "Smoke Test Agent",
                "AGENT_WEB_ORG_NAME": "",
                "AGENT_WEB_ORG_URL": "",
                "AGENT_WEB_STATUS_URL": "",
                "AGENT_WEB_GATEWAY_URL": "https://gateway.smoke.invalid",
                "AGENT_WEB_FEATURE_RAG": "false",
            },
            publish=(3000,),
        )
        status, body = wait_for_http(container.url(3000, f"{base_path}/login"), container=container)
        if status != 200:
            raise SmokeFailure(f"{base_path}/login answered {status}\n{container.logs()}")
        if "Smoke Test Agent" not in body:
            raise SmokeFailure(
                "the page does not carry the name the container was given; the identity is "
                f"still resolved at build time\n{body[:2000]}"
            )
        if "Cloud Agent" in body:
            raise SmokeFailure(
                "the page carries the neutral default as well as the configured name"
            )

        evidence = [
            f"GET {base_path or '/'}/login answered 200",
            "the rendered identity is the one the container was given (Smoke Test Agent)",
            "the neutral default does not appear",
        ]

        if base_path:
            bare_status, _ = http_status(container.url(3000, "/login"))
            if bare_status != 404:
                raise SmokeFailure(
                    f"a variant mounted at {base_path} also answers /login ({bare_status}); "
                    "that would be a second, unprefixed copy of the same routes"
                )
            evidence.append("the bare /login answers 404, so the variant serves one mount point")
        return evidence
    finally:
        container.stop()
        if not keep:
            _drop_image(image)


def verify_control(image: str, *, suffix: str, keep: bool) -> list[str]:
    """Migrate a real database, then answer /healthz.

    The migration is not incidental: the compose file's `migrate` service runs
    this same image, and a missing driver in it once took the control plane, the
    MCP relay and the migration down together because both wait on it. Starting
    the API alone would not have caught that.
    """
    network = Network(f"smoke-control-{suffix}")
    postgres = Container.for_run("control-pg", suffix)
    app = Container.for_run("control-app", suffix)
    migrate = Container.for_run("control-migrate", suffix)

    network.create()
    try:
        # Containers stop inside this block, before the network is removed: an
        # endpoint still attached makes the removal fail, and a failure nobody
        # handles is a network left behind on a shared runner.
        try:
            postgres.start(
                "postgres:16-alpine",
                env={
                    "POSTGRES_USER": "cloudagent",
                    "POSTGRES_PASSWORD": "smoke",
                    "POSTGRES_DB": "cloudagent",
                },
                network=network.name,
            )
            # Poll the log line rather than sleeping a guessed interval.
            deadline = time.monotonic() + STARTUP_TIMEOUT_S
            while time.monotonic() < deadline:
                if "database system is ready to accept connections" in postgres.logs():
                    break
                time.sleep(POLL_INTERVAL_S)
            else:
                raise SmokeFailure(f"postgres never became ready\n{postgres.logs()}")

            # One setting, two readers: the service talks asyncpg and wants a
            # bare `postgresql://`, while Alembic rewrites the same value for
            # its synchronous engine (`cloud_agent.storage.dsn`). Spelling it
            # `postgresql+asyncpg://` — the shape a SQLAlchemy-shaped guess
            # produces — stops the API with `invalid DSN: scheme is expected to
            # be either "postgresql" or "postgres"`. This check exists to be
            # configured the way a deployment is, and this is that way.
            dsn = f"postgresql://cloudagent:smoke@{postgres.name}:5432/cloudagent"
            environment = _control_environment(dsn)

            migrate.start(
                image,
                env=environment,
                network=network.name,
                entrypoint="alembic",
                command=("upgrade", "head"),
            )
            migrate.wait(timeout=STARTUP_TIMEOUT_S * 2)
            if migrate.exit_code() != 0:
                raise SmokeFailure(
                    f"`alembic upgrade head` exited {migrate.exit_code()} in the image the "
                    f"deploy runs it from\n{migrate.logs()}"
                )

            app.start(image, env=environment, network=network.name, publish=(8000,))
            status, _ = wait_for_http(app.url(8000, "/healthz"), container=app)
            if status != 200:
                raise SmokeFailure(f"/healthz answered {status}\n{app.logs()}")
            return [
                "`alembic upgrade head` ran to completion in this image",
                "the API answered /healthz 200 against a real database",
            ]
        finally:
            app.stop()
            migrate.stop()
            postgres.stop()
    finally:
        network.remove()
        if not keep:
            _drop_image(image)


def _control_environment(dsn: str) -> dict[str, str]:
    """The smallest environment a control plane boots in, from deploy/local.

    Secrets are placeholders with the right *shape*: the point is to start the
    artifact, not to authenticate anything. Addresses that are never called
    during startup point at `.invalid` on purpose — an artifact that needs the
    internet to start is an artifact that cannot be deployed from a cold host.
    """
    import base64
    import os

    return {
        "AGENT_DATABASE_URL": dsn,
        "AGENT_SESSION_SECRET": base64.b64encode(os.urandom(32)).decode(),
        "AGENT_CONTROL_TOKEN_SECRET": base64.b64encode(os.urandom(32)).decode(),
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": _fernet_key(),
        "AGENT_WORKSPACE_BROKER_TOKEN": "smoke-broker-token",
        # Required once a gateway is configured at all: the app refuses to start
        # without it, because verifying identity is the whole point of the
        # service it fronts. `deploy/control-plane/.env.example` is the
        # authority for this list, and a deployment supplies it in the host's
        # `.env`.
        "GATEWAY_ISSUER": "https://gateway.smoke.invalid",
        "GATEWAY_BASE_URL": "https://gateway.smoke.invalid",
        "GATEWAY_JWKS_URL": "https://gateway.smoke.invalid/v1/identity/jwks",
        "GATEWAY_AUTHORIZE_URL": "https://gateway.smoke.invalid/authorize",
        "GATEWAY_GRANT_DISPATCH_TOKEN": "smoke-dispatch-token",
        "AGENT_SESSION_REDIRECT_URI": "https://agents.smoke.invalid/api/v1/session/callback",
        "AGENT_WEB_BASE_URL": "https://agents.smoke.invalid",
        "AGENT_SESSION_COOKIE_SECURE": "false",
        "AGENT_DEPLOYMENT_SHA": "0" * 40,
        "AGENT_REPO_ALLOWLIST": "smoke/demo",
    }


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def verify_runner(image: str, *, suffix: str, keep: bool) -> list[str]:
    """Its modules import, and the Docker CLI and `nft` it drives are there.

    A runner that claims work needs a host, a socket and an enrolled credential,
    so this stops short of that on purpose: what it catches is the class of
    failure a build cannot see — a wheel that installs but does not import, the
    `COPY --from=docker` line going missing from the image, and the `nftables`
    package the host-rule helper runs from this same image.
    """
    container = Container.for_run("runner", suffix)
    try:
        container.start(
            image,
            entrypoint="python",
            command=(
                "-c",
                "import cloud_agent_host.runner, cloud_agent_host.workspace_broker, "
                "cloud_agent_host.sandbox, cloud_agent_host.network_guard; print('imports ok')",
            ),
        )
        container.wait()
        if container.exit_code() != 0:
            raise SmokeFailure(f"the runner's modules do not import\n{container.logs()}")

        cli = Container.for_run("runner-docker", suffix)
        try:
            cli.start(image, entrypoint="docker", command=("--version",))
            cli.wait()
            if cli.exit_code() != 0:
                raise SmokeFailure(
                    "the Docker CLI is missing from the runner image, so no sandbox could ever "
                    f"be started\n{cli.logs()}"
                )
            version = cli.logs().strip().splitlines()[-1] if cli.logs().strip() else ""
        finally:
            cli.stop()

        nft = Container.for_run("runner-nft", suffix)
        try:
            nft.start(image, entrypoint="nft", command=("--version",))
            nft.wait()
            if nft.exit_code() != 0:
                raise SmokeFailure(
                    "nft is missing from the runner image, so no host could install its "
                    f"sandbox rules and every runner would refuse to start\n{nft.logs()}"
                )
            nft_version = nft.logs().strip().splitlines()[-1] if nft.logs().strip() else ""
        finally:
            nft.stop()
        return [
            "the runner's modules import in the image",
            f"docker CLI present: {version}",
            f"nft present: {nft_version}",
        ]
    finally:
        container.stop()
        if not keep:
            _drop_image(image)


def verify_sandbox(image: str, *, suffix: str, keep: bool) -> list[str]:
    """Each CLI reports the version its own label claims.

    The labels are what a release manifest publishes as `runtime_catalog`, and
    what the host stamps onto an attempt. `install-runtimes.sh` already refuses a
    binary that disagrees with its label while building; this checks the finished
    image, where a later layer could have changed what is on `PATH`.
    """
    labels = image_labels(image)
    claims = {
        key[len(RUNTIME_LABEL_PREFIX) : -len(RUNTIME_LABEL_SUFFIX)]: value
        for key, value in labels.items()
        if key.startswith(RUNTIME_LABEL_PREFIX) and key.endswith(RUNTIME_LABEL_SUFFIX)
    }
    if not claims:
        raise SmokeFailure(
            f"{image} carries no {RUNTIME_LABEL_PREFIX}*{RUNTIME_LABEL_SUFFIX} labels, so a "
            "release cannot say which runtimes it contains"
        )

    unknown = sorted(set(claims) - set(RUNTIME_COMMANDS))
    if unknown:
        raise SmokeFailure(
            f"these runtimes are labelled but this check has no command for them: {unknown}. "
            "Add them to RUNTIME_COMMANDS rather than letting them go unverified."
        )

    evidence: list[str] = []
    for runtime, claimed in sorted(claims.items()):
        container = Container.for_run(f"sandbox-{runtime}", suffix)
        try:
            # The same query the build makes, for the same reason: the version
            # may arrive on stderr, and a CLI that prints a banner first puts it
            # on the last line. `install-runtimes.sh` compares `2>&1 | tail -1`
            # against the pin it installed, so this compares the same thing
            # against the same label.
            container.start(
                image,
                command=(
                    "/bin/bash",
                    "-lc",
                    f"{RUNTIME_COMMANDS[runtime]} --version 2>&1 | tail -1",
                ),
            )
            container.wait()
            output = container.logs()
            if container.exit_code() != 0:
                raise SmokeFailure(
                    f"`{RUNTIME_COMMANDS[runtime]} --version` exited {container.exit_code()} "
                    f"in the finished image\n{output}"
                )
            if claimed not in output:
                raise SmokeFailure(
                    f"{runtime} is labelled {claimed!r}, but the binary reports {output.strip()!r}"
                )
            evidence.append(f"{runtime} reports {claimed}")
        finally:
            container.stop()
    if not keep:
        _drop_image(image)
    return evidence


def verify_tunnel(image: str, *, suffix: str, keep: bool) -> list[str]:
    """The entrypoint runs, and an unreachable peer is a loud failure.

    There is no meaningful standalone success for this image — it exists to hold
    a hop open to a specific peer — so the check is that the script in the image
    actually executes and refuses to sit there having forwarded nothing. That is
    the failure mode that matters: a tunnel that appears to run while carrying
    nothing is how a whole host's jobs fail as "the broker is unreachable".
    """
    probe = Container.for_run("tunnel-ssh", suffix)
    try:
        probe.start(image, entrypoint="ssh", command=("-V",))
        probe.wait()
        if probe.exit_code() != 0:
            raise SmokeFailure(f"the image has no usable ssh client\n{probe.logs()}")
    finally:
        probe.stop()

    hop = Container.for_run("tunnel-hop", suffix)
    try:
        hop.start(
            image,
            env={
                "TUNNEL_SSH_DESTINATION": "nobody@127.0.0.1",
                "TUNNEL_SSH_PORT": "1",
                "TUNNEL_SSH_KEY": "/dev/null",
                "TUNNEL_SSH_KNOWN_HOSTS": "/dev/null",
            },
        )
        hop.wait(timeout=60)
        logs = hop.logs()
        if hop.exit_code() == 0:
            raise SmokeFailure(
                "the entrypoint exited 0 with no reachable peer, so a dead tunnel would look "
                f"like a working one\n{logs}"
            )
        return [
            "the entrypoint executed with a complete environment and attempted the hop",
            f"an unreachable peer is a non-zero exit ({hop.exit_code()}), not a silent stall",
        ]
    finally:
        hop.stop()
        if not keep:
            _drop_image(image)


def verify_identity(image: str, role: str, variant: str | None, revision: str | None) -> list[str]:
    """The labels a release manifest will be checked against, inside the image.

    A publication that pushes an image whose revision label says `unknown` has
    produced a release no resolver will accept, and nothing between the build
    and the registry would have said so: the manifest would have been generated
    from what the build *meant* to pass, and the refusal would land on the
    deployment. Checked here, on the image that is about to be pushed.
    """
    labels = image_labels(image)
    evidence: list[str] = []

    declared_role = labels.get("io.hybridinference.artifact.role")
    if declared_role != role:
        raise SmokeFailure(
            f"the image declares artifact role {declared_role!r}, expected {role!r}; a role is "
            "verified against the image, so an unlabelled one can stand in for nothing"
        )
    evidence.append(f"artifact role label says {role}")

    if variant is not None:
        expected_path = {"root": "", "agents": "/agents"}[variant]
        declared_path = labels.get("io.hybridinference.artifact.base_path")
        if declared_path != expected_path:
            raise SmokeFailure(
                f"the image declares base path {declared_path!r}, expected {expected_path!r} for "
                f"variant {variant!r}"
            )
        evidence.append(f"base path label says {expected_path or '(root)'}")

    declared_revision = labels.get("org.opencontainers.image.revision")
    if revision:
        if declared_revision != revision:
            raise SmokeFailure(
                f"the revision label is {declared_revision!r}, expected {revision!r}; a release "
                "whose images cannot name their commit is refused by every resolver"
            )
        evidence.append(f"revision label says {revision[:12]}")
    elif declared_revision in {None, "", "unknown"}:
        evidence.append("no --revision given, and the image does not claim one")

    return evidence


def verify(
    role: str,
    variant: str | None,
    image: str,
    suffix: str,
    keep: bool,
    revision: str | None = None,
) -> list[str]:
    """Run one artifact's start check and return what it proved."""
    if role not in ROLES:
        raise SmokeFailure(f"unknown role {role!r}; this check knows {sorted(ROLES)}")

    identity = verify_identity(image, role, variant, revision)
    if role == "web":
        if variant not in ROLES["web"].variants:
            raise SmokeFailure(f"web needs --variant, one of {list(ROLES['web'].variants)}")
        started = verify_web(image, variant=variant or "", suffix=suffix, keep=keep)
    elif role == "control":
        started = verify_control(image, suffix=suffix, keep=keep)
    elif role == "runner":
        started = verify_runner(image, suffix=suffix, keep=keep)
    elif role == "sandbox":
        started = verify_sandbox(image, suffix=suffix, keep=keep)
    elif role == "tunnel":
        started = verify_tunnel(image, suffix=suffix, keep=keep)
    else:
        raise SmokeFailure(f"role {role!r} is declared but has no check")
    return [*identity, *started]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Not `required=True`: `--list` answers what the roles are, and demanding a
    # role to ask that is a puzzle rather than a check.
    parser.add_argument("--role", choices=sorted(ROLES))
    parser.add_argument("--variant", default=None, help="web only: root or agents")
    parser.add_argument("--image", help="The image reference to start.")
    parser.add_argument(
        "--suffix",
        default=str(int(time.time())),
        help="Names this run's containers, so concurrent runs do not collide.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="The 40-hex commit the image should name in its revision label. "
        "Passed by the workflow that built it; omitted when checking an image by hand.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the container and image behind for inspection.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print every role, its architecture and what starting proves.",
    )
    arguments = parser.parse_args(argv)

    if arguments.list:
        for role in ROLES.values():
            print(
                f"{role.name} ({role.arch}): proves {role.proves}; does not prove {role.does_not_prove}"
            )
        return 0

    if not arguments.role or not arguments.image:
        parser.error("--role and --image are required unless --list is given")

    role = ROLES[arguments.role]
    target = f"{role.name}/{arguments.variant}" if arguments.variant else role.name
    print(f"smoke: {target} ({role.arch}) proves {role.proves}")
    try:
        evidence = verify(
            arguments.role,
            arguments.variant,
            arguments.image,
            arguments.suffix,
            arguments.keep,
            arguments.revision,
        )
    except SmokeFailure as error:
        print(f"::error title=Artifact does not start::{target}: {error}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as error:
        print(f"::error title=Artifact does not start::{target}: {error}", file=sys.stderr)
        return 1

    for line in evidence:
        print(f"  proved: {line}")
    print(f"smoke: {target} is up")
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
