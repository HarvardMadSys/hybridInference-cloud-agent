"""One DSN, two drivers — and the rewrite is for the *synchronous* one only.

`AGENT_DATABASE_URL` is a single setting, and two different things read it. The
service talks **asyncpg**, which wants a bare ``postgresql://``. Alembic runs
through SQLAlchemy's *synchronous* engine, and SQLAlchemy resolves a bare
``postgresql://`` to **psycopg2** — a package this project does not depend on,
because it ships psycopg 3.

So a correct-looking configuration fails at ``alembic upgrade head`` with
``ModuleNotFoundError: psycopg2``: a missing module, named after a driver
nobody chose, on a run whose only visible subject is a database. The compose
file's ``migrate`` service gates the API on that upgrade, so the whole stack
stops on it.

The alternative — asking operators to configure two spellings of the same
database — trades a startup failure for a class of silent mismatch where the
service and its migrations point at different servers. Rewriting here is the
narrower answer: one setting, and whichever driver is doing the reading gets a
DSN it can actually load.
"""

from __future__ import annotations

#: The sync driver this project actually depends on.
SYNC_DRIVER = "postgresql+psycopg"

#: Async drivers a caller may reasonably have written, and which SQLAlchemy's
#: synchronous engine cannot use at all — it raises rather than falling back.
_ASYNC_DRIVERS = ("postgresql+asyncpg://", "postgres+asyncpg://")

#: Bare forms. ``postgres://`` is the older spelling and still what several
#: hosting providers hand out.
_BARE = ("postgresql://", "postgres://")


def sync_sqlalchemy_url(dsn: str) -> str:
    """Return ``dsn`` with a driver SQLAlchemy's **synchronous** engine can load.

    Named for the direction it rewrites in. This is not a general-purpose DSN
    normaliser and must not be reused as one: it turns ``+asyncpg`` into
    ``+psycopg``, which is right for Alembic and exactly wrong for the service,
    whose whole database layer is asyncpg. A caller that wants "tidy up this
    DSN" and reaches for this gets a service that cannot connect.

    Args:
        dsn: Whatever ``AGENT_DATABASE_URL`` was set to.

    Returns:
        The same database, addressed through psycopg 3 when the given spelling
        would have resolved to a driver that is absent or asynchronous. A DSN
        that already names a usable sync driver is returned **unchanged** —
        rewriting one would silently override a deliberate choice, and
        double-rewriting one would corrupt it.
    """
    for prefix in _ASYNC_DRIVERS:
        if dsn.startswith(prefix):
            return SYNC_DRIVER + "://" + dsn[len(prefix) :]
    # Any other explicit driver is the caller's decision; leave it alone.
    if dsn.startswith(("postgresql+", "postgres+")):
        return dsn
    for prefix in _BARE:
        if dsn.startswith(prefix):
            return SYNC_DRIVER + "://" + dsn[len(prefix) :]
    return dsn
