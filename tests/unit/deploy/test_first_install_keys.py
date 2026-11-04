"""The control-plane deployer writes a new installation's keys, and only a new one's.

`deploy/control-plane/deploy.sh` generates the bundled database's password and
connection, the account key and the settings root key when they are missing,
but only where it can prove nothing depends on them yet. These run the packaged
deployer over the recorded Docker boundary of `test_artifact_deploy.py`, whose
stand-in answers the volume and container questions from `TEST_*` variables.

See docs/agents/specs/2026-11-04-first-install-keys.zh-CN.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.unit.deploy.test_artifact_deploy import COMMIT, deployment, run

_HEX = re.compile(r"[0-9a-f]{64}")
_REQUIRED = ("AGENT_DATABASE_URL", "AGENT_POSTGRES_PASSWORD", "AGENT_LOCAL_SESSION_SECRET")
_GENERATED = (*_REQUIRED, "AGENT_SETTINGS_ENCRYPTION_KEY")


def _values(dotenv: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in dotenv.read_text().splitlines() if "=" in line)


def _new_installation(tmp_path: Path, *, keep: str = "") -> tuple[Path, dict, Path]:
    """A deployment root that has never deployed, holding ``keep`` instead of its keys."""
    script, env = deployment(tmp_path, "control-plane")
    root = Path(env["DEPLOY_ROOT"])
    dotenv = root / ".env"
    lines = [
        line for line in dotenv.read_text().splitlines() if line.split("=")[0] not in _REQUIRED
    ]
    # The example's empty lines are where the generated values belong.
    dotenv.write_text("\n".join([*lines, "AGENT_SETTINGS_ENCRYPTION_KEY=", keep]) + "\n")
    dotenv.chmod(0o640)
    (root / ".deployed-sha").unlink()
    return script, env, dotenv


def test_a_new_installation_gets_its_keys_once(tmp_path: Path) -> None:
    script, env, dotenv = _new_installation(tmp_path)

    first, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert first.returncode == 0, first.stdout + first.stderr
    values = _values(dotenv)
    password = values["AGENT_POSTGRES_PASSWORD"]
    assert values["AGENT_DATABASE_URL"] == (
        f"postgresql://cloudagent:{password}@postgres:5432/cloudagent"
    )
    secrets = [values[name] for name in _GENERATED if name != "AGENT_DATABASE_URL"]
    assert all(_HEX.fullmatch(secret) for secret in secrets)
    assert len(set(secrets)) == len(secrets)
    # Filled in place: one line each, and the file is its owner's alone.
    text = dotenv.read_text()
    assert all(text.count(f"\n{name}=") + text.startswith(f"{name}=") == 1 for name in _GENERATED)
    assert dotenv.stat().st_mode & 0o077 == 0
    assert not any(secret in first.stdout + first.stderr for secret in secrets)
    assert "generated AGENT_POSTGRES_PASSWORD AGENT_DATABASE_URL" in first.stdout
    # The application keeps the service keys; nothing writes them here any more.
    assert "AGENT_COMPUTE_SECRET_KEY" not in text

    # The first attempt created the database volume; the next one reuses all of it.
    env["TEST_VOLUMES"] = "artifact-test_agent-postgres"
    second, _ = run(script, env, "--phase", "prepare", COMMIT)
    assert second.returncode == 0, second.stdout + second.stderr
    assert dotenv.read_text() == text
    assert "generated" not in second.stdout


def test_values_already_set_are_never_replaced(tmp_path: Path) -> None:
    keep = "\n".join(
        [
            "AGENT_DATABASE_URL=postgresql://cloudagent:chosen@postgres:5432/cloudagent",
            "AGENT_POSTGRES_PASSWORD=chosen",
            "export AGENT_SETTINGS_ENCRYPTION_KEY = 'operator-chosen-settings-root-key-material'",
        ]
    )
    script, env, dotenv = _new_installation(tmp_path, keep=keep)
    # A value the deployer's own environment supplies is set too, and stays there.
    env["AGENT_LOCAL_SESSION_SECRET"] = "supplied-by-the-caller-environment-0123456789"
    before = dotenv.read_text()

    result, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert dotenv.read_text() == before
    assert "generated" not in result.stdout


@pytest.mark.parametrize("deployed", [False, True])
def test_a_legacy_session_secret_root_is_never_given_a_new_settings_key(
    tmp_path: Path, deployed: bool
) -> None:
    """An older deployment's settings were encrypted under AGENT_SESSION_SECRET."""
    script, env, dotenv = _new_installation(
        tmp_path, keep="AGENT_SESSION_SECRET=legacy-root-of-every-saved-setting-0123"
    )
    if deployed:
        for name in _REQUIRED:
            env[name] = "set-by-the-caller"
        (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").write_text("previous-generation\n")

    result, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _values(dotenv)["AGENT_SETTINGS_ENCRYPTION_KEY"] == ""
    assert "AGENT_SETTINGS_ENCRYPTION_KEY" not in result.stdout + result.stderr
    if deployed:
        assert "generated" not in result.stdout
    else:
        generated = "AGENT_POSTGRES_PASSWORD AGENT_DATABASE_URL AGENT_LOCAL_SESSION_SECRET into"
        assert f"generated {generated}" in result.stdout


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ({"deployed": "1"}, "has completed a deployment before"),
        ({"TEST_VOLUMES": "other\nartifact-test_agent-postgres"}, "database volume"),
        ({"TEST_LABELLED_VOLUMES": "restored-volume"}, "database volume"),
        ({"TEST_PROJECT_CONTAINERS": "0123456789ab"}, "containers of Compose project"),
        ({"TEST_DOCKER_DOWN": "1"}, "Docker could not list"),
        (
            {"url": "postgresql://agent:secret@db.example.org:5432/agent"},
            "other than the bundled one",
        ),
    ],
)
def test_a_root_that_may_hold_data_is_refused_rather_than_given_keys(
    tmp_path: Path, condition: dict[str, str], reason: str
) -> None:
    script, env, dotenv = _new_installation(
        tmp_path,
        keep=f"AGENT_DATABASE_URL={condition['url']}" if "url" in condition else "",
    )
    if condition.pop("deployed", None):
        (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").write_text("previous-generation\n")
    condition.pop("url", None)
    env.update(condition)
    before = dotenv.read_text()

    result, calls = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode != 0
    assert reason in result.stderr
    assert "AGENT_POSTGRES_PASSWORD" in result.stderr
    assert "never new ones" in result.stderr
    assert dotenv.read_text() == before
    assert not any(call["args"][:1] == ["compose"] and "pull" in call["args"] for call in calls)


def test_a_deployed_root_without_any_settings_root_is_warned_not_given_one(
    tmp_path: Path,
) -> None:
    """Compose does not require the key, and a private override may supply it."""
    script, env = deployment(tmp_path, "control-plane")
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    before = dotenv.read_text()

    result, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "WARNING: AGENT_SETTINGS_ENCRYPTION_KEY is not set and was not generated" in result.stdout
    )
    assert dotenv.read_text() == before


@pytest.mark.parametrize(
    ("keep", "message"),
    [
        (
            "AGENT_DATABASE_URL=postgresql://cloudagent:chosen@postgres:5432/cloudagent",
            "set the password the URL uses",
        ),
        ("AGENT_POSTGRES_PASSWORD=chosen", "set the URL with that password"),
    ],
)
def test_half_a_database_connection_is_not_completed_by_guessing(
    tmp_path: Path, keep: str, message: str
) -> None:
    script, env, dotenv = _new_installation(tmp_path, keep=keep)
    before = dotenv.read_text()

    result, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode != 0
    assert message in result.stderr
    assert dotenv.read_text() == before


def test_abandoning_an_attempt_writes_no_keys(tmp_path: Path) -> None:
    script, env, dotenv = _new_installation(tmp_path)
    before = dotenv.read_text()

    result, _ = run(script, env, "--phase", "abandon")

    assert result.returncode == 0, result.stdout + result.stderr
    assert dotenv.read_text() == before
