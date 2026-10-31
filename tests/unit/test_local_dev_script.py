"""`deploy/local/dev.sh`: the database is checked before it is migrated, and secrets stay private.

The script runs over stand-in `docker` and `docker-compose` commands that
record what they are asked, so the order of its steps and the env file it
writes are read directly. The production deploy's own order is tested with
`deploy/control-plane/deploy.sh`; these are the local stack's.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

_LOCAL_DEV_SCRIPT = Path(__file__).resolve().parents[2] / "deploy/local/dev.sh"


_RETAINED_LOCAL_ENV = (
    "AGENT_POSTGRES_PASSWORD=retained-password\n"
    "AGENT_DATABASE_URL=postgresql://cloudagent:retained-password@postgres/cloudagent\n"
    "AGENT_SANDBOX_MANAGER_TOKEN=keep\n"
    "LOCAL_PROJECT_NAME=freeinference-local\n"
    "LOCAL_RELAY_NETWORK=freeinference-local-agent-relays\n"
    "LOCAL_CHAT_NETWORK_POOL=10.240.0.0/16\n"
    "LOCAL_EGRESS_DNS_SERVER=192.168.5.1\n"
    "LOCAL_EDGE_PORT=13002\n"
    "AGENT_WEB_BASE_URL=http://127.0.0.1:13002\n"
)


def _local_up(tmp_path: Path, *, check_status: int) -> tuple[subprocess.CompletedProcess, list]:
    """`dev.sh up --no-build` over a stand-in Compose; the namespace check answers ``check_status``."""
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    (script.parent / ".env.local").write_text(_RETAINED_LOCAL_ENV)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    compose_log = tmp_path / "compose.log"
    docker = fake_bin / "docker"
    # No legacy containers to retire; every other question goes unanswered.
    docker.write_text('#!/bin/sh\n[ "$1" = ps ] && exit 0\nexit 1\n')
    docker.chmod(0o755)
    compose = fake_bin / "docker-compose"
    compose.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >>"$COMPOSE_LOG"\n'
        'case "$*" in\n'
        "  *' exec -T postgres pg_isready') exit 0 ;;\n"
        f"  *' migrate python -m cloud_agent.identity_namespace --check') exit {check_status} ;;\n"
        # The full stack is where this test stops: nothing after it is asked.
        "  *' up --detach --no-build') exit 23 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    compose.chmod(0o755)
    result = subprocess.run(
        ["sh", str(script), "up", "--no-build"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMPOSE_LOG": str(compose_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, compose_log.read_text().splitlines()


def test_local_up_checks_the_database_read_only_before_migrating_it(tmp_path: Path) -> None:
    """The order the production deploy keeps: probe, check, then migrate, then start."""
    _result, calls = _local_up(tmp_path, check_status=0)

    def first(fragment: str) -> int:
        return next(index for index, call in enumerate(calls) if call.endswith(fragment))

    probe = next(
        index for index, call in enumerate(calls) if " run --rm --no-deps migrate python -c" in call
    )
    check = first(" run --rm --no-deps migrate python -m cloud_agent.identity_namespace --check")
    migrate = first(" run --rm --no-deps migrate")
    assert probe < check < migrate
    assert any(call.endswith(" up --detach --no-build") for call in calls[migrate:])


def test_a_refused_namespace_check_migrates_and_starts_nothing(tmp_path: Path) -> None:
    """A Gateway-era database is left as it was: no migration, no full stack."""
    result, calls = _local_up(tmp_path, check_status=1)

    assert result.returncode != 0
    assert any(call.endswith("identity_namespace --check") for call in calls)
    assert not any(call.endswith(" run --rm --no-deps migrate") for call in calls)
    assert not any(" up --detach --no-build" in call for call in calls)


def _local_config(script: Path, fake_bin: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(script), "config"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


def _local_checkout(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # `docker volume ls` answers that no Postgres volume exists; nothing else answers.
    (fake_bin / "docker").write_text(
        '#!/bin/sh\ncase "$1 $2" in "volume ls") exit 0 ;; esac\nexit 1\n'
    )
    (fake_bin / "docker-compose").write_text("#!/bin/sh\n")
    for name in ("docker", "docker-compose"):
        (fake_bin / name).chmod(0o755)
    return script, fake_bin


def _env_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


_SECRET_NAMES = (
    "AGENT_POSTGRES_PASSWORD",
    "AGENT_LOCAL_SESSION_SECRET",
    "AGENT_SETTINGS_ENCRYPTION_KEY",
    "AGENT_CONTROL_TOKEN_SECRET",
    "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
    "AGENT_SANDBOX_MANAGER_TOKEN",
    "LOCAL_ENROLL_TOKEN",
)


def test_a_fresh_local_env_is_private_with_its_own_account_and_settings_keys(
    tmp_path: Path,
) -> None:
    generated = []
    for name in ("first", "second"):
        script, fake_bin = _local_checkout(tmp_path / name)
        result = _local_config(script, fake_bin)
        assert result.returncode == 0, result.stderr
        dotenv = script.parent / ".env.local"
        # Created under umask 077: readable by its owner alone.
        assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
        generated.append(_env_values(dotenv))
    for values in generated:
        secrets = [values[name] for name in _SECRET_NAMES]
        assert all(re.fullmatch(r"[0-9a-f]{64}", secret) for secret in secrets), values
        assert len(set(secrets)) == len(secrets), "each secret is drawn on its own"
        # The account system's key alone: a Gateway session secret is never written.
        assert "AGENT_SESSION_SECRET" not in values
    first, second = generated
    assert all(first[name] != second[name] for name in _SECRET_NAMES)


def test_an_upgraded_local_env_gains_its_account_keys_and_keeps_every_other(
    tmp_path: Path,
) -> None:
    script, fake_bin = _local_checkout(tmp_path)
    dotenv = script.parent / ".env.local"
    dotenv.write_text(_RETAINED_LOCAL_ENV)
    dotenv.chmod(0o600)
    before = _env_values(dotenv)

    result = _local_config(script, fake_bin)

    assert result.returncode == 0, result.stderr
    after = _env_values(dotenv)
    assert {name: after[name] for name in before} == before
    session, settings = after["AGENT_LOCAL_SESSION_SECRET"], after["AGENT_SETTINGS_ENCRYPTION_KEY"]
    assert re.fullmatch(r"[0-9a-f]{64}", session) and re.fullmatch(r"[0-9a-f]{64}", settings)
    assert session != settings
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    # Run again: what was generated is kept, never rotated.
    assert _local_config(script, fake_bin).returncode == 0
    assert _env_values(dotenv) == after
