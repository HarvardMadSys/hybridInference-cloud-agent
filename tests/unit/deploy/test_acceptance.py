"""What the acceptance asserts about a deployment, without needing a daemon.

The acceptance itself needs Docker, a registry and ninety seconds; these are the
decisions inside it that can be wrong quietly. A synthetic environment missing a
variable the stack requires fails with an interpolation error that says nothing
about the release. A stage list read as a set passes a deployment that ran the
identity check before the migration. And a stop at the *wrong* place reads
exactly like the expected stop at the host fence, which is the one failure this
whole job exists to distinguish.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from deploy.release import acceptance as ac, release_manifest as rm

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "deploy/control-plane/docker-compose.yml"


def good_log() -> str:
    """A deploy that reached the fence and stopped there."""
    return "\n".join(
        [
            "[deploy] previous: unknown",
            "[deploy] pulling release images",
            "[deploy] starting deployment database",
            "[deploy] migrating deployment database",
            "[deploy] checking the Agent database identity issuer",
            "[deploy] waiting for bootstrap control plane at http://127.0.0.1:8000/healthz",
            "[deploy] bootstrap control plane: ok",
            "[deploy] bootstrap MCP relay: ok",
            "[deploy] waiting for target abc host readiness and reverse sandbox manager probes",
            ac.FENCE_REFUSAL + " no host has heartbeated",
        ]
    )


def result(log: str, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["bash"], returncode=returncode, stdout=log, stderr="")


# ------------------------------------------------------------------ variants


def test_the_variants_are_the_ones_a_release_carries() -> None:
    """One table per repository, or a deployment serves a path its image does not."""
    assert {variant: base for variant, (base, _) in ac.VARIANTS.items()} == {
        name: path for path, name in rm.WEB_VARIANTS.items()
    }


@pytest.mark.parametrize(
    ("variant", "base", "api"), [("root", "", "/api"), ("agents", "/agents", "/agents/api")]
)
def test_the_mount_shape_and_the_browser_path_are_one_decision(
    variant: str, base: str, api: str
) -> None:
    environment = ac.synthetic_environment(COMPOSE, variant, "p")
    assert environment["AGENT_WEB_VARIANT"] == variant
    assert environment["AGENT_WEB_BASE_PATH"] == base
    assert environment["AGENT_WEB_API_URL"] == api


# --------------------------------------------------------------- environment


def test_the_environment_satisfies_every_variable_the_stack_requires() -> None:
    environment = ac.synthetic_environment(COMPOSE, "agents", "p")
    missing = sorted(ac.required_variables(COMPOSE) - set(environment))
    assert not missing, f"the stack would refuse to interpolate: {missing}"


def test_the_release_s_own_variables_are_never_in_the_deployment_s_env() -> None:
    """The negative case exists because a deployment that sets these is refused."""
    environment = ac.synthetic_environment(COMPOSE, "agents", "p")
    assert "AGENT_DEPLOYMENT_SHA" not in environment
    assert not [name for name in environment if name.endswith("_IMAGE")]
    assert not [name for name in ac.required_variables(COMPOSE) if name.endswith("_IMAGE")]
    assert "AGENT_DEPLOYMENT_SHA" not in ac.required_variables(COMPOSE)


def test_the_database_url_matches_the_password_the_postgres_service_gets() -> None:
    environment = ac.synthetic_environment(COMPOSE, "agents", "p")
    password = environment["AGENT_POSTGRES_PASSWORD"]
    assert environment["AGENT_DATABASE_URL"] == (
        f"postgresql://cloudagent:{password}@postgres:5432/cloudagent"
    )
    assert environment["AGENT_DATABASE_URL"].startswith("postgresql://"), (
        "asyncpg refuses a SQLAlchemy-shaped scheme, and Alembic rewrites this same value"
    )


def test_two_acceptances_on_one_machine_do_not_share_a_port() -> None:
    first = ac.synthetic_environment(COMPOSE, "agents", "one")
    second = ac.synthetic_environment(COMPOSE, "agents", "two")
    ports = [
        value
        for environment in (first, second)
        for key, value in environment.items()
        if key.endswith("_PORT")
    ]
    assert len(set(ports)) == len(ports)
    assert first["COMPOSE_PROJECT"] == "one" and second["COMPOSE_PROJECT"] == "two"


class _ProbeSocket:
    """Stands in for `socket.socket()`: reports the port the kernel is told to."""

    def __init__(self, port: int) -> None:
        self._port = port

    def bind(self, _address: tuple[str, int]) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self._port)

    def __enter__(self) -> _ProbeSocket:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _offered(monkeypatch: pytest.MonkeyPatch, ports: list[int], held: set[int]) -> None:
    """Make `free_port` see exactly these ports, and start from exactly `held`."""
    offered = iter(ports)
    monkeypatch.setattr(ac, "_HANDED_OUT_PORTS", held)
    monkeypatch.setattr(ac.socket, "socket", lambda: _ProbeSocket(next(offered)))


def test_free_port_skips_a_port_it_has_already_handed_out(monkeypatch) -> None:
    """The kernel offers the port straight back once the probe socket closes.

    This is the 2026-09-15 CI failure made reproducible. Six calls returned
    58361, 60585, 50619, 33503, 33503, 45119; the duplicate failed
    `test_two_acceptances_on_one_machine_do_not_share_a_port` above, on a commit
    that changed nothing in this file. Asking the kernel to repeat itself is not
    something a test can do, so the repetition is supplied — and the assertion
    above is left exactly as it was, because it was right.
    """
    _offered(monkeypatch, [33503, 33503, 45119], held=set())

    assert [ac.free_port(), ac.free_port()] == [33503, 45119]


def test_free_port_reports_an_exhausted_search_instead_of_spinning(monkeypatch) -> None:
    """A range with nothing left to give is a failure, not a hang."""
    _offered(monkeypatch, [33503] * ac._FREE_PORT_ATTEMPTS, held={33503})

    with pytest.raises(ac.AcceptanceFailure, match="was free in"):
        ac.free_port()


@pytest.mark.parametrize(
    "message",
    [
        "Error response from daemon: Bind for 127.0.0.1:33503 failed: port is already allocated",
        "failed to bind host port for 127.0.0.1:33503: address already in use",
        "Error starting userland proxy: listen tcp4 127.0.0.1:33503: bind: address already in use",
    ],
)
@pytest.mark.parametrize("operation", ["deploy", "compose"])
def test_docker_port_conflicts_are_reported_from_both_startup_paths(
    monkeypatch, tmp_path, message, operation
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result(message))

    with pytest.raises(ac.HostPortConflict, match="33503"):
        if operation == "deploy":
            ac.run_deploy(tmp_path, tmp_path, tmp_path / "manifest.json", fence_timeout=1)
        else:
            ac.compose("test", tmp_path / "compose.yml", tmp_path, "up", "-d", "web")


@pytest.mark.parametrize(
    "message",
    [
        "application failed: address already in use",
        "failed to bind host port for 127.0.0.1:33503: permission denied",
        "could not connect to the Docker daemon",
        ac.FENCE_REFUSAL,
    ],
)
def test_unrelated_errors_are_not_classified_as_port_conflicts(message) -> None:
    ac.check_host_port_conflict(result(message))


def test_a_successful_command_is_not_retried_for_old_error_text() -> None:
    ac.check_host_port_conflict(
        result("Bind for 127.0.0.1:33503 failed: port is already allocated", returncode=0)
    )


@pytest.mark.parametrize("phase", ["deploy", "web"])
def test_acceptance_cleans_up_and_reallocates_ports_after_a_conflict(
    monkeypatch, tmp_path, phase
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"source_commit": "a" * 40}))
    environments = []
    calls = []
    conflict = "Bind for 127.0.0.1:33503 failed: port is already allocated"
    monkeypatch.setattr(ac, "extract_recipe", lambda *a: REPO_ROOT)

    def deploy(recipe, root, manifest, **kwargs):
        environments.append(
            dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines())
        )
        calls.append("deploy")
        if phase == "deploy" and len(environments) == 1:
            ac.check_host_port_conflict(result(conflict))
        return result(good_log())

    def compose(*args, **kwargs):
        calls.append("web")
        if phase == "web" and len(environments) == 1:
            ac.check_host_port_conflict(result(conflict))

    def teardown(project):
        assert project == "acceptance-test"
        calls.append("cleanup")

    monkeypatch.setattr(ac, "run_deploy", deploy)
    monkeypatch.setattr(ac, "compose", compose)
    monkeypatch.setattr(ac, "teardown", teardown)
    monkeypatch.setattr(ac, "docker", lambda *a: "")
    monkeypatch.setattr(ac, "generated_compose", lambda root: root / "compose.yml")
    monkeypatch.setattr(ac, "observed_services", lambda *a: {})
    monkeypatch.setattr(ac.aa, "expected_services", lambda *a: {})
    monkeypatch.setattr(ac.aa, "check_running", lambda *a: ["running images verified"])

    evidence = ac.accept(
        manifest, tmp_path / "recipe.tar.gz", "agents", tmp_path, "acceptance-test"
    )

    assert evidence[-1] == "running images verified"
    expected = ["deploy", "cleanup", "deploy", "web"]
    if phase == "web":
        expected.insert(1, "web")
    assert calls == expected
    ports = [value for env in environments for key, value in env.items() if key.endswith("_PORT")]
    assert len(set(ports)) == len(ports) == 6
    assert environments[0]["COMPOSE_PROJECT"] == environments[1]["COMPOSE_PROJECT"]


@pytest.mark.parametrize("failure", ["persistent_conflict", "unrelated", "cleanup"])
def test_acceptance_stops_when_retry_cannot_recover(monkeypatch, tmp_path, failure) -> None:
    attempts = []
    cleanups = []

    def attempt(*args):
        attempts.append(1)
        if failure == "unrelated":
            raise ac.AcceptanceFailure("image verification failed")
        raise ac.HostPortConflict("host port is occupied")

    monkeypatch.setattr(ac, "_accept_once", attempt)
    monkeypatch.setattr(ac, "teardown", lambda project: cleanups.append(project))
    monkeypatch.setattr(
        ac, "docker", lambda *a: "leftover-container" if failure == "cleanup" else ""
    )
    expected = {
        "persistent_conflict": "after 3 attempts",
        "unrelated": "image verification failed",
        "cleanup": "remain after cleanup",
    }

    with pytest.raises(ac.AcceptanceFailure, match=expected[failure]):
        ac.accept(tmp_path / "manifest", tmp_path / "recipe", "agents", tmp_path, "acceptance-test")

    assert len(attempts) == (3 if failure == "persistent_conflict" else 1)
    assert len(cleanups) == {"persistent_conflict": 2, "unrelated": 0, "cleanup": 1}[failure]


def test_an_unknown_variant_is_refused() -> None:
    with pytest.raises(ac.AcceptanceFailure, match="unknown variant"):
        ac.synthetic_environment(COMPOSE, "console", "p")


def test_the_env_file_is_written_the_way_a_deploy_host_configures_one(tmp_path: Path) -> None:
    path = ac.write_env(tmp_path, {"B": "2", "A": "1"})
    assert path.read_text() == "A=1\nB=2\n"


# -------------------------------------------------------------------- stages


def test_a_deploy_that_reached_the_fence_and_stopped_is_accepted() -> None:
    evidence = ac.check_stages(result(good_log()))
    assert len(evidence) == len(ac.STAGES) + 1
    assert "host fence refused" in evidence[-1]


@pytest.mark.parametrize("stage", [marker for marker, _ in ac.STAGES])
def test_a_stage_that_never_happened_is_refused(stage: str) -> None:
    with pytest.raises(ac.AcceptanceFailure, match="never got as far as"):
        ac.check_stages(result(good_log().replace(stage, "something else")))


def test_the_stages_are_checked_in_order() -> None:
    """A set would accept a deployment that migrated after checking identity."""
    lines = good_log().splitlines()
    migrate = next(i for i, line in enumerate(lines) if "migrating deployment database" in line)
    identity = next(i for i, line in enumerate(lines) if "identity issuer" in line)
    lines[migrate], lines[identity] = lines[identity], lines[migrate]
    with pytest.raises(ac.AcceptanceFailure, match="never got as far as"):
        ac.check_stages(result("\n".join(lines)))


def test_a_deployment_that_succeeded_without_a_host_is_refused() -> None:
    """The fence is the only thing standing between a control plane and an unreplaced pool."""
    with pytest.raises(
        ac.AcceptanceFailure, match="readiness fence passed without an execution host"
    ):
        ac.check_stages(result(good_log(), returncode=0))


def test_a_failure_after_the_fence_was_reached_is_not_the_expected_stop() -> None:
    log = good_log().replace(ac.FENCE_REFUSAL, "[deploy] ERROR: something else entirely")
    with pytest.raises(ac.AcceptanceFailure, match="not \\*at\\* it"):
        ac.check_stages(result(log))


# -------------------------------------------------------------------- recipe


def archive(entries: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buffer.seek(0)
    return buffer


def write_archive(path: Path, entries: dict[str, bytes]) -> Path:
    path.write_bytes(archive(entries).getvalue())
    return path


def test_a_recipe_root_carrying_source_is_not_a_recipe(tmp_path: Path) -> None:
    source = write_archive(
        tmp_path / "r.tar.gz",
        {
            "recipe/deploy/DEPLOY_PROTOCOL": b"v3\n",
            "recipe/apps/backend/cloud_agent/__init__.py": b"",
        },
    )
    with pytest.raises(Exception, match="apps"):
        ac.extract_recipe(source, tmp_path / "out")


def test_a_recipe_that_ships_a_dockerfile_is_refused(tmp_path: Path) -> None:
    source = write_archive(
        tmp_path / "r.tar.gz",
        {"recipe/deploy/DEPLOY_PROTOCOL": b"v3\n", "recipe/deploy/Dockerfile.web": b"FROM x\n"},
    )
    with pytest.raises(ac.AcceptanceFailure, match="could build its own image"):
        ac.extract_recipe(source, tmp_path / "out")


def test_an_archive_without_a_recipe_root_is_refused(tmp_path: Path) -> None:
    source = write_archive(tmp_path / "r.tar.gz", {"deploy/DEPLOY_PROTOCOL": b"v3\n"})
    with pytest.raises(ac.AcceptanceFailure, match="does not unpack to a `recipe/` root"):
        ac.extract_recipe(source, tmp_path / "out")


def test_the_real_recipe_unpacks_as_one(tmp_path: Path) -> None:
    """The archive this repository actually builds is a root the acceptance accepts."""
    from deploy.release import build_recipe

    source = tmp_path / "recipe.tar.gz"
    source.write_bytes(build_recipe.build(REPO_ROOT))
    root = ac.extract_recipe(source, tmp_path / "out")
    assert (root / "deploy/control-plane/deploy.sh").is_file()
    assert (root / "deploy/release/release_env.py").is_file()


# --------------------------------------------------------------------- paths


def test_the_deploy_script_is_handed_an_absolute_manifest(tmp_path: Path, monkeypatch) -> None:
    """`deploy.sh` cds into the deployment root before it reads anything.

    A relative path is therefore resolved against a directory the caller never
    saw. The first live run of this acceptance died on `release manifest does
    not exist: release/release.json`, a file that was right there in the
    workspace — and the recipe's helper does absolutize the value, but only
    after the existence check it had already failed.
    """
    manifest = tmp_path / "release" / "release.json"
    manifest.parent.mkdir()
    manifest.write_text("{}")
    captured: dict[str, str] = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)
    ac.run_deploy(
        tmp_path / "recipe", tmp_path / "root", Path("release/release.json"), fence_timeout=1
    )

    passed = captured["AGENT_RELEASE_MANIFEST"]
    assert Path(passed).is_absolute(), f"{passed!r} is resolved against the deployment root"
    assert Path(passed).resolve() == manifest.resolve()


def test_the_deploy_never_inherits_a_commit_or_a_source_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """Variables a CI runner may already carry, and which would change the run."""
    captured: dict[str, str] = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    for name in ("DEPLOY_SHA", "SOURCE_REPO", "COMPOSE_PROJECT", "ALLOW_DIRTY"):
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ac.run_deploy(tmp_path / "recipe", tmp_path / "root", tmp_path / "m.json", fence_timeout=1)

    assert not [
        name
        for name in ("DEPLOY_SHA", "SOURCE_REPO", "COMPOSE_PROJECT", "ALLOW_DIRTY")
        if name in captured
    ]
    assert captured["AGENT_DEPLOY_MODE"] == "artifacts"
    assert captured["AGENT_DEPLOY_PYTHON"] == sys.executable


# --------------------------------------------------------------- diagnosis


def test_the_password_is_fixed_so_a_failure_can_be_reproduced() -> None:
    """Random bought nothing here and cost a run to find that out.

    The database is reachable only from the stack's own network, holds
    synthetic data, and is removed with the project.
    """
    first = ac.synthetic_environment(COMPOSE, "agents", "one")
    second = ac.synthetic_environment(COMPOSE, "agents", "two")
    assert first["AGENT_POSTGRES_PASSWORD"] == second["AGENT_POSTGRES_PASSWORD"]
    assert first["AGENT_DATABASE_URL"] == second["AGENT_DATABASE_URL"]
    assert ac.ACCEPTANCE_PASSWORD in first["AGENT_DATABASE_URL"]


def test_a_missing_stage_reports_the_whole_log() -> None:
    """The tail is where a deploy ends; the cause is usually further up.

    The second live acceptance failed with `password authentication failed`
    after the migration had connected with the same setting — and the four
    thousand characters this used to print began *after* the migration's own
    output.
    """
    noise = "\n".join(f"line {index}" for index in range(600))
    with pytest.raises(ac.AcceptanceFailure) as failure:
        ac.check_stages(result(noise + "\n" + good_log().replace("pulling release images", "x")))
    assert "line 0" in str(failure.value), "the earliest output is what explains a late failure"


def test_the_credential_probe_compares_without_disclosing(monkeypatch) -> None:
    """It runs in a log anyone can read, so it reports agreement, not values."""
    secret = "hunter2-hunter2"

    def fake_docker(*arguments: str, check: bool = True) -> str:
        if arguments[0] == "ps":
            service = arguments[arguments.index("--filter", 3) + 1].split("=")[-1]
            return f"container-{service}"
        if arguments[0] == "inspect" and "control-plane" in arguments[1]:
            return json.dumps(
                [f"AGENT_DATABASE_URL=postgresql://cloudagent:{secret}@postgres:5432/x"]
            )
        return json.dumps([f"POSTGRES_PASSWORD={secret}"])

    monkeypatch.setattr(ac, "docker", fake_docker)
    findings = ac.database_credentials("p")

    assert len(findings) == 2
    joined = " ".join(findings)
    assert secret not in joined, "the probe must not print the password it compares"
    digest = hashlib.sha256(secret.encode()).hexdigest()[:12]
    assert joined.count(digest) == 2, "matching passwords report the same digest on both halves"
    assert f"{len(secret)} characters" in joined


def test_the_probe_says_so_when_there_is_nothing_to_inspect(monkeypatch) -> None:
    monkeypatch.setattr(ac, "docker", lambda *a, **k: "")
    assert ac.database_credentials("p") == [
        "postgres: no container to inspect",
        "control-plane: no container to inspect",
    ]


# ------------------------------------------------------------------ network


def test_each_acceptance_gets_a_network_of_its_own() -> None:
    """The stack's network is named, not project-scoped, so two stacks collide.

    `${AGENT_NETWORK_NAME:-cloud-agent}` is deliberate — the gateway's console
    resolves `web` and `control-plane` by name — but it means a second stack on
    one machine joins the first's bridge and attaches a container under the
    same `postgres` alias. Docker's DNS round-robins between them, so a
    connection reaches whichever it likes: the second and third live
    acceptances failed with `password authentication failed` against staging's
    database, which had never been given the password they held.
    """
    first = ac.synthetic_environment(COMPOSE, "agents", "run-one")
    second = ac.synthetic_environment(COMPOSE, "agents", "run-two")
    assert first["AGENT_NETWORK_NAME"] != second["AGENT_NETWORK_NAME"]
    assert "run-one" in first["AGENT_NETWORK_NAME"]
    assert first["AGENT_NETWORK_NAME"] != "cloud-agent", (
        "the default is the shared one, which is what a staging deployment is already on"
    )


def test_the_compose_file_still_takes_the_network_name_from_that_variable() -> None:
    """If the stack stopped reading it, the isolation above would be silent."""
    assert "${AGENT_NETWORK_NAME:-cloud-agent}" in COMPOSE.read_text()


def test_teardown_removes_the_network_this_run_created(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, check: bool = True) -> str:
        calls.append(arguments)
        if arguments[0] == "network" and arguments[1] == "ls":
            return "net-1" if "name=p-control" in arguments else ""
        return ""

    monkeypatch.setattr(ac, "docker", fake_docker)
    ac.teardown("p")
    assert ("network", "rm", "net-1") in calls


# ----------------------------------------------------------- release values


def test_the_web_bring_up_carries_the_commit_the_env_file_must_not(monkeypatch) -> None:
    """`AGENT_DEPLOYMENT_SHA` is the release's to supply, not the deployment's.

    It is absent from the `.env` on purpose — a host that could name the commit
    it serves would make the exact-SHA readiness fence compare a value against
    itself — so the deploy script exports it from the manifest. A command issued
    by the acceptance has to do the same, and Compose interpolates the whole
    file before choosing a service, so even `up web` fails without it.
    """
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        captured["__argv__"] = " ".join(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ac.compose(
        "p",
        Path("/tmp/x.yml"),
        Path("/tmp/root"),
        "up",
        "-d",
        "web",
        env={"AGENT_DEPLOYMENT_SHA": "a" * 40},
    )

    assert captured["AGENT_DEPLOYMENT_SHA"] == "a" * 40
    assert "up -d web" in captured["__argv__"]


def test_the_commit_is_never_written_into_the_deployment_env_file() -> None:
    environment = ac.synthetic_environment(COMPOSE, "agents", "p")
    assert "AGENT_DEPLOYMENT_SHA" not in environment


def test_the_acceptance_raises_every_service_it_then_inspects() -> None:
    """The fence leaves nothing running: `web` never started, and the two
    listeners that did were stopped again by `deploy.sh`'s EXIT trap, which
    restores what a failed bring-up had bootstrapped. So the check has to raise
    them, and it has to raise exactly the set it is about to compare."""
    source = (REPO_ROOT / "deploy/release/acceptance.py").read_text()
    assert 'services = sorted(aa.STACK_SERVICES["control-plane"])' in source
    assert "*services," in source
    deploy = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
    assert "cleanup_failed_bootstrap" in deploy and "compose stop" in deploy, (
        "if the deploy stopped restoring bootstrap services, this step would be raising "
        "containers that were already up — worth knowing, not worth guessing"
    )
