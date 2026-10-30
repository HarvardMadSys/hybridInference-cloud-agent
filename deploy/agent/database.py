"""Database identity, connection switch and migration, run on the control-plane machine.

Every database command goes through the PostgreSQL client programs the local
policy names (``database_tools``: ``psql``, ``pg_dump``, ``pg_restore``, each an
argv prefix, often ``docker run --rm -i --network <net> -e PGHOST ... postgres:15
psql``). Connection details travel only in ``PG*`` environment variables,
never in argv; a new password reaches the server as a SCRAM verifier on
stdin, so not even a statement log holds it.

Same database (``database_check``): the candidate connection must read the
token the control plane wrote over its own connection, and write a reply the
control plane then reads back. A clone cannot pass.

Migration (``database_migrate``): refuse a non-empty target; fence every
writer by making the source read-only and ending its other sessions; dump,
restore and compare every table's row count and the migration revision;
switch. Each step is journaled, so ``database resume`` continues after a
crash and ``database unfence`` undoes the fence before the switch. Once the
control plane runs healthy on the target, writes have resumed there and the
source is never switched back to.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from deploy.agent.operations import OperationFailed, Outcome
from deploy.agent.store import parse_env, render_env

if TYPE_CHECKING:
    from deploy.agent.operations import Executor

_URL = "AGENT_DATABASE_URL"
_CP_SERVICES = ["control-plane", "mcp-relay"]
_HEX = re.compile(r"^[0-9a-f]+$")
MIGRATION_STEPS = ("preflight", "fence", "copy", "verify", "switch", "complete")


def pg_env(dsn: str) -> dict[str, str]:
    """``PG*`` variables for a ``postgresql://`` URL; the URL itself never reaches argv."""
    parts = urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))
    if parts.scheme not in {"postgresql", "postgres"} or not parts.hostname:
        raise OperationFailed("the database URL is not a postgresql:// URL")
    env = {
        "PGHOST": parts.hostname,
        "PGPORT": str(parts.port or 5432),
        "PGDATABASE": unquote(parts.path.lstrip("/")) or "postgres",
        "PGUSER": unquote(parts.username or ""),
        "PGPASSWORD": unquote(parts.password or ""),
    }
    query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"]
    return env


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """A PostgreSQL SCRAM-SHA-256 verifier, so the plaintext never reaches the server."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


class Database:
    """The policy's client programs against one connection."""

    def __init__(self, executor: Executor, dsn: str) -> None:
        """Bind to a connection URL."""
        tools = executor.store.policy().get("database_tools") or {}
        if not all(
            isinstance(tools.get(n), list) and tools.get(n)
            for n in ("psql", "pg_dump", "pg_restore")
        ):
            raise OperationFailed(
                "this machine's policy names no PostgreSQL client programs (database_tools)"
            )
        self.tools = tools
        self.executor = executor
        self.env = pg_env(dsn)

    def _runner(self) -> Any:
        return self.executor.runner

    def sql(self, statement: str, *, stdin: bytes | None = None) -> list[str]:
        """Run one statement (or stdin) and return its unaligned output rows."""
        argv = [*self.tools["psql"], "-X", "-At", "-v", "ON_ERROR_STOP=1"]
        runner = self._runner()
        if stdin is not None:
            result = runner.stream(
                argv, cwd=self.executor.store.deploy_root, env=self.env, stdin_data=stdin
            )
        else:
            result = runner.run(
                [*argv, "-c", statement], cwd=self.executor.store.deploy_root, env=self.env
            )
        if not result.ok:
            raise OperationFailed(f"a database statement failed (exit {result.code})")
        return [line for line in result.output.splitlines() if line.strip()]

    def scalar(self, statement: str) -> str:
        rows = self.sql(statement)
        return rows[0] if rows else ""

    def identity(self) -> dict[str, str]:
        """What this connection says the database is; the live probe is the proof."""
        datid = self.scalar("SELECT oid FROM pg_database WHERE datname = current_database()")
        try:
            system = self.scalar("SELECT system_identifier FROM pg_control_system()")
        except OperationFailed:
            system = ""
        return {"datid": datid, "system_identifier": system}

    def table_counts(self) -> dict[str, int]:
        """Every public table and its row count."""
        tables = self.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        counts = {}
        for table in tables:
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
                raise OperationFailed("a table name is not a plain identifier")
            counts[table] = int(self.scalar(f'SELECT count(*) FROM public."{table}"') or 0)
        return counts


def _new_dsn(executor: Executor, sealed: dict[str, Any]) -> str:
    return executor.open_values({_URL: sealed[_URL]})[_URL]


def database_check(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], _record: str
) -> Outcome:
    """Read the control plane's probe token over the candidate connection and reply over it."""
    probe_id, token = params["probe_id"], params["probe_token"]
    if not (_HEX.fullmatch(probe_id) and _HEX.fullmatch(token)):
        raise OperationFailed("the probe is malformed")
    candidate = _new_dsn(executor, sealed)
    if params.get("rotate_password"):
        # The new password is not set yet; prove identity with the current one.
        current = pg_env(parse_env(executor.store.current_env()).get(_URL, ""))
        db = Database(executor, candidate)
        db.env["PGPASSWORD"] = current["PGPASSWORD"]
    else:
        db = Database(executor, candidate)
    seen = db.scalar(f"SELECT token FROM agent_database_probes WHERE probe_id = '{probe_id}'")
    if seen != token:
        raise OperationFailed(
            "the candidate connection does not see the control plane's probe: not the same database"
        )
    reply = secrets.token_hex(32)
    agent = executor.agent_id
    if not _HEX.fullmatch(agent) and agent != "local":
        raise OperationFailed("the agent id is malformed")
    db.sql(
        "UPDATE agent_database_probes SET responses = responses || "
        f"jsonb_build_object('{agent}', '{reply}') WHERE probe_id = '{probe_id}'"
    )
    return Outcome(
        "succeeded",
        "the candidate connection reached the same database",
        {"reply": reply, **db.identity()},
    )


def database_switch(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], _record: str
) -> Outcome:
    """Write the new connection and restart; with a password rotation, set it first."""
    new = _new_dsn(executor, sealed)
    rotated = False
    if params.get("rotate_password") and params.get("leader"):
        current = parse_env(executor.store.current_env()).get(_URL, "")
        password = pg_env(new)["PGPASSWORD"]
        if len(password) < 16:
            raise OperationFailed("the new password must be at least 16 characters")
        verifier = scram_verifier(password)
        Database(executor, current).sql(
            "", stdin=f"ALTER ROLE CURRENT_USER WITH PASSWORD '{verifier}';\n".encode()
        )
        rotated = True
    Database(executor, new).scalar("SELECT 1")
    previous_rev = executor.store.adopt_existing()
    rev = executor.store.stage_version(render_env(executor.store.current_env(), {_URL: new}))
    executor.store.activate_version(rev)
    healthy, states = executor._restart_and_check(_CP_SERVICES)
    if healthy:
        return Outcome(
            "succeeded",
            f"connected with configuration version {rev}",
            {"config_rev": rev, "password_rotated": rotated},
        )
    if rotated:
        # The old password no longer works: going back would be a second outage.
        return Outcome(
            "failed",
            "the services did not come back on the new connection, and the old password no longer "
            "works, so the previous version was not restored; fix forward with the new connection",
            {"config_rev": rev, "password_rotated": True, "services": states},
        )
    executor.store.activate_version(previous_rev)
    executor._restart_and_check(_CP_SERVICES)
    return Outcome(
        "failed",
        f"the services did not come back on the new connection; restored version {previous_rev}",
        {"config_rev": previous_rev},
    )


def _journal(executor: Executor, record_id: str) -> dict[str, Any]:
    record = executor.store.operation(record_id) or {"id": record_id}
    record.setdefault("migration", {"done": []})
    return record


def _mark(executor: Executor, record: dict[str, Any], step: str, **facts: Any) -> None:
    record["migration"]["done"].append(step)
    record["migration"].update(facts)
    executor.store.save_operation(record)


def database_migrate(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], record_id: str
) -> Outcome:
    """Copy the database to an empty target and switch; resumable from the journal."""
    target_dsn = _new_dsn(executor, sealed)
    source_dsn = parse_env(executor.store.current_env()).get(_URL, "")
    if not source_dsn:
        raise OperationFailed("this machine's .env has no AGENT_DATABASE_URL")
    record = _journal(executor, record_id)
    done = set(record["migration"]["done"])
    source, target = Database(executor, source_dsn), Database(executor, target_dsn)
    dump = executor.store.root / "migrations" / f"{params['operation_id']}.dump"
    try:
        if "preflight" not in done:
            if target.scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") != "0":
                raise OperationFailed("the target database is not empty; it is never overwritten")
            if (
                source.identity() == target.identity()
                and source.env["PGHOST"] == target.env["PGHOST"]
            ):
                raise OperationFailed("the target is the source database")
            _mark(executor, record, "preflight")
        if "fence" not in done:
            name = source.env["PGDATABASE"].replace('"', '""')
            source.sql(f'ALTER DATABASE "{name}" SET default_transaction_read_only = on')
            source.sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            _mark(executor, record, "fence")
        if "copy" not in done:
            dump.parent.mkdir(parents=True, exist_ok=True)
            dumped = executor.runner.stream(
                [*source.tools["pg_dump"], "-Fc", "--no-owner", "--no-privileges"],
                cwd=executor.store.deploy_root,
                env=source.env,
                stdout_path=dump,
            )
            if not dumped.ok:
                raise OperationFailed(f"pg_dump failed (exit {dumped.code})")
            os.chmod(dump, 0o600)
            restored = executor.runner.stream(
                [
                    *target.tools["pg_restore"],
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "-d",
                    target.env["PGDATABASE"],
                ],
                cwd=executor.store.deploy_root,
                env=target.env,
                stdin_path=dump,
            )
            if not restored.ok:
                raise OperationFailed(
                    f"pg_restore failed (exit {restored.code}); the target holds a partial copy"
                )
            _mark(executor, record, "copy")
        if "verify" not in done:
            before, after = source.table_counts(), target.table_counts()
            if before != after:
                differing = sorted(
                    t for t in set(before) | set(after) if before.get(t) != after.get(t)
                )
                raise OperationFailed(
                    "row counts differ after the copy: " + ", ".join(differing[:10])
                )
            revision = "SELECT version_num FROM alembic_version"
            if source.scalar(revision) != target.scalar(revision):
                raise OperationFailed("the migration revision differs after the copy")
            _mark(executor, record, "verify", tables=len(after))
    except OperationFailed as exc:
        if "fence" in set(record["migration"]["done"]) and "switch" not in set(
            record["migration"]["done"]
        ):
            _unfence(source)
            record["migration"]["unfenced"] = True
            executor.store.save_operation(record)
            raise OperationFailed(f"{exc}; the source database is writable again") from exc
        raise
    if "switch" not in done:
        previous_rev = executor.store.adopt_existing()
        rev = executor.store.stage_version(
            render_env(executor.store.current_env(), {_URL: target_dsn})
        )
        executor.store.activate_version(rev)
        healthy, _states = executor._restart_and_check(_CP_SERVICES)
        if not healthy:
            # Nothing has written to the target yet: go back to the source and reopen it.
            executor.store.activate_version(previous_rev)
            _unfence(source)
            executor._restart_and_check(_CP_SERVICES)
            raise OperationFailed(
                "the control plane did not come up on the target; switched back to the writable source"
            )
        _mark(executor, record, "switch", config_rev=rev)
    if dump.exists():
        dump.unlink()
    _mark(executor, record, "complete")
    return Outcome(
        "succeeded",
        "the control plane runs on the target database; the source stays read-only and is not switched back to",
        {
            "tables": record["migration"].get("tables"),
            "config_rev": record["migration"].get("config_rev"),
        },
    )


def _unfence(source: Database) -> None:
    name = source.env["PGDATABASE"].replace('"', '""')
    source.sql(f'ALTER DATABASE "{name}" RESET default_transaction_read_only')


def local_command(executor: Executor, args: Any) -> Outcome:
    """``database status|unfence|resume --operation-id``: recovery without the control plane."""
    record = executor.store.operation(args.operation_id) if args.operation_id else None
    if args.database_command == "status":
        migrations = [
            r for r in executor.store.operations() if r.get("operation") == "database_migrate"
        ]
        return Outcome(
            "succeeded",
            "migration journal",
            {
                "migrations": [
                    {"id": r["id"], "state": r.get("state"), "migration": r.get("migration")}
                    for r in migrations
                ]
            },
        )
    if record is None or record.get("operation") != "database_migrate":
        return Outcome("rejected", "name a database_migrate operation with --operation-id")
    done = set((record.get("migration") or {}).get("done") or [])
    if args.database_command == "unfence":
        if "switch" in done:
            return Outcome(
                "rejected",
                "the control plane already switched to the target; the source is not reopened",
            )
        _unfence(Database(executor, parse_env(executor.store.current_env()).get(_URL, "")))
        return Outcome("succeeded", "the source database is writable again")
    import getpass

    from deploy.agent.deployment_contract import seal, sealed_purpose

    target = getpass.getpass("target AGENT_DATABASE_URL: ")
    box = seal(
        executor.store.identity()["public_key"], target, sealed_purpose(executor.agent_id, _URL)
    )
    return executor.execute(
        "database_migrate",
        {"operation_id": record["params"]["operation_id"]},
        {_URL: box},
        intent_id=record["id"],
    )


def register(executor: Executor) -> None:
    """Add the database handlers to ``executor``."""
    executor.handlers["database_check"] = database_check
    executor.handlers["database_switch"] = database_switch
    executor.handlers["database_migrate"] = database_migrate


__all__ = ["MIGRATION_STEPS", "Database", "pg_env", "register", "scram_verifier"]
