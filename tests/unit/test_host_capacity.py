"""The host capacity collector: formulas, bounds, and what "missing" means.

Two things are being pinned here, and the second matters more than the first.
The arithmetic has to be right — a CPU percentage that multiplies by the core
count, counts ``guest`` twice, or treats iowait as busy is a wrong number on a
page an operator uses to decide where to send work. But the *absence* of a
number has to be right too: every path that cannot measure something must say
so rather than report zero, because an idle machine and an unmeasured one look
identical on a chart and mean opposite things.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

import pytest

from cloud_agent_host import host_capacity
from cloud_agent_host.chat_sandbox import (
    CHAT_NAMESPACE_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL,
)
from cloud_agent_host.host_capacity import (
    CHAT_NAMESPACE_LABEL as MIRRORED_NAMESPACE_LABEL,
    CHAT_SANDBOX_ID_LABEL as MIRRORED_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL as MIRRORED_SANDBOX_LABEL,
    CPU_MISSING_COUNTER_RESET,
    CPU_MISSING_INVALID_SAMPLE,
    CPU_MISSING_SOURCE_UNAVAILABLE,
    CPU_MISSING_WARMING_UP,
    INVENTORY_AVAILABLE,
    INVENTORY_PARTIAL,
    INVENTORY_UNAVAILABLE,
    MAX_SAMPLE_AGE_MS,
    MEMORY_MISSING_INVALID_SAMPLE,
    MEMORY_MISSING_SOURCE_UNAVAILABLE,
    PROC_MAX_BYTES,
    REASON_DOCKER_LINE_LIMIT,
    REASON_DOCKER_MALFORMED_OUTPUT,
    REASON_DOCKER_OUTPUT_LIMIT,
    REASON_DOCKER_TIMEOUT,
    REASON_DOCKER_UNAVAILABLE,
    REASON_NAMESPACE_UNSET,
    REASON_UNKNOWN_STATE,
    REASON_UNSCOPED_LEGACY_CONTAINERS,
    STORAGE_MISSING_SOURCE_UNAVAILABLE,
    STORAGE_MISSING_UNSUPPORTED,
    STORAGE_MISSING_UNVERIFIED,
    VERDICT_MAX_BYTES,
    CommandResult,
    CommandStatus,
    HostCapacityCollector,
    OverlayRootStorageProbe,
    StorageMissing,
    StorageSample,
    build_inventory,
    build_storage_probe,
    capacity_enabled,
    describe_storage_probe,
    docker_list_argv,
    find_root_mount,
    main,
    parse_boot_id,
    parse_cpu_stat,
    parse_docker_rows,
    parse_meminfo,
    parse_storage_verdict,
    root_overlay_upperdir,
    run_bounded_command,
    storage_bytes_in_range,
    storage_usage,
)

NAMESPACE = "staging-agent"
BOOT_ID = "00000000-0000-4000-8000-00000000000a"


def cpu_line(
    *,
    user: int = 100,
    nice: int = 0,
    system: int = 100,
    idle: int = 800,
    iowait: int = 0,
    irq: int = 0,
    softirq: int = 0,
    steal: int = 0,
    guest: int = 0,
    guest_nice: int = 0,
) -> str:
    """Render one aggregate ``/proc/stat`` line with the ten documented columns."""
    return (
        f"cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} "
        f"{guest} {guest_nice}\n"
    )


def stat_text(*, cpus: int = 4, **counters: int) -> str:
    """Render a whole ``/proc/stat`` with ``cpus`` per-CPU lines."""
    body = cpu_line(**counters)
    return body + "".join(f"cpu{index} 1 0 1 2 0 0 0 0 0 0\n" for index in range(cpus))


def meminfo_text(*, total_kib: int = 1024, available_kib: int | None = 512) -> str:
    """Render a ``/proc/meminfo`` with, optionally, no ``MemAvailable``."""
    lines = [
        f"MemTotal:       {total_kib} kB",
        "MemFree:        1024 kB",
        "Buffers:           0 kB",
    ]
    if available_kib is not None:
        lines.append(f"MemAvailable:   {available_kib} kB")
    return "\n".join(lines) + "\n"


def listing_row(
    container_id: str,
    *,
    namespace: str | None = NAMESPACE,
    sandbox_id: str = "sandbox-1",
    state: str = "running",
) -> str:
    """Render one line of the collector's Docker listing."""
    return json.dumps(
        {
            "id": container_id,
            "namespace": namespace,
            "sandbox_id": sandbox_id,
            "state": state,
        }
    )


class FakeHost:
    """A host whose proc files and Docker answers the test controls."""

    def __init__(self, *, files: dict[str, str] | None = None, stdout: str = "") -> None:
        self.files = files if files is not None else {}
        self.stdout = stdout
        self.commands: list[list[str]] = []
        self.status = CommandStatus.OK
        self.now = 1000.0

    def read(self, name: str) -> str | None:
        return self.files.get(name)

    def run(self, argv: Any) -> CommandResult:
        self.commands.append(list(argv))
        return CommandResult(status=self.status, stdout=self.stdout)

    def collector(self, *, namespace: str = NAMESPACE, interval_s: float = 30.0):
        return HostCapacityCollector(
            namespace=namespace,
            reader=self.read,
            command_runner=self.run,
            clock=lambda: self.now,
            interval_s=interval_s,
        )


def healthy_files(**counters: int) -> dict[str, str]:
    """A host with all three proc files present."""
    return {
        "boot_id": BOOT_ID,
        "stat": stat_text(**counters),
        "meminfo": meminfo_text(),
    }


# ── The four labels the collector mirrors ─────────────────────────────


def test_the_mirrored_labels_match_the_sandbox_module() -> None:
    """The collector cannot import the sandbox stack, so the copy is checked.

    ``cloud_agent/capacity_protocol.py`` imports this module from the control
    plane, which must not drag in egress, container backends and guest
    observers just to read a CPU percentage. The constants are therefore
    duplicated — and a rename on one side would silently make every container
    foreign, which reads as an idle host rather than as a bug.
    """
    assert MIRRORED_SANDBOX_LABEL == CHAT_SANDBOX_LABEL
    assert MIRRORED_NAMESPACE_LABEL == CHAT_NAMESPACE_LABEL
    assert MIRRORED_SANDBOX_ID_LABEL == CHAT_SANDBOX_ID_LABEL


# ── Parsing ───────────────────────────────────────────────────────────


def test_cpu_stat_counts_guests_inside_user_and_nice() -> None:
    """guest/guest_nice are already in user/nice and must not be added again."""
    counters = parse_cpu_stat(
        stat_text(user=100, nice=10, system=50, idle=800, guest=60, guest_nice=5)
    )
    assert counters.fields == (100, 10, 50, 800, 0, 0, 0, 0)
    assert counters.total == 960
    assert counters.logical_count == 4


def test_cpu_stat_rejects_a_truncated_or_negative_line() -> None:
    with pytest.raises(ValueError):
        parse_cpu_stat("cpu  1 2 3\n")
    with pytest.raises(ValueError):
        parse_cpu_stat("cpu  1 -2 3 4 5 6 7 8\n")
    with pytest.raises(ValueError):
        parse_cpu_stat("cpu  1 2 3 4 5 6 7 8\n")


def test_meminfo_converts_kib_to_bytes_and_refuses_to_invent_available() -> None:
    assert parse_meminfo(meminfo_text(total_kib=2048, available_kib=512)) == (
        2048 * 1024,
        512 * 1024,
    )
    total, available = parse_meminfo(meminfo_text(available_kib=None))
    assert total == 1024 * 1024
    assert available is None


def test_boot_id_requires_a_uuid() -> None:
    assert parse_boot_id(BOOT_ID + "\n") == BOOT_ID
    assert parse_boot_id("not-a-uuid") is None
    assert parse_boot_id("") is None


# ── The shared formulas ───────────────────────────────────────────────


def test_cpu_percent_is_not_multiplied_by_the_core_count() -> None:
    """One busy core out of four is 25%, not 100% and not 400%."""
    assert host_capacity.cpu_busy_percent(delta_total=400, delta_idle=300) == pytest.approx(25.0)


def test_cpu_percent_excludes_iowait_and_includes_steal() -> None:
    """Steal is time this machine could not run; iowait is not CPU pressure."""
    # 100 ticks elapsed, 40 idle, 10 iowait, 50 busy (of which 10 stolen).
    assert host_capacity.cpu_busy_percent(delta_total=100, delta_idle=50) == pytest.approx(50.0)


def test_cpu_percent_refuses_an_impossible_interval() -> None:
    assert host_capacity.cpu_busy_percent(delta_total=0, delta_idle=0) is None
    assert host_capacity.cpu_busy_percent(delta_total=10, delta_idle=11) is None
    assert host_capacity.cpu_busy_percent(delta_total=10, delta_idle=-1) is None


def test_memory_usage_uses_mem_available() -> None:
    usage = host_capacity.memory_usage(total_bytes=1000, available_bytes=250)
    assert usage is not None
    assert usage.used_bytes == 750
    assert usage.used_percent == pytest.approx(75.0)
    assert host_capacity.memory_usage(total_bytes=0, available_bytes=0) is None
    assert host_capacity.memory_usage(total_bytes=100, available_bytes=101) is None
    assert host_capacity.memory_usage(total_bytes=100, available_bytes=None) is None


# ── Sampling: CPU ─────────────────────────────────────────────────────


def test_the_first_sample_warms_up_but_memory_is_immediate() -> None:
    """CPU needs two samples; memory does not, and must not wait for one."""
    host = FakeHost(files=healthy_files())
    sample = host.collector().sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_WARMING_UP
    assert sample.cpu_logical_count == 4
    assert sample.memory_total_bytes == 1024 * 1024
    assert sample.memory_available_bytes == 512 * 1024
    assert sample.memory_missing_reason is None


def test_the_second_sample_reports_the_busy_share_of_the_interval() -> None:
    host = FakeHost(files=healthy_files(user=100, system=100, idle=800))
    collector = host.collector()
    collector.sample()
    host.now += 30.0
    # Four CPUs over thirty seconds account for 120 ticks; thirty of them busy
    # is 25% of the machine, and the denominator is the elapsed ticks rather
    # than four times anything.
    host.files["stat"] = stat_text(user=115, system=115, idle=890)
    sample = collector.sample()
    assert sample.cpu_percent == pytest.approx(25.0)
    assert sample.cpu_window_seconds == pytest.approx(30.0)
    assert sample.cpu_missing_reason is None


def test_a_rebooted_host_rebuilds_the_baseline_instead_of_averaging() -> None:
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    host.now += 30.0
    host.files["boot_id"] = "00000000-0000-4000-8000-00000000000b"
    sample = collector.sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_COUNTER_RESET
    # Memory is unaffected: it is a reading, not a delta.
    assert sample.memory_available_bytes == 512 * 1024


def test_a_hot_plug_changes_the_baseline_not_the_denominator() -> None:
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    host.now += 30.0
    host.files["stat"] = stat_text(cpus=5, user=115, system=115, idle=890)
    sample = collector.sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_COUNTER_RESET
    assert sample.cpu_logical_count == 5


def test_counters_that_go_backwards_rebuild_the_baseline() -> None:
    host = FakeHost(files=healthy_files(idle=800))
    collector = host.collector()
    collector.sample()
    host.now += 30.0
    host.files["stat"] = stat_text(idle=700)
    sample = collector.sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_COUNTER_RESET


def test_a_gap_longer_than_the_window_rebuilds_the_baseline() -> None:
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    host.now += 91.0
    host.files["stat"] = stat_text(user=115, system=115, idle=890)
    sample = collector.sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_COUNTER_RESET


def test_a_zero_denominator_is_an_invalid_sample_not_a_zero_percent() -> None:
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    host.now += 30.0
    # Same counters, thirty seconds later: nothing was accounted.
    sample = collector.sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_INVALID_SAMPLE


def test_an_unreadable_proc_file_is_missing_rather_than_zero() -> None:
    host = FakeHost(files={"boot_id": BOOT_ID, "meminfo": meminfo_text()})
    sample = host.collector().sample()
    assert sample.cpu_percent is None
    assert sample.cpu_missing_reason == CPU_MISSING_SOURCE_UNAVAILABLE
    assert sample.memory_total_bytes is not None


def test_without_a_boot_id_neither_cpu_nor_memory_is_reported() -> None:
    host = FakeHost(files={"stat": stat_text(), "meminfo": meminfo_text()})
    sample = host.collector().sample()
    assert sample.linux_boot_id is None
    assert sample.cpu_missing_reason == CPU_MISSING_SOURCE_UNAVAILABLE
    assert sample.memory_missing_reason == MEMORY_MISSING_SOURCE_UNAVAILABLE
    assert sample.memory_total_bytes is None


def test_memory_beyond_its_own_total_is_invalid() -> None:
    host = FakeHost(
        files={"boot_id": BOOT_ID, "meminfo": meminfo_text(total_kib=100, available_kib=200)}
    )
    sample = host.collector().sample()
    assert sample.memory_total_bytes is None
    assert sample.memory_missing_reason == MEMORY_MISSING_INVALID_SAMPLE


# ── Sampling: the container inventory ─────────────────────────────────


def test_a_fork_shares_one_container_and_is_counted_once() -> None:
    """The count is of entities. Two members in one container are one VM."""
    rows = parse_docker_rows(listing_row("a" * 64, sandbox_id="shared-group"))
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.status == INVENTORY_AVAILABLE
    assert inventory.running == 1
    assert inventory.duplicate_sandbox_id is False


def test_two_containers_for_one_sandbox_id_are_both_counted() -> None:
    """A duplicate is an anomaly, not a rounding opportunity: both hold memory."""
    rows = parse_docker_rows(
        "\n".join(
            [
                listing_row("a" * 64, sandbox_id="shared-group"),
                listing_row("b" * 64, sandbox_id="shared-group"),
            ]
        )
    )
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.running == 2
    assert inventory.duplicate_sandbox_id is True


def test_a_foreign_namespace_is_not_this_deployments_business() -> None:
    rows = parse_docker_rows(
        "\n".join(
            [
                listing_row("a" * 64, namespace="some-other-stack"),
                listing_row("b" * 64, namespace=NAMESPACE),
            ]
        )
    )
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.running == 1
    assert inventory.status == INVENTORY_AVAILABLE


def test_an_unscoped_container_makes_the_count_a_lower_bound() -> None:
    rows = parse_docker_rows(
        "\n".join(
            [
                listing_row("a" * 64, namespace=None),
                listing_row("b" * 64, namespace=NAMESPACE, state="exited"),
            ]
        )
    )
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.status == INVENTORY_PARTIAL
    assert inventory.reason == REASON_UNSCOPED_LEGACY_CONTAINERS
    assert inventory.running == 0
    assert inventory.stopped == 1
    assert inventory.unscoped_legacy_containers == 1


def test_paused_is_not_folded_into_stopped() -> None:
    """A paused container has not released its memory."""
    rows = parse_docker_rows(listing_row("a" * 64, state="paused"))
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.stopped == 0
    assert inventory.other_states["paused"] == 1


def test_an_unknown_state_makes_the_inventory_partial() -> None:
    rows = parse_docker_rows(listing_row("a" * 64, state="hibernating"))
    inventory = build_inventory(rows, namespace=NAMESPACE)
    assert inventory.status == INVENTORY_PARTIAL
    assert inventory.reason == REASON_UNKNOWN_STATE


def test_every_state_is_reported_at_a_fixed_key() -> None:
    inventory = build_inventory([], namespace=NAMESPACE)
    assert inventory.as_payload()["other_states"] == {
        "created": 0,
        "paused": 0,
        "restarting": 0,
        "removing": 0,
        "dead": 0,
    }


def test_only_a_complete_empty_listing_is_zero() -> None:
    host = FakeHost(files=healthy_files(), stdout="")
    sample = host.collector().sample()
    assert sample.inventory.status == INVENTORY_AVAILABLE
    assert sample.inventory.running == 0
    assert sample.inventory.stopped == 0


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CommandStatus.TIMEOUT, REASON_DOCKER_TIMEOUT),
        (CommandStatus.OUTPUT_LIMIT, REASON_DOCKER_OUTPUT_LIMIT),
        (CommandStatus.LINE_LIMIT, REASON_DOCKER_LINE_LIMIT),
        (CommandStatus.UNAVAILABLE, REASON_DOCKER_UNAVAILABLE),
    ],
)
def test_a_failed_listing_is_unavailable_and_reports_no_counts(
    status: CommandStatus, reason: str
) -> None:
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64))
    host.status = status
    sample = host.collector().sample()
    assert sample.inventory.status == INVENTORY_UNAVAILABLE
    assert sample.inventory.reason == reason
    assert sample.inventory.running is None
    assert sample.inventory.as_payload()["other_states"] == dict.fromkeys(
        ("created", "paused", "restarting", "removing", "dead")
    )
    # The CPU and memory halves are independent and still reported.
    assert sample.memory_available_bytes == 512 * 1024


def test_a_malformed_listing_invalidates_the_whole_inventory() -> None:
    """A parsed prefix is a smaller number that looks like a real one."""
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64) + "\n{not json}\n")
    sample = host.collector().sample()
    assert sample.inventory.status == INVENTORY_UNAVAILABLE
    assert sample.inventory.reason == REASON_DOCKER_MALFORMED_OUTPUT


def test_an_unset_namespace_reports_nothing_rather_than_everything() -> None:
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64, namespace=None))
    sample = host.collector(namespace="").sample()
    assert sample.inventory.status == INVENTORY_UNAVAILABLE
    assert sample.inventory.reason == REASON_NAMESPACE_UNSET
    assert host.commands == []


def test_the_listing_is_one_read_only_argument_vector() -> None:
    argv = docker_list_argv(docker_binary="docker")
    assert argv[:4] == ["docker", "container", "ls", "--all"]
    assert "--size" not in argv
    assert "inspect" not in argv
    assert f"label={CHAT_SANDBOX_LABEL}=true" in argv


# ── Publishing: age and bounds ────────────────────────────────────────


def test_nothing_is_published_before_the_first_sample() -> None:
    host = FakeHost(files=healthy_files())
    payload, age_ms = host.collector().latest()
    assert payload is None
    assert age_ms is None


def test_the_published_age_is_recomputed_not_re_stamped() -> None:
    """The age is how old the observation is, measured at send time."""
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    assert collector.latest()[1] == 0
    host.now += 12.5
    payload, age_ms = collector.latest()
    assert age_ms == 12_500
    assert payload is not None
    assert payload["sample_age_ms"] == 12_500


def test_an_observation_too_old_to_send_is_withheld() -> None:
    """A stalled collector reads as stale, which is the truth."""
    host = FakeHost(files=healthy_files())
    collector = host.collector()
    collector.sample()
    host.now += (MAX_SAMPLE_AGE_MS + 1) / 1000.0
    assert collector.latest() == (None, None)


def test_the_payload_carries_the_v1_shape() -> None:
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64))
    collector = host.collector()
    collector.sample()
    payload, _ = collector.latest()
    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["source"] == "linux_host_proc_and_docker_list"
    assert payload["linux_boot_id"] == BOOT_ID
    assert payload["inventory"]["namespace"] == NAMESPACE
    assert set(payload["missing"]) == {"cpu", "memory", "storage"}
    # No probe was declared on this collector, which is what "storage is off"
    # looks like on the wire: no sub-block, and a reason the page can explain.
    assert payload["storage"] is None
    assert payload["missing"]["storage"] == "disabled"
    assert json.loads(json.dumps(payload)) == payload


# ── The enable switch ─────────────────────────────────────────────────


def test_capacity_is_enabled_unless_explicitly_turned_off() -> None:
    assert capacity_enabled({}) is True
    assert capacity_enabled({"AGENT_HOST_CAPACITY_ENABLED": ""}) is True
    assert capacity_enabled({"AGENT_HOST_CAPACITY_ENABLED": "1"}) is True
    for value in ("0", "false", "FALSE", "no", "off"):
        assert capacity_enabled({"AGENT_HOST_CAPACITY_ENABLED": value}) is False


# ── The bounded child process ─────────────────────────────────────────


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_missing_binary_is_unavailable_rather_than_an_exception() -> None:
    result = run_bounded_command(
        ["definitely-not-a-real-binary-xyz"],
        timeout_s=2.0,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        max_lines=10,
    )
    assert result.status is CommandStatus.UNAVAILABLE


def test_a_slow_child_is_killed_at_the_deadline() -> None:
    started = time.monotonic()
    result = run_bounded_command(
        _python("import time; time.sleep(30)"),
        timeout_s=0.4,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        max_lines=10,
    )
    assert result.status is CommandStatus.TIMEOUT
    assert time.monotonic() - started < 10.0


def test_output_is_capped_while_being_read_not_after() -> None:
    result = run_bounded_command(
        _python("import sys\nsys.stdout.write('x' * (8 * 1024 * 1024))"),
        timeout_s=5.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=1024,
        max_lines=4096,
    )
    assert result.status is CommandStatus.OUTPUT_LIMIT
    # A capped run returns nothing at all: half a listing is a smaller fleet.
    assert result.stdout == ""


def test_a_line_flood_is_capped_too() -> None:
    result = run_bounded_command(
        _python("import sys\nfor _ in range(100000): sys.stdout.write('{}\\n')"),
        timeout_s=5.0,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024,
        max_lines=100,
    )
    assert result.status is CommandStatus.LINE_LIMIT


def test_a_successful_child_returns_its_stdout() -> None:
    result = run_bounded_command(
        _python('print(\'{"id": "abc"}\')'),
        timeout_s=5.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=1024,
        max_lines=10,
    )
    assert result.status is CommandStatus.OK
    assert result.stdout.strip() == '{"id": "abc"}'


# ── The loop itself ───────────────────────────────────────────────────


def test_a_failing_sample_does_not_stop_the_sampler() -> None:
    """A thread that dies is never restarted; the page would freeze for good."""

    class Exploding:
        def __init__(self) -> None:
            self.calls = 0

        def read(self, name: str) -> str | None:
            self.calls += 1
            raise RuntimeError("boom")

    exploding = Exploding()
    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        reader=exploding.read,
        command_runner=lambda argv: CommandResult(status=CommandStatus.UNAVAILABLE),
        interval_s=0.01,
    )
    collector.start()
    time.sleep(0.2)
    collector.stop()
    assert exploding.calls > 1


# ── The real file reader ──────────────────────────────────────────────


def test_the_default_reader_reads_only_the_three_mounted_files(tmp_path) -> None:
    """The mount point is a contract; nothing else in it is read."""
    (tmp_path / "boot_id").write_text(BOOT_ID + "\n")
    (tmp_path / "stat").write_text(stat_text())
    (tmp_path / "meminfo").write_text(meminfo_text())

    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        proc_root=tmp_path,
        command_runner=lambda argv: CommandResult(status=CommandStatus.OK, stdout=""),
    )
    sample = collector.sample()

    assert sample.linux_boot_id == BOOT_ID
    assert sample.memory_total_bytes == 1024 * 1024
    assert sample.inventory.status == INVENTORY_AVAILABLE


def test_a_proc_file_that_is_not_utf8_is_missing_rather_than_replaced(tmp_path) -> None:
    """Bad bytes must not become a number nobody can trace."""
    (tmp_path / "boot_id").write_text(BOOT_ID)
    (tmp_path / "stat").write_bytes(b"cpu  \xff\xfe 1 2 3 4 5 6\n")
    (tmp_path / "meminfo").write_text(meminfo_text())

    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        proc_root=tmp_path,
        command_runner=lambda argv: CommandResult(status=CommandStatus.OK, stdout=""),
    )
    sample = collector.sample()

    assert sample.cpu_missing_reason == CPU_MISSING_SOURCE_UNAVAILABLE
    # The other half is a separate file and is unaffected.
    assert sample.memory_total_bytes == 1024 * 1024


def test_an_oversized_proc_file_is_refused(tmp_path) -> None:
    """A proc file is a few kilobytes; an enormous one is not that file."""
    (tmp_path / "boot_id").write_text(BOOT_ID)
    (tmp_path / "stat").write_text("cpu  1 2 3 4 5 6 7 8\n" + "x" * (PROC_MAX_BYTES + 1))
    (tmp_path / "meminfo").write_text(meminfo_text())

    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        proc_root=tmp_path,
        command_runner=lambda argv: CommandResult(status=CommandStatus.OK, stdout=""),
    )

    assert collector.sample().cpu_missing_reason == CPU_MISSING_SOURCE_UNAVAILABLE


def test_a_file_that_cannot_be_opened_is_missing(tmp_path) -> None:
    """A missing mount is the deployment's business, not an exception."""
    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        proc_root=tmp_path / "not-mounted",
        command_runner=lambda argv: CommandResult(status=CommandStatus.OK, stdout=""),
    )
    sample = collector.sample()

    assert sample.linux_boot_id is None
    assert sample.cpu_missing_reason == CPU_MISSING_SOURCE_UNAVAILABLE
    assert sample.memory_missing_reason == MEMORY_MISSING_SOURCE_UNAVAILABLE


# ── Storage: the probe, its verdict, and the numbers it will not invent ──

GIB = 1024**3
UPPERDIR = "/mnt/agent-docker/overlay2/9f1c/diff"
MOUNT_ROOT = "/mnt/agent-docker"


def overlay_mountinfo(upperdir: str | None = UPPERDIR, *, fstype: str = "overlay") -> str:
    """A mount table whose ``/`` record is an overlay with the given upper layer."""
    options = "rw,lowerdir=/mnt/agent-docker/overlay2/l/AAAA"
    if upperdir is not None:
        options += f",upperdir={upperdir},workdir={upperdir.rsplit('/', 1)[0]}/work"
    return (
        "# a second mount, to prove the lookup is by mount point\n"
        "24 30 0:22 / /proc rw,nosuid - proc proc rw\n"
        f"36 35 98:0 / / rw,relatime - {fstype} overlay {options}\n"
    )


def verdict_document(entries: dict[str, dict], *, schema_version: int = 1) -> str:
    """The deployment's attestation document, in the shape the script writes."""
    return json.dumps(
        {
            "schema_version": schema_version,
            "written_at": "2026-09-16T08:00:00Z",
            "deployment_sha": "3a191e5",
            "entries": entries,
        }
    )


class FakeStatvfs:
    """A ``statvfs`` result built from the four fields the probe reads."""

    def __init__(
        self,
        *,
        size: int = 100 * GIB,
        free: int = 40 * GIB,
        available: int = 35 * GIB,
        frsize: int = 4096,
    ) -> None:
        self.f_frsize = frsize
        self.f_blocks = size // frsize
        self.f_bfree = free // frsize
        self.f_bavail = available // frsize


def storage_probe(
    *,
    mountinfo: str | None = None,
    verdict: str | None = None,
    statvfs: Any = None,
) -> OverlayRootStorageProbe:
    """A probe over the three inputs a container's own readings come from."""
    return OverlayRootStorageProbe(
        mountinfo_reader=lambda: overlay_mountinfo() if mountinfo is None else mountinfo,
        verdict_reader=lambda: verdict,
        statvfs=statvfs if statvfs is not None else (lambda path: FakeStatvfs()),
    )


def verified_verdict(*, size: int = 100 * GIB, upperdir: str = UPPERDIR) -> str:
    return verdict_document(
        {
            upperdir: {
                "verdict": "verified",
                "mount_root": MOUNT_ROOT,
                "fstype": "xfs",
                "size_bytes": size,
                "reason": None,
            }
        }
    )


def test_the_root_record_is_selected_by_mount_point_not_by_position() -> None:
    """One lookup, not a mount-table parser: ``/`` is the only record asked for."""
    mount = find_root_mount(overlay_mountinfo())

    assert mount is not None
    assert mount.mount_point == "/"
    assert mount.fstype == "overlay"
    assert mount.upperdir == UPPERDIR
    assert root_overlay_upperdir(overlay_mountinfo()) == UPPERDIR


def test_a_root_that_is_not_an_overlay_cannot_be_measured() -> None:
    assert storage_probe(mountinfo=overlay_mountinfo(fstype="ext4")).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNSUPPORTED
    )


def test_an_overlay_without_an_upper_layer_cannot_be_measured() -> None:
    assert storage_probe(mountinfo=overlay_mountinfo(upperdir=None)).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNSUPPORTED
    )


def test_a_mount_table_that_cannot_be_read_is_source_unavailable() -> None:
    """Not knowing the conditions is a different statement from knowing they fail."""
    probe = OverlayRootStorageProbe(
        mountinfo_reader=lambda: None,
        verdict_reader=lambda: verified_verdict(),
        statvfs=lambda path: FakeStatvfs(),
    )

    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)


def test_a_failing_statvfs_is_source_unavailable() -> None:
    def explode(path: str) -> Any:
        raise OSError("device is gone")

    assert storage_probe(verdict=verified_verdict(), statvfs=explode).sample() == StorageMissing(
        reason=STORAGE_MISSING_SOURCE_UNAVAILABLE
    )


def test_a_reading_outside_the_wire_range_is_source_unavailable() -> None:
    """``available`` above ``free`` cannot happen; it is not forwarded anyway."""
    impossible = FakeStatvfs(size=100 * GIB, free=10 * GIB, available=20 * GIB)

    assert storage_probe(verdict=verified_verdict(), statvfs=lambda path: impossible).sample() == (
        StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)
    )


def test_a_verified_reading_is_published_with_the_attested_mount_root() -> None:
    sample = storage_probe(verdict=verified_verdict()).sample()

    assert isinstance(sample, StorageSample)
    assert sample.total_bytes == 100 * GIB
    assert sample.free_bytes == 40 * GIB
    assert sample.available_bytes == 35 * GIB
    assert sample.target == MOUNT_ROOT


def test_a_missing_verdict_withholds_the_number() -> None:
    """The failure direction of a monitoring field is silence, never a number."""
    assert storage_probe(verdict=None).sample() == StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)


def test_each_sample_reopens_the_verdict_after_unlink_and_replacement(tmp_path) -> None:
    """A long-lived probe must observe the current file, not cache a verdict or FD."""
    verdict_file = tmp_path / "verdict.json"
    verdict_file.write_text(verified_verdict())
    probe = OverlayRootStorageProbe(
        verdict_path=verdict_file,
        mountinfo_reader=lambda: overlay_mountinfo(),
        statvfs=lambda path: FakeStatvfs(),
    )
    reading = probe.sample()
    assert isinstance(reading, StorageSample)

    verdict_file.unlink()
    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)
    verdict_file.write_text(verdict_document({}))
    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(verified_verdict())
    replacement.replace(verdict_file)
    assert probe.sample() == reading


@pytest.mark.parametrize(
    "document",
    [
        "",
        "not json at all",
        "[]",
        '{"schema_version":2,"entries":{}}',
        '{"schema_version":1}',
        '{"schema_version":1,"entries":[]}',
    ],
)
def test_an_unusable_verdict_document_withholds_the_number(document: str) -> None:
    assert parse_storage_verdict(document) is None
    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_an_entry_that_is_not_an_object_is_skipped_not_believed() -> None:
    """A corrupt sibling row must not be read as a verdict, or take one away."""
    document = verdict_document({UPPERDIR: "verified"})  # type: ignore[dict-item]

    assert parse_storage_verdict(document) == {}
    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_an_oversized_verdict_file_is_refused(tmp_path) -> None:
    """The ceiling is what stops a corrupt file from being read every cycle."""
    verdict_file = tmp_path / "verdict"
    verdict_file.write_text(verified_verdict() + " " * VERDICT_MAX_BYTES)
    probe = OverlayRootStorageProbe(
        verdict_path=verdict_file,
        mountinfo_reader=lambda: overlay_mountinfo(),
        statvfs=lambda path: FakeStatvfs(),
    )

    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)


def test_a_verdict_for_another_container_does_not_apply() -> None:
    """A rebuilt container gets a new upper directory, so the old entry expires."""
    document = verified_verdict(upperdir="/mnt/agent-docker/overlay2/other/diff")

    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_an_entry_that_says_unsupported_is_reported_as_unsupported() -> None:
    document = verdict_document(
        {
            UPPERDIR: {
                "verdict": "unsupported",
                "mount_root": MOUNT_ROOT,
                "fstype": "xfs",
                "size_bytes": 100 * GIB,
                "reason": "df reports a quota",
            }
        }
    )

    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNSUPPORTED
    )


def test_an_entry_that_says_unverified_stays_unverified() -> None:
    document = verdict_document({UPPERDIR: {"verdict": "unverified", "size_bytes": 100 * GIB}})

    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_an_unknown_verdict_value_withholds_the_number() -> None:
    """A docment from a newer script is not a licence to publish anything."""
    document = verdict_document({UPPERDIR: {"verdict": "probably-fine", "size_bytes": 100 * GIB}})

    assert storage_probe(verdict=document).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_a_resized_filesystem_needs_a_new_verdict() -> None:
    """Resizing or replacing the backend changes the size the host attested."""
    growing = verified_verdict(size=200 * GIB)

    assert storage_probe(verdict=growing).sample() == StorageMissing(
        reason=STORAGE_MISSING_UNVERIFIED
    )


def test_a_malformed_sibling_entry_does_not_take_this_containers_entry_away() -> None:
    document = json.loads(verified_verdict())
    document["entries"]["/other/diff"] = "not-an-object"
    document["entries"]["/third/diff"] = {"mount_root": MOUNT_ROOT}

    entries = parse_storage_verdict(json.dumps(document))

    assert entries is not None
    assert set(entries) == {UPPERDIR}
    assert storage_probe(verdict=json.dumps(document)).sample() == StorageSample(
        total_bytes=100 * GIB,
        free_bytes=40 * GIB,
        available_bytes=35 * GIB,
        target=MOUNT_ROOT,
    )


def test_storage_usage_keeps_the_reserved_blocks_out_of_used() -> None:
    """``used = total - free``, over ``used + available`` — GNU df's own ratio."""
    usage = storage_usage(total_bytes=100 * GIB, free_bytes=40 * GIB, available_bytes=35 * GIB)

    assert usage is not None
    assert usage.used_bytes == 60 * GIB
    assert usage.used_percent == pytest.approx(100 * 60 / 95)


@pytest.mark.parametrize(
    ("total", "free", "available"),
    [
        (None, 1, 1),
        (100, None, 1),
        (100, 1, None),
        (100, 20, 30),  # available above free
        (100, 200, 10),  # free above total
        (100, 100, 0),  # every block reserved and none written: no denominator
        (0, 0, 0),  # an empty filesystem has no percentage either
    ],
)
def test_storage_usage_refuses_a_triple_it_cannot_turn_into_a_ratio(
    total: int | None, free: int | None, available: int | None
) -> None:
    assert storage_usage(total_bytes=total, free_bytes=free, available_bytes=available) is None


def test_storage_usage_reports_a_full_and_an_empty_filesystem() -> None:
    full = storage_usage(total_bytes=100, free_bytes=0, available_bytes=0)
    empty = storage_usage(total_bytes=100, free_bytes=100, available_bytes=100)

    assert full is not None and full.used_bytes == 100 and full.used_percent == 100.0
    assert empty is not None and empty.used_bytes == 0 and empty.used_percent == 0.0


def test_the_wire_range_admits_zero_and_the_json_integer_ceiling() -> None:
    assert storage_bytes_in_range(total_bytes=0, free_bytes=0, available_bytes=0)
    assert storage_bytes_in_range(
        total_bytes=2**53 - 1, free_bytes=2**53 - 1, available_bytes=2**53 - 1
    )
    assert not storage_bytes_in_range(total_bytes=2**53, free_bytes=0, available_bytes=0)
    assert not storage_bytes_in_range(total_bytes=10, free_bytes=-1, available_bytes=0)


def test_the_probe_switch_defaults_to_the_root_overlay_and_off_means_off() -> None:
    assert isinstance(build_storage_probe({}), OverlayRootStorageProbe)
    assert isinstance(
        build_storage_probe({"AGENT_HOST_STORAGE_PROBE": "overlay_root"}), OverlayRootStorageProbe
    )
    assert build_storage_probe({"AGENT_HOST_STORAGE_PROBE": "off"}) is None
    assert build_storage_probe({"AGENT_HOST_STORAGE_PROBE": "OFF"}) is None


def test_an_unknown_probe_name_reports_unsupported_instead_of_measuring_root() -> None:
    probe = build_storage_probe({"AGENT_HOST_STORAGE_PROBE": "zfs_dataset"})

    assert probe is not None
    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_UNSUPPORTED)


def test_the_describe_command_reports_the_same_reading_the_probe_would(tmp_path) -> None:
    """The deployment attests with this document; a second implementation would drift."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(overlay_mountinfo())
    described = describe_storage_probe(mountinfo_path=mountinfo, statvfs=lambda path: FakeStatvfs())
    probe = OverlayRootStorageProbe(
        mountinfo_path=mountinfo,
        verdict_reader=lambda: verified_verdict(),
        statvfs=lambda path: FakeStatvfs(),
    )
    sampled = probe.sample()

    assert described["ok"] is True
    assert described["fstype"] == "overlay"
    assert described["upperdir"] == UPPERDIR
    assert described["total_bytes"] == 100 * GIB
    assert isinstance(sampled, StorageSample)
    assert sampled.total_bytes == described["total_bytes"]
    assert sampled.free_bytes == described["free_bytes"]
    assert sampled.available_bytes == described["available_bytes"]


@pytest.mark.parametrize(
    "trailer",
    [
        pytest.param(b"\xe9\n", id="a byte no UTF-8 decoder accepts"),
        pytest.param(b"#" * PROC_MAX_BYTES, id="a mount table past the size ceiling"),
    ],
)
def test_a_mount_table_the_probe_refuses_is_one_the_deployment_refuses(
    tmp_path, trailer: bytes
) -> None:
    """Both readers, one answer.

    The deployment writes a `verified` entry for whatever `describe` reports,
    and the runner publishes whatever it can sample. When one reader tolerates
    a mount table the other rejects, the rollout log says the disk was verified
    while the page says it was never measured, and nothing on either side
    explains the difference.
    """
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(overlay_mountinfo().encode() + trailer)
    described = describe_storage_probe(mountinfo_path=mountinfo, statvfs=lambda path: FakeStatvfs())
    probe = OverlayRootStorageProbe(
        mountinfo_path=mountinfo,
        verdict_reader=lambda: verified_verdict(),
        statvfs=lambda path: FakeStatvfs(),
    )

    assert described["ok"] is False
    assert described["reason"] == STORAGE_MISSING_SOURCE_UNAVAILABLE
    assert probe.sample() == StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)


def test_the_module_command_line_runs_with_every_definition_it_needs() -> None:
    """`python -m` stops at the `__main__` block, so its position is behavior.

    Run as a subprocess, because importing this module for a test defines the
    whole of it either way: only the real command proves that nothing the
    describe path reaches is defined after the block that starts it.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "cloud_agent_host.host_capacity", "describe"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert isinstance(json.loads(completed.stdout)["ok"], bool)


def test_the_describe_command_says_why_when_the_topology_is_not_supported(tmp_path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(overlay_mountinfo(fstype="ext4"))
    described = describe_storage_probe(mountinfo_path=mountinfo, statvfs=lambda path: FakeStatvfs())

    assert described["ok"] is False
    assert described["reason"] == STORAGE_MISSING_UNSUPPORTED
    assert described["fstype"] == "ext4"


def test_the_describe_cli_prints_json_and_refuses_unknown_arguments(capsys) -> None:
    assert main(["describe"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] in (True, False)
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


# ── Storage in the collector ──────────────────────────────────────────


def test_the_collector_publishes_the_probe_reading_with_its_source() -> None:
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64))
    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        reader=host.read,
        command_runner=host.run,
        storage_probe=storage_probe(verdict=verified_verdict()),
        clock=lambda: host.now,
    )

    payload = collector.sample().as_payload()
    storage = payload["storage"]

    assert storage["source"] == "linux_overlay_root_statvfs"
    assert storage["target"] == MOUNT_ROOT
    assert storage["total_bytes"] == 100 * GIB
    assert storage["free_bytes"] == 40 * GIB
    assert storage["available_bytes"] == 35 * GIB
    assert payload["missing"]["storage"] is None


def test_a_round_without_a_sample_carries_the_reason_and_no_numbers() -> None:
    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64))
    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        reader=host.read,
        command_runner=host.run,
        storage_probe=storage_probe(verdict=None),
        clock=lambda: host.now,
    )

    payload = collector.sample().as_payload()

    assert payload["storage"] is None
    assert payload["missing"]["storage"] == STORAGE_MISSING_UNVERIFIED
    # A missing storage reading is not a missing host: memory is instantaneous
    # and the container count came from the same round.
    assert payload["memory_total_bytes"] is not None
    assert payload["inventory"]["status"] == "available"


def test_a_probe_that_raises_costs_only_its_own_field() -> None:
    class Exploding:
        source = "exploding"

        def sample(self) -> StorageSample:
            raise RuntimeError("probe is broken")

    host = FakeHost(files=healthy_files(), stdout=listing_row("a" * 64))
    collector = HostCapacityCollector(
        namespace=NAMESPACE,
        reader=host.read,
        command_runner=host.run,
        storage_probe=Exploding(),
        clock=lambda: host.now,
    )

    sample = collector.sample()

    assert sample.storage_missing_reason == STORAGE_MISSING_SOURCE_UNAVAILABLE
    assert sample.memory_total_bytes is not None
    assert sample.inventory.status == INVENTORY_AVAILABLE
