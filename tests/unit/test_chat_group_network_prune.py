"""The orphan-bridge prune: which of this namespace's bridges it removes, and how.

It lists only this namespace's bridges, inspects them together, falls back to
one at a time when that fails, and removes only a stale bridge that no retained
chat or running environment test still uses. Against the model of Docker in
:mod:`tests.unit.test_chat_group_network`.
"""

from __future__ import annotations

import logging

from tests.unit.test_chat_group_network import (
    _NAMESPACE,
    _group_bridge,
    _owned_labels,
    _spec,
    _world,
)

from cloud_agent_host.chat_sandbox import CHAT_NAMESPACE_LABEL, CHAT_NETWORK_LABEL
from cloud_agent_host.environment_tests import environment_run_spec
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor


def test_a_stale_orphan_bridge_and_its_exceptions_are_pruned(monkeypatch):
    manager, docker, guard = _world(monkeypatch)
    orphan = _spec(thread="athr_gone", sandbox="asbx_gone")
    name, _ = _group_bridge(manager, docker, orphan, age_s=3600)
    manager._network_lifecycle._attach_relays(name)
    bridge = manager._network_lifecycle._network().bridge_name(name)
    guard.relays[bridge] = {("10.240.9.3", 8080)}

    manager.network.prune_orphan_networks([])

    assert docker.network(name) is None
    assert guard.relays[bridge] == set()


def test_prune_keeps_a_young_bridge_and_the_live_bridge_of_a_retained_chat(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    young, _ = _group_bridge(manager, docker, _spec(thread="athr_new", sandbox="asbx_new"))
    live_spec = _spec()
    docker.add_sandbox(live_spec, running=True)
    live, live_network = _group_bridge(
        manager, docker, live_spec, age_s=3600, subnet="10.240.10.0/29"
    )
    docker.connect(live_network["Id"], "sandbox-container", [])

    manager.network.prune_orphan_networks([live_spec])

    assert docker.network(young) is not None
    assert docker.network(live) is not None


def test_prune_reports_but_never_touches_legacy_or_foreign_networks(monkeypatch, caplog):
    manager, docker, _ = _world(monkeypatch)
    legacy = docker.add_network(
        "freeinference-chat-net-legacy",
        subnet="10.240.200.0/29",
        internal=True,
        labels={CHAT_NETWORK_LABEL: "true", CHAT_NAMESPACE_LABEL: _NAMESPACE},
        age_s=3600,
    )
    foreign_spec = _spec(thread="athr_f", sandbox="asbx_f")
    foreign = docker.add_network(
        "freeinference-chat-net-foreign",
        subnet="10.240.201.0/29",
        labels={
            **_owned_labels(manager, foreign_spec),
            CHAT_NAMESPACE_LABEL: "sibling-stack",
        },
        age_s=3600,
    )

    with caplog.at_level(logging.WARNING):
        manager.network.prune_orphan_networks([])

    assert docker.network(legacy["Id"]) is not None
    assert docker.network(foreign["Id"]) is not None
    assert "chat_legacy_network_found" in caplog.text


def test_prune_lists_only_this_namespace_and_inspects_its_bridges_together(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    orphans = [
        _group_bridge(
            manager,
            docker,
            _spec(thread=f"athr_gone{index}", sandbox=f"asbx_gone{index}"),
            age_s=3600,
            subnet=f"10.240.{20 + index}.0/29",
        )[1]
        for index in range(2)
    ]
    foreign = docker.add_network(
        "freeinference-chat-net-foreign",
        subnet="10.240.30.0/29",
        labels={CHAT_NETWORK_LABEL: "true", CHAT_NAMESPACE_LABEL: "sibling-stack"},
        age_s=3600,
    )
    docker.calls.clear()

    manager.network.prune_orphan_networks([])

    listing = next(argv for argv in docker.calls if argv[1:3] == ["network", "ls"])
    assert f"label={CHAT_NAMESPACE_LABEL}={_NAMESPACE}" in listing
    inspections = [argv for argv in docker.calls if argv[1:3] == ["network", "inspect"]]
    assert inspections[0] == ["docker", "network", "inspect", *(net["Id"] for net in orphans)]
    assert not any(foreign["Id"] in argv for argv in docker.calls), "never read, never touched"
    assert all(docker.network(net["Id"]) is None for net in orphans)
    assert docker.network(foreign["Id"]) is not None


def test_a_bridge_docker_cannot_inspect_leaves_the_others_to_be_pruned(monkeypatch, caplog):
    """The bridges are inspected together, and one at a time when that fails.

    Docker fails a whole inspection for one bridge it cannot read. That used to
    end the pass before any bridge was examined, so a bridge that persistently
    failed inspection left every leaked bridge on the host holding its subnet.
    Now only that bridge is skipped, and logged, for the next pass.
    """
    manager, docker, _ = _world(monkeypatch)
    unreadable, *orphans = (
        _group_bridge(
            manager,
            docker,
            _spec(thread=f"athr_gone{index}", sandbox=f"asbx_gone{index}"),
            age_s=3600,
            subnet=f"10.240.{20 + index}.0/29",
        )[1]
        for index in range(3)
    )
    docker.uninspectable.add(unreadable["Id"])
    docker.calls.clear()

    with caplog.at_level(logging.WARNING):
        manager.network.prune_orphan_networks([])

    assert docker.network(unreadable["Id"]) is not None, "left for the next pass"
    assert all(docker.network(net["Id"]) is None for net in orphans)
    refs = [net["Id"] for net in (unreadable, *orphans)]
    inspections = [argv[3:] for argv in docker.calls if argv[1:3] == ["network", "inspect"]]
    assert inspections[:4] == [refs, *([ref] for ref in refs)], "together, then one at a time"
    failures = [
        getattr(record, "network", None)
        for record in caplog.records
        if record.getMessage() == "chat_network_prune_failed"
    ]
    assert failures == [unreadable["Id"]]


def test_prune_reads_whose_bridge_it_is_from_the_listing_it_is_given(monkeypatch):
    manager, docker, _ = _world(monkeypatch)
    live_spec = _spec()
    docker.add_sandbox(live_spec, running=True)
    live, live_network = _group_bridge(manager, docker, live_spec, age_s=3600)
    docker.connect(live_network["Id"], "sandbox-container", [])
    listed = [manager._require(live_spec)]
    docker.calls.clear()

    manager.network.prune_orphan_networks([live_spec], listed)

    assert docker.network(live) is not None, "the live bridge of a retained chat stays"
    assert not any(argv[1:3] == ["container", "inspect"] for argv in docker.calls)


def test_a_running_environment_runs_bridge_is_not_an_orphan(monkeypatch, caplog):
    """An environment test or prebuild runs in a group of its own, never a retained chat.

    Discovery skips its container, so reconciliation names no group for its
    bridge, and a run outlives the five-minute grace. The run's own container
    on the bridge says whose it is: the run removes it when it ends, and until
    then the host listing and a supervisor start go on as usual.
    """
    manager, docker, _ = _world(monkeypatch)
    spec = environment_run_spec("run-1", 1, "sha256:image-pinned")
    name, network = _group_bridge(manager, docker, spec, age_s=360)
    manager._network_lifecycle._attach_relays(name)
    docker.add_sandbox(spec, running=True)
    docker.connect(network["Id"], "sandbox-container", [])
    # What ChatSandboxManager.list() finds: it never adopts an environment run.
    monkeypatch.setattr(manager, "list", lambda: [])
    supervisor = SharedChatSessionSupervisor(manager)

    with caplog.at_level(logging.WARNING):
        assert supervisor.list() == []
        supervisor.start()
        supervisor.close()

    assert docker.names_on(name) == {"relay-gateway", "relay-mcp", "sandbox-container"}
    assert "chat_network_prune_failed" not in caplog.text


def test_one_bridge_that_cannot_be_pruned_leaves_the_others_to_be_pruned(monkeypatch, caplog):
    manager, docker, _ = _world(monkeypatch)
    held = _spec(thread="athr_held", sandbox="asbx_held")
    held_name, held_network = _group_bridge(manager, docker, held, age_s=3600)
    manager._network_lifecycle._attach_relays(held_name)
    docker.add_container("stranger", running=True)
    docker.connect(held_network["Id"], "stranger", ["something-else"])
    orphan = _spec(thread="athr_gone", sandbox="asbx_gone")
    orphan_name, _ = _group_bridge(manager, docker, orphan, age_s=3600, subnet="10.240.10.0/29")
    manager._network_lifecycle._attach_relays(orphan_name)

    with caplog.at_level(logging.WARNING):
        manager.network.prune_orphan_networks([])

    assert docker.names_on(held_name) == {"relay-gateway", "relay-mcp", "stranger"}
    assert docker.network(orphan_name) is None
    assert "chat_network_prune_failed" in caplog.text
