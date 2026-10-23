"""Network-guard coverage for disposable and concurrently stopping workloads."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from tests.unit.test_chat_group_network import _world
from tests.unit.test_shared_session_supervisor import _Manager, _members, _spec

from cloud_agent_host.chat_sandbox_models import CHAT_PROBE_LABEL
from cloud_agent_host.environment_tests import environment_run_spec
from cloud_agent_host.guarded_workloads import running_guarded_workloads
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.errors import SandboxError


class _RecordingManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.exact_stops: list[tuple[str, str | None, bool]] = []

    def stop(self, spec, *, expected_container_id=None, release_networks=True):
        self.exact_stops.append((spec.sandbox_id, expected_container_id, release_networks))
        return super().stop(
            spec,
            expected_container_id=expected_container_id,
            release_networks=release_networks,
        )


def test_guard_discovery_includes_an_environment_run(monkeypatch) -> None:
    manager, docker, _guard = _world(monkeypatch)
    spec = environment_run_spec("run-1", 3, "sha256:image-pinned")
    docker.add_sandbox(spec, running=True)

    def run(argv, **kwargs):
        if argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(
                returncode=0,
                stdout="sandbox-container\t",
                stderr="",
            )
        return docker(argv, **kwargs)

    monkeypatch.setattr(manager, "_run", run)
    inventory = running_guarded_workloads(manager)

    assert inventory.complete is True
    assert inventory.unresolved_container_ids == ()
    assert inventory.workloads[0].inspection.labels["hybridinference.environment-test"] == "run-1"


def test_guard_discovery_ignores_a_networkless_qualification_probe() -> None:
    manager = _RecordingManager()
    probe = replace(_spec(), labels={CHAT_PROBE_LABEL: "1"})
    manager._run = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        returncode=0, stdout=f"{probe.container_name}\t1"
    )

    def unexpected_inspect(*_args, **_kwargs):
        raise AssertionError("qualification probe must not be inspected")

    manager.inspect = unexpected_inspect  # type: ignore[method-assign]

    inventory = running_guarded_workloads(manager)

    assert inventory.workloads == ()


def test_malformed_owned_workload_is_stopped_without_hiding_valid_workloads() -> None:
    manager = _RecordingManager()
    good = _spec()
    manager.items[good.container_name] = manager._inspection(good)
    listed = manager._run
    inspect = manager.inspect
    raw_stops: list[str] = []
    malformed_running = True

    def list_with_malformed(argv, **kwargs):
        nonlocal malformed_running
        if argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(
                returncode=0,
                stdout=("malformed-container\t\n" if malformed_running else "")
                + f"{good.container_name}\t",
                stderr="",
            )
        if argv[1] == "stop":
            raw_stops.append(argv[-1])
            malformed_running = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return listed(argv, **kwargs)

    def inspect_one(container_id):
        if container_id == "malformed-container":
            raise SandboxError("Docker returned an invalid inspection")
        return inspect(container_id)

    manager._run = list_with_malformed  # type: ignore[method-assign]
    manager.inspect = inspect_one  # type: ignore[method-assign]
    manager.host_rule_failures = 2

    assert SharedChatSessionSupervisor(manager).guard_network() == []

    assert raw_stops == ["malformed-container"]
    assert manager.host_rule_checks == 2
    assert manager.host_rule_renewals == 0
    assert manager.exact_stops == [(good.sandbox_id, "container-shared", True)]


def test_exact_stop_follows_a_renamed_original_not_its_name_replacement(monkeypatch) -> None:
    manager, docker, _guard = _world(monkeypatch)
    spec = _spec()
    original = docker.add_sandbox(spec, running=True)
    original["Name"] = "/renamed-original"
    replacement = docker.add_container(
        "replacement-container",
        name=spec.container_name,
        running=True,
        labels=original["labels"],
        image=original["image"],
    )

    stopped = manager.stop(spec, expected_container_id=original["Id"], release_networks=False)

    assert stopped.container_id == original["Id"]
    assert original["running"] is False
    assert replacement["running"] is True


def test_environment_run_only_host_is_checked_and_exact_stopped() -> None:
    manager = _RecordingManager()
    spec = environment_run_spec("run-1", 7, "sha256:image")
    inspection = replace(
        manager._inspection(spec),
        container_id="environment-container",
    )
    manager.items[spec.container_name] = inspection
    supervisor = SharedChatSessionSupervisor(manager)
    manager.host_rule_failures = 2

    assert supervisor.guard_network() == []

    assert manager.host_rule_running == [[spec], [spec]]
    assert manager.exact_stops == [(spec.sandbox_id, "environment-container", True)]
    stopped = manager.items[spec.container_name]
    assert stopped.running is False
    assert stopped.networks == ()


def test_structurally_invalid_host_rule_readback_stops_the_workload() -> None:
    manager = _RecordingManager()
    spec = environment_run_spec("run-1", 7, "sha256:image")
    inspection = replace(
        manager._inspection(spec),
        container_id="environment-container",
    )
    manager.items[spec.container_name] = inspection
    checks = 0

    def invalid_readback(_running) -> None:
        nonlocal checks
        checks += 1
        raise KeyError("nftables JSON has no table")

    manager.verify_host_rules = invalid_readback  # type: ignore[method-assign]

    assert SharedChatSessionSupervisor(manager).guard_network() == []

    assert checks == 2
    assert manager.exact_stops == [(spec.sandbox_id, "environment-container", True)]
    assert manager.items[spec.container_name].running is False


def test_failed_structured_stop_falls_back_to_the_raw_exact_id() -> None:
    manager = _RecordingManager()
    spec = environment_run_spec("run-1", 7, "sha256:image")
    inspection = replace(
        manager._inspection(spec),
        container_id="environment-container",
    )
    manager.items[spec.container_name] = inspection
    listed = manager._run
    raw_stops: list[str] = []

    def run(argv, **kwargs):
        if argv[1] == "stop":
            raw_stops.append(argv[-1])
            manager.items[spec.container_name] = replace(inspection, running=False)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return listed(argv, **kwargs)

    def invalid_structured_stop(
        stopped_spec,
        *,
        expected_container_id=None,
        release_networks=True,
    ):
        manager.exact_stops.append(
            (stopped_spec.sandbox_id, expected_container_id, release_networks)
        )
        raise AttributeError("Docker inspection has no State")

    manager._run = run  # type: ignore[method-assign]
    manager.stop = invalid_structured_stop  # type: ignore[method-assign]
    manager.host_rule_failures = 2

    assert SharedChatSessionSupervisor(manager).guard_network() == []

    assert manager.exact_stops == [(spec.sandbox_id, "environment-container", True)]
    assert raw_stops == ["environment-container"]
    assert manager.items[spec.container_name].running is False


def test_unavailable_workload_inventory_requires_fresh_rule_check() -> None:
    manager = _RecordingManager()

    manager._run = lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="")  # type: ignore[method-assign]
    supervisor = SharedChatSessionSupervisor(manager)

    assert supervisor.guard_network() == []

    assert manager.host_rule_checks == 1
    assert manager.host_rule_renewals == 0
    assert manager.exact_stops == []


def test_unavailable_inventory_and_failed_rule_check_stops_starting_group() -> None:
    manager = _RecordingManager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    group = supervisor._groups[_spec().sandbox_id]
    group.inspection = replace(group.inspection, running=False)
    group.starting = True
    manager._run = lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="")  # type: ignore[method-assign]
    manager.host_rule_failures = 2

    assert supervisor.guard_network() == []

    assert manager.host_rule_checks == 2
    assert manager.host_rule_renewals == 0
    assert manager.exact_stops == [(_spec().sandbox_id, "container-shared", True)]
    assert manager.items[_spec().container_name].running is False


def test_failed_malformed_stop_and_rule_check_exact_stops_known_id() -> None:
    manager = _RecordingManager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    listed = manager._run
    raw_stops: list[str] = []

    def run(argv, **kwargs):
        if argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(returncode=0, stdout="container-shared\t", stderr="")
        if argv[1] == "stop":
            raw_stops.append(argv[-1])
            if len(raw_stops) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="stop failed")
            manager.items[_spec().container_name] = replace(
                manager.items[_spec().container_name], running=False
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return listed(argv, **kwargs)

    def invalid_inspection(_container_id):
        raise AttributeError("Docker inspection has no State")

    manager._run = run  # type: ignore[method-assign]
    manager.inspect = invalid_inspection  # type: ignore[method-assign]

    def invalid_exact_stop(spec, *, expected_container_id=None, release_networks=True):
        manager.exact_stops.append((spec.sandbox_id, expected_container_id, release_networks))
        raise SandboxError("Docker returned an invalid inspection")

    manager.stop = invalid_exact_stop  # type: ignore[method-assign]
    manager.host_rule_failures = 2

    assert supervisor.guard_network() == []

    assert raw_stops == ["container-shared"] * 3
    assert manager.host_rule_checks == 2
    assert manager.exact_stops == [(_spec().sandbox_id, "container-shared", True)]
    assert manager.items[_spec().container_name].running is False


def test_post_failure_rescan_stops_a_same_name_replacement_too(caplog) -> None:
    manager = _RecordingManager()
    spec = environment_run_spec("run-1", 7, "sha256:image")
    original = replace(manager._inspection(spec), container_id="original-container")
    manager.items[spec.container_name] = original
    verify = manager.verify_host_rules

    def replace_after_check(running) -> None:
        try:
            verify(running)
        finally:
            if manager.host_rule_checks == 2:
                manager.items[spec.container_name] = replace(
                    original, container_id="replacement-container"
                )

    manager.verify_host_rules = replace_after_check  # type: ignore[method-assign]
    manager.host_rule_failures = 2
    supervisor = SharedChatSessionSupervisor(manager)

    assert supervisor.guard_network() == []

    current = manager.items[spec.container_name]
    assert current.container_id == "replacement-container"
    assert current.running is False
    assert manager.exact_stops == [
        (spec.sandbox_id, "original-container", True),
        (spec.sandbox_id, "replacement-container", True),
    ]
    assert any(
        getattr(record, "event", None) == "sandbox_network_exact_stop_failed"
        for record in caplog.records
    )


def test_post_failure_rescan_stops_a_new_environment_run() -> None:
    manager = _RecordingManager()
    first = environment_run_spec("run-1", 7, "sha256:image")
    second = environment_run_spec("run-2", 8, "sha256:image")
    first_inspection = replace(manager._inspection(first), container_id="environment-container-1")
    second_inspection = replace(manager._inspection(second), container_id="environment-container-2")
    manager.items[first.container_name] = first_inspection
    second_check = threading.Event()
    finish_check = threading.Event()
    errors: list[BaseException] = []
    verify = manager.verify_host_rules

    def pause_second_check(running) -> None:
        try:
            verify(running)
        finally:
            if manager.host_rule_checks == 2:
                second_check.set()
                assert finish_check.wait(timeout=5)

    manager.verify_host_rules = pause_second_check  # type: ignore[method-assign]
    manager.host_rule_failures = 2
    supervisor = SharedChatSessionSupervisor(manager)

    def guard() -> None:
        try:
            supervisor.guard_network()
        except BaseException as exc:
            errors.append(exc)

    guarding = threading.Thread(target=guard)
    guarding.start()
    assert second_check.wait(timeout=2)
    manager.items[second.container_name] = second_inspection
    finish_check.set()
    guarding.join(timeout=5)

    assert not guarding.is_alive()
    assert errors == []
    assert manager.items[first.container_name].running is False
    assert manager.items[second.container_name].running is False
    assert manager.exact_stops == [
        (first.sandbox_id, first_inspection.container_id, True),
        (second.sandbox_id, second_inspection.container_id, True),
    ]


def test_guard_exact_stops_a_stopping_group_then_its_archive_finishes() -> None:
    manager = _RecordingManager()
    prepare_entered = threading.Event()
    finish_prepare = threading.Event()
    archived: list[tuple[bool, tuple[str, ...]]] = []

    def prepare(_spec, inspection) -> None:
        assert inspection.running is True
        prepare_entered.set()
        assert finish_prepare.wait(timeout=5)

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_hook=lambda _spec, inspection, _members: archived.append(
            (inspection.running, inspection.networks)
        ),
    )
    supervisor.register_manifest(_spec(), _members())
    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    errors: list[BaseException] = []

    def stop_group() -> None:
        try:
            supervisor.stop_if_idle(
                "athr_source",
                generation=_spec().generation,
                reason="unguarded",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    stopping = threading.Thread(target=stop_group)
    stopping.start()
    assert prepare_entered.wait(timeout=2)
    assert supervisor._members["athr_source"].stopping is True

    manager.host_rule_failures = 2
    try:
        assert supervisor.guard_network() == []
        current = manager.items[_spec().container_name]
        assert current.running is False
        assert current.networks == ("agent-net",)
        assert manager.exact_stops[0] == (
            _spec().sandbox_id,
            "container-shared",
            False,
        )
    finally:
        finish_prepare.set()
        stopping.join(timeout=5)

    assert not stopping.is_alive()
    assert errors == []
    assert archived == [(False, ("agent-net",))]
    assert manager.items[_spec().container_name].networks == ()
