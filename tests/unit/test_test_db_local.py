"""``scripts/test-db-local.sh``: which databases it drops, creates and leaves alone.

The script shares one PostgreSQL between every checkout on a machine, so what
must not regress is whose databases it touches. Docker and ``uv`` are faked on
``PATH``: the fake ``docker`` answers ``psql`` the way the server would and
records every statement it was sent, and the fake ``uv`` stands in for pytest
and writes the JUnit report the script checks. The real container is exercised
by running the script itself (see ``docs/developer/development.md``).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "test-db-local.sh"
NAME = f"ca_{hashlib.sha1(str(REPO.resolve()).encode()).hexdigest()[:10]}_test_db"
OWNER = "test-db-local owner: "

FAKE_DOCKER = r"""#!/usr/bin/env python3
import os, sys
log = os.environ["FAKE_LOG"]
args = sys.argv[1:]
with open(log, "a") as out:
    out.write("docker " + " ".join(args) + "\n")
if args[:2] == ["container", "inspect"]:
    print("running" if "{{.State.Status}}" in args else "container-id")
elif "pg_isready" in args:
    pass
elif "psql" in args and "ON_ERROR_STOP=1" not in args:
    # The lock session: answer each statement as it arrives, as psql does.
    for line in sys.stdin:
        with open(log, "a") as out:
            out.write("lock " + line)
        print("t", flush=True)
elif "psql" in args:
    sql = sys.stdin.read()
    with open(log, "a") as out:
        out.write("sql " + sql.replace("\n", " ") + "\n")
    if "'exists:'" in sql:
        print(os.environ.get("FAKE_OWNER", ""))
    elif "starts_with(shobj_description" in sql:
        sys.stdout.write(os.environ.get("FAKE_MARKED", ""))
"""

FAKE_UV = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:2] == ["run", "pytest"]:
    with open(os.environ["FAKE_LOG"], "a") as out:
        out.write("pytest " + "\0".join(args[2:]) + "\n")
    report = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
    with open(report, "w") as out:
        out.write(os.environ.get("FAKE_JUNIT", ""))
    sys.exit(int(os.environ.get("FAKE_PYTEST_STATUS", "0")))
if args[:2] == ["run", "python"]:
    os.execv(sys.executable, [sys.executable, *args[2:]])
sys.exit(f"unexpected uv call: {args}")
"""

PASSED = '<testsuites><testsuite><testcase classname="t" name="a"/></testsuite></testsuites>'
SKIPPED = (
    '<testsuites><testsuite><testcase classname="t" name="a">'
    '<skipped type="pytest.skip" message="PostgreSQL not available"/>'
    "</testcase></testsuite></testsuites>"
)


@pytest.fixture
def fake(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("uv", FAKE_UV)):
        tool = bin_dir / name
        tool.write_text(body)
        tool.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()

    def run(*args: str, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        values = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TEST_DB_", "FAKE_", "UV_"))
        }
        values.update(
            {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_LOG": str(log),
                "FAKE_JUNIT": PASSED,
                "TEST_DB_WORKERS": "3",
                **env,
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=values,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        calls = log.read_text().splitlines()
        log.write_text("")
        return result, calls

    return run


def _sql(calls: list[str]) -> list[str]:
    return [call.removeprefix("sql ") for call in calls if call.startswith("sql ")]


def _pytest(calls: list[str]) -> list[str]:
    (call,) = [call for call in calls if call.startswith("pytest ")]
    return call.removeprefix("pytest ").split("\0")


def test_a_run_locks_then_recreates_this_checkouts_databases_then_runs_the_tier(fake) -> None:
    result, calls = fake("run", "tests/integration/storage", "-x")
    assert result.returncode == 0, result.stderr
    # The lock comes first: a second run in the same checkout must not drop
    # databases the first is still using.
    first_sql = next(i for i, call in enumerate(calls) if call.startswith("sql "))
    assert any(call.startswith("lock ") for call in calls[:first_sql])
    owner_check, drop, create = _sql(calls)
    assert "'exists:'" in owner_check
    assert "DROP DATABASE %I WITH (FORCE)" in drop
    assert create.split("; ")[:4] == [
        f"CREATE DATABASE {NAME}",
        f"CREATE DATABASE {NAME}_gw0",
        f"CREATE DATABASE {NAME}_gw1",
        f"CREATE DATABASE {NAME}_gw2",
    ]
    assert f"COMMENT ON DATABASE {NAME} IS '{OWNER}{REPO.resolve()}'" in create
    args = _pytest(calls)
    assert args[:5] == ["-n", "3", "--dist", "loadfile", "-q"]
    # The tier marker always comes before the caller's own arguments, so a
    # path cannot widen the run to the live external tests.
    assert args[-4:] == ["-m", "dbtest and not external", "tests/integration/storage", "-x"]
    assert "1 collected, 0 skipped" in result.stdout


def test_a_run_that_skipped_a_database_test_fails(fake) -> None:
    result, _ = fake("run", FAKE_JUNIT=SKIPPED)
    assert result.returncode != 0
    assert "database tests skipped, so this run proved nothing" in result.stderr


def test_a_run_that_ran_no_database_test_fails(fake) -> None:
    result, _ = fake("run", FAKE_JUNIT="<testsuites><testsuite/></testsuites>")
    assert result.returncode != 0
    assert "no database test ran" in result.stderr


def test_a_failed_pytest_is_the_exit_status(fake) -> None:
    result, _ = fake("run", FAKE_PYTEST_STATUS="3", FAKE_JUNIT=SKIPPED)
    assert result.returncode == 3


def test_a_leftover_test_db_name_does_not_choose_the_database(fake) -> None:
    # `eval "$(… env)"` in another worktree leaves this set in the shell.
    result, calls = fake("env", TEST_DB_NAME="ca_neighbour_test_db")
    assert result.returncode == 0, result.stderr
    assert f"export TEST_DB_NAME={NAME}" in result.stdout.splitlines()
    assert "export TEST_DB_PORT=54339" in result.stdout.splitlines()
    assert all("neighbour" not in call for call in calls)


def test_a_database_another_existing_checkout_owns_is_refused(fake, tmp_path: Path) -> None:
    result, calls = fake("run", FAKE_OWNER=f"exists:{OWNER}{tmp_path}")
    assert result.returncode == 2
    assert f"belongs to {tmp_path}, which still exists" in result.stderr
    assert not any("DROP DATABASE" in sql for sql in _sql(calls))


def test_a_database_this_script_did_not_make_is_refused(fake) -> None:
    for command in ("run", "reset"):
        result, calls = fake(command, FAKE_OWNER="exists:")
        assert result.returncode == 2
        assert "was not made by this script; refusing to drop it" in result.stderr
        assert not any("DROP DATABASE" in sql for sql in _sql(calls))


@pytest.mark.parametrize("owner", ["self", "gone"])
def test_a_database_of_this_or_a_removed_checkout_is_recreated(
    fake, tmp_path: Path, owner: str
) -> None:
    path = REPO.resolve() if owner == "self" else tmp_path / "removed-worktree"
    result, calls = fake("run", FAKE_OWNER=f"exists:{OWNER}{path}")
    assert result.returncode == 0, result.stderr
    assert any("DROP DATABASE" in sql for sql in _sql(calls))


def test_clean_drops_only_marked_databases_whose_checkout_is_gone(fake, tmp_path: Path) -> None:
    marked = f"ca_gone_test_db\t{tmp_path / 'removed'}\nca_alive_test_db\t{tmp_path}\n"
    result, calls = fake("clean", FAKE_MARKED=marked)
    assert result.returncode == 0, result.stderr
    drops = [sql for sql in _sql(calls) if "DROP DATABASE" in sql]
    assert len(drops) == 1
    assert result.stdout.splitlines() == [
        f"dropped ca_gone_test_db (its checkout {tmp_path / 'removed'} is gone)"
    ]


@pytest.mark.parametrize(
    "env, message",
    [
        ({"TEST_DB_LOCAL_NAME": "x; drop"}, "must be lower-case letters"),
        ({"TEST_DB_LOCAL_NAME": "production"}, "contain _test_"),
        ({"TEST_DB_LOCAL_NAME": "Mixed_test_db"}, "must be lower-case letters"),
        ({"TEST_DB_WORKERS": "0"}, "TEST_DB_WORKERS must be a positive whole number"),
    ],
)
def test_an_unusable_setting_stops_before_any_database_is_dropped(
    fake, env: dict[str, str], message: str
) -> None:
    result, calls = fake("run", **env)
    assert result.returncode == 1
    assert message in result.stderr
    assert not any("DROP DATABASE" in sql for sql in _sql(calls))


def test_an_unknown_command_shows_the_usage(fake) -> None:
    result, calls = fake("drop-everything")
    assert result.returncode == 1
    assert "unknown command 'drop-everything'" in result.stderr
    assert "test-db-local.sh run [pytest args...]" in result.stderr
    assert calls == []


def test_the_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert sys.platform != "win32"
