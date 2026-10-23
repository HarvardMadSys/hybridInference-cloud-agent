"""The host proves its Kata runtime once, again only on change, and its children inherit that."""

from __future__ import annotations

import json
import os
import stat
import uuid
from types import SimpleNamespace

import pytest

from cloud_agent_host import runtime_qualification as qualification
from cloud_agent_host.network_guard import VERIFICATION_KEY
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, ProcessBackend, SandboxError


def _write_epoch(path, *, state="ready"):
    path.write_text(json.dumps({"version": 1, "state": state, "generation": str(uuid.uuid4())}))
    path.chmod(0o644)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    # A macOS checkout is user-owned. Keep all file-type/mode/symlink checks;
    # exercise the production root-ownership check separately below.
    safe_mode = qualification._require_safe_mode
    monkeypatch.setattr(
        qualification,
        "_require_safe_mode",
        lambda observed, *, directory, root: safe_mode(observed, directory=directory, root=False),
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    epoch = runtime / "epoch.json"
    _write_epoch(epoch)
    image = {"id": "sha256:original-image", "inspections": 0}

    def image_id(_backend):
        image["inspections"] += 1
        return image["id"]

    monkeypatch.setattr(qualification, "_image_id", image_id)
    backend = ContainerBackend(image="fixture@sha256:abc", runtime=KATA_RUNTIME)
    probes: list[str] = []

    def preflight(*, check_network=True):
        assert check_network is False, "mutable network verification remains per turn"
        probes.append("probe")
        backend._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": "guest-kernel"}

    monkeypatch.setattr(backend, "preflight", preflight)
    monkeypatch.setattr(backend, "version_stamp", lambda: dict(backend._kernel_probe))
    host = qualification.HostQualification(backend, runtime_state_path=str(epoch))
    return SimpleNamespace(backend=backend, host=host, probes=probes, epoch=epoch, image=image)


def _child(lab, verdict, *, backend=None):
    """Adopt ``verdict`` the way a turn process would, on a backend of its own."""
    child = backend or ContainerBackend(image="fixture@sha256:abc", runtime=KATA_RUNTIME)
    adopted = qualification.adopt_verdict(child, verdict, runtime_state_path=str(lab.epoch))
    return adopted, child


def test_one_probe_serves_every_turn_until_something_changes(lab):
    assert lab.host.refresh()
    verdicts = []
    for _ in range(3):
        assert lab.host.refresh()
        verdicts.append(lab.host.verdict())
    assert lab.probes == ["probe"]
    adopted, child = _child(lab, verdicts[-1])
    assert adopted
    assert child._kernel_probe["guest_kernel"] == "guest-kernel"
    assert verdicts[0] == verdicts[-1]


def test_a_verdict_survives_the_json_handoff(lab):
    """The child reads the verdict back from the turn payload on its stdin."""
    assert lab.host.refresh()
    adopted, _child_backend = _child(lab, json.loads(json.dumps(lab.host.verdict())))
    assert adopted


class _Guard:
    """A guard whose every hand-over is counted; the real one renews when not recent."""

    def __init__(self) -> None:
        self.handovers = 0

    def handover(self) -> dict[str, object]:
        self.handovers += 1
        return {"verified_at": 1.0, "helper_image": "sha256:helper"}


@pytest.mark.parametrize("network", [True, False])
def test_a_handover_carries_the_verdict_and_the_hosts_verification_of_the_rules(
    lab, monkeypatch, network
):
    """Both travel in the one hand-off; the rules' only where there are rules to verify."""
    guard = _Guard()
    monkeypatch.setattr(lab.backend, "sandbox_network", object() if network else None)
    monkeypatch.setattr(lab.backend, "network_guard", lambda: guard)
    assert lab.host.refresh()

    handed = lab.host.handover()

    assert handed[qualification.VERDICT_KEY] == lab.host.verdict()
    if network:
        assert handed[VERIFICATION_KEY] == {"verified_at": 1.0, "helper_image": "sha256:helper"}
    else:
        assert VERIFICATION_KEY not in handed
    assert guard.handovers == (1 if network else 0)


def test_a_host_without_a_verdict_still_hands_over_its_verification_of_the_rules(monkeypatch):
    """A runc host has no verdict to give, but its children still start no helper."""
    guard = _Guard()
    backend = ContainerBackend(image="fixture@sha256:abc")
    monkeypatch.setattr(backend, "sandbox_network", object())
    monkeypatch.setattr(backend, "network_guard", lambda: guard)

    assert qualification.HostQualification(backend).handover() == {
        VERIFICATION_KEY: {"verified_at": 1.0, "helper_image": "sha256:helper"}
    }


@pytest.mark.parametrize("change", ["epoch", "image", "recheck"])
def test_each_visible_change_requires_a_new_probe(lab, change):
    assert lab.host.refresh()
    if change == "epoch":
        _write_epoch(lab.epoch)
    elif change == "image":
        lab.image["id"] = "sha256:rebuilt-image"
    else:
        lab.host._checked_at -= qualification.RECHECK_S
    if change != "recheck":
        assert lab.host.verdict() is None, "a turn handed over now probes for itself"
    assert lab.host.refresh()
    assert lab.probes == ["probe", "probe"]
    assert lab.host.verdict() is not None


def test_a_verdict_from_before_an_update_is_not_adopted(lab):
    assert lab.host.refresh()
    verdict = lab.host.verdict()
    _write_epoch(lab.epoch)
    adopted, child = _child(lab, verdict)
    assert not adopted, "the verdict describes a runtime that is no longer installed"
    assert child._kernel_probe["guest_kernel"] is None


@pytest.mark.parametrize("failure", ["epoch_change", "image_change", "probe_error"])
def test_a_change_during_the_probe_leaves_no_verdict(lab, monkeypatch, failure):
    def preflight(*, check_network):
        lab.backend._kernel_probe["guest_kernel"] = "guest-kernel"
        if failure == "epoch_change":
            _write_epoch(lab.epoch)
        elif failure == "image_change":
            lab.image["id"] = "sha256:rebuilt-during-probe"
        else:
            raise SandboxError("probe failed")

    monkeypatch.setattr(lab.backend, "preflight", preflight)
    with pytest.raises(SandboxError):
        lab.host.refresh()
    assert lab.host.verdict() is None


def test_a_failing_probe_backs_off_instead_of_running_on_every_poll(lab, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(qualification.time, "monotonic", lambda: clock[0])
    failing = [True]

    def preflight(*, check_network):
        lab.probes.append("probe")
        lab.backend._kernel_probe = {"host_kernel": os.uname().release, "guest_kernel": None}
        if failing[0]:
            raise SandboxError("observer self-test failed")
        lab.backend._kernel_probe["guest_kernel"] = "guest-kernel"

    monkeypatch.setattr(lab.backend, "preflight", preflight)
    for _ in range(3):
        with pytest.raises(SandboxError, match="self-test"):
            lab.host.refresh()
    assert lab.probes == ["probe"], "a failed probe is not repeated inside its backoff"
    clock[0] += qualification.LAUNCH_BACKOFF_S
    with pytest.raises(SandboxError, match="self-test"):
        lab.host.refresh()
    assert lab.probes == ["probe", "probe"]
    clock[0] += qualification.LAUNCH_BACKOFF_S
    with pytest.raises(SandboxError, match="self-test"):
        lab.host.refresh()
    assert lab.probes == ["probe", "probe"], "each failure doubles the wait"
    clock[0] += qualification.LAUNCH_BACKOFF_S
    failing[0] = False
    assert lab.host.refresh()
    assert lab.probes == ["probe", "probe", "probe"]
    assert lab.host.verdict() is not None, "a recovered runtime is reused at once"


def test_without_an_epoch_or_kata_every_child_probes_for_itself(lab):
    lab.epoch.unlink()
    assert lab.host.refresh() is False
    assert lab.host.verdict() is None
    assert not lab.probes
    runc = ContainerBackend(image="fixture", runtime="runc")
    assert qualification.HostQualification(runc).refresh() is False
    adopted, _child_backend = _child(lab, {"guest_kernel": "guest"}, backend=runc)
    assert not adopted
    assert qualification.adopt_verdict(ProcessBackend(acknowledged_unsafe=True), {}) is False


def test_a_runtime_update_stops_claims_turns_and_full_probes(lab, monkeypatch):
    assert lab.host.refresh()
    verdict = lab.host.verdict()
    _write_epoch(lab.epoch, state="updating")
    with pytest.raises(SandboxError, match="being updated"):
        lab.host.refresh()
    with pytest.raises(SandboxError, match="being updated"):
        _child(lab, verdict)
    monkeypatch.setattr(
        qualification,
        "check_runtime_state",
        lambda: (_ for _ in ()).throw(SandboxError("being updated")),
    )
    with pytest.raises(SandboxError, match="being updated"):
        ContainerBackend(image="fixture", runtime=KATA_RUNTIME).preflight()


@pytest.mark.parametrize(
    "verdict",
    [
        None,
        "not-a-verdict",
        {"epoch": "stale"},
        {"guest_kernel": None},
        {"guest_kernel": ""},
        {"guest_kernel": os.uname().release},
    ],
)
def test_a_child_probes_for_itself_rather_than_trust_an_unusable_verdict(lab, verdict):
    if isinstance(verdict, dict) and "epoch" not in verdict:
        verdict = {**verdict, "epoch": qualification.check_runtime_state(str(lab.epoch))}
    adopted, child = _child(lab, verdict)
    assert not adopted
    assert child._kernel_probe["guest_kernel"] is None


def test_the_image_id_comes_from_docker_or_not_at_all(monkeypatch):
    backend = ContainerBackend(image="fixture", runtime=KATA_RUNTIME)
    answers = iter(
        [
            SimpleNamespace(returncode=0, stdout="sha256:abc\n", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="No such image"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )
    monkeypatch.setattr(qualification.subprocess, "run", lambda *a, **k: next(answers))
    assert qualification._image_id(backend) == "sha256:abc"
    for _ in range(2):
        with pytest.raises(SandboxError, match="could not be inspected"):
            qualification._image_id(backend)


@pytest.mark.parametrize(
    "kind",
    ["symlink", "directory_symlink", "world_writable", "group_writable", "fifo", "malformed"],
)
def test_runtime_marker_must_be_safe(lab, kind):
    if kind == "symlink":
        target = lab.epoch.with_name("actual.json")
        lab.epoch.rename(target)
        lab.epoch.symlink_to(target)
    elif kind == "directory_symlink":
        original = lab.epoch.parent
        target = original.with_name("actual-runtime")
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
    elif kind in {"world_writable", "group_writable"}:
        lab.epoch.chmod(0o666 if kind == "world_writable" else 0o664)
    elif kind == "fifo":
        lab.epoch.unlink()
        os.mkfifo(lab.epoch)
    else:
        lab.epoch.write_text("{}")
    with pytest.raises(SandboxError):
        qualification.check_runtime_state(str(lab.epoch))


def test_runtime_marker_must_be_root_owned():
    observed = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1234)
    with pytest.raises(SandboxError, match="ownership"):
        qualification._require_safe_mode(observed, directory=False, root=True)
