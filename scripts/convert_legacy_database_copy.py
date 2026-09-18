"""Convert an isolated old-chain database copy without discarding its history.

Never run this against a live database. The dedicated DSN, exact database name,
name prefix, source revision and absence of other clients are independent guards.
The entire source schema survives as legacy_archive. Namespace adoption is a
separate operator action, after establishing the original issuer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "0025_retained_followup_gate"
BASELINE = "0001_schema_baseline"
ARCHIVE = "legacy_archive"
DSN_ENV = "LEGACY_COPY_DATABASE_URL"
ARCHIVE_ONLY = {"agent_job_artifacts", "agent_thread_sandboxes", "alembic_version"}


class ConversionError(Exception):
    """The copy cannot be converted under the supported contract."""


def table(schema: str, name: str) -> sql.Composed:
    """Quote both identifiers; never interpolate a database object's name."""
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def tables(conn: psycopg.Connection[Any], schema: str) -> list[str]:
    """Return creation order, which the baseline defines as dependency order."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.oid",
            (schema,),
        )
    ]


def columns(conn: psycopg.Connection[Any], schema: str, name: str) -> list[str]:
    """Read the ordered columns of one table."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, name),
        )
    ]


def count(conn: psycopg.Connection[Any], schema: str, name: str) -> int:
    """Count rows without returning their private contents."""
    return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(table(schema, name))).fetchone()[
        0
    ]


def fingerprints(conn: psycopg.Connection[Any], schema: str) -> dict[str, dict[str, Any]]:
    """Hash complete rows locally; reports contain hashes and counts only."""
    result = {}
    for name in tables(conn, schema):
        digest = hashlib.sha256()
        n = 0
        for (row,) in conn.execute(
            sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text").format(
                table(schema, name)
            )
        ):
            data = row.encode()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            n += 1
        result[name] = {"rows": n, "sha256": digest.hexdigest()}
    return result


def audit(conn: psycopg.Connection[Any], expected_database: str) -> dict[str, Any]:
    """Refuse a live/unknown source before any DDL or row write."""
    if not re.fullmatch(r"agent_legacy_copy_[a-z0-9_]+", expected_database):
        raise ConversionError("database must be an explicitly named agent_legacy_copy_* copy")
    actual = conn.execute("SELECT current_database()").fetchone()[0]
    if actual != expected_database:
        raise ConversionError("the DSN does not name the explicitly acknowledged copy")
    if conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
        "AND pid<>pg_backend_pid() AND backend_type='client backend'"
    ).fetchone()[0]:
        raise ConversionError("copy has other clients; disconnect them before conversion")
    if conn.execute("SELECT to_regnamespace(%s)", (ARCHIVE,)).fetchone()[0] is not None:
        raise ConversionError("legacy_archive already exists; do not re-convert this database")
    revision = conn.execute("SELECT version_num FROM public.alembic_version").fetchall()
    if revision != [(SOURCE_REVISION,)]:
        raise ConversionError(f"only the audited {SOURCE_REVISION} source is supported")
    if conn.execute(
        "SELECT count(*) FROM public.agent_jobs "
        "WHERE state NOT IN ('succeeded','failed','cancelled')"
    ).fetchone()[0]:
        raise ConversionError("source contains nonterminal jobs; a drain must precede the snapshot")
    if conn.execute("SELECT count(*) FROM public.agent_attempts WHERE status='running'").fetchone()[
        0
    ]:
        raise ConversionError("source contains running attempts")
    if conn.execute("SELECT count(*) FROM public.agent_runner_policy").fetchone()[0] != 1:
        raise ConversionError("source must have exactly one runner policy")
    if conn.execute(
        "SELECT count(*) FROM public.agent_session_forks "
        "WHERE state IN ('preparing','materialized')"
    ).fetchone()[0]:
        raise ConversionError("source contains unfinished session forks")
    if conn.execute(
        "SELECT count(*) FROM public.agent_sandbox_snapshots "
        "WHERE state IN ('pending','capturing','publishing')"
    ).fetchone()[0]:
        raise ConversionError("source contains unfinished publications")
    if conn.execute(
        "SELECT count(*) FROM public.agent_sandboxes WHERE mode<>'shared_live' "
        "OR active_operation_count<>0"
    ).fetchone()[0]:
        raise ConversionError("source has an unsupported sandbox mode or active operation")
    if conn.execute(
        "SELECT count(*) FROM public.agent_attempts WHERE continuity_mode "
        "NOT IN ('hot','warm_native','replay')"
    ).fetchone()[0]:
        raise ConversionError("source has an unknown continuity mode; do not erase its meaning")
    return {
        "database": actual,
        "source_revision": SOURCE_REVISION,
        "source": fingerprints(conn, "public"),
        "retired_replay_attempts": conn.execute(
            "SELECT count(*) FROM public.agent_attempts WHERE continuity_mode='replay'"
        ).fetchone()[0],
    }


def migration_sql(revisions: str) -> str:
    """Render the real migration graph; only its outer transactions are omitted."""
    env = {**os.environ, "AGENT_DATABASE_URL": "postgresql://unused/unused"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            revisions,
            "--sql",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ConversionError("the repository's offline migration SQL did not render")
    # Generated by Alembic, not operator-supplied SQL. Inner DO-block BEGINs are
    # indented and remain intact. One transaction must own archive + conversion.
    return "\n".join(
        line for line in result.stdout.splitlines() if line not in {"BEGIN;", "COMMIT;"}
    )


def projection(name: str, column: str) -> sql.Composable:
    """The four explicit transforms needed by the rewritten baseline."""
    if (name, column) == ("agent_jobs", "current_attempt_id"):
        return sql.SQL("NULL")
    if (name, column) == ("agent_attempts", "thread_id"):
        return sql.SQL("(SELECT j.thread_id FROM legacy_archive.agent_jobs j WHERE j.id=s.job_id)")
    if (name, column) == ("agent_thread_sandbox_memberships", "user_id"):
        return sql.SQL(
            "(SELECT t.user_id FROM legacy_archive.agent_threads t WHERE t.id=s.thread_id)"
        )
    if (name, column) == ("agent_attempts", "continuity_mode"):
        return sql.SQL("CASE WHEN s.continuity_mode='replay' THEN NULL ELSE s.continuity_mode END")
    return sql.SQL("s.{}").format(sql.Identifier(column))


def copy_baseline(conn: psycopg.Connection[Any]) -> None:
    """Populate the baseline with constraints enabled and without inventing owners."""
    source_tables = set(tables(conn, ARCHIVE))
    target_tables = set(tables(conn, "public"))
    unexpected = source_tables - target_tables - ARCHIVE_ONLY
    if unexpected:
        raise ConversionError(f"source tables need an explicit disposition: {sorted(unexpected)}")
    conn.execute("DELETE FROM public.agent_runner_policy")  # baseline's empty singleton only
    for name in tables(conn, "public"):
        if name == "alembic_version" or name not in source_tables:
            continue
        source_columns = columns(conn, ARCHIVE, name)
        derived = {
            "agent_attempts": {"thread_id"},
            "agent_thread_sandbox_memberships": {"user_id"},
        }.get(name, set())
        selected = [c for c in columns(conn, "public", name) if c in source_columns or c in derived]
        conn.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} s").format(
                table("public", name),
                sql.SQL(",").join(map(sql.Identifier, selected)),
                sql.SQL(",").join(projection(name, c) for c in selected),
                table(ARCHIVE, name),
            )
        )
    conn.execute(
        "UPDATE public.agent_jobs j SET current_attempt_id=s.current_attempt_id "
        "FROM legacy_archive.agent_jobs s WHERE s.id=j.id"
    )
    conn.execute(
        "INSERT INTO public.agent_snapshot_jobs(snapshot_id,job_id,created_at) "
        "SELECT s.id,v.value,s.created_at FROM legacy_archive.agent_sandbox_snapshots s "
        "CROSS JOIN LATERAL jsonb_array_elements_text(s.included_job_ids) v"
    )
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def restore_sequences(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Keep both actual IDs and previously allocated/deleted sequence high water."""
    restored = {}
    for name in tables(conn, "public"):
        for column in columns(conn, "public", name):
            target = conn.execute(
                "SELECT pg_get_serial_sequence(%s,%s)", (f"public.{name}", column)
            ).fetchone()[0]
            if target is None:
                continue
            maximum = conn.execute(
                sql.SQL("SELECT max({}) FROM {}").format(
                    sql.Identifier(column), table("public", name)
                )
            ).fetchone()[0]
            source_exists = name in tables(conn, ARCHIVE) and column in columns(conn, ARCHIVE, name)
            source = (
                conn.execute(
                    "SELECT pg_get_serial_sequence(%s,%s)", (f"{ARCHIVE}.{name}", column)
                ).fetchone()[0]
                if source_exists
                else None
            )
            source_value, source_called = (1, False)
            if source:
                # pg_get_serial_sequence returns a qualified identifier, never an SQL fragment.
                source_value, source_called = conn.execute(
                    sql.SQL("SELECT last_value,is_called FROM {}").format(
                        sql.Identifier(*source.split("."))
                    )
                ).fetchone()
            high = max(maximum or 1, source_value)
            called = maximum is not None or source_called
            conn.execute("SELECT setval(%s,%s,%s)", (target, high, called))
            restored[target] = high
    return restored


def verify_live(conn: psycopg.Connection[Any], *, baseline: bool = False) -> dict[str, Any]:
    """Compare retained public fields exactly, excluding declared transformations."""
    comparisons = {}
    current = set(tables(conn, "public"))
    for name in tables(conn, ARCHIVE):
        if name in ARCHIVE_ONLY or name not in current:
            continue
        shared = [c for c in columns(conn, ARCHIVE, name) if c in columns(conn, "public", name)]
        # The additive MCP migration moves legacy per-thread choices into bindings.
        if name == "agent_threads" and not baseline:
            shared = [c for c in shared if c != "mcp_servers"]
        source_expr = [
            projection(name, c)
            if (name, c) == ("agent_attempts", "continuity_mode")
            else sql.SQL("s.{}").format(sql.Identifier(c))
            for c in shared
        ]
        left = sql.SQL("SELECT {} FROM {} s").format(
            sql.SQL(",").join(source_expr), table(ARCHIVE, name)
        )
        right = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(",").join(map(sql.Identifier, shared)), table("public", name)
        )
        diff = conn.execute(
            sql.SQL(
                "SELECT count(*) FROM (({} EXCEPT ALL {}) UNION ALL ({} EXCEPT ALL {})) differences"
            ).format(left, right, right, left)
        ).fetchone()[0]
        if diff:
            raise ConversionError(f"{name}: retained fields differ in {diff} row(s)")
        comparisons[name] = count(conn, "public", name)
    if baseline:
        return {"compared_tables": comparisons}
    # Job config moved to threads. Every historical turn must still read the
    # same repo/setup/profile rather than silently inherit a different turn's.
    changed = conn.execute(
        "SELECT count(*) FROM legacy_archive.agent_jobs j JOIN public.agent_threads t "
        "ON t.id=j.thread_id WHERE (j.repo,j.setup_script,j.execution_profile) "
        "IS DISTINCT FROM (t.repo,t.setup_script,t.execution_profile)"
    ).fetchone()[0]
    if changed:
        raise ConversionError("per-turn configuration is not uniform within its thread")
    return {"compared_tables": comparisons, "historical_job_configuration": "identical"}


def convert(conn: psycopg.Connection[Any], expected_database: str) -> dict[str, Any]:
    """Run one atomic conversion; any copy/constraint/comparison failure rolls it back."""
    before = audit(conn, expected_database)
    baseline_sql = migration_sql(BASELINE)
    additive_sql = migration_sql(BASELINE + ":head")
    conn.execute("ALTER SCHEMA public RENAME TO legacy_archive")
    conn.execute("REVOKE ALL ON SCHEMA legacy_archive FROM PUBLIC")
    conn.execute("CREATE SCHEMA public")
    conn.execute("SET LOCAL search_path=public")
    conn.execute(baseline_sql)
    copy_baseline(conn)
    baseline_verification = verify_live(conn, baseline=True)
    sequences = restore_sequences(conn)
    conn.execute(additive_sql)
    verification = verify_live(conn)
    if fingerprints(conn, ARCHIVE) != before["source"]:
        raise ConversionError("the source archive changed during conversion")
    before.update(
        {
            "target_revision": conn.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()[0],
            "archive_unchanged": True,
            "sequences": sequences,
            "verification": verification,
            "baseline_verification": baseline_verification,
            "namespace": "unbound: verify original issuer and explicitly adopt before serving",
            "status": "converted copy; not authorized to serve traffic",
        }
    )
    return before


def main() -> int:
    """Audit by default; --apply changes only the explicitly acknowledged copy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get(DSN_ENV):
        parser.error(f"{DSN_ENV} is required; AGENT_DATABASE_URL is never used")
    os.umask(0o077)
    try:
        # Reserve a new writable report before opening the database. A bad
        # evidence path must not be discovered after a conversion commits.
        with args.report.open("x", encoding="utf-8") as report:
            report.write('{"status":"in progress; no committed result recorded"}\n')
            report.flush()
            os.fsync(report.fileno())
            with psycopg.connect(os.environ[DSN_ENV]) as conn:
                conn.execute("SET LOCAL timezone='UTC'")
                if args.apply:
                    result = convert(conn, args.database)
                else:
                    conn.execute("SET TRANSACTION READ ONLY")
                    result = audit(conn, args.database)
                    result["status"] = "audited only; no database changed"
            report.seek(0)
            report.write(json.dumps(result, indent=2) + "\n")
            report.truncate()
            report.flush()
            os.fsync(report.fileno())
    except psycopg.Error as error:
        # DETAIL can contain a full row, including credentials or user prompts.
        print(
            f"conversion refused: SQLSTATE={error.sqlstate}; "
            f"constraint={error.diag.constraint_name or 'none'}; transaction rolled back",
            file=sys.stderr,
        )
        return 1
    except ConversionError as error:
        print(f"conversion refused: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("report I/O failed; inspect the copy before retrying", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("database", "source_revision", "status")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
