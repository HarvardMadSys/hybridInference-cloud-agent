"""Run this version's host code against the previous release's control plane, and back.

Layer A of the mixed-version test (docs/agents/specs/2026-11-05-host-protocol-v2.zh-CN.md
§3.2): the host protocol on the wire, with no images. The previous release is
``git archive``d and given a virtual environment of its own from its own lock
(``uv sync --frozen``), so its code runs on its own dependencies.

Four pairings, each on a random database migrated by its control plane's own
Alembic graph:

* this host and the previous control plane (direction 1), and the previous
  release with itself, the baseline direction 1 must reproduce;
* the previous host and this control plane (direction 2), and this checkout
  with itself, direction 2's baseline.

Each starts the control plane's real ``create_app()`` on loopback
(``tests/support/mixed_protocol_control.py``) and has the host's own
enrollment, claim and attempt-scoped client walk the §3.4 steps a sandbox is
not needed for (``tests/support/mixed_protocol_host.py``), against a scripted
sandbox manager. A pairing passes when every call answered as the script
expects, neither side's models dropped a field the other sent or refused one
they require, and the control plane's database ends the way the same-version
baseline left it.

Four more pairings cover the control plane's direct calls to a host's sandbox
manager (``tests/support/mixed_protocol_manager.py``): this release's
``SandboxManagerClient`` against the previous release's sandbox manager app,
the previous client against this one, and each with itself. Behind each app is
the fake supervisor from that release's own tests. A pairing passes when every
call was accepted by both ends, the manager's request models dropped nothing,
and no answer lacks a key the client's own release's manager sends.

Never touches a configured database: it creates and drops its own on the server
the ``TEST_DB_*`` variables name. CI resolves ``--base`` from the last
published release reachable from the change's base commit, as for
``check_rolling_upgrade.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tests" / "support"
#: Generous next to the ~10s a pairing takes, so only a hang reaches it.
PAIRING_TIMEOUT_S = 240.0


#: Fields a receiver is known to drop, by receiver, route and field, each with
#: the marker that removes the sender's side. Nothing else may be dropped.
ACCEPTED_DROPS = {
    # COMPAT(remove-after=2026-11-30): sandbox_manager_client.py stops sending
    # lease_generation with terminal suspend and resume; delete these then.
    ("sandbox manager", "/workspaces/{workspace_id}/terminals/suspend", "lease_generation"),
    ("sandbox manager", "/workspaces/{workspace_id}/terminals/resume", "lease_generation"),
}


def _dropped(receiver: str, route: str, fields: list[str]) -> list[str]:
    return [field for field in fields if (receiver, route, field) not in ACCEPTED_DROPS]


@dataclass(frozen=True)
class Release:
    """One side of a pairing: a checkout and the interpreter of its own environment."""

    label: str
    tree: Path
    python: str


def _connection() -> dict[str, Any]:
    return {
        "host": os.environ.get("TEST_DB_HOST", "localhost"),
        "port": int(os.environ.get("TEST_DB_PORT", "5432")),
        "user": os.environ.get("TEST_DB_USER", "postgres"),
        "password": os.environ.get("TEST_DB_PASSWORD", "postgres"),
    }


def _dsn(database: str) -> str:
    c = _connection()
    return (
        f"postgresql://{quote(c['user'], safe='')}:{quote(c['password'], safe='')}"
        f"@{c['host']}:{c['port']}/{database}"
    )


def previous_release(base: str, directory: Path) -> Release:
    """Archive ``base`` and give it an environment from its own lock file."""
    tree = directory / "previous"
    tree.mkdir()
    archive = directory / "previous.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive}", base], cwd=ROOT, check=True
    )
    with tarfile.open(archive) as contents:
        contents.extractall(tree, filter="data")
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(tree / ".venv")
    # With the dev group: the sandbox manager pairing borrows its tests' fake.
    subprocess.run(["uv", "sync", "--frozen", "--quiet"], cwd=tree, env=env, check=True)
    return Release(base, tree, str(tree / ".venv" / "bin" / "python"))


#: All the two processes inherit: the developer's own settings (a stray
#: deployment SHA, say) would change what is tested.
_INHERITED = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def _base_env(dsn: str, host: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in _INHERITED if key in os.environ}
    env.update(
        {
            "PYTHONPATH": "",
            "AGENT_DATABASE_URL": dsn,
            "MIXED_PROTOCOL_HOST": host,
            # Generated per run, so nothing here is a credential of anything.
            "AGENT_SANDBOX_MANAGER_TOKEN": secrets.token_urlsafe(32),
        }
    )
    return env


def _control_plane_env(base: dict[str, str]) -> dict[str, str]:
    return {
        **base,
        "AGENT_LOCAL_SESSION_SECRET": secrets.token_urlsafe(32),
        "AGENT_SETTINGS_ENCRYPTION_KEY": secrets.token_urlsafe(32),
        "AGENT_CONTROL_TOKEN_SECRET": secrets.token_urlsafe(32),
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": secrets.token_urlsafe(32),
        "AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS": "127.0.0.1",
        # Not one outbound request: the MCP catalog and anything else that
        # would reach the internet meets a closed port instead.
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }


async def _line(process: asyncio.subprocess.Process, markers: tuple[str, ...]) -> tuple[str, str]:
    """The next stdout line that starts with one of ``markers``; logs are skipped."""
    assert process.stdout is not None
    while True:
        raw = await process.stdout.readline()
        if not raw:
            raise RuntimeError(f"exited ({await process.wait()}) before {' or '.join(markers)}")
        line = raw.decode(errors="replace").rstrip("\n")
        for marker in markers:
            if line.startswith(marker + " "):
                return marker, line[len(marker) + 1 :]


async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()


async def _migrate(release: Release, env: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        release.python,
        "-m",
        "alembic",
        "upgrade",
        "head",
        cwd=release.tree,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{release.label} migration failed:\n{stderr.decode()[-2000:]}")


_STATE_SQL = {
    "jobs": """
        SELECT j.id, j.turn_no, j.state, j.cancel_requested, j.detail,
               (SELECT array_agg(a.status || '/' || COALESCE(a.context_status, '-')
                                 ORDER BY a.attempt_no)
                  FROM agent_attempts a WHERE a.job_id = j.id) AS attempts,
               (SELECT array_agg(e.event_type ORDER BY e.seq)
                  FROM agent_job_events e WHERE e.job_id = j.id) AS events
          FROM agent_jobs j ORDER BY j.turn_no
    """,
    "members": """
        SELECT native_session_id IS NOT NULL AS native_session_recorded,
               native_state, context_status, activation_generation, process_generation,
               residency_generation, resident_state, residency_status,
               active_attempt_id IS NOT NULL AS attempt_active, admitted_job_id,
               last_settled_job_id
          FROM agent_thread_sandbox_memberships ORDER BY created_at
    """,
    "groups": """
        SELECT state, generation, start_seq, active_operation_count,
               idle_deadline IS NOT NULL AS idle_deadline_set,
               stop_requested_at IS NOT NULL AS stop_requested
          FROM agent_sandboxes ORDER BY created_at
    """,
    "hosts": "SELECT status FROM agent_runner_hosts ORDER BY host",
}


async def _state(dsn: str) -> dict[str, Any]:
    """What a turn's protocol leaves in the database, with every random id replaced."""
    conn = await asyncpg.connect(dsn)
    try:
        state = {
            name: [dict(row) for row in await conn.fetch(sql)] for name, sql in _STATE_SQL.items()
        }
    finally:
        await conn.close()
    turns = {row.pop("id"): f"turn {row['turn_no']}" for row in state["jobs"]}
    for row in state["members"]:
        for key in ("admitted_job_id", "last_settled_job_id"):
            row[key] = turns.get(row[key], row[key])
    return json.loads(json.dumps(state, default=str))


async def run_pairing(host: Release, control: Release, workdir: Path) -> dict[str, Any]:
    """Run the scripted host against the control plane; return what both sides saw."""
    database = f"mixed_protocol_test_{uuid.uuid4().hex}"
    name = f"{host.label} host -> {control.label} control plane"
    admin = await asyncpg.connect(**_connection(), database="postgres")
    processes: list[asyncio.subprocess.Process] = []
    snapshots: dict[str, Any] = {}
    logs = workdir / f"{uuid.uuid4().hex}"
    logs.mkdir()
    started = time.monotonic()
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _dsn(database)
        host_name = "mixed-protocol-host"
        base = _base_env(dsn, host_name)
        control_env = _control_plane_env(base)
        await _migrate(control, control_env)
        with (
            (logs / "control-plane.log").open("wb") as control_log,
            (logs / "host.log").open("wb") as host_log,
        ):
            cp = await asyncio.create_subprocess_exec(
                control.python,
                str(SUPPORT / "mixed_protocol_control.py"),
                str(control.tree),
                cwd=control.tree,
                env=control_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=control_log,
            )
            processes.append(cp)
            _, ready = await _line(cp, ("READY",))
            fixtures = json.loads(ready)
            host_env = {
                **base,
                "AGENT_HOST_CREDENTIAL_FILE": str(logs / "host-credential"),
                "NO_PROXY": "127.0.0.1,localhost",
            }
            runner = await asyncio.create_subprocess_exec(
                host.python,
                str(SUPPORT / "mixed_protocol_host.py"),
                str(host.tree),
                f"http://127.0.0.1:{fixtures['port']}",
                fixtures["enroll_token"],
                cwd=host.tree,
                env=host_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=host_log,
            )
            processes.append(runner)
            while True:
                marker, payload = await _line(runner, ("CALL", "DONE"))
                if marker == "DONE":
                    report = json.loads(payload)
                    break
                call = json.loads(payload)
                if call["op"] == "snapshot":
                    # The database between steps, not only at the end: two
                    # runs can converge after one of them skipped a step.
                    snapshots[call["label"]] = await _state(dsn)
                    await _send(runner, {"ok": True, "result": {}})
                    continue
                await _send(cp, call)
                _, answer = await _line(cp, ("ANSWER",))
                await _send(runner, json.loads(answer))
            await _send(cp, {"op": "report"})
            _, answer = await _line(cp, ("ANSWER",))
            received = json.loads(answer)["result"]["calls"]
            await _send(cp, {"op": "quit"})
            await asyncio.wait_for(cp.wait(), 30)
            await asyncio.wait_for(runner.wait(), 30)
        return {
            "name": name,
            "report": report,
            "received": received,
            "state": {**snapshots, "at the end": await _state(dsn)},
            "seconds": round(time.monotonic() - started, 1),
            "logs": str(logs),
        }
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


def problems(result: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    """Everything wrong with one pairing, each as a line a reviewer can act on."""
    found: list[str] = []
    report = result["report"]
    if report["error"]:
        found.append(f"the host script stopped:\n{report['error'].rstrip()}")
    found.extend(f"host: {failure}" for failure in report["failures"])
    for call in report["calls"]:
        where = f"[{call['step']}] {call['method']} {call['route']}"
        if call["status"] >= 400:
            found.append(f"{where} answered {call['status']}")
        if call["contract"]:
            found.append(
                f"{where}: the host's own response model refuses the answer: {call['contract']}"
            )
        if dropped := _dropped("host", call["route"], call["dropped"]):
            found.append(f"{where}: the host's response model drops {dropped}")
    for call in result["received"]:
        where = f"{call['method']} {call['route']}"
        if dropped := _dropped("control plane", call["route"], call["dropped"]):
            found.append(f"{where}: the control plane's request model drops {dropped}")
    for call in report["sandbox_manager"]:
        where = f"sandbox manager {call['method']} {call['route']}"
        if call["status"] >= 400:
            found.append(f"{where} answered {call['status']}: the script does not cover this call")
        if call["contract"]:
            found.append(f"{where}: the host's request model refuses the body: {call['contract']}")
        if dropped := _dropped("sandbox manager", call["route"], call["dropped"]):
            found.append(f"{where}: the host's request model drops {dropped}")
    # Sorted: a continuity report travels on a thread of its own beside the
    # process record, so the two may arrive in either order.
    sent = sorted((call["method"], call["route"], call["status"]) for call in report["calls"])
    got = sorted((call["method"], call["route"], call["status"]) for call in result["received"])
    if sent != got:
        found.append(
            f"the host's calls and the control plane's do not match:\n  host: {sent}\n  cp:   {got}"
        )
    if baseline is not None:
        for label, expected_state in baseline["state"].items():
            if result["state"].get(label) != expected_state:
                found.append(
                    f"the database {label} differs from the same-version baseline's:\n"
                    + _diff(expected_state, result["state"].get(label))
                )
        statuses = sorted((c["step"], c["route"], c["status"]) for c in report["calls"])
        expected = sorted((c["step"], c["route"], c["status"]) for c in baseline["report"]["calls"])
        if statuses != expected:
            found.append(
                "the calls or their answers differ from the baseline's:\n"
                + _diff({"calls": expected}, {"calls": statuses})
            )
    return found


def _diff(expected: Any, actual: Any) -> str:
    import difflib

    lines = difflib.unified_diff(
        json.dumps(expected, indent=1, sort_keys=True).splitlines(),
        json.dumps(actual, indent=1, sort_keys=True).splitlines(),
        "baseline",
        "mixed",
        lineterm="",
    )
    return "\n".join(f"  {line}" for line in lines)


async def run_manager_pairing(client: Release, manager: Release, workdir: Path) -> dict[str, Any]:
    """The control plane's sandbox manager calls from one release to another's manager."""
    name = f"{client.label} control plane -> {manager.label} sandbox manager"
    env = {key: os.environ[key] for key in _INHERITED if key in os.environ} | {"PYTHONPATH": ""}
    script = str(SUPPORT / "mixed_protocol_manager.py")
    logs = workdir / f"{uuid.uuid4().hex}"
    logs.mkdir()
    started = time.monotonic()
    processes: list[asyncio.subprocess.Process] = []
    try:
        with (
            (logs / "sandbox-manager.log").open("wb") as manager_log,
            (logs / "client.log").open("wb") as client_log,
        ):
            served = await asyncio.create_subprocess_exec(
                manager.python,
                script,
                "serve",
                str(manager.tree),
                cwd=manager.tree,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=manager_log,
            )
            processes.append(served)
            _, ready = await _line(served, ("READY",))
            caller = await asyncio.create_subprocess_exec(
                client.python,
                script,
                "call",
                str(client.tree),
                f"http://127.0.0.1:{json.loads(ready)['port']}",
                cwd=client.tree,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=client_log,
            )
            processes.append(caller)
            _, done = await _line(caller, ("DONE",))
            assert served.stdin is not None
            served.stdin.write(b"report\n")
            await served.stdin.drain()
            _, recorded = await _line(served, ("REPORT",))
            await asyncio.wait_for(served.wait(), 30)
            await asyncio.wait_for(caller.wait(), 30)
        return {
            "name": name,
            "calls": json.loads(done)["calls"],
            "received": json.loads(recorded)["calls"],
            "seconds": round(time.monotonic() - started, 1),
            "logs": str(logs),
        }
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


def manager_problems(result: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    """Everything wrong with one sandbox manager pairing."""
    found = [
        f"{call['call']}: the control plane's client refused the answer: {call['error']}"
        for call in result["calls"]
        if call["error"]
    ]
    for call in result["received"]:
        where = f"sandbox manager {call['method']} {call['route']}"
        if call["status"] is None or call["status"] >= 400:
            found.append(f"{where} answered {call['status']}")
        if dropped := _dropped("sandbox manager", call["route"], call["dropped"]):
            found.append(f"{where}: the sandbox manager's request model drops {dropped}")
    if baseline is not None:
        # The answers are plain JSON to the client, so the contract is what its
        # own release's sandbox manager answers: nothing it reads may be missing.
        expected = {call["call"]: set(call.get("keys") or ()) for call in baseline["calls"]}
        for call in result["calls"]:
            missing = expected.get(call["call"], set()) - set(call.get("keys") or ())
            if missing and not call["error"]:
                found.append(
                    f"{call['call']}: the answer lacks {sorted(missing)}, which the client's "
                    "own release's sandbox manager sends"
                )
    return found


async def check(base: str, report: Path | None = None) -> int:
    current = Release("this checkout", ROOT, sys.executable)
    with tempfile.TemporaryDirectory(prefix="mixed-protocol-") as directory:
        workdir = Path(directory)
        started = time.monotonic()
        previous = await asyncio.to_thread(previous_release, base, workdir)
        print(
            f"previous release {base}: environment ready in {time.monotonic() - started:.0f}s",
            flush=True,
        )
        pairings = [
            (current, previous),  # direction 1
            (previous, previous),  # its baseline
            (previous, current),  # direction 2
            (current, current),  # its baseline
        ]
        managers = [
            (current, previous),  # this control plane's calls, the previous manager
            (previous, previous),
            (previous, current),  # the previous control plane's calls, this manager
            (current, current),
        ]
        everything = await asyncio.wait_for(
            asyncio.gather(
                *(run_pairing(host, cp, workdir) for host, cp in pairings),
                *(run_manager_pairing(client, sm, workdir) for client, sm in managers),
            ),
            PAIRING_TIMEOUT_S,
        )
        results, wires = everything[:4], everything[4:]
        verdicts = [
            (results[0], problems(results[0], results[1])),
            (results[1], problems(results[1], None)),
            (results[2], problems(results[2], results[3])),
            (results[3], problems(results[3], None)),
        ]
        failed = False
        for result, found in verdicts:
            calls = len(result["report"]["calls"])
            if found:
                failed = True
                print(f"FAIL {result['name']} ({calls} calls, {result['seconds']}s):", flush=True)
                for problem in found:
                    print(f"  - {problem}", flush=True)
                # The processes' own logs explain a crash or a server error;
                # the problems above already say everything else.
                if result["report"]["error"] or any(
                    call["status"] >= 500 for call in result["report"]["calls"]
                ):
                    for log in ("host.log", "control-plane.log"):
                        tail = (Path(result["logs"]) / log).read_text(errors="replace")[-3000:]
                        print(f"  --- {log} (tail) ---\n{tail}", flush=True)
            else:
                print(f"pass {result['name']} ({calls} calls, {result['seconds']}s)", flush=True)
        # A client's baseline is its own release's sandbox manager.
        for wire, found in (
            (wires[0], manager_problems(wires[0], wires[3])),
            (wires[1], manager_problems(wires[1], None)),
            (wires[2], manager_problems(wires[2], wires[1])),
            (wires[3], manager_problems(wires[3], None)),
        ):
            if found:
                failed = True
                print(f"FAIL {wire['name']} ({len(wire['calls'])} calls):", flush=True)
                for problem in found:
                    print(f"  - {problem}", flush=True)
                for log in ("sandbox-manager.log", "client.log"):
                    tail = (Path(wire["logs"]) / log).read_text(errors="replace")[-2000:]
                    print(f"  --- {log} (tail) ---\n{tail}", flush=True)
            else:
                print(
                    f"pass {wire['name']} ({len(wire['calls'])} calls, {wire['seconds']}s)",
                    flush=True,
                )
        if report is not None:
            report.write_text(json.dumps(everything, indent=1, default=str))
        print(f"mixed protocol check took {time.monotonic() - started:.0f}s", flush=True)
        return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="the previous release's tag")
    parser.add_argument("--report", type=Path, help="also write every pairing's record here")
    arguments = parser.parse_args()
    sys.exit(asyncio.run(check(arguments.base, arguments.report)))
