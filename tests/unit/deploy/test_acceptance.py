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

import io
import subprocess
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
            "[deploy] waiting for target abc host readiness and reverse broker probes",
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
        {"recipe/deploy/DEPLOY_PROTOCOL": b"v3\n", "recipe/cloud_agent/__init__.py": b""},
    )
    with pytest.raises(Exception, match="cloud_agent"):
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
