"""Test an actual previous release process against the candidate schema.

Uses a disposable database, an archived Git tree and a persistent old-code
worker. Never migrates a configured live database. CI resolves --base from the
last published release reachable from the change's base commit.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]


def migrate(tree: Path, env: dict[str, str]) -> None:
    """Use each tree's own Alembic graph, just as its release does."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=tree, env=env, check=True
    )


async def check(base: str) -> None:
    """Hold the old process open through upgrade and an idempotent retry."""
    database = f"rolling_test_{uuid.uuid4().hex}"
    connection = {
        "host": os.environ.get("TEST_DB_HOST", "localhost"),
        "port": int(os.environ.get("TEST_DB_PORT", "5432")),
        "user": os.environ.get("TEST_DB_USER", "postgres"),
        "password": os.environ.get("TEST_DB_PASSWORD", "postgres"),
    }
    admin = await asyncpg.connect(**connection, database="postgres")
    worker = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        from urllib.parse import quote

        dsn = (
            f"postgresql://{quote(connection['user'], safe='')}:{quote(connection['password'], safe='')}"
            f"@{connection['host']}:{connection['port']}/{database}"
        )
        env = {**os.environ, "AGENT_DATABASE_URL": dsn, "PYTHONPATH": ""}
        env.pop("CODEX_VERSION", None)
        with tempfile.TemporaryDirectory(prefix="rolling-upgrade-") as directory:
            old = Path(directory) / "previous"
            old.mkdir()
            archive = Path(directory) / "previous.tar"
            subprocess.run(
                ["git", "archive", "--format=tar", f"--output={archive}", base],
                cwd=ROOT,
                check=True,
            )
            with tarfile.open(archive) as contents:
                contents.extractall(old, filter="data")
            await asyncio.to_thread(migrate, old, env)
            worker = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "tests/support/rolling_api_worker.py"),
                str(old),
                cwd=old,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            assert worker.stdout is not None and worker.stdin is not None
            assert await asyncio.wait_for(worker.stdout.readline(), 60) == b"READY\n", (
                "previous release failed before upgrade"
            )
            for stage in ("upgrade", "retry"):
                await asyncio.to_thread(migrate, ROOT, env)
                worker.stdin.write(b"CHECK\n")
                await worker.stdin.drain()
                assert await asyncio.wait_for(worker.stdout.readline(), 60) == b"PASS\n", (
                    f"previous API failed after {stage}"
                )
                print(
                    f"previous release {base}: business reads/writes pass after {stage}", flush=True
                )
            assert await asyncio.wait_for(worker.wait(), 10) == 0
    finally:
        if worker is not None and worker.returncode is None:
            worker.kill()
            await worker.wait()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    asyncio.run(check(parser.parse_args().base))
