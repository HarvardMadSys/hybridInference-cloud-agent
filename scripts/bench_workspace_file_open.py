#!/usr/bin/env python3
"""Measure how long one retained-workspace file open takes.

Opening a file in the chat's Files panel walks this path::

    browser -> CP GET /v1/agent/jobs/{job}/files
            -> broker GET /workspaces/{workspace}/files   (httpx, host network)
            -> Supervisor.operation_opened()               (in-memory lock)
            -> ChatSandboxManager.require_network_phase()   (docker CLI: network
                                                            + container inspect,
                                                            Kata guest hosts sync)
            -> retained_workspace.read_retained_file()
            -> ChatSandboxManager.exec_capture()            (docker exec into the
                                                            retained container)
            -> /usr/bin/python3 -I -c <read helper>         (fresh interpreter per
                                                            open, uncached)

Only the last two steps run inside the sandbox; everything else is per-open
fixed overhead.  Nothing in the repository currently times any of it, so this
script exists to produce numbers instead of guesses.

Three modes, in increasing fidelity:

``helper``
    Runs the exact in-guest read program as a local subprocess.  This is the
    interpreter-spawn + read + JSON-emit floor of one open.  It uses the host
    interpreter named by ``--python`` (Debian bookworm ships Python 3.11, so
    ``--python python3.11`` is the closest local match) and the host CPU, so
    treat it as a floor, not the guest's own number.

``stack``
    Serves a broker-shaped FastAPI route in-process and calls it with the real
    :class:`WorkspaceBrokerClient`.  A fake manager swaps ``docker exec`` for a
    local subprocess, so this measures *everything except* the Docker/Kata
    boundary, the real network, and the control plane's database work.

``docker``
    Builds a real retained container with the platform's own
    :class:`ChatSandboxManager` — two backplane networks, the proxy/gateway/MCP
    relay aliases and a per-chat internal bridge — then times the two steps one
    open actually costs on the broker: ``require_network_phase()`` and
    ``read_retained_file()``.  This is the only mode that measures the Docker
    boundary, and it reports how many ``docker`` CLI invocations one open made.
    It needs a working Docker daemon and ``--image`` (see ``--build-image``).

``live``
    Times the real public endpoint ``GET /v1/agent/jobs/{job}/files`` against a
    running deployment.  This is the only mode that yields a true end-to-end
    number, and it needs ``--url``, ``--token`` and ``--job-id``.

Examples::

    python scripts/bench_workspace_file_open.py --mode helper --python python3.11
    python scripts/bench_workspace_file_open.py --mode stack --python python3.11
    python scripts/bench_workspace_file_open.py --mode docker --build-image
    python scripts/bench_workspace_file_open.py --mode live \\
        --url https://cloud-agent.example --token "$AGENT_SESSION_TOKEN" \\
        --job-id 00000000-0000-0000-0000-000000000000 --iterations 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cloud_agent_host.retained_workspace import _READ_PROGRAM

#: (label, relative path, human description).  An empty path lists the root.
CASES: tuple[tuple[str, str, str], ...] = (
    ("dir-root", "", "directory listing, 40 entries"),
    ("file-4k", "small.txt", "text file, 4 KiB"),
    ("file-64k", "medium.py", "text file, 64 KiB"),
    ("file-512k", "large.txt", "text file, 512 KiB (preview ceiling)"),
)

MAX_PREVIEW_BYTES = 512 * 1024
MAX_DIRECTORY_ENTRIES = 5000


class Sample:
    """One measured case."""

    def __init__(self, label: str, description: str, values: list[float]) -> None:
        self.label = label
        self.description = description
        self.values = sorted(values)

    def percentile(self, fraction: float) -> float:
        if not self.values:
            raise ValueError("no samples")
        index = min(len(self.values) - 1, max(0, round(fraction * (len(self.values) - 1))))
        return self.values[index]

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "case": self.label,
            "description": self.description,
            "n": len(self.values),
            "min_ms": round(self.values[0], 1),
            "p50_ms": round(self.percentile(0.50), 1),
            "p95_ms": round(self.percentile(0.95), 1),
            "max_ms": round(self.values[-1], 1),
            "mean_ms": round(statistics.fmean(self.values), 1),
        }


def build_fixture(root: Path) -> None:
    """Create a small deterministic workspace tree."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    for index in range(30):
        (root / f"entry-{index:02d}.md").write_text(f"# entry {index}\n", encoding="utf-8")
    for index in range(10):
        (root / "src" / f"module-{index:02d}.py").write_text(
            f"def f{index}():\n    return {index}\n", encoding="utf-8"
        )
    (root / "small.txt").write_text("x" * (4 * 1024), encoding="utf-8")
    (root / "medium.py").write_text("# comment line\n" * (64 * 1024 // 15), encoding="utf-8")
    (root / "large.txt").write_text("y" * (512 * 1024), encoding="utf-8")


def timed(run, iterations: int, warmup: int) -> list[float]:
    for _ in range(warmup):
        run()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        run()
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


# --------------------------------------------------------------------------
# mode: helper
# --------------------------------------------------------------------------


def helper_argv(python: str, root: Path, relative: str) -> list[str]:
    return [
        python,
        "-I",
        "-c",
        _READ_PROGRAM,
        str(root),
        relative,
        str(MAX_PREVIEW_BYTES),
        str(MAX_DIRECTORY_ENTRIES),
    ]


def run_helper(python: str, root: Path, iterations: int, warmup: int) -> list[Sample]:
    samples: list[Sample] = []
    for label, relative, description in CASES:
        argv = helper_argv(python, root, relative)

        def once(argv: list[str] = argv) -> None:
            result = subprocess.run(argv, capture_output=True, cwd="/", check=False)
            if result.returncode != 0:
                raise SystemExit(
                    f"read helper failed ({result.returncode}): "
                    f"{result.stderr.decode('utf-8', 'replace').strip()[:300]}"
                )

        samples.append(Sample(label, description, timed(once, iterations, warmup)))
    return samples


# --------------------------------------------------------------------------
# mode: stack
# --------------------------------------------------------------------------


class _LocalExecManager:
    """Stand-in for ChatSandboxManager that spawns the helper on this host."""

    def __init__(self, python: str, root: Path) -> None:
        self._python = python
        self._root = root

    def exec_capture(self, spec, argv, **_kwargs):
        if argv[1:4] != ["-I", "-c", _READ_PROGRAM]:
            raise SystemExit(f"unexpected helper argv: {argv[:4]!r}")
        # argv[4] is the guest CHAT_WORKDIR; argv[5:] carries the request data.
        command = [self._python, *argv[1:4], str(self._root), *argv[5:]]
        return subprocess.run(command, capture_output=True, cwd="/", check=False)


def run_stack(python: str, root: Path, iterations: int, warmup: int) -> list[Sample]:
    import secrets

    import uvicorn
    from fastapi import FastAPI, Header, HTTPException, Query

    from cloud_agent.workspace_broker_client import WorkspaceBrokerClient
    from cloud_agent_host.retained_workspace import read_retained_file

    backend = SimpleNamespace(manager=_LocalExecManager(python, root), spec=object())
    app = FastAPI()
    expected_token = "bench"

    @app.get("/workspaces/{workspace_id}/files")
    def files(
        workspace_id: str,
        path: str = Query("", max_length=4096),
        x_agent_workspace_token: str | None = Header(default=None),
    ):
        if not x_agent_workspace_token or not secrets.compare_digest(
            expected_token, x_agent_workspace_token
        ):
            raise HTTPException(status_code=401, detail="invalid workspace broker credential")
        return read_retained_file(
            backend,
            path,
            max_preview_bytes=MAX_PREVIEW_BYTES,
            max_directory_entries=MAX_DIRECTORY_ENTRIES,
        )

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("local broker-shaped server did not start")
        time.sleep(0.02)

    client = WorkspaceBrokerClient(f"http://127.0.0.1:{port}", "bench")
    try:
        return asyncio.run(_stack_samples(client, iterations, warmup))
    finally:
        server.should_exit = True


async def _stack_samples(client, iterations: int, warmup: int, cases=CASES) -> list[Sample]:
    samples: list[Sample] = []
    for label, relative, description in cases:
        for _ in range(warmup):
            await client.files("bench-workspace", relative)
        values: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            payload = await client.files("bench-workspace", relative)
            values.append((time.perf_counter() - started) * 1000.0)
            if "kind" not in payload:
                raise SystemExit(f"unexpected broker payload: {payload!r}")
        samples.append(Sample(label, description, values))
    return samples


# --------------------------------------------------------------------------
# mode: docker
# --------------------------------------------------------------------------

BENCH_IMAGE = "cae-bench-guest:bookworm"
BENCH_NAMESPACE = "cae-file-open-bench"
BENCH_SETUP_NET = "cae-bench-setup"
BENCH_AGENT_NET = "cae-bench-agent"
BENCH_PROXY = "cae-bench-proxy"
BENCH_GATEWAY = "cae-bench-gateway"
BENCH_MCP = "cae-bench-mcp"

_SEED_SCRIPT = """
set -e
mkdir -p /workspace/src
i=0
while [ $i -lt 30 ]; do printf '# entry %s\\n' "$i" > "/workspace/entry-$i.md"; i=$((i+1)); done
i=0
while [ $i -lt 10 ]; do printf 'def f%s():\\n    return %s\\n' "$i" "$i" > "/workspace/src/module-$i.py"; i=$((i+1)); done
python3 -c "open('/workspace/small.txt','w').write('x'*4096); open('/workspace/medium.py','w').write('# comment line\\n'*(64*1024//15)); open('/workspace/large.txt','w').write('y'*(512*1024))"
chown -R 10001:10001 /workspace
"""


def _docker(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"docker {' '.join(argv)} failed: {result.stderr.strip()[:300]}")
    return result


def build_bench_image() -> None:
    """Build a bookworm guest carrying ``/usr/bin/python3`` (3.11), like the sandbox."""
    with tempfile.TemporaryDirectory(prefix="cae-bench-image-") as directory:
        (Path(directory) / "Dockerfile").write_text(
            "FROM debian:bookworm-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends python3 \\\n"
            " && rm -rf /var/lib/apt/lists/*\n",
            encoding="utf-8",
        )
        _docker("build", "-q", "-t", BENCH_IMAGE, directory)


class DockerSandbox:
    """A real retained container, built and started by the platform's own manager."""

    def __init__(self, image: str) -> None:
        from cloud_agent_host.chat_sandbox import (
            ChatSandboxManager,
            ChatSandboxSpec,
            RetainedChatBackend,
        )
        from cloud_agent_host.egress import EgressPolicy, EgressTier
        from cloud_agent_host.sandbox import ContainerBackend

        self.image = image
        policy = EgressPolicy(
            tiers={"setup": EgressTier.TRUSTED, "agent": EgressTier.PLATFORM_ONLY},
            networks={
                EgressTier.TRUSTED: BENCH_SETUP_NET,
                EgressTier.PLATFORM_ONLY: BENCH_AGENT_NET,
            },
            proxies={EgressTier.TRUSTED: f"http://{BENCH_PROXY}:3128"},
            no_proxy=(BENCH_GATEWAY, BENCH_MCP),
        )
        self.manager = ChatSandboxManager(
            ContainerBackend(
                image=image,
                egress=policy,
                sandbox_namespace=BENCH_NAMESPACE,
                chat_network_pool="10.250.0.0/16",
            )
        )
        self.spec = ChatSandboxSpec(
            thread_id="bench-thread",
            sandbox_id="bench-sandbox",
            generation=1,
            runtime_id="claude-code",
            memory_limit="1g",
            cpu_limit="1",
            resident_slot_capacity=2,
        )
        self.backend = RetainedChatBackend(self.manager, self.spec)

    def _relay(self, name: str, network: str, alias: str) -> None:
        _docker("rm", "-f", name, check=False)
        _docker(
            "run",
            "-d",
            "--name",
            name,
            "--hostname",
            alias,
            "--network",
            network,
            "--network-alias",
            alias,
            self.image,
            "sleep",
            "infinity",
        )

    def up(self) -> None:
        """Reproduce the two backplanes, their relays, and one chat bridge."""
        _docker("network", "create", BENCH_SETUP_NET, check=False)
        _docker("network", "create", BENCH_AGENT_NET, check=False)
        self._relay(BENCH_PROXY, BENCH_SETUP_NET, BENCH_PROXY)
        self._relay(BENCH_GATEWAY, BENCH_AGENT_NET, BENCH_GATEWAY)
        self._relay(BENCH_MCP, BENCH_AGENT_NET, BENCH_MCP)
        _docker("rm", "-f", self.spec.container_name, check=False)
        self.manager.create(self.spec)
        self.manager.transition_to_agent_network(self.spec)
        self.manager.start(self.spec)
        self.manager.exec_capture(
            self.spec,
            ["/bin/sh", "-c", _SEED_SCRIPT],
            workdir="/",
            user_uid=0,
            user_gid=0,
            timeout_s=120,
        )

    def down(self) -> None:
        from cloud_agent_host.chat_sandbox import CHAT_NAMESPACE_LABEL

        _docker("rm", "-f", self.spec.container_name, check=False)
        for name in (BENCH_PROXY, BENCH_GATEWAY, BENCH_MCP):
            _docker("rm", "-f", name, check=False)
        listed = _docker(
            "network",
            "ls",
            "--filter",
            f"label={CHAT_NAMESPACE_LABEL}={BENCH_NAMESPACE}",
            "--format",
            "{{.Name}}",
            check=False,
        )
        for name in listed.stdout.split():
            _docker("network", "rm", name, check=False)
        for name in (BENCH_SETUP_NET, BENCH_AGENT_NET):
            _docker("network", "rm", name, check=False)

    def verify_and_read(self, relative: str) -> dict[str, float]:
        """One open, split into the broker's two steps."""
        from cloud_agent_host.retained_workspace import read_retained_file

        started = time.perf_counter()
        self.manager.require_network_phase(self.spec, "agent")
        verified = time.perf_counter()
        payload = read_retained_file(
            self.backend,
            relative,
            max_preview_bytes=MAX_PREVIEW_BYTES,
            max_directory_entries=MAX_DIRECTORY_ENTRIES,
        )
        finished = time.perf_counter()
        if "kind" not in payload:
            raise SystemExit(f"unexpected read payload: {payload!r}")
        return {
            "verify_ms": (verified - started) * 1000.0,
            "read_ms": (finished - verified) * 1000.0,
            "total_ms": (finished - started) * 1000.0,
        }

    def count_one_open(self) -> dict[str, object]:
        """Count the ``docker`` CLI invocations one open makes, with real docker."""
        from cloud_agent_host.retained_workspace import read_retained_file

        return self._census(
            lambda: (
                self.manager.require_network_phase(self.spec, "agent"),
                read_retained_file(
                    self.backend,
                    "small.txt",
                    max_preview_bytes=MAX_PREVIEW_BYTES,
                    max_directory_entries=MAX_DIRECTORY_ENTRIES,
                ),
            )
        )

    def cold_open(self, relative: str) -> dict[str, float]:
        """One open after an idle stop, split the way the broker's wake sees it.

        Emulates ``operation_opened()`` on a stopped group: inspect, then the
        supervisor's ``manager.start(...)`` wake, then the ordinary verify and
        read of a file open.
        """
        from cloud_agent_host.retained_workspace import read_retained_file

        started = time.perf_counter()
        self.manager.inspect(self.spec.container_name)
        self.manager.start(self.spec, network_phase="agent")
        woke = time.perf_counter()
        self.manager.require_network_phase(self.spec, "agent")
        verified = time.perf_counter()
        payload = read_retained_file(
            self.backend,
            relative,
            max_preview_bytes=MAX_PREVIEW_BYTES,
            max_directory_entries=MAX_DIRECTORY_ENTRIES,
        )
        finished = time.perf_counter()
        if "kind" not in payload:
            raise SystemExit(f"unexpected read payload: {payload!r}")
        return {
            "wake_ms": (woke - started) * 1000.0,
            "verify_ms": (verified - woke) * 1000.0,
            "read_ms": (finished - verified) * 1000.0,
            "total_ms": (finished - started) * 1000.0,
        }

    def idle_stop(self) -> None:
        """Stop the container the way the supervisor's idle deadline does."""
        self.manager.stop(self.spec)

    def count_cold_open(self) -> dict[str, object]:
        """Count the ``docker`` CLI invocations one cold open makes, stop included."""
        from cloud_agent_host.retained_workspace import read_retained_file

        return self._census(
            lambda: (
                self.manager.stop(self.spec),
                self.manager.inspect(self.spec.container_name),
                self.manager.start(self.spec, network_phase="agent"),
                self.manager.require_network_phase(self.spec, "agent"),
                read_retained_file(
                    self.backend,
                    "small.txt",
                    max_preview_bytes=MAX_PREVIEW_BYTES,
                    max_directory_entries=MAX_DIRECTORY_ENTRIES,
                ),
            )
        )

    @staticmethod
    def _census(operation) -> dict[str, object]:
        """Run one operation and count every ``docker`` CLI invocation it makes."""
        from collections import Counter

        from cloud_agent_host import chat_sandbox

        calls: list[list[str]] = []
        real_run = chat_sandbox.subprocess.run

        def spy(command, *args, **kwargs):
            calls.append(list(command))
            return real_run(command, *args, **kwargs)

        chat_sandbox.subprocess.run = spy
        try:
            operation()
        finally:
            chat_sandbox.subprocess.run = real_run

        by_subcommand: Counter[str] = Counter()
        for command in calls:
            if command and command[0].endswith("docker"):
                by_subcommand[" ".join(command[1:3])] += 1
        return {"total": len(calls), "by_subcommand": dict(sorted(by_subcommand.items()))}


def run_docker(
    iterations: int,
    warmup: int,
    image: str,
    keep: bool,
    with_http: bool,
    cold: int = 0,
) -> tuple[list[Sample], list[dict[str, object]], dict[str, object]]:
    sandbox = DockerSandbox(image)
    totals: list[Sample] = []
    breakdown: list[dict[str, object]] = []
    meta: dict[str, object] = {}
    try:
        sandbox.up()
        meta["counted_invocations"] = sandbox.count_one_open()
        meta["image"] = image
        for label, relative, description in CASES:
            measured = []
            for index in range(warmup + iterations):
                result = sandbox.verify_and_read(relative)
                if index >= warmup:
                    measured.append(result)
            totals.append(Sample(label, description, [row["total_ms"] for row in measured]))
            breakdown.append(
                {
                    "case": label,
                    "verify_ms": _stats([row["verify_ms"] for row in measured]),
                    "read_ms": _stats([row["read_ms"] for row in measured]),
                }
            )
        if cold:
            samples = []
            for _ in range(cold):
                sandbox.idle_stop()
                samples.append(sandbox.cold_open("small.txt"))
            meta["cold"] = {
                "n": cold,
                "wake_ms": _stats([row["wake_ms"] for row in samples]),
                "verify_ms": _stats([row["verify_ms"] for row in samples]),
                "read_ms": _stats([row["read_ms"] for row in samples]),
                "total_ms": _stats([row["total_ms"] for row in samples]),
            }
            meta["counted_cold_invocations"] = sandbox.count_cold_open()
        if with_http:
            meta["http"] = _docker_http_samples(sandbox, iterations, warmup)
    finally:
        if keep:
            print(f"kept container {sandbox.spec.container_name} for inspection")
        else:
            sandbox.down()
    return totals, breakdown, meta


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))]

    return {
        "p50_ms": round(at(0.50), 1),
        "p95_ms": round(at(0.95), 1),
        "min_ms": round(ordered[0], 1),
        "max_ms": round(ordered[-1], 1),
    }


def _docker_http_samples(
    sandbox: DockerSandbox, iterations: int, warmup: int
) -> list[dict[str, float | int | str]]:
    """Serve the broker-shaped route over the real container and time the client."""
    import secrets

    import uvicorn
    from fastapi import FastAPI, Header, HTTPException, Query

    from cloud_agent.workspace_broker_client import WorkspaceBrokerClient
    from cloud_agent_host.retained_workspace import read_retained_file

    app = FastAPI()
    expected_token = "bench"

    @app.get("/workspaces/{workspace_id}/files")
    def files(
        workspace_id: str,
        path: str = Query("", max_length=4096),
        x_agent_workspace_token: str | None = Header(default=None),
    ):
        if not x_agent_workspace_token or not secrets.compare_digest(
            expected_token, x_agent_workspace_token
        ):
            raise HTTPException(status_code=401, detail="invalid workspace broker credential")
        sandbox.manager.require_network_phase(sandbox.spec, "agent")
        return read_retained_file(
            sandbox.backend,
            path,
            max_preview_bytes=MAX_PREVIEW_BYTES,
            max_directory_entries=MAX_DIRECTORY_ENTRIES,
        )

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("local broker-shaped server did not start")
        time.sleep(0.02)
    client = WorkspaceBrokerClient(f"http://127.0.0.1:{port}", expected_token)
    try:
        samples = asyncio.run(_stack_samples(client, iterations, warmup, cases=CASES[:2]))
        return [sample.as_dict() for sample in samples]
    finally:
        server.should_exit = True


# --------------------------------------------------------------------------
# mode: live
# --------------------------------------------------------------------------


def run_live(
    url: str, token: str, job_id: str, iterations: int, warmup: int, cases
) -> list[Sample]:
    return asyncio.run(_live_samples(url, token, job_id, iterations, warmup, cases))


async def _live_samples(
    url: str,
    token: str,
    job_id: str,
    iterations: int,
    warmup: int,
    cases,
) -> list[Sample]:
    import httpx

    samples: list[Sample] = []
    async with httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(60.0),
    ) as client:
        for label, relative, description in cases:
            for _ in range(warmup):
                response = await client.get(
                    f"/v1/agent/jobs/{job_id}/files", params={"path": relative}
                )
                response.raise_for_status()
            values: list[float] = []
            for _ in range(iterations):
                started = time.perf_counter()
                response = await client.get(
                    f"/v1/agent/jobs/{job_id}/files", params={"path": relative}
                )
                values.append((time.perf_counter() - started) * 1000.0)
                response.raise_for_status()
            samples.append(Sample(label, description, values))
    return samples


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def report(
    mode: str,
    samples: list[Sample],
    as_json: bool,
    breakdown: list[dict[str, object]] | None = None,
    meta: dict[str, object] | None = None,
) -> None:
    rows = [sample.as_dict() for sample in samples]
    if as_json:
        payload: dict[str, object] = {"mode": mode, "samples": rows}
        if breakdown:
            payload["breakdown"] = breakdown
        if meta:
            payload["meta"] = meta
        print(json.dumps(payload, indent=2))
        return
    print(f"\nmode: {mode}")
    print(f"{'case':<11} {'n':>4} {'min':>8} {'p50':>8} {'p95':>8} {'max':>8}  description")
    for row in rows:
        print(
            f"{row['case']:<11} {row['n']:>4} {row['min_ms']:>8} {row['p50_ms']:>8} "
            f"{row['p95_ms']:>8} {row['max_ms']:>8}  {row['description']}"
        )
    print("(all values in milliseconds)")
    if breakdown:
        print("\nbroker steps, milliseconds")
        print(f"{'case':<11} {'verify p50':>11} {'verify p95':>11} {'read p50':>9} {'read p95':>9}")
        for row in breakdown:
            verify = row["verify_ms"]
            read = row["read_ms"]
            print(
                f"{row['case']:<11} {verify['p50_ms']:>11} {verify['p95_ms']:>11} "
                f"{read['p50_ms']:>9} {read['p95_ms']:>9}"
            )
    for key, value in (meta or {}).items():
        if key in {"counted_invocations", "counted_cold_invocations"}:
            label = "one open" if key == "counted_invocations" else "one cold open (stop included)"
            print(f"\ndocker CLI invocations for {label}: {value['total']}")
            for name, count in value["by_subcommand"].items():
                print(f"  {count:>2}x docker {name}")
        elif key == "cold":
            print(f"\ncold open after an idle stop, milliseconds (n={value['n']})")
            print(f"{'step':<10} {'p50':>8} {'p95':>8} {'min':>8} {'max':>8}")
            for step in ("wake_ms", "verify_ms", "read_ms", "total_ms"):
                row = value[step]
                print(
                    f"{step.removesuffix('_ms'):<10} {row['p50_ms']:>8} {row['p95_ms']:>8} "
                    f"{row['min_ms']:>8} {row['max_ms']:>8}"
                )
        elif key == "http":
            print("\nwith the broker-shaped HTTP route in front, milliseconds")
            for row in value:
                print(f"  {row['case']:<11} p50 {row['p50_ms']:>7}  p95 {row['p95_ms']:>7}")
        else:
            print(f"{key}: {value}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__ or "", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("helper", "stack", "docker", "live"), default="helper")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter that stands in for the sandbox's /usr/bin/python3",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable samples")
    parser.add_argument("--url", help="live: control-plane base URL")
    parser.add_argument("--token", help="live: bearer token for the control plane")
    parser.add_argument("--job-id", help="live: chat job whose workspace to read")
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="live: repeatable file path to open (default: the four fixture cases)",
    )
    parser.add_argument("--image", default=BENCH_IMAGE, help="docker: guest image to run")
    parser.add_argument(
        "--build-image",
        action="store_true",
        help="docker: build the bookworm guest image before measuring",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="docker: leave the container, relays and networks behind for inspection",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="docker: also time the broker-shaped HTTP route in front of the container",
    )
    parser.add_argument(
        "--cold",
        type=int,
        default=0,
        help="docker: also time this many opens that begin from an idle-stopped container",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "live":
        missing = [name for name in ("url", "token", "job_id") if not getattr(args, name)]
        if missing:
            print(
                "live mode needs " + ", ".join("--" + name.replace("_", "-") for name in missing),
                file=sys.stderr,
            )
            return 2
        cases = (
            tuple((path, path, "operator-supplied path") for path in args.paths)
            if args.paths
            else CASES
        )
        samples = run_live(args.url, args.token, args.job_id, args.iterations, args.warmup, cases)
        report("live", samples, args.json)
        return 0

    if args.mode == "docker":
        if args.build_image:
            build_bench_image()
        samples, breakdown, meta = run_docker(
            args.iterations, args.warmup, args.image, args.keep, args.http, args.cold
        )
        report("docker", samples, args.json, breakdown, meta)
        return 0

    with tempfile.TemporaryDirectory(prefix="file-open-bench-") as directory:
        root = Path(directory)
        build_fixture(root)
        if args.mode == "helper":
            samples = run_helper(args.python, root, args.iterations, args.warmup)
        else:
            samples = run_stack(args.python, root, args.iterations, args.warmup)
    report(args.mode, samples, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
