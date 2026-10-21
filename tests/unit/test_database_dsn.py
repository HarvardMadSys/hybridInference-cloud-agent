"""One `AGENT_DATABASE_URL`, read by two drivers.

The service talks asyncpg and wants a bare `postgresql://`. Alembic goes
through SQLAlchemy's synchronous engine, which resolves a bare `postgresql://`
to **psycopg2** — absent here, because this project ships psycopg 3.

So the compose file's own DSN, written the way the service needs it, failed at
`alembic upgrade head` with `ModuleNotFoundError: psycopg2`: a missing module
named after a driver nobody chose, on a run whose visible subject is a
database. And `migrate` gates the API, so the whole stack stopped there.

The tests below are about the shape of the rewrite rather than its mechanics,
because getting it slightly wrong is worse than not having it: a
double-rewritten DSN names a driver that does not exist, and an over-eager one
overrides a driver somebody chose on purpose.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from cloud_agent.storage.dsn import SYNC_DRIVER, sync_sqlalchemy_url

REPO = Path(__file__).resolve().parents[2]

_TAIL = "user:pw@db.example:5432/cloudagent"


@pytest.mark.parametrize(
    "written",
    [
        "postgresql://",
        # The older spelling, and still what several hosting providers hand out.
        "postgres://",
    ],
)
def test_a_bare_dsn_reaches_the_driver_this_project_ships(written: str) -> None:
    """The case the compose file actually produces."""
    assert sync_sqlalchemy_url(written + _TAIL) == f"{SYNC_DRIVER}://{_TAIL}"


def test_a_dsn_that_already_names_psycopg_is_untouched() -> None:
    """Rewriting it again would produce `postgresql+psycopg+psycopg://`.

    Worth its own case because the obvious implementation — a substring
    replace — passes the two above and fails this one.
    """
    already = f"{SYNC_DRIVER}://{_TAIL}"

    assert sync_sqlalchemy_url(already) == already


@pytest.mark.parametrize("prefix", ["postgresql+asyncpg://", "postgres+asyncpg://"])
def test_an_async_driver_is_rewritten_rather_than_passed_through(prefix: str) -> None:
    """SQLAlchemy's *sync* engine cannot use asyncpg at all — it raises rather
    than falling back, so passing it through leaves the same broken state the
    rewrite exists to fix, one error message further along.

    Not in the reported cases; found while writing them. An operator who set
    `+asyncpg` was being explicit about the driver the *service* uses, which is
    exactly the confusion this whole function exists for.
    """
    assert sync_sqlalchemy_url(prefix + _TAIL) == f"{SYNC_DRIVER}://{_TAIL}"


def test_another_sync_driver_is_left_alone() -> None:
    """`+psycopg2` is a deliberate choice by somebody who installed it.

    Fail-safe direction: this function exists to fix a DSN that names *no*
    driver, not to overrule one that does. Silently redirecting an explicit
    driver is how a rewrite becomes its own bug report.
    """
    explicit = f"postgresql+psycopg2://{_TAIL}"

    assert sync_sqlalchemy_url(explicit) == explicit


def test_a_non_postgres_dsn_is_not_mangled() -> None:
    """Nothing here is Postgres-only by accident; it is by prefix."""
    assert sync_sqlalchemy_url("sqlite:///local.db") == "sqlite:///local.db"


def test_alembic_env_normalises_what_the_environment_gives_it(monkeypatch) -> None:
    """**The wiring, not just the helper.**

    A correct function nothing calls is the shape of the original bug. This
    drives `migrations/env.py`'s own resolver with a bare DSN and asserts what
    Alembic would actually be handed.
    """
    import importlib.util
    from pathlib import Path
    from unittest.mock import MagicMock

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("_agent_alembic_env", repo / "migrations/env.py")
    assert spec and spec.loader

    # `env.py` runs migrations as an import side effect under Alembic. Stub the
    # context so importing it here resolves the URL and does nothing else.
    context = MagicMock()
    context.is_offline_mode.return_value = True
    monkeypatch.setitem(__import__("sys").modules, "alembic", MagicMock(context=context))
    monkeypatch.setenv("AGENT_DATABASE_URL", f"postgresql://{_TAIL}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._database_url() == f"{SYNC_DRIVER}://{_TAIL}"


def test_the_driver_the_rewrite_names_is_a_runtime_dependency() -> None:
    """The rewrite is only half of it; the package has to ship.

    `sync_sqlalchemy_url` exists because a bare `postgresql://` resolves to
    psycopg2, which this project does not depend on. It rewrites to psycopg 3
    — and psycopg 3 then sat in the **dev** dependency group, so it was
    present for every test here and absent from the image the `migrate`
    service runs. The same failure as the one this module was written for,
    one layer down: `ModuleNotFoundError: No module named 'psycopg'`, and no
    table was ever created.

    A runtime check cannot catch this, because the test environment installs
    the dev group. So this reads the manifest instead: whatever driver the
    rewrite names must be declared where the deployed image will get it.
    """
    manifest = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = manifest["project"]["dependencies"]

    driver = SYNC_DRIVER.partition("+")[2]
    assert driver, f"{SYNC_DRIVER!r} names no driver after the `+`"

    declared = {re.split(r"[<>=!\[ ]", spec, maxsplit=1)[0].lower() for spec in runtime}
    assert driver.lower() in declared, (
        f"migrations run through {SYNC_DRIVER}, but {driver!r} is not a "
        f"runtime dependency — it is installed for tests and missing from the "
        f"image `migrate` runs. Declared: {sorted(declared)}"
    )
