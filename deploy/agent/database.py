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

Migration (``database_migrate``): refuse a non-empty, older or
extension-lacking target; fence every writer by making the source read-only and
ending its other sessions; dump, restore and compare every table's row count,
the migration revision and the identity namespace; mark maintenance over in the
target; switch. Each step is journaled with both connections sealed to this
agent's own key, so an interrupted migration resumes by itself when the agent
restarts (or with ``database resume``), and ``database unfence`` undoes the
fence before the switch. Once the control plane runs healthy on the target,
writes have resumed there and the source is never switched back to.
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

from deploy.agent.deployment_contract import seal, sealed_purpose
from deploy.agent.operations import OperationFailed, Outcome
from deploy.agent.store import parse_env, render_env

if TYPE_CHECKING:
    from pathlib import Path

    from deploy.agent.operations import Executor

_URL = "AGENT_DATABASE_URL"
_CP_SERVICES = ["control-plane", "mcp-relay"]
_HEX = re.compile(r"^[0-9a-f]+$")
MIGRATION_STEPS = ("preflight", "fence", "copy", "verify", "switch", "complete")
_REVISION = "SELECT version_num FROM alembic_version"
_NAMESPACE = "SELECT gateway_issuer FROM agent_identity_namespace"


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


def _connects(executor: Executor, dsn: str) -> bool:
    try:
        Database(executor, dsn).scalar("SELECT 1")
    except OperationFailed:
        return False
    return True


def _set_password(executor: Executor, current_dsn: str, new_dsn: str) -> None:
    """Set the new password over the current connection, unless a retry finds it set already.

    Once it succeeds the old password no longer works, so every later failure
    has to say so: nothing can restore the previous connection.
    """
    password = pg_env(new_dsn)["PGPASSWORD"]
    if len(password) < 16:
        raise OperationFailed("the new password must be at least 16 characters")
    if _connects(executor, new_dsn):
        return  # an earlier attempt set it and stopped before the switch
    verifier = scram_verifier(password)
    Database(executor, current_dsn).sql(
        "", stdin=f"ALTER ROLE CURRENT_USER WITH PASSWORD '{verifier}';\n".encode()
    )
    if not _connects(executor, new_dsn):
        raise OperationFailed(
            "the password was changed but the new connection does not work, and the old password "
            "no longer works; fix forward with a working connection for the new password"
        )


def database_switch(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], _record: str
) -> Outcome:
    """Write the new connection and restart; with a password rotation, set it first."""
    new = _new_dsn(executor, sealed)
    current_env = parse_env(executor.store.current_env())
    rotated = bool(params.get("rotate_password"))
    if rotated and params.get("leader"):
        _set_password(executor, current_env.get(_URL, ""), new)
    elif not _connects(executor, new):
        raise OperationFailed("the new connection does not reach the database")
    changes = {_URL: new}
    if rotated and "AGENT_POSTGRES_PASSWORD" in current_env:
        # The bundled PostgreSQL's own record of its password stays true.
        changes["AGENT_POSTGRES_PASSWORD"] = pg_env(new)["PGPASSWORD"]
    previous_rev = executor.store.adopt_existing()
    rev = executor.store.stage_version(render_env(executor.store.current_env(), changes))
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
    executor.progress()


def _note(executor: Executor, record: dict[str, Any], **facts: Any) -> None:
    """Journal a fact before the step it guards, so a crash cannot lose it."""
    record["migration"].update(facts)
    executor.store.save_operation(record)


def _seal_locally(executor: Executor, value: str) -> dict[str, Any]:
    identity = executor.store.identity()
    return seal(identity["public_key"], value, sealed_purpose(executor.agent_id, _URL))


def _connections(
    executor: Executor, record: dict[str, Any], sealed: dict[str, Any]
) -> tuple[str, str]:
    """Source and target URLs, journaled sealed to this agent's own key.

    Ciphertext only this machine's ``key.pem`` opens: a resume needs no one to
    enter the target again, and the journal never holds a password. The source
    is pinned here because ``.env`` names the target once the switch starts.
    """
    migration = record["migration"]
    if "sealed_target" not in migration:
        _note(executor, record, sealed_target=sealed[_URL])
    target = executor.open_values({_URL: migration["sealed_target"]})[_URL]
    if "sealed_source" not in migration:
        source = parse_env(executor.store.current_env()).get(_URL, "")
        if not source:
            raise OperationFailed("this machine's .env has no AGENT_DATABASE_URL")
        _note(executor, record, sealed_source=_seal_locally(executor, source))
    return executor.open_values({_URL: migration["sealed_source"]})[_URL], target


def _preflight(source: Database, target: Database) -> None:
    """Refuse a target that holds anything, is the source, or cannot take this database."""
    if target.scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") != "0":
        raise OperationFailed("the target database is not empty; it is never overwritten")
    same_cluster = source.identity() == target.identity()
    if same_cluster and (
        source.env["PGHOST"] == target.env["PGHOST"] or source.identity().get("system_identifier")
    ):
        raise OperationFailed("the target is the source database")
    version = "SELECT current_setting('server_version_num')::int / 10000"
    if int(target.scalar(version) or 0) < int(source.scalar(version) or 0):
        raise OperationFailed("the target's PostgreSQL is older than the source's")
    installed = set(source.sql("SELECT extname FROM pg_extension"))
    available = set(target.sql("SELECT name FROM pg_available_extensions"))
    if installed - available:
        raise OperationFailed(
            "the target lacks extensions the source uses: "
            + ", ".join(sorted(installed - available))
        )


def _fence(source: Database) -> None:
    """Make the source read-only and end every session a role of ours can end."""
    name = source.env["PGDATABASE"].replace('"', '""')
    source.sql(f'ALTER DATABASE "{name}" SET default_transaction_read_only = on')
    source.sql(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend' AND pg_has_role(usesysid, 'MEMBER')"
    )


def _copy(executor: Executor, source: Database, target: Database, dump: Path) -> None:
    dump.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(dump, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    dumped = executor.runner.stream(
        [*source.tools["pg_dump"], "-Fc", "--no-owner", "--no-privileges"],
        cwd=executor.store.deploy_root,
        env=source.env,
        stdout_path=dump,
    )
    if not dumped.ok:
        raise OperationFailed(f"pg_dump failed (exit {dumped.code})")
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
        raise OperationFailed(f"pg_restore failed (exit {restored.code})")


def _verify(source: Database, target: Database) -> int:
    before, after = source.table_counts(), target.table_counts()
    if before != after:
        differing = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
        raise OperationFailed("row counts differ after the copy: " + ", ".join(differing[:10]))
    if source.scalar(_REVISION) != target.scalar(_REVISION):
        raise OperationFailed("the migration revision differs after the copy")
    if _has_table(source, "agent_identity_namespace") and (
        source.scalar(_NAMESPACE) != target.scalar(_NAMESPACE)
    ):
        raise OperationFailed("the identity namespace differs after the copy")
    return len(after)


def _clear_partial_copy(target: Database) -> None:
    """Empty a target this migration found empty and has only part-filled."""
    target.sql("", stdin=b"DROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n")


def _abort(
    executor: Executor,
    record: dict[str, Any],
    source: Database,
    target: Database,
    dump: Path,
    reason: OperationFailed,
) -> OperationFailed:
    """Put the source back before anything switched; the error says what state it left."""
    migration = record["migration"]
    notes = [str(reason)]
    if migration.get("fencing"):
        try:
            _unfence(source)
            _end_maintenance(source, "aborted", "the move failed; the source is writable again")
            notes.append("the source database is writable again")
        except OperationFailed:
            notes.append(
                "the source database is STILL READ-ONLY: run `database unfence --operation-id "
                f"{record['id']}` on this machine"
            )
    if migration.get("copying"):
        try:
            _clear_partial_copy(target)
            notes.append("the partial copy was removed from the target")
        except OperationFailed:
            notes.append("the target still holds a partial copy; empty it before retrying")
    dump.unlink(missing_ok=True)
    for name in ("fencing", "copying"):
        migration.pop(name, None)
    migration["done"] = []
    executor.store.save_operation(record)
    return OperationFailed("; ".join(notes))


def database_migrate(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], record_id: str
) -> Outcome:
    """Copy the database to an empty target and switch; resumable from the journal."""
    record = _journal(executor, record_id)
    source_dsn, target_dsn = _connections(executor, record, sealed)
    migration = record["migration"]
    done = set(migration["done"])
    source, target = Database(executor, source_dsn), Database(executor, target_dsn)
    dump = executor.store.root / "migrations" / f"{params['operation_id']}.dump"
    try:
        if "preflight" not in done:
            _preflight(source, target)
            _mark(executor, record, "preflight")
        if "fence" not in done:
            _note(executor, record, fencing=True)
            _fence(source)
            _mark(executor, record, "fence")
        if "copy" not in done:
            if migration.get("copying"):
                _clear_partial_copy(target)  # a crash left this migration's own part-copy
            _note(executor, record, copying=True)
            _copy(executor, source, target, dump)
            _mark(executor, record, "copy")
        if "verify" not in done:
            _mark(executor, record, "verify", tables=_verify(source, target))
        if "switch" not in done:
            # The copy says migrating; the target must not refuse writes once it serves.
            _end_maintenance(target, "completed", "moved to this database")
    except OperationFailed as exc:
        raise _abort(executor, record, source, target, dump, exc) from exc
    if "switch" not in done:
        if "previous_rev" not in migration:
            _note(executor, record, previous_rev=executor.store.adopt_existing())
        _note(executor, record, switching=True)
        rev = executor.store.stage_version(
            render_env(executor.store.current_env(), {_URL: target_dsn})
        )
        executor.store.activate_version(rev)
        healthy, _states = executor._restart_and_check(_CP_SERVICES)
        if not healthy:
            # Writes have not resumed anywhere: go back to the source and reopen it.
            executor.store.activate_version(int(migration["previous_rev"]))
            _unfence(source)
            _end_maintenance(source, "aborted", "the control plane did not come up on the target")
            executor._restart_and_check(_CP_SERVICES)
            migration.pop("switching", None)
            migration.pop("fencing", None)
            migration.pop("copying", None)
            migration["done"] = []
            dump.unlink(missing_ok=True)
            executor.store.save_operation(record)
            raise OperationFailed(
                "the control plane did not come up on the target; switched back to the writable "
                "source. The target keeps its copy: empty it before moving again"
            )
        _mark(executor, record, "switch", config_rev=rev)
    dump.unlink(missing_ok=True)
    _mark(executor, record, "complete")
    return Outcome(
        "succeeded",
        "the control plane runs on the target database; the source stays read-only and is not switched back to",
        {"tables": migration.get("tables"), "config_rev": migration.get("config_rev")},
    )


def _unfence(source: Database) -> None:
    """Undo the fence, from a session that must first leave the read-only default it fenced."""
    name = source.env["PGDATABASE"].replace('"', '""')
    source.sql(
        "",
        stdin=(
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE;\n"
            f'ALTER DATABASE "{name}" RESET default_transaction_read_only;\n'
        ).encode(),
    )


def _has_table(db: Database, name: str) -> bool:
    return db.scalar(f"SELECT to_regclass('public.{name}') IS NOT NULL") == "t"


def _end_maintenance(db: Database, state: str, detail: str) -> None:
    """Close the maintenance state in one database; states and details are this module's."""
    if _has_table(db, "agent_maintenance"):
        db.sql(
            f"UPDATE agent_maintenance SET state = '{state}', detail = '{detail}', "
            "updated_at = NOW() WHERE state = 'migrating'"
        )


def recover(executor: Executor, record: dict[str, Any]) -> Outcome | None:
    """Continue a migration a restart interrupted; ``None`` if this record is not one."""
    migration = record.get("migration") or {}
    if record.get("operation") != "database_migrate" or "sealed_target" not in migration:
        return None
    return executor.execute(
        "database_migrate",
        {"operation_id": record["params"]["operation_id"]},
        {_URL: migration["sealed_target"]},
        intent_id=record.get("intent_id"),
        record_id=record["id"],
        origin=str(record.get("origin") or "local"),
    )


def _public_journal(record: dict[str, Any]) -> dict[str, Any]:
    migration = {
        k: v
        for k, v in (record.get("migration") or {}).items()
        if k not in {"sealed_target", "sealed_source"}
    }
    return {"id": record["id"], "state": record.get("state"), "migration": migration}


def local_command(executor: Executor, args: Any) -> Outcome:
    """``database status|unfence|resume --operation-id``: recovery without the control plane."""
    record = executor.store.operation(args.operation_id) if args.operation_id else None
    if args.database_command == "status":
        return Outcome(
            "succeeded",
            "migration journal",
            {
                "migrations": [
                    _public_journal(r)
                    for r in executor.store.operations()
                    if r.get("operation") == "database_migrate"
                ]
            },
        )
    if record is None or record.get("operation") != "database_migrate":
        return Outcome("rejected", "name a database_migrate operation with --operation-id")
    migration = record.get("migration") or {}
    if args.database_command == "unfence":
        if "sealed_source" not in migration:
            return Outcome("rejected", "this migration never reached the source")
        if "switch" in migration.get("done", []) or migration.get("switching"):
            return Outcome(
                "rejected",
                "the control plane already switched to the target; the source is not reopened",
            )
        source = Database(executor, executor.open_values({_URL: migration["sealed_source"]})[_URL])
        _unfence(source)
        _end_maintenance(source, "aborted", "unfenced on the machine")
        migration.pop("fencing", None)
        migration["done"] = [s for s in migration.get("done", []) if s == "preflight"]
        record.update(state="failed", detail="unfenced on the machine; the source is writable")
        executor.store.save_operation(record)
        return Outcome("succeeded", "the source database is writable again")
    outcome = recover(executor, record)
    if outcome is None:
        return Outcome("rejected", "this migration has no journaled target; start it again")
    return outcome


def register(executor: Executor) -> None:
    """Add the database handlers to ``executor``."""
    executor.handlers["database_check"] = database_check
    executor.handlers["database_switch"] = database_switch
    executor.handlers["database_migrate"] = database_migrate


__all__ = ["MIGRATION_STEPS", "Database", "pg_env", "recover", "register", "scram_verifier"]
