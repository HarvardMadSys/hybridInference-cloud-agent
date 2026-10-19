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
    # FAKE_LOCK=f is another run holding it; a blocking wait then succeeds.
    for line in sys.stdin:
        with open(log, "a") as out:
            out.write("lock " + line)
        print(os.environ.get("FAKE_LOCK", "t") if "try" in line else "locked", flush=True)
elif "psql" in args:
    sql = sys.stdin.read()
    # psql variables, e.g. {"name": ..., "mark": ...}, as the script set them.
    names = dict(a.split("=", 1) for a in args if "=" in a and a != "ON_ERROR_STOP=1")
    with open(log, "a") as out:
        out.write("sql " + repr(names) + " " + sql.replace("\n", " ") + "\n")
    if os.environ.get("FAKE_FAIL", "\0") in sql:
        # The server going away mid-query, or a statement timing out.
        sys.exit("psql: error: server closed the connection unexpectedly")
    if "ORDER BY datname" in sql:
        # The databases the query selects, with their marks: "name<TAB>comment".
        # As the server would, the workers' only when it asks for the whole set.
        rows = [row for row in os.environ.get("FAKE_SET", "").splitlines() if row]
        if "starts_with(datname, :'name' || '_gw')" not in sql:
            rows = [row for row in rows if row.split("|", 1)[0] == names["name"]]
        sys.stdout.write("".join(row.replace("|", "\t") + "\n" for row in rows))
    elif "regexp_replace" in sql:
        sys.stdout.write(os.environ.get("FAKE_MARKED", "").replace("|", "\t"))
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


def _drops(calls: list[str]) -> list[str]:
    return [sql for sql in _sql(calls) if "DROP DATABASE" in sql]


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
    assert "ORDER BY datname" in owner_check
    # Unconditional: every database of this name's set, which the owner
    # check has just found to be this checkout's or a removed one's.
    assert "DROP DATABASE %I WITH (FORCE)" in drop and "'mark': ''" in drop
    # Every database is marked, the workers' too, so a later owner check of
    # any one of them knows whose it is.
    mark = f"{OWNER}{REPO.resolve()}"
    expected = []
    for name in (NAME, f"{NAME}_gw0", f"{NAME}_gw1", f"{NAME}_gw2"):
        expected += [f"CREATE DATABASE {name}", f"COMMENT ON DATABASE {name} IS '{mark}'"]
    assert create.removeprefix("{} ").removesuffix("; ").split("; ") == expected
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


@pytest.mark.parametrize("member", ["", "_gw1"])
def test_a_database_another_existing_checkout_owns_is_refused(
    fake, tmp_path: Path, member: str
) -> None:
    # A worker's database counts as much as the base one: the drop removes it
    # too, whatever the base database says.
    rows = f"{NAME}|{OWNER}{REPO.resolve()}\n{NAME}{member or '_gw0'}|{OWNER}{REPO.resolve()}\n"
    rows += f"{NAME}{member}|{OWNER}{tmp_path}\n"
    result, calls = fake("run", FAKE_SET=rows)
    assert result.returncode == 2
    assert f"{NAME}{member} belongs to {tmp_path}, which still exists" in result.stderr
    assert _drops(calls) == []


@pytest.mark.parametrize("member", ["", "_gw2"])
def test_a_database_this_script_did_not_make_is_refused(fake, member: str) -> None:
    for command in ("run", "reset"):
        result, calls = fake(command, FAKE_SET=f"{NAME}{member}|\n")
        assert result.returncode == 2
        assert f"{NAME}{member} exists and was not made by this script" in result.stderr
        assert _drops(calls) == []


@pytest.mark.parametrize("owner", ["self", "gone"])
def test_a_database_of_this_or_a_removed_checkout_is_recreated(
    fake, tmp_path: Path, owner: str
) -> None:
    path = REPO.resolve() if owner == "self" else tmp_path / "removed-worktree"
    rows = f"{NAME}|{OWNER}{path}\n{NAME}_gw0|{OWNER}{path}\n"
    result, calls = fake("run", FAKE_SET=rows)
    assert result.returncode == 0, result.stderr
    assert len(_drops(calls)) == 1


def test_clean_drops_under_the_lock_only_what_still_carries_the_stale_mark(
    fake, tmp_path: Path
) -> None:
    gone = tmp_path / "removed"
    marked = f"ca_gone_test_db|{gone}\nca_alive_test_db|{tmp_path}\n"
    result, calls = fake("clean", FAKE_MARKED=marked)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"dropped ca_gone_test_db (its checkout {gone} is gone)"]
    (drop,) = _drops(calls)
    # Limited to the mark read before the lock: databases a new checkout took
    # over in between carry its mark instead, and stay.
    assert f"'name': 'ca_gone_test_db', 'mark': '{OWNER}{gone}'" in drop
    lock = next(i for i, call in enumerate(calls) if "test-db-local:ca_gone_test_db" in call)
    assert lock < calls.index(next(c for c in calls if "DROP DATABASE" in c))
    assert all("ca_alive_test_db" not in call for call in calls if call.startswith("lock "))


@pytest.mark.parametrize("command", ["run", "env", "reset"])
def test_a_failed_owner_query_drops_nothing(fake, tmp_path: Path, command: str) -> None:
    # Answered, the query would refuse: another existing checkout owns a worker.
    rows = f"{NAME}_gw0|{OWNER}{tmp_path}\n"
    result, calls = fake(command, FAKE_SET=rows, FAKE_FAIL="ORDER BY datname")
    assert result.returncode == 1
    assert f"could not read who owns {NAME}'s databases; dropping nothing" in result.stderr
    assert _drops(calls) == []
    assert not any(call.startswith("pytest ") for call in calls)


def test_a_failed_clean_scan_drops_nothing(fake, tmp_path: Path) -> None:
    marked = f"ca_gone_test_db|{tmp_path / 'removed'}\n"
    result, calls = fake("clean", FAKE_MARKED=marked, FAKE_FAIL="regexp_replace")
    assert result.returncode == 1
    assert "could not list the marked databases; dropping nothing" in result.stderr
    assert _drops(calls) == []


def test_clean_keeps_a_set_a_run_is_using(fake, tmp_path: Path) -> None:
    result, calls = fake(
        "clean", FAKE_MARKED=f"ca_gone_test_db|{tmp_path / 'removed'}\n", FAKE_LOCK="f"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["kept ca_gone_test_db (a run is using it)"]
    assert _drops(calls) == []


@pytest.mark.parametrize(
    "env, message",
    [
        ({"TEST_DB_LOCAL_NAME": "x; drop"}, "must be lower-case letters"),
        ({"TEST_DB_LOCAL_NAME": "production"}, "contain _test_"),
        ({"TEST_DB_LOCAL_NAME": "Mixed_test_db"}, "must be lower-case letters"),
        # A base name shaped like a worker's would fall in another set.
        ({"TEST_DB_LOCAL_NAME": "overlap_test_db_gw0"}, "must not end in _gw<digits>"),
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
