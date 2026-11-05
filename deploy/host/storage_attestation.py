#!/usr/bin/env python3
"""Attest runner storage against its host filesystem without project dependencies.

The host deployment invokes this executable after every fatal readiness check.
It deliberately uses only the Python standard library and commands already
required by the host deployment.  A failed refresh never makes a healthy host
unavailable; preparing the verdict bind source is the separate, fatal command.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CALL_TIMEOUT_SECONDS = 8
KILL_AFTER_SECONDS = 2
ATTESTATION_BUDGET_SECONDS = 30
DIAGNOSTIC_FLOOR_BYTES = 1024 * 1024 * 1024
MAX_SAFE_JSON_INTEGER = 2**53 - 1


def log(message: str) -> None:
    """Write a deployment information line."""
    print(f"[sandbox-host] {message}")


def warn(message: str) -> None:
    """Write a nonfatal deployment warning."""
    print(f"[sandbox-host] WARNING: {message}", file=sys.stderr)


@dataclass(frozen=True)
class CommandResult:
    """The observable result of one bounded host command."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    started: bool = True


class BoundedCommands:
    """Run host commands under the deployment's TERM/KILL contract."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        timeout_command: str = "timeout",
    ) -> None:
        self.monotonic = monotonic
        self._run = run
        self.timeout_command = timeout_command

    def available(self) -> bool:
        """Return whether GNU timeout can enforce the subprocess bound."""
        return shutil.which(self.timeout_command) is not None

    def run(self, deadline: float, command: Sequence[str]) -> CommandResult:
        """Run one command inside both the per-call and aggregate deadlines."""
        remaining = int(deadline - self.monotonic() - KILL_AFTER_SECONDS)
        if remaining <= 0:
            return CommandResult(returncode=1, started=False)
        duration = min(remaining, CALL_TIMEOUT_SECONDS)
        try:
            completed = self._run(
                [
                    self.timeout_command,
                    "-k",
                    str(KILL_AFTER_SECONDS),
                    str(duration),
                    *command,
                ],
                capture_output=True,
                check=False,
            )
        except OSError as error:
            return CommandResult(returncode=127, stderr=str(error).encode())
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class VerdictEntry:
    """One runner upper-layer verdict in the persisted version-one schema."""

    verdict: str
    mount_root: str | None
    fstype: str | None
    size: str
    reason: str | None

    def document(self) -> dict[str, object]:
        """Return the byte-compatible JSON value used by existing readers."""
        return {
            "verdict": self.verdict,
            "mount_root": self.mount_root or None,
            "fstype": self.fstype or None,
            "size_bytes": int(self.size) if self.size.isdigit() else None,
            "reason": self.reason or None,
        }


@dataclass(frozen=True)
class ContainerReading:
    """The fields used from a runner's own host-capacity description."""

    key: str
    host_path: str
    total: str
    payload: bytes


@dataclass(frozen=True)
class HelperReading:
    """A validated response from the read-only helper container."""

    mount_root: str
    fstype: str
    total: int
    upper_total: int
    used: int
    available: int


@dataclass(frozen=True)
class HelperAttempt:
    """The helper's status, diagnostics, and optional validated response."""

    returncode: int
    stderr: bytes
    reading: HelperReading | None


@dataclass
class HostMeasurement:
    """Host-side filesystem identity and capacity for one upper directory."""

    mount_root: str
    fstype: str
    root_size: str
    upper_size: str
    helper_upper: str | None = None
    helper: HelperReading | None = None


@dataclass(frozen=True)
class AttestationConfig:
    """Inputs fixed by the invoking deployment process."""

    verdict_file: Path
    deployment_sha: str
    daemon_json: Path
    probe: str
    runners: tuple[str, ...]
    budget_seconds: int


def write_verdict(
    verdict_file: Path,
    deployment_sha: str,
    entries: dict[str, VerdictEntry],
    *,
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> bool:
    """Atomically replace the version-one verdict document beside its reader."""
    try:
        verdict_file.parent.mkdir(parents=True, exist_ok=True)
        verdict_file.parent.chmod(0o755)
    except OSError:
        warn(
            f"could not prepare the storage-probe directory {verdict_file.parent}; "
            "storage attestation not refreshed"
        )
        return False

    written_at = now().astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = {
        "schema_version": 1,
        "written_at": written_at,
        "deployment_sha": deployment_sha,
        "entries": {key: value.document() for key, value in entries.items()},
    }
    temporary = Path(f"{verdict_file}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.write("\n")
        temporary.chmod(0o644)
        os.replace(temporary, verdict_file)
    except OSError:
        temporary.unlink(missing_ok=True)
        warn(
            f"could not write the storage verdict to {verdict_file}; "
            "storage attestation not refreshed"
        )
        return False
    log(f"storage verdict written to {verdict_file} ({len(entries)} entries)")
    return True


def _json_object(payload: bytes, *, replace_errors: bool = False) -> dict[str, Any]:
    try:
        errors = "replace" if replace_errors else "strict"
        value = json.loads(payload.decode("utf-8", errors=errors))
    except (UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    return "" if value is None else str(value)


_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _host_path(value: str) -> str:
    """Decode the octal escapes used by mountinfo without changing its key."""
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _container_reading(runner: str, result: CommandResult) -> ContainerReading | None:
    if result.returncode in {124, 137}:
        warn(
            f"runner {runner} did not answer the storage probe description within its "
            "budget; leaving its storage unverified"
        )
        return None
    if result.returncode != 0:
        warn(
            f"runner {runner} could not run the storage probe description; "
            "leaving its storage unverified"
        )
        return None
    document = _json_object(result.stdout, replace_errors=True)
    reason = _text(document, "reason")
    upperdir = _text(document, "upperdir")
    if not document.get("ok") or not upperdir:
        warn(
            f"runner {runner} cannot describe its storage filesystem "
            f"({reason or 'no reason'}); it will report no reading"
        )
        return None
    return ContainerReading(
        key=upperdir,
        host_path=_host_path(upperdir),
        total=_text(document, "total_bytes") or "0",
        payload=result.stdout,
    )


def _parse_helper(payload: bytes) -> HelperReading | None:
    value = _json_object(payload)
    try:
        fields = [
            value[name]
            for name in (
                "mount_root",
                "fstype",
                "total_bytes",
                "upper_total_bytes",
                "used_bytes",
                "available_bytes",
            )
        ]
        text_fields = fields[:2]
        counters = fields[2:]
        if not all(
            isinstance(item, str) and item and not any(ord(character) < 32 for character in item)
            for item in text_fields
        ) or not text_fields[0].startswith("/"):
            return None
        if not all(type(item) is int and 0 <= item <= MAX_SAFE_JSON_INTEGER for item in counters):
            return None
        if counters[2] + counters[3] > counters[0]:
            return None
    except (KeyError, TypeError):
        return None
    return HelperReading(
        mount_root=text_fields[0],
        fstype=text_fields[1],
        total=counters[0],
        upper_total=counters[1],
        used=counters[2],
        available=counters[3],
    )


def _mount_plan(payload: bytes, upperdir: str) -> tuple[str, str, str, str, str] | None:
    try:
        mounts = json.loads(payload.decode("utf-8"))["filesystems"]
        if not upperdir.startswith("/") or posixpath.normpath(upperdir) != upperdir:
            return None
        candidates = [
            mount
            for mount in mounts
            if upperdir == mount["target"] or upperdir.startswith(mount["target"].rstrip("/") + "/")
        ]
        mount = max(reversed(candidates), key=lambda item: len(item["target"]))
        target = mount["target"]
        root = mount["fsroot"]
        upper_root = posixpath.normpath(posixpath.join(root, posixpath.relpath(upperdir, target)))
        fields = (target, mount["fstype"], mount["maj:min"], root, upper_root)
        if not all(
            isinstance(item, str)
            and item
            and not any(ord(character) < 32 or character == "," for character in item)
            for item in (*fields, upperdir)
        ):
            return None
    except (UnicodeError, ValueError, KeyError, TypeError):
        return None
    return fields


def _helper_command(
    *,
    name: str,
    image: str,
    target: str,
    upperdir: str,
    device: str,
    fsroot: str,
    upper_root: str,
    fstype: str,
    helper_seconds: int,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--label",
        "org.freeinference.storage-attestation=true",
        "--runtime",
        "runc",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "16",
        "--memory",
        "64m",
        "--cpus",
        "0.25",
        "--mount",
        f"type=bind,src={target},dst=/disk,readonly,bind-recursive=disabled,"
        "bind-propagation=rslave",
        "--mount",
        f"type=bind,src={upperdir},dst=/upper,readonly,bind-recursive=disabled,"
        "bind-propagation=rslave",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "python",
        image,
        "-m",
        "cloud_agent_host.storage_attestation",
        target,
        device,
        fsroot,
        upper_root,
        fstype,
        str(helper_seconds),
    ]


def _run_helper(
    commands: BoundedCommands, deadline: float, runner: str, upperdir: str
) -> HelperAttempt:
    plan_result = commands.run(
        deadline,
        ["findmnt", "--kernel", "--list", "--json", "--output", "TARGET,FSTYPE,MAJ:MIN,FSROOT"],
    )
    if plan_result.returncode != 0:
        return HelperAttempt(1, b"", None)
    plan = _mount_plan(plan_result.stdout, upperdir)
    if plan is None:
        return HelperAttempt(1, b"", None)
    target, fstype, device, fsroot, upper_root = plan

    inspect = commands.run(deadline, ["docker", "inspect", "--format", "{{.Image}}", runner])
    image = inspect.stdout.decode("utf-8", errors="replace").strip()
    if inspect.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        return HelperAttempt(1, b"", None)

    helper_seconds = int(deadline - commands.monotonic() - KILL_AFTER_SECONDS - 1)
    if helper_seconds <= 0:
        return HelperAttempt(1, b"", None)
    helper_seconds = min(helper_seconds, 5)
    name = f"agent-storage-attest-{os.getpid()}-{secrets.randbelow(32768)}"
    result = commands.run(
        deadline,
        _helper_command(
            name=name,
            image=image,
            target=target,
            upperdir=upperdir,
            device=device,
            fsroot=fsroot,
            upper_root=upper_root,
            fstype=fstype,
            helper_seconds=helper_seconds,
        ),
    )
    if result.returncode != 0:
        commands.run(deadline, ["docker", "rm", "-f", name])
        return HelperAttempt(result.returncode, result.stderr, None)
    return HelperAttempt(0, result.stderr, _parse_helper(result.stdout))


def _direct_mount(
    commands: BoundedCommands, deadline: float, upperdir: str
) -> tuple[str, str] | None:
    result = commands.run(
        deadline,
        ["findmnt", "-n", "-o", "FSTYPE,TARGET", "-T", upperdir],
    )
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return None
    fields = lines[0].split(maxsplit=1)
    if len(fields) != 2 or not fields[1].startswith("/"):
        return None
    return fields[0], fields[1]


def _df_size(commands: BoundedCommands, deadline: float, path: str) -> str:
    result = commands.run(deadline, ["df", "-B1", "--output=size", path])
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return ""
    value = "".join(lines[-1].split())
    return value if value.isdigit() else ""


def _quota_configured(commands: BoundedCommands, deadline: float, daemon_json: Path) -> bool:
    try:
        if "overlay2.size" in daemon_json.read_text(encoding="utf-8", errors="replace"):
            return True
    except OSError:
        pass
    result = commands.run(deadline, ["ps", "-eo", "args="])
    return result.returncode == 0 and b"overlay2.size" in result.stdout


def _occupancy(payload: bytes) -> tuple[str, str] | None:
    reading = _json_object(payload)
    size = reading.get("total_bytes")
    free = reading.get("free_bytes")
    available = reading.get("available_bytes")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not isinstance(free, int)
        or isinstance(free, bool)
        or not isinstance(available, int)
        or isinstance(available, bool)
    ):
        return None
    return str(size - free), str(available)


class StorageAttestor:
    """Orchestrate all nonfatal storage proofs for a host deployment."""

    def __init__(
        self,
        config: AttestationConfig,
        *,
        commands: BoundedCommands | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.monotonic = monotonic
        self.sleep = sleep
        self.commands = commands or BoundedCommands(monotonic=monotonic)
        self.deadline = monotonic() + min(config.budget_seconds, ATTESTATION_BUDGET_SECONDS)

    def run(self) -> int:
        """Refresh the verdict if possible, always preserving deployment liveness."""
        if self.config.probe == "off":
            log(
                "storage measurement is switched off; clearing the verdict so replicas "
                "from before the switch stop publishing capacity"
            )
            write_verdict(self.config.verdict_file, self.config.deployment_sha, {})
            return 0
        if not self.config.runners:
            warn("no runner replica is available to attest storage against")
            return 0
        if not self.commands.available():
            warn(
                "coreutils 'timeout' is not on PATH; skipping storage attestation rather "
                "than risking an unbounded probe call"
            )
            return 0
        if self._out_of_time():
            warn(
                "the rollout's readiness fence has no room left for a storage "
                "attestation; leaving storage unverified"
            )
            return 0

        results = self._describe_all()
        entries: dict[str, VerdictEntry] = {}
        for runner in self.config.runners:
            if self._out_of_time():
                warn(
                    "the storage attestation ran out of its budget; the remaining "
                    "replicas keep reporting unverified"
                )
                break
            reading = _container_reading(runner, results[runner])
            if reading is None:
                continue
            entries[reading.key] = self._attest_runner(runner, reading)
        write_verdict(self.config.verdict_file, self.config.deployment_sha, entries)
        return 0

    def _out_of_time(self) -> bool:
        return self.deadline - self.monotonic() <= KILL_AFTER_SECONDS

    def _describe_all(self) -> dict[str, CommandResult]:
        command = ["python", "-m", "cloud_agent_host.host_capacity", "describe"]
        workers = max(1, len(self.config.runners))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                runner: pool.submit(
                    self.commands.run, self.deadline, ["docker", "exec", runner, *command]
                )
                for runner in self.config.runners
            }
            return {runner: future.result() for runner, future in futures.items()}

    def _attest_runner(self, runner: str, reading: ContainerReading) -> VerdictEntry:
        measurement, failure = self._locate_host_filesystem(runner, reading)
        if failure is not None:
            return failure
        assert measurement is not None
        failure = self._prove_sizes(runner, reading, measurement)
        if failure is not None:
            return failure
        agrees, note = self._usage_agrees(runner, reading, measurement)
        if agrees:
            log(
                f"runner {runner}: storage verified against {measurement.mount_root} "
                f"({measurement.fstype or 'unknown filesystem'}, {measurement.root_size} B)"
            )
            return VerdictEntry(
                "verified", measurement.mount_root, measurement.fstype, measurement.root_size, None
            )
        warn(f"runner {runner}: occupancy readings did not agree; {note}")
        return VerdictEntry(
            "unverified",
            measurement.mount_root,
            measurement.fstype,
            measurement.root_size,
            f"the container's occupancy never matched the host's: {note}",
        )

    def _locate_host_filesystem(
        self, runner: str, reading: ContainerReading
    ) -> tuple[HostMeasurement | None, VerdictEntry | None]:
        direct = _direct_mount(self.commands, self.deadline, reading.host_path)
        if direct is not None:
            fstype, mount_root = direct
            return HostMeasurement(mount_root, fstype, "", ""), None

        helper = _run_helper(self.commands, self.deadline, runner, reading.host_path)
        if helper.reading is not None:
            value = helper.reading
            return (
                HostMeasurement(
                    value.mount_root,
                    value.fstype,
                    str(value.total),
                    str(value.upper_total),
                    reading.host_path,
                    value,
                ),
                None,
            )
        if helper.returncode != 0:
            self._report_helper_failure(runner, helper)
            reason = (
                f"the read-only storage helper exited {helper.returncode} instead of "
                f"locating the filesystem containing {reading.host_path}"
            )
        else:
            warn(
                f"runner {runner}: findmnt could not locate the filesystem containing "
                f"{reading.host_path}"
            )
            reason = f"the host could not locate the filesystem containing {reading.host_path}"
        return None, VerdictEntry("unverified", None, None, reading.total, reason)

    @staticmethod
    def _report_helper_failure(runner: str, helper: HelperAttempt) -> None:
        warn(
            f"runner {runner}: the read-only storage helper exited {helper.returncode}; "
            "leaving its storage unverified"
        )
        lines = helper.stderr.decode("utf-8", errors="replace").splitlines()
        if lines:
            print(f"[sandbox-host] {runner} storage helper said:", file=sys.stderr)
            for line in lines[-5:]:
                print(line, file=sys.stderr)

    def _prove_sizes(
        self, runner: str, reading: ContainerReading, measurement: HostMeasurement
    ) -> VerdictEntry | None:
        root_size = measurement.root_size or _df_size(
            self.commands, self.deadline, measurement.mount_root
        )
        if not root_size:
            warn(f"runner {runner}: df could not size {measurement.mount_root}")
            return VerdictEntry(
                "unverified",
                measurement.mount_root,
                measurement.fstype,
                reading.total,
                f"df could not size {measurement.mount_root} on the host",
            )
        measurement.root_size = root_size
        if _quota_configured(self.commands, self.deadline, self.config.daemon_json):
            warn(
                f"runner {runner}: daemon configuration sets overlay2.size; storage "
                "capacity would be a quota, not the disk"
            )
            return VerdictEntry(
                "unsupported",
                measurement.mount_root,
                measurement.fstype,
                root_size,
                "the Docker daemon is configured with overlay2.size, so this reading is a "
                "project quota",
            )
        upper_size = measurement.upper_size or _df_size(
            self.commands, self.deadline, reading.host_path
        )
        if not upper_size:
            warn(
                f"runner {runner}: df could not size {reading.host_path}; leaving its "
                "storage unverified"
            )
            return VerdictEntry(
                "unverified",
                measurement.mount_root,
                measurement.fstype,
                root_size,
                f"df could not size {reading.host_path} within the attestation's budget",
            )
        measurement.upper_size = upper_size
        return self._size_mismatch(runner, reading, measurement)

    @staticmethod
    def _size_mismatch(
        runner: str, reading: ContainerReading, measurement: HostMeasurement
    ) -> VerdictEntry | None:
        if measurement.upper_size != measurement.root_size:
            warn(
                f"runner {runner}: the upper directory and its mount root disagree on "
                "size; treating the topology as unsupported"
            )
            reason = (
                f"df reports {measurement.upper_size} B for {reading.host_path} but "
                f"{measurement.root_size} B for its mount root, which is a project quota"
            )
        elif reading.total != measurement.root_size:
            warn(
                f"runner {runner}: the container and the host disagree on the filesystem's "
                "size; treating the topology as unsupported"
            )
            reason = (
                f"the container reports {reading.total} B for its root filesystem but the "
                f"host reports {measurement.root_size} B for {measurement.mount_root}"
            )
        else:
            return None
        return VerdictEntry(
            "unsupported",
            measurement.mount_root,
            measurement.fstype,
            measurement.root_size,
            reason,
        )

    def _usage_agrees(
        self, runner: str, reading: ContainerReading, measurement: HostMeasurement
    ) -> tuple[bool, str]:
        tolerance = max(int(measurement.root_size) // 100, DIAGNOSTIC_FLOOR_BYTES)
        note = "the reading could not be taken"
        container_payload = reading.payload
        helper = measurement.helper
        for attempt in range(1, 4):
            if self._out_of_time():
                return (
                    False,
                    "the attestation ran out of its budget before the occupancy could be compared",
                )
            if attempt > 1:
                result = self.commands.run(
                    self.deadline,
                    [
                        "docker",
                        "exec",
                        runner,
                        "python",
                        "-m",
                        "cloud_agent_host.host_capacity",
                        "describe",
                    ],
                )
                container_payload = result.stdout if result.returncode == 0 else b""
                if measurement.helper_upper is not None:
                    helper = _run_helper(
                        self.commands, self.deadline, runner, measurement.helper_upper
                    ).reading
            container_line = _occupancy(container_payload)
            host_line = self._host_occupancy(measurement, helper)
            agrees, note = self._compare_occupancy(container_line, host_line, tolerance)
            if agrees:
                return True, note
            if attempt < 3:
                pause = min(int(self.deadline - self.monotonic()), 2)
                if pause > 0:
                    self.sleep(pause)
        return False, note

    def _host_occupancy(
        self, measurement: HostMeasurement, helper: HelperReading | None
    ) -> tuple[str, str] | None:
        if measurement.helper_upper is not None:
            if (
                helper is not None
                and helper.mount_root == measurement.mount_root
                and str(helper.total) == measurement.root_size
                and str(helper.upper_total) == measurement.root_size
            ):
                return str(helper.used), str(helper.available)
            return None
        result = self.commands.run(
            self.deadline,
            ["df", "-B1", "--output=used,avail", measurement.mount_root],
        )
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return None
        fields = lines[-1].split()
        return (fields[0], fields[1]) if len(fields) >= 2 else None

    @staticmethod
    def _compare_occupancy(
        container: tuple[str, str] | None,
        host: tuple[str, str] | None,
        tolerance: int,
    ) -> tuple[bool, str]:
        if container is None or host is None:
            return (
                False,
                f"a reading did not arrive within {CALL_TIMEOUT_SECONDS}s "
                "(the probe or df was cut short)",
            )
        values = (*container, *host)
        if not all(value.isdigit() for value in values):
            return False, "one of the two readings was not numeric"
        container_used, container_available, host_used, host_available = map(int, values)
        delta_used = abs(container_used - host_used)
        delta_available = abs(container_available - host_available)
        note = (
            f"used differs by {delta_used} B and available by {delta_available} B "
            f"(tolerance {tolerance} B)"
        )
        return delta_used <= tolerance and delta_available <= tolerance, note


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--verdict-file", type=Path, required=True)
    prepare.add_argument("--deployment-sha", default="")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--verdict-file", type=Path, required=True)
    verify.add_argument("--deployment-sha", default="")
    verify.add_argument("--daemon-json", type=Path, default=Path("/etc/docker/daemon.json"))
    verify.add_argument("--probe", required=True)
    verify.add_argument("--budget-seconds", type=int, required=True)
    verify.add_argument("--runner", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one deployment-facing attestation command."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        return 0 if write_verdict(arguments.verdict_file, arguments.deployment_sha, {}) else 1
    config = AttestationConfig(
        verdict_file=arguments.verdict_file,
        deployment_sha=arguments.deployment_sha,
        daemon_json=arguments.daemon_json,
        probe=arguments.probe,
        runners=tuple(arguments.runner),
        budget_seconds=arguments.budget_seconds,
    )
    return StorageAttestor(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
