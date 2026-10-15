"""Installation snapshots: the shared contract, and each host step that builds or uses one."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tests.unit.test_repository_environments import _Backend
from tests.unit.test_sandbox_restore import _inspection
from tests.unit.test_shared_session_supervisor import _Manager, _members, _spec

from cloud_agent_host.chat_sandbox import (
    CHAT_ENVIRONMENT_SNAPSHOT_LABEL,
    ENVIRONMENT_TEST_LABEL,
    ChatSandboxManager,
)
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.sandbox_checkpoints import CheckpointArchiveInvalid
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.environment import (
    BoundEnvironment,
    EnvironmentVariable,
    environment_payload,
)
from cloud_agent_shared.environment_snapshots import (
    EnvironmentSnapshotRef,
    is_lockfile,
    owner_hash,
    snapshot_image_reference,
    snapshot_object_key,
)

OWNER = owner_hash("alice")
SNAPSHOT = "5" * 32
IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
FINGERPRINT = "f" * 64


def _ref(**overrides: Any) -> EnvironmentSnapshotRef:
    fields: dict[str, Any] = {
        "snapshot_id": SNAPSHOT,
        "owner": OWNER,
        "object_key": snapshot_object_key(OWNER, SNAPSHOT),
        "sha256": "e" * 64,
        "size_bytes": 10,
        "base_image": IMAGE,
        "source_sha": "c" * 40,
        "lockfiles": {"package-lock.json": "d" * 40},
        "built_at": "2026-10-15T00:00:00Z",
        **overrides,
    }
    return EnvironmentSnapshotRef.model_validate(fields)


# -- the shared contract ---------------------------------------------------------


def test_a_snapshot_reference_names_only_its_owners_key_and_a_pullable_image():
    assert _ref().object_key.startswith("cloud-agent/sandbox/env/v1/")
    with pytest.raises(ValueError, match="belong"):
        _ref(object_key=snapshot_object_key(owner_hash("bob"), SNAPSHOT))
    with pytest.raises(ValueError, match="pullable"):
        _ref(base_image="cloud-agent-sandbox@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="manifests"):
        _ref(lockfiles={"README.md": "d" * 40})
    assert snapshot_image_reference(OWNER, SNAPSHOT) == (
        f"freeinference-environment/{OWNER[:32]}:{SNAPSHOT}"
    )


@pytest.mark.parametrize(
    ("path", "manifest"),
    [
        ("package-lock.json", True),
        ("web/pnpm-lock.yaml", True),
        ("requirements-dev.txt", True),
        ("services/api/uv.lock", True),
        ("README.md", False),
        ("package.json", False),
    ],
)
def test_lockfiles_are_recognised_by_name_in_any_directory(path, manifest):
    assert is_lockfile(path) is manifest


def test_an_older_host_gets_the_binding_without_snapshot_fields():
    bound = BoundEnvironment(
        binding_id="b", update_script="make gen", setup_fingerprint=FINGERPRINT
    )
    old = environment_payload(bound, ["environment-v1"])
    assert "update_script" not in old and "setup_fingerprint" not in old
    # What an older host parses has no field it would refuse.
    BoundEnvironment.model_validate(old)
    assert environment_payload(bound, ["environment-snapshot-v1"])["update_script"] == "make gen"


def test_the_setup_fingerprint_follows_only_what_installation_sees(monkeypatch):
    from cloud_agent.storage.repositories.environments import (
        optional_setup_fingerprint,
        setup_fingerprint,
    )

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "fingerprint-test-key")
    build = EnvironmentVariable(name="NPM_TOKEN", value="build-secret-1", phase="setup")
    runtime = EnvironmentVariable(name="API_KEY", value="runtime-secret-1", phase="runtime")
    first = setup_fingerprint("npm ci", [build, runtime])
    assert first == setup_fingerprint("npm ci", [runtime, build])
    assert first == setup_fingerprint(
        "npm ci", [build, runtime.model_copy(update={"value": "runtime-secret-2"})]
    )
    assert first != setup_fingerprint(
        "npm ci", [build.model_copy(update={"value": "build-secret-2"}), runtime]
    )
    assert first != setup_fingerprint("npm ci --force", [build, runtime])
    assert "build-secret-1" not in first
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY")
    monkeypatch.setattr(
        "cloud_agent.settings.setting_env", lambda name, default=None: default, raising=False
    )
    assert optional_setup_fingerprint("npm ci", [build]) is None


# -- creating a container from a snapshot ---------------------------------------


def test_a_snapshot_container_runs_its_image_and_is_never_taken_for_a_test():
    spec = replace(_spec(), sandbox_image_digest=IMAGE)
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    reference = snapshot_image_reference(OWNER, SNAPSHOT)

    command = manager.build_create_command(
        spec, image=reference, environment_snapshot=(OWNER, SNAPSHOT)
    )

    assert f"{CHAT_ENVIRONMENT_SNAPSHOT_LABEL}={OWNER}:{SNAPSHOT}" in command
    # The image inherits the test run's label; an empty one is never a test.
    assert f"{ENVIRONMENT_TEST_LABEL}=" in command
    with pytest.raises(SandboxError, match="does not match its environment snapshot"):
        manager.build_create_command(spec, image=IMAGE, environment_snapshot=(OWNER, SNAPSHOT))
    with pytest.raises(SandboxError, match="not both"):
        manager.build_create_command(
            spec, image=reference, checkpoint_id="c" * 32, environment_snapshot=(OWNER, SNAPSHOT)
        )
    labelled = {**spec.docker_labels(), CHAT_ENVIRONMENT_SNAPSHOT_LABEL: f"{OWNER}:{SNAPSHOT}"}
    manager._verify_identity(_inspection(spec, image=reference, labels=labelled), spec)
    with pytest.raises(SandboxError, match="outside this chat/generation"):
        manager._verify_identity(
            _inspection(spec, image=reference, labels=spec.docker_labels()), spec
        )
    with pytest.raises(SandboxError, match="outside this chat/generation"):
        other = snapshot_image_reference(OWNER, "6" * 32)
        manager._verify_identity(_inspection(spec, image=other, labels=labelled), spec)


class _SnapshotManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[dict[str, Any]] = []

    def create(self, spec, *, image=None, environment_snapshot=None, facts=None, observer=None):
        self.created.append({"image": image, "environment_snapshot": environment_snapshot})
        return super().create(spec, facts=facts, observer=observer)


@pytest.mark.parametrize(
    ("hook", "failure", "image"),
    [
        (lambda snapshot: "loaded-image", None, "loaded-image"),
        (lambda snapshot: (_ for _ in ()).throw(CheckpointArchiveInvalid("bad")), "invalid", None),
        (lambda snapshot: (_ for _ in ()).throw(SandboxError("offline")), "unavailable", None),
        (None, "unavailable", None),
    ],
)
def test_a_new_group_falls_back_to_the_image_whatever_the_snapshot_does(hook, failure, image):
    manager = _SnapshotManager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.environment_snapshot_hook = hook

    supervisor.register_manifest(_spec(), _members(), environment_snapshot=_ref())

    assert manager.created[-1]["image"] == image
    assert (manager.created[-1]["environment_snapshot"] is not None) == (failure is None)
    assert supervisor._groups[_spec().sandbox_id].environment_snapshot_failure == failure


# -- preparation in a container from a snapshot -----------------------------------


class _MarkedBackend(_Backend):
    def __init__(self, marker: dict[str, Any] | None):
        super().__init__()
        self.written: list[dict[str, Any]] = []
        raw = json.dumps(marker).encode() if marker is not None else None
        self.manager.read_environment_setup_marker = lambda spec, **_: raw
        self.manager.write_environment_setup_marker = lambda spec, payload, **_: (
            self.written.append(json.loads(payload))
        )


def _bound(**overrides: Any) -> BoundEnvironment:
    fields: dict[str, Any] = {
        "binding_id": "task-binding",
        "setup_script": "install",
        "update_script": "generate",
        "start_script": "start",
        "setup_fingerprint": FINGERPRINT,
        **overrides,
    }
    return BoundEnvironment(**fields)


def test_a_snapshot_installation_is_used_and_its_update_script_runs_once(tmp_path):
    backend = _MarkedBackend(
        {
            "v": 1,
            "setup_fingerprint": FINGERPRINT,
            "binding_id": "prebuild",
            "snapshot_id": SNAPSHOT,
        }
    )
    prepare = EnvironmentPreparation(tmp_path)

    result = prepare.prepare(backend, _bound())

    assert [call.argv[-1] for call in backend.calls] == ["generate", "start"]
    setup = next(step for step in result["steps"] if step["phase"] == "setup")
    assert setup["ran"] is False and setup["snapshot_id"] == SNAPSHOT
    # The layer now says this workspace's installation is complete.
    assert backend.written[-1]["binding_id"] == "task-binding"
    prepare.prepare(backend, _bound())
    assert len(backend.calls) == 2


def test_a_restored_workspace_neither_reinstalls_nor_updates(tmp_path):
    backend = _MarkedBackend(
        {"v": 1, "setup_fingerprint": FINGERPRINT, "binding_id": "task-binding"}
    )
    EnvironmentPreparation(tmp_path).prepare(backend, _bound())
    assert [call.argv[-1] for call in backend.calls] == ["start"]


@pytest.mark.parametrize(
    "marker",
    [
        None,
        {"v": 1, "setup_fingerprint": "0" * 64, "binding_id": "prebuild"},
        {"v": 2, "setup_fingerprint": FINGERPRINT, "binding_id": "prebuild"},
    ],
)
def test_without_a_matching_marker_the_installation_runs_and_is_marked(tmp_path, marker):
    backend = _MarkedBackend(marker)
    EnvironmentPreparation(tmp_path).prepare(backend, _bound())
    assert [call.argv[-1] for call in backend.calls] == ["install", "start"]
    assert backend.written == [
        {"v": 1, "setup_fingerprint": FINGERPRINT, "binding_id": "task-binding"}
    ]


def test_a_binding_without_a_fingerprint_never_reads_or_writes_a_marker(tmp_path):
    backend = _MarkedBackend({"v": 1, "setup_fingerprint": FINGERPRINT, "binding_id": "prebuild"})
    EnvironmentPreparation(tmp_path).prepare(backend, _bound(setup_fingerprint=""))
    assert [call.argv[-1] for call in backend.calls] == ["install", "start"]
    assert backend.written == []


# -- the runner's choice for a new workspace --------------------------------------


def _source(lockfiles):
    return SimpleNamespace(lockfiles=lambda: lockfiles)


def _job(snapshot=None):
    return SimpleNamespace(environment=_bound(), environment_snapshot=snapshot)


@pytest.mark.parametrize(
    ("snapshot", "image", "lockfiles", "new_group", "expected"),
    [
        (_ref(), IMAGE, {"package-lock.json": "d" * 40}, True, ("use", None)),
        (None, IMAGE, {"package-lock.json": "d" * 40}, True, (None, "absent")),
        (_ref(), "other", {"package-lock.json": "d" * 40}, True, (None, "image_changed")),
        (_ref(), IMAGE, {"package-lock.json": "0" * 40}, True, (None, "lockfiles_changed")),
        (_ref(), IMAGE, None, True, (None, None)),
        (_ref(), IMAGE, {"package-lock.json": "d" * 40}, False, (None, None)),
    ],
)
def test_a_new_workspace_starts_from_a_snapshot_only_when_everything_matches(
    snapshot, image, lockfiles, new_group, expected
):
    from cloud_agent_host.runner import _choose_environment_snapshot

    chosen, outcome = _choose_environment_snapshot(
        _job(snapshot),
        replace(_spec(), sandbox_image_digest=image),
        _source(lockfiles),
        new_group=new_group,
    )
    assert (("use" if chosen is snapshot and chosen else None), outcome) == expected


def test_lockfiles_are_fingerprinted_by_git_object(tmp_path):
    from cloud_agent_host.runner import tracked_lockfiles

    git = ["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text("{}\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "app.py").write_text("print()\n")
    (tmp_path / "requirements.txt").write_text("")
    subprocess.run([*git, "add", "web/package-lock.json", "uv.lock", "app.py"], check=True)
    blob = subprocess.run(
        [*git, "hash-object", "uv.lock"], capture_output=True, text=True, check=True
    ).stdout.strip()

    found = tracked_lockfiles(str(tmp_path))

    # Untracked manifests do not count: the checkout is what the task uses.
    assert set(found or {}) == {"web/package-lock.json", "uv.lock"}
    assert found["uv.lock"] == blob


# -- saving a snapshot from an environment run ------------------------------------


def test_a_snapshot_is_marked_emptied_of_the_repository_stopped_then_uploaded(monkeypatch):
    from cloud_agent_host import environment_tests

    events: list[Any] = []
    manager = SimpleNamespace(
        backend=SimpleNamespace(uid=1000, gid=1000),
        write_environment_setup_marker=lambda spec, payload, **kw: events.append(
            ("marker", json.loads(payload), kw["expected_container_id"])
        ),
        exec_capture=lambda spec, argv, **kw: (
            events.append(("exec", argv[-1], kw["user_uid"]))
            or subprocess.CompletedProcess(argv, 0, b"", b"")
        ),
        stop=lambda spec, **kw: events.append(("stop", kw["expected_container_id"])),
    )
    captured: dict[str, Any] = {}

    class _Supervisor:
        def capture_environment_snapshot(self, **kwargs):
            events.append(("capture",))
            captured.update(kwargs)
            return {"snapshot_id": SNAPSHOT, "object_key": "key", "base_image": IMAGE}

    monkeypatch.setattr(
        "cloud_agent_host.runner._session_supervisor_from_env", lambda **_: _Supervisor()
    )
    receipt = environment_tests._save_snapshot(
        manager,
        _spec(),
        _bound(),
        {"id": "run-1", "lease_generation": 3},
        {"snapshot_id": SNAPSHOT, "owner": OWNER},
        container_id="container-1",
        source_sha="c" * 40,
        lockfiles={"uv.lock": "d" * 40},
    )

    assert [event[0] for event in events] == ["marker", "exec", "stop", "capture"]
    assert events[0][1]["snapshot_id"] == SNAPSHOT and events[0][2] == "container-1"
    assert "git ls-files" in events[1][1] and "rm -rf .git" in events[1][1]
    assert events[1][2] == 1000
    assert captured["run_id"] == "run-1" and captured["owner"] == OWNER
    assert receipt["lockfiles"] == {"uv.lock": "d" * 40} and receipt["source_sha"] == "c" * 40
    with pytest.raises(RuntimeError, match="manifests"):
        environment_tests._save_snapshot(
            manager,
            _spec(),
            _bound(),
            {"id": "run-1", "lease_generation": 3},
            {"snapshot_id": SNAPSHOT, "owner": OWNER},
            container_id="container-1",
            source_sha="c" * 40,
            lockfiles=None,
        )


# -- which environment runs save a snapshot ---------------------------------------


def _execute(monkeypatch, tmp_path, **run_fields: Any) -> tuple[list[str], dict[str, Any]]:
    """Run one environment run against stand-ins; return its steps and final report."""
    from cloud_agent_host import environment_tests

    events: list[str] = []

    class _RunManager:
        def __init__(self, backend):
            self.backend = backend

        def create(self, spec):
            events.append("create")

        def start(self, spec):
            return SimpleNamespace(container_id="container-1")

        def prepare_shared_workspace(self, spec):
            pass

        def inspect(self, name):
            return SimpleNamespace(container_id="container-1") if "create" in events else None

        def remove_environment_test(self, spec, *, container_id):
            events.append("remove")

    class _Source:
        def __init__(self, job, **kwargs):
            pass

        def import_into(self, manager, spec):
            return "c" * 40

        def lockfiles(self):
            return {"uv.lock": "d" * 40}

        def discard(self):
            pass

    class _Preparation:
        root = tmp_path

        def prepare(self, backend, environment, **kwargs):
            events.append("install, start" if environment.start_script else "install")

    def save(*args, **kwargs):
        events.append("save")
        return {"snapshot_id": SNAPSHOT}

    reports: list[dict[str, Any]] = []

    def post(path, json):
        reports.append(json)
        return httpx.Response(200, request=httpx.Request("POST", f"http://t{path}"))

    monkeypatch.setattr(environment_tests, "ChatSandboxManager", _RunManager)
    monkeypatch.setattr(environment_tests, "RetainedChatBackend", lambda manager, spec: None)
    monkeypatch.setattr(environment_tests, "EnvironmentPreparation", _Preparation)
    monkeypatch.setattr(environment_tests, "_save_snapshot", save)
    monkeypatch.setattr("cloud_agent_host.runner._SourcePrefetch", _Source)
    environment_tests.execute_test(
        client=SimpleNamespace(post=post),
        run={
            "id": "run-1",
            "lease_generation": 1,
            "repo": "org/repo",
            "base_sha": "c" * 40,
            "environment": _bound().model_dump(mode="json"),
            **run_fields,
        },
        backend=SimpleNamespace(
            uid=1000, gid=1000, version_stamp=lambda: {"sandbox_image_digest": IMAGE}
        ),
    )
    return events, reports[-1]


@pytest.mark.parametrize(
    ("purpose", "steps"),
    [
        # A test's start script ran with runtime values: its container is never saved,
        # even when a control plane names a snapshot for it.
        ("test", ["create", "install, start", "remove"]),
        ("prebuild", ["create", "install", "save", "remove"]),
    ],
)
def test_only_a_prebuild_saves_its_container(monkeypatch, tmp_path, purpose, steps):
    events, final = _execute(
        monkeypatch, tmp_path, purpose=purpose, snapshot={"snapshot_id": SNAPSHOT, "owner": OWNER}
    )
    assert events == steps
    assert final["state"] == "succeeded" and final["cleanup_complete"] is True


def test_a_prebuild_whose_snapshot_is_gone_installs_nothing(monkeypatch, tmp_path):
    events, final = _execute(monkeypatch, tmp_path, purpose="prebuild", snapshot=None)
    assert events == []
    assert final["state"] == "failed" and "no installation snapshot" in final["detail"]


# -- dropping retired snapshots ---------------------------------------------------


class _RetiringSupervisor:
    def __init__(self, held: list[str] | None) -> None:
        self.held = held
        self.deletes: list[list[dict[str, Any]]] = []

    def delete_environment_snapshots(self, items):
        self.deletes.append([dict(item) for item in items])
        return [item["snapshot_id"] for item in items if item["object_key"]]

    def environment_snapshot_images(self):
        from cloud_agent_host.session_supervisor_client import SessionSupervisorError

        if self.held is None:
            raise SessionSupervisorError(404, "sandbox manager refused")
        return self.held


def _retire(monkeypatch, supervisor, *, retired_status: int = 200) -> list[list[str]]:
    from cloud_agent_host import environment_tests

    monkeypatch.setattr(
        "cloud_agent_host.runner._session_supervisor_from_env", lambda **_: supervisor
    )
    asked: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deletions"):
            deletion = {
                "snapshot_id": SNAPSHOT,
                "owner": OWNER,
                "object_key": "k",
                "version_id": "v",
            }
            return httpx.Response(200, json={"snapshots": [deletion]})
        if request.url.path.endswith("/retired"):
            ids = json.loads(request.content)["snapshot_ids"]
            asked.append(ids)
            return httpx.Response(
                retired_status, json={"snapshots": [{"snapshot_id": ids[0], "owner": OWNER}]}
            )
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as client:
        environment_tests._delete_retired_snapshots(client)
    return asked


def test_a_host_drops_every_retired_image_it_holds(monkeypatch):
    held = [f"{index:032x}" for index in range(150)]
    supervisor = _RetiringSupervisor(held)
    asked = _retire(monkeypatch, supervisor)
    # Stored objects first, then the host's own images, a batch at a time.
    assert supervisor.deletes[0][0]["object_key"] == "k"
    assert [len(batch) for batch in asked] == [100, 50]
    assert [batch[0]["snapshot_id"] for batch in supervisor.deletes[1:]] == [held[0], held[100]]
    assert all(item["object_key"] is None for batch in supervisor.deletes[1:] for item in batch)


@pytest.mark.parametrize(("held", "status"), [(None, 200), ([SNAPSHOT], 404)])
def test_older_components_still_delete_stored_objects(monkeypatch, held, status):
    supervisor = _RetiringSupervisor(held)
    _retire(monkeypatch, supervisor, retired_status=status)
    assert len(supervisor.deletes) == 1 and supervisor.deletes[0][0]["object_key"] == "k"


def test_a_host_without_a_sandbox_manager_has_no_snapshots_to_drop(monkeypatch):
    from cloud_agent_host import environment_tests

    def missing(**_):
        raise SandboxError("restartable chat execution needs AGENT_SANDBOX_MANAGER_URL")

    monkeypatch.setattr("cloud_agent_host.runner._session_supervisor_from_env", missing)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"snapshots": []})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t") as client:
        environment_tests._delete_retired_snapshots(client)
    assert requests == []


def test_snapshot_images_are_named_by_their_ids(tmp_path):
    from cloud_agent_host.sandbox_checkpoints import (
        SandboxCheckpointCapture,
        SandboxCheckpointStore,
    )

    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{SNAPSHOT}\nlatest\n{SNAPSHOT}\n", "")

    capture = SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "checkpoints"),
        run=run,
    )
    assert capture.environment_images() == [SNAPSHOT]
    assert "reference=freeinference-environment/*:*" in seen[0]
