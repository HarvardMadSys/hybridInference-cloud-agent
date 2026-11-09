"""The control plane's deployment keys live in their own volume, and only a new one gets new ones.

`cloud_agent.deployment.deployment_keys` is what the Compose `keys` service
runs before anything that needs a key: it stores what the environment sets,
keeps what is stored, and generates what is missing only while the bundled
database volume holds no database. `deploy/control-plane/load-keys.sh`, the
control-plane image's entrypoint, reads the files back.

See docs/agents/specs/2026-11-09-no-env-file.zh-CN.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from cloud_agent.deployment import deployment_keys as keys_module
from cloud_agent.deployment.deployment_keys import KeysRefused, export, main, prepare

REPO = Path(__file__).resolve().parents[3]
_HEX = re.compile(r"[0-9a-f]{64}")
_URL = "AGENT_DATABASE_URL"
_PASSWORD = "AGENT_POSTGRES_PASSWORD"
_SESSION = "AGENT_LOCAL_SESSION_SECRET"
_ROOT = "AGENT_SETTINGS_ENCRYPTION_KEY"
_LEGACY = "AGENT_SESSION_SECRET"
_GENERATED = (_PASSWORD, _URL, _SESSION, _ROOT)


@pytest.fixture
def volumes(tmp_path: Path) -> tuple[Path, Path]:
    keys = tmp_path / "keys"
    database = tmp_path / "database"
    keys.mkdir()
    database.mkdir()
    return keys, database


def _stored(keys: Path) -> dict[str, str]:
    """Every stored value, byte for byte."""
    return {path.name: path.read_text() for path in keys.iterdir()}


def _existing_database(database: Path) -> None:
    (database / "PG_VERSION").write_text("16\n")


def test_a_new_installation_gets_its_keys_once(volumes: tuple[Path, Path]) -> None:
    keys, database = volumes

    outcome = prepare({}, keys, database)

    assert outcome.generated == list(keys_module.STORED[:4])
    values = _stored(keys)
    assert set(values) == set(_GENERATED)
    password = values[_PASSWORD]
    assert values[_URL] == f"postgresql://cloudagent:{password}@postgres:5432/cloudagent"
    secrets = [values[name] for name in (_PASSWORD, _SESSION, _ROOT)]
    assert all(_HEX.fullmatch(secret) for secret in secrets)
    assert len(set(secrets)) == len(secrets)
    assert all((keys / name).stat().st_mode & 0o777 == 0o400 for name in values)

    # Postgres has since created its database; the next start reuses every key.
    _existing_database(database)
    again = prepare({}, keys, database)
    assert again.generated == again.stored == []
    assert _stored(keys) == values


def test_the_bundled_database_name_and_user_come_from_the_environment(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes

    prepare({"AGENT_POSTGRES_USER": "agents", "AGENT_POSTGRES_DB": "agentdb"}, keys, database)

    url = _stored(keys)[_URL]
    assert url.startswith("postgresql://agents:") and url.endswith("@postgres:5432/agentdb")


def test_a_name_that_cannot_go_into_a_connection_is_refused(volumes: tuple[Path, Path]) -> None:
    keys, database = volumes

    with pytest.raises(KeysRefused, match="letters, digits and _"):
        prepare({"AGENT_POSTGRES_USER": "agent@x"}, keys, database)
    assert not any(keys.iterdir())


def test_an_upgrade_stores_its_env_values_and_can_then_drop_them(
    volumes: tuple[Path, Path],
) -> None:
    """An existing deployment's .env is copied, so its lines can go after one start."""
    keys, database = volumes
    _existing_database(database)
    environment = {
        _URL: "postgresql://cloudagent:chosen@postgres:5432/cloudagent",
        _PASSWORD: "chosen",
        _SESSION: "operator-chosen-session-key-0123456789abcdef",
        _ROOT: "operator-chosen-settings-root-key-material",
    }

    outcome = prepare(environment, keys, database)

    assert outcome.generated == []
    assert sorted(outcome.stored) == sorted(environment)
    assert _stored(keys) == environment
    # The lines are deleted from .env: the stored values serve unchanged.
    assert prepare({_URL: "", _ROOT: "  "}, keys, database).stored == []
    assert _stored(keys) == environment


def test_the_environment_wins_over_a_stored_value(volumes: tuple[Path, Path]) -> None:
    keys, database = volumes
    prepare({}, keys, database)
    _existing_database(database)

    outcome = prepare({_ROOT: "a-key-the-operator-set-on-purpose-0123"}, keys, database)

    assert outcome.stored == [_ROOT]
    assert _stored(keys)[_ROOT] == "a-key-the-operator-set-on-purpose-0123"


@pytest.mark.parametrize("new", [True, False])
def test_a_legacy_session_secret_root_is_never_given_a_new_settings_key(
    volumes: tuple[Path, Path], new: bool
) -> None:
    """An older deployment's settings were encrypted under AGENT_SESSION_SECRET."""
    keys, database = volumes
    environment = {_LEGACY: "legacy-root-of-every-saved-setting-0123"}
    if not new:
        _existing_database(database)
        environment |= {_URL: "postgresql://u:p@postgres/db", _SESSION: "s" * 40}

    outcome = prepare(environment, keys, database)

    assert _ROOT not in _stored(keys)
    assert _stored(keys)[_LEGACY] == environment[_LEGACY]
    assert _ROOT not in outcome.generated
    assert outcome.warnings == []


@pytest.mark.parametrize(
    ("environment", "existing", "reason"),
    [
        ({}, True, "already holds a database"),
        ({_PASSWORD: "chosen"}, True, "already holds a database"),
        (
            {_URL: "postgresql://agent:secret@db.example.org:5432/agent"},
            False,
            "other than the bundled one",
        ),
    ],
)
def test_a_deployment_that_may_hold_data_is_refused_rather_than_given_keys(
    volumes: tuple[Path, Path], environment: dict[str, str], existing: bool, reason: str
) -> None:
    keys, database = volumes
    if existing:
        _existing_database(database)

    with pytest.raises(KeysRefused, match=reason) as refused:
        prepare(environment, keys, database)

    assert _SESSION in str(refused.value)
    assert "never new ones" in str(refused.value)
    assert not (keys / _ROOT).exists()
    assert not (keys / _SESSION).exists()


def test_an_unreadable_database_volume_is_not_taken_for_a_new_one(
    volumes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    keys, database = volumes

    def unreadable(self: Path) -> bool:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "exists", unreadable)

    with pytest.raises(KeysRefused, match="could not be read"):
        prepare({}, keys, database)


def test_a_deployment_without_a_settings_root_is_warned_not_given_one(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes
    _existing_database(database)
    environment = {_URL: "postgresql://u:p@postgres/db", _SESSION: "s" * 40}

    outcome = prepare(environment, keys, database)

    assert outcome.generated == []
    assert not (keys / _ROOT).exists()
    assert outcome.warnings and _ROOT in outcome.warnings[0]


def test_the_bundled_password_is_the_one_its_connection_names(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes
    _existing_database(database)
    environment = {
        _URL: "postgresql://cloudagent:p%40ss@postgres:5432/cloudagent",
        _SESSION: "s" * 40,
    }

    prepare(environment | {_ROOT: "r" * 40}, keys, database)

    assert _stored(keys)[_PASSWORD] == "p@ss"


def test_a_bundled_connection_without_a_password_is_not_guessed(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes

    with pytest.raises(KeysRefused, match="without a password"):
        prepare({_URL: "postgresql://cloudagent@postgres:5432/cloudagent"}, keys, database)


def test_with_an_external_database_the_bundled_one_only_needs_some_password(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes
    environment = {
        _URL: "postgresql://agent:secret@db.example.org:5432/agent",
        _SESSION: "s" * 40,
        _ROOT: "r" * 40,
    }

    prepare(environment, keys, database)

    assert _HEX.fullmatch(_stored(keys)[_PASSWORD])
    assert _stored(keys)[_URL] == environment[_URL]


def test_a_new_installation_completes_the_connection_for_its_chosen_password(
    volumes: tuple[Path, Path],
) -> None:
    keys, database = volumes

    prepare({_PASSWORD: "chosen"}, keys, database)

    assert _stored(keys)[_URL] == "postgresql://cloudagent:chosen@postgres:5432/cloudagent"


@pytest.mark.parametrize("password", ["a/b?c#d@e:f", "per%cent and space"])
def test_a_chosen_password_is_percent_encoded_in_the_connection_only(
    volumes: tuple[Path, Path], password: str
) -> None:
    """The DSN must still name the bundled host and give back the password exactly."""
    keys, database = volumes

    prepare({_PASSWORD: password}, keys, database)

    url = urlsplit(_stored(keys)[_URL])
    assert url.hostname == "postgres"
    assert url.port == 5432
    assert url.password is not None and unquote(url.password) == password
    assert _stored(keys)[_PASSWORD] == password


def test_a_given_key_is_stored_byte_for_byte(volumes: tuple[Path, Path]) -> None:
    """The settings root is read raw, so its spaces and `$` are part of the key."""
    keys, database = volumes
    _existing_database(database)
    root = "  root with $dollar ${BRACES} and spaces  "
    environment = {_URL: "postgresql://u:p@postgres/db", _SESSION: "s" * 40, _ROOT: root}

    prepare(environment, keys, database)

    assert _stored(keys)[_ROOT] == root
    assert (keys / _ROOT).read_bytes() == root.encode()
    # Spaces alone are no value, as Compose's empty default is none.
    assert prepare({_ROOT: "   "}, keys, database).stored == []
    assert _stored(keys)[_ROOT] == root


def test_a_multi_line_value_is_refused(volumes: tuple[Path, Path]) -> None:
    keys, database = volumes

    with pytest.raises(KeysRefused, match="single line"):
        prepare({_ROOT: "one\ntwo"}, keys, database)


def test_the_service_names_what_it_did_and_never_a_value(
    volumes: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keys, database = volumes
    for name in (*keys_module.STORED, "AGENT_POSTGRES_USER", "AGENT_POSTGRES_DB"):
        monkeypatch.delenv(name, raising=False)

    assert main(["--keys-dir", str(keys), "--database-dir", str(database)]) == 0

    output = capsys.readouterr()
    assert "generated AGENT_DATABASE_URL AGENT_POSTGRES_PASSWORD" in output.out
    assert not any(value in output.out + output.err for value in _stored(keys).values())


def test_a_refusal_exits_non_zero_with_its_reason(
    volumes: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keys, database = volumes
    _existing_database(database)
    for name in keys_module.STORED:
        monkeypatch.delenv(name, raising=False)

    assert main(["--keys-dir", str(keys), "--database-dir", str(database)]) == 1
    assert "already holds a database" in capsys.readouterr().err


def test_export_writes_env_lines_that_restore_the_keys(volumes: tuple[Path, Path]) -> None:
    keys, database = volumes
    prepare({}, keys, database)
    stored = _stored(keys)

    lines = export(keys)

    assert lines == [f"{name}='{stored[name]}'" for name in keys_module.STORED if name in stored]


def test_a_key_compose_cannot_carry_literally_is_not_exported(
    volumes: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    keys, _ = volumes
    (keys / _ROOT).write_text("it's a key")

    with pytest.raises(KeysRefused, match="single quote"):
        export(keys)
    assert main(["export", "--keys-dir", str(keys)]) == 1
    assert "it's a key" not in capsys.readouterr().out


def test_an_export_restored_through_compose_is_the_same_bytes(
    volumes: tuple[Path, Path], tmp_path: Path
) -> None:
    """The backup and restore round trip, through Compose's own `.env` parser."""
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose CLI is needed for its actual .env parser")
    keys, database = volumes
    _existing_database(database)
    original = {
        _URL: "postgresql://cloudagent:p%24ss@postgres:5432/cloudagent",
        _SESSION: "session $HOME ${PATH} key with  spaces ",
        _ROOT: ' $$ root ${UNSET:-default} `tick` \\ "quote" ',
        _LEGACY: "legacy#not-a-comment",
    }
    prepare(original, keys, database)
    backup = tmp_path / "keys.env"
    backup.write_text("\n".join(export(keys)) + "\n")

    # The restoring machine's `keys` service gets exactly what Compose resolves.
    stack = REPO / "deploy/control-plane/docker-compose.yml"
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(stack),
            "--env-file",
            str(backup),
            "config",
            "--format",
            "json",
        ],
        env={"PATH": os.environ["PATH"], "AGENT_DEPLOYMENT_SHA": "0" * 40},
        capture_output=True,
        text=True,
        check=True,
    )
    # `config` writes a literal `$` as `$$` so its output re-renders the same;
    # the container itself receives the single `$`.
    environment = {
        name: value.replace("$$", "$")
        for name, value in json.loads(rendered.stdout)["services"]["keys"]["environment"].items()
    }
    restored = tmp_path / "restored"
    restored.mkdir()
    prepare(environment, restored, database)

    for name in original:
        assert (restored / name).read_bytes() == (keys / name).read_bytes(), name


@pytest.fixture
def loader(tmp_path: Path) -> tuple[Path, Path]:
    """The image's entrypoint, reading a test directory instead of the volume."""
    keys = tmp_path / "mounted keys"
    keys.mkdir()
    script = tmp_path / "load-keys.sh"
    source = (REPO / "deploy/control-plane/load-keys.sh").read_text()
    assert "keys=/run/cloud-agent/keys\n" in source
    script.write_text(source.replace("keys=/run/cloud-agent/keys\n", f"keys='{keys}'\n"))
    return script, keys


def _run_loader(script: Path, environment: dict[str, str]) -> dict[str, str]:
    shell = shutil.which("sh")
    assert shell
    result = subprocess.run(
        [shell, str(script), "env"],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_the_entrypoint_fills_only_what_the_environment_leaves_empty(
    loader: tuple[Path, Path],
) -> None:
    script, keys = loader
    (keys / _URL).write_text("postgresql://stored@postgres/db\n")
    (keys / _ROOT).write_text("stored-root\n")
    (keys / _PASSWORD).write_text("the database user's alone\n")

    seen = _run_loader(script, {_URL: "", _ROOT: "set-by-the-environment"})

    assert seen[_URL] == "postgresql://stored@postgres/db"
    assert seen[_ROOT] == "set-by-the-environment"
    # Only the control plane's own keys; the database password is Postgres's.
    assert _PASSWORD not in seen
    assert _SESSION not in seen


def test_the_entrypoint_runs_its_command_without_a_volume(loader: tuple[Path, Path]) -> None:
    script, keys = loader
    shutil.rmtree(keys)

    assert _URL not in _run_loader(script, {})


def test_the_control_plane_deploys_without_a_dotenv(tmp_path: Path) -> None:
    """A deployment root needs no .env: its keys are the volume's, its overrides optional."""
    from tests.unit.deploy.test_artifact_deploy import deployment, run

    script, env = deployment(tmp_path, "control-plane")
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    for line in dotenv.read_text().splitlines():
        name, _, value = line.partition("=")
        env.setdefault(name, value)
    dotenv.unlink()

    result, calls = run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    compose_calls = [call["args"] for call in calls if call["args"][:1] == ["compose"]]
    assert compose_calls
    assert not any("--env-file" in args for args in compose_calls)
    assert not dotenv.exists()
