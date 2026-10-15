"""SSH access: the shared state model and how a host applies it.

The in-guest scripts run here for real against a temporary tree, as the guest
would run them, so their file modes, swaps, narrowing and newest-state record
are exercised rather than described.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError
from tests.support.ssh_keys import host_key, private_key

from cloud_agent_host import ssh_access as host_ssh
from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE, ChatSandboxSpec
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from pathlib import Path

from cloud_agent_shared.ssh_access import (
    SshAccess,
    SshMemberAccess,
    SshServerAccess,
    host_key_fingerprint,
)

_KEY = private_key()
_HOST_KEY = host_key()


def _server(name: str = "gpu", address: str = "93.184.216.34", port: int = 22, **extra: Any):
    return SshServerAccess(
        server_id=extra.pop("server_id", "3f9c2a1e-0000-4000-8000-" + name.encode().hex()[:12]),
        name=name,
        address=address,
        port=port,
        username=extra.pop("username", "ubuntu"),
        host_key_type=extra.pop("host_key_type", "ssh-ed25519"),
        host_key=extra.pop("host_key", _HOST_KEY),
        private_key=extra.pop("private_key", _KEY),
    )


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest="sha256:image-pinned",
    )


# ── the shared model ─────────────────────────────────────────────────────


def test_a_server_never_shows_its_key() -> None:
    server = _server()
    assert _KEY not in repr(server)
    assert _KEY not in repr(SshAccess(revision=1, members=[SshMemberAccess(thread_id="t")]))


@pytest.mark.parametrize(
    "change",
    [
        {"name": "github.com"},
        {"name": "GPU"},
        {"name": "g"},
        {"address": "10.0.0.5"},
        {"address": "203.0.113.7"},
        {"address": "169.254.169.254"},
        {"address": "2001:db8::1"},
        {"address": "example.com"},
        {"port": 0},
        {"username": "-oProxyCommand=sh"},
        {"username": "root admin"},
        {"host_key_type": "ssh-dss"},
        {"host_key_type": "ssh-rsa"},
        {"host_key": "not base64!"},
        {"private_key": _KEY.splitlines()[0] + "\nabc\n"},
        {"private_key": _KEY + "Host *\n  ProxyCommand sh\n"},
    ],
)
def test_values_that_could_change_the_configuration_are_refused(change) -> None:
    with pytest.raises(ValidationError):
        _server(**change)


def test_targets_are_the_union_and_secrets_are_every_key() -> None:
    access = SshAccess(
        revision=4,
        members=[
            SshMemberAccess(thread_id="a", slot_id=0, servers=[_server("gpu"), _server("db")]),
            SshMemberAccess(thread_id="b", servers=[_server("gpu"), _server("lab", port=2222)]),
        ],
    )
    assert access.targets() == {("93.184.216.34", 22), ("93.184.216.34", 2222)}
    assert set(access.secrets()) == {"ssh-key:gpu", "ssh-key:db", "ssh-key:lab"}
    assert [member.thread_id for member in access.resident_members()] == ["a"]


def test_the_runner_places_its_own_member_and_drops_a_stale_one_in_that_slot() -> None:
    access = SshAccess(
        revision=2,
        members=[
            SshMemberAccess(thread_id="old", slot_id=3, servers=[_server()]),
            SshMemberAccess(thread_id="mine", servers=[_server("db")]),
        ],
    )
    placed = access.with_slot("mine", 3)
    assert [(member.thread_id, member.slot_id) for member in placed.members] == [("mine", 3)]


def test_a_member_or_slot_may_appear_once() -> None:
    with pytest.raises(ValidationError):
        SshAccess(
            revision=1,
            members=[SshMemberAccess(thread_id="a"), SshMemberAccess(thread_id="a")],
        )
    with pytest.raises(ValidationError):
        SshMemberAccess(thread_id="a", servers=[_server("gpu"), _server("gpu", port=2222)])


def test_the_fingerprint_is_what_ssh_keygen_prints() -> None:
    import hashlib

    expected = base64.b64encode(hashlib.sha256(base64.b64decode(_HOST_KEY)).digest()).decode()
    assert host_key_fingerprint(_HOST_KEY) == "SHA256:" + expected.rstrip("=")


# ── rendering ────────────────────────────────────────────────────────────


def test_each_server_is_pinned_to_its_address_key_and_host_key() -> None:
    gpu = _server("gpu")
    config = host_ssh.render_config(2, gpu)
    base = f"/run/agent-members/2/ssh/servers/{gpu.server_id}"
    assert "Host gpu\n" in config
    assert "  HostName 93.184.216.34" in config
    assert f"  IdentityFile {base}/key" in config
    assert f"  UserKnownHostsFile {base}/known_hosts" in config
    assert "  StrictHostKeyChecking yes" in config
    assert "  BatchMode yes" in config
    assert "  ForwardAgent no" in config
    assert "  Port 2222" in host_ssh.render_config(2, _server("db", port=2222))
    known = host_ssh.render_known_hosts(gpu)
    assert known == f"cloud-agent-ssh-{gpu.server_id} ssh-ed25519 {_HOST_KEY}\n"
    assert host_ssh.include_line(2) == "Include /run/agent-members/2/ssh/servers/*/config"


def test_an_rsa_host_key_is_checked_with_sha2_signatures() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    blob = (
        public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
        .split()[1]
    )
    config = host_ssh.render_config(0, _server(host_key_type="ssh-rsa", host_key=blob))
    assert "  HostKeyAlgorithms rsa-sha2-512,rsa-sha2-256" in config


def test_a_digest_names_one_exact_entry_and_never_its_key() -> None:
    gpu = _server("gpu")
    assert gpu.digest() == _server("gpu").digest()
    assert gpu.digest() != _server("gpu", private_key=private_key()).digest(), "a new key"
    assert gpu.digest() != _server("gpu", host_key=host_key()).digest(), "a new host key"
    assert gpu.digest() != _server("gpu", port=2222).digest()
    assert _KEY not in gpu.digest()


# ── the in-guest scripts, run for real ───────────────────────────────────


def _run(script: str, request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )


def _gate(
    root: Path, revision: int, members: dict[str, list[str]], slots: dict[str, int]
) -> dict[str, Any]:
    result = _run(
        host_ssh._GATE_SCRIPT,
        {
            "revision": revision,
            "members": members,
            "slots": slots,
            "state_file": str(root / ".ssh-access-state"),
            "root": str(root),
            "owner_uid": os.getuid(),
            "capacity": 5,
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_gate_records_the_newest_state_and_answers_an_older_one_with_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "members"
    root.mkdir(mode=0o711)
    for slot in range(3):
        (root / str(slot) / "ssh").mkdir(parents=True)
    (root / "3").mkdir()
    (root / "3" / "ssh").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    # A slot that is itself a link is never entered, so nothing behind it goes.
    (tmp_path / "outside" / "ssh").mkdir(parents=True)
    (root / "4").symlink_to(tmp_path / "outside")
    gpu, db = "a" * 64, "b" * 64

    first = _gate(root, 5, {"athr_a": [gpu]}, {"athr_a": 1, "athr_b": 0})

    assert first == {
        "fresh": True,
        "revision": 5,
        "members": {"athr_a": [gpu]},
        "keep_slots": [1],
    }
    assert not (root / "0" / "ssh").exists(), "a chat allowed nothing loses its directory"
    assert (root / "1" / "ssh").is_dir()
    assert not (root / "3" / "ssh").is_symlink()
    assert (tmp_path / "elsewhere").is_dir(), "a planted link is removed, not followed"
    assert (tmp_path / "outside" / "ssh").is_dir(), "a linked slot is not entered"

    # An older state is answered with the newest, which it cannot widen.
    (root / "0" / "ssh").mkdir()
    older = _gate(root, 4, {"athr_a": [gpu, db], "athr_b": [db]}, {"athr_a": 1, "athr_b": 0})
    assert older == {**first, "fresh": False}
    assert not (root / "0" / "ssh").exists(), "what the newest closed stays closed"
    assert json.loads((root / ".ssh-access-state").read_text())["revision"] == 5

    assert _gate(root, 5, {"athr_a": [gpu]}, {"athr_a": 1})["fresh"], "a retry is the newest"


def test_the_gate_refuses_a_member_root_others_can_write(tmp_path: Path) -> None:
    root = tmp_path / "members"
    root.mkdir()
    root.chmod(0o777)
    result = _run(
        host_ssh._GATE_SCRIPT,
        {
            "revision": 1,
            "members": {},
            "slots": {},
            "state_file": str(root / ".ssh-access-state"),
            "root": str(root),
            "owner_uid": os.getuid(),
            "capacity": 1,
        },
    )
    assert result.returncode != 0


def _servers(target: Path) -> list[str]:
    return sorted(path.name for path in (target / "servers").iterdir())


def _localize(payload: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    text = json.dumps(payload)
    text = text.replace("/run/agent-members", str(tmp_path / "members"))
    return json.loads(text.replace("/home/", str(tmp_path / "home") + "/"))


def test_a_starting_member_gets_private_files_and_the_include_comes_first(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home" / "agent-member-1"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host personal\n  User me\n")
    (tmp_path / "members" / "1").mkdir(parents=True)
    gpu, db = _server("gpu"), _server("db")

    result = _run(host_ssh._MEMBER_SCRIPT, _localize(host_ssh.write_payload(1, [gpu]), tmp_path))

    assert result.returncode == 0, result.stderr
    target = tmp_path / "members" / "1" / "ssh"
    entry = target / "servers" / gpu.server_id
    assert (entry / "key").read_text() == _KEY
    assert (entry / "digest").read_text().strip() == gpu.digest()
    assert stat.S_IMODE((entry / "key").stat().st_mode) == 0o400
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    lines = (home / ".ssh" / "config").read_text().splitlines()
    assert lines == [f"Include {target}/servers/*/config", "Host personal", "  User me"]
    assert stat.S_IMODE((home / ".ssh" / "config").stat().st_mode) == 0o600

    # The next turn replaces the directory whole and keeps one include line.
    again = _run(host_ssh._MEMBER_SCRIPT, _localize(host_ssh.write_payload(1, [db]), tmp_path))
    assert again.returncode == 0, again.stderr
    assert _servers(target) == [db.server_id]
    assert (home / ".ssh" / "config").read_text().count("Include ") == 1


def test_narrowing_only_removes_what_the_newest_state_no_longer_allows(tmp_path: Path) -> None:
    (tmp_path / "members" / "1").mkdir(parents=True)
    gpu, db = _server("gpu"), _server("db")
    write = _localize(host_ssh.write_payload(1, [gpu, db]), tmp_path)
    assert _run(host_ssh._MEMBER_SCRIPT, write).returncode == 0
    target = tmp_path / "members" / "1" / "ssh"

    def narrow(keep: set[str]) -> None:
        result = _run(
            host_ssh._MEMBER_SCRIPT, _localize(host_ssh.narrow_payload(1, keep), tmp_path)
        )
        assert result.returncode == 0, result.stderr

    narrow({gpu.digest(), _server("lab").digest()})
    assert _servers(target) == [gpu.server_id], "an allowed server it never had is not added"
    assert sorted(path.name for path in target.iterdir()) == ["servers"]

    narrow({_server("gpu", private_key=private_key()).digest()})
    assert _servers(target) == [], "a new key for the same server is not the key it holds"


# ── applying a state ─────────────────────────────────────────────────────


class FakeBackend:
    gid = 10001


class FakeManager:
    """Runs the real in-guest scripts against a temporary tree, as the guest would."""

    def __init__(self, tmp_path: Path, *, fail_slot: int | None = None) -> None:
        self.backend = FakeBackend()
        self.tmp_path = tmp_path
        self.fail_slot = fail_slot
        self.execs: list[dict[str, Any]] = []
        self.targets: list[set[tuple[str, int]]] = []
        root = tmp_path / "members"
        root.mkdir(mode=0o711)
        for slot in range(_spec().resident_slot_capacity):
            (root / str(slot)).mkdir()
            (tmp_path / "home" / f"agent-member-{slot}").mkdir(parents=True)

    def exec_capture(self, spec, argv, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        request = json.loads(kwargs["input_bytes"])
        self.execs.append({"argv": argv, "request": request, **kwargs})
        if self.fail_slot is not None and kwargs["user_uid"] == MEMBER_UID_BASE + self.fail_slot:
            return subprocess.CompletedProcess(argv, 1, b"", b"tmpfs is full")
        local = _localize(request, self.tmp_path)
        if argv[3] == host_ssh._GATE_SCRIPT:
            local["owner_uid"] = os.getuid()
        result = _run(argv[3], local)
        return subprocess.CompletedProcess(
            argv, result.returncode, result.stdout.encode(), result.stderr.encode()
        )

    def sync_ssh_targets(self, spec, endpoints: set[tuple[str, int]]) -> None:
        self.targets.append(set(endpoints))

    def held(self, slot: int) -> list[str]:
        """The server ids a member holds a key for, by name."""
        servers = self.tmp_path / "members" / str(slot) / "ssh" / "servers"
        if not servers.is_dir():
            return []
        names = []
        for entry in servers.iterdir():
            config = (entry / "config").read_text()
            names.append(config.split("Host ", 1)[1].split("\n", 1)[0])
        return sorted(names)


def _access(revision: int, *members: tuple[str, int | None, list[SshServerAccess]]) -> SshAccess:
    return SshAccess(
        revision=revision,
        members=[
            SshMemberAccess(thread_id=thread, slot_id=slot, servers=servers)
            for thread, slot, servers in members
        ],
    )


def _apply(manager: FakeManager, access: SshAccess, starting: str | None) -> dict[str, Any]:
    return host_ssh.apply_ssh_access(
        manager,  # type: ignore[arg-type]
        _spec(),
        access,
        starting_thread_id=starting,
    )


_GPU = _server("gpu")
_DB = _server("db", port=2222)
_LAB = _server("lab", port=2200)


def test_a_starting_turn_writes_its_own_keys_and_everyone_else_only_narrows(
    tmp_path: Path,
) -> None:
    manager = FakeManager(tmp_path)
    access = _access(7, ("a", 2, [_GPU]), ("b", 3, [_DB]), ("c", 4, []), ("d", None, [_LAB]))

    assert _apply(manager, access, "a") == {"newest": True, "revision": 7}

    gate, *members = manager.execs
    assert gate["user_uid"] == 0
    assert manager.targets == [
        {("93.184.216.34", 22), ("93.184.216.34", 2222), ("93.184.216.34", 2200)}
    ]
    assert [(call["user_uid"], call["request"]["mode"]) for call in members] == [
        (MEMBER_UID_BASE + 2, "write"),
        (MEMBER_UID_BASE + 3, "narrow"),
    ]
    assert (manager.held(2), manager.held(3)) == (["gpu"], []), "b's key waits for b's turn"
    assert all(_KEY not in " ".join(call["argv"]) for call in manager.execs)
    assert _KEY not in json.dumps([gate["request"], members[1]["request"]])


def test_a_push_takes_access_away_and_never_brings_a_key(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    _apply(manager, _access(5, ("a", 0, [_GPU, _DB]), ("b", 1, [_GPU])), "a")
    _apply(manager, _access(6, ("a", 0, [_GPU, _DB]), ("b", 1, [_GPU])), "b")

    # The owner turns db off for a and lab on for b, then gives gpu a new key.
    _apply(manager, _access(7, ("a", 0, [_GPU]), ("b", 1, [_GPU, _LAB])), None)
    assert (manager.held(0), manager.held(1)) == (["gpu"], ["gpu"])
    rekeyed = _server("gpu", private_key=private_key())
    _apply(manager, _access(8, ("a", 0, [rekeyed]), ("b", 1, [rekeyed, _LAB])), None)
    assert (manager.held(0), manager.held(1)) == ([], [])

    pushes = [call for call in manager.execs[-6:] if call["user_uid"] != 0]
    assert {call["request"]["mode"] for call in pushes} == {"narrow"}
    assert all(_KEY not in json.dumps(call["request"]) for call in pushes)
    assert manager.targets[-1] == {("93.184.216.34", 22), ("93.184.216.34", 2200)}

    # b's next turn brings what it was given meanwhile, with the keys it masks.
    _apply(manager, _access(9, ("a", 0, [rekeyed]), ("b", 1, [rekeyed, _LAB])), "b")
    assert manager.held(1) == ["gpu", "lab"]


def test_an_older_state_is_applied_only_as_far_as_the_newest_allows(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    # The owner turned db off (revision 9) after a's claim read revision 8.
    _apply(manager, _access(9, ("a", 0, [_GPU])), None)
    opened = list(manager.targets)

    result = _apply(manager, _access(8, ("a", 0, [_GPU, _DB])), "a")

    assert result == {"newest": False, "revision": 9}
    assert manager.held(0) == ["gpu"], "the late claim still gets what is allowed"
    assert manager.targets == opened, "an older state leaves the bridge to the newest"


def test_a_newest_state_that_fails_closes_the_bridge(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path, fail_slot=1)
    with pytest.raises(SandboxError, match="slot 1"):
        _apply(manager, _access(4, ("a", 1, [_GPU])), "a")
    assert manager.targets == [{("93.184.216.34", 22)}, set()]

    # An older claim arriving afterwards does not reopen it; a newer one does.
    manager.fail_slot = None
    assert _apply(manager, _access(3, ("a", 1, [_GPU])), "a")["newest"] is False
    assert manager.targets[-1] == set()
    assert _apply(manager, _access(5, ("a", 1, [_GPU])), "a")["newest"] is True
    assert manager.targets[-1] == {("93.184.216.34", 22)}


def test_an_older_state_that_fails_leaves_the_bridge_alone(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    _apply(manager, _access(9, ("a", 1, [_GPU])), None)
    manager.fail_slot = 1
    with pytest.raises(SandboxError):
        _apply(manager, _access(8, ("a", 1, [_GPU])), "a")
    assert manager.targets == [{("93.184.216.34", 22)}]


def test_a_slot_outside_the_sandbox_is_refused_before_anything_runs(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    with pytest.raises(SandboxError, match="outside"):
        _apply(manager, _access(1, ("a", 99, [_GPU])), "a")
    assert manager.execs == []


@pytest.mark.parametrize(
    "answer",
    [
        b"not json",
        b'{"fresh": "yes", "revision": 1, "members": {}, "keep_slots": []}',
        b'{"fresh": true, "revision": 1, "members": {"a": ["../x"]}, "keep_slots": []}',
    ],
)
def test_an_unreadable_gate_answer_is_refused(tmp_path: Path, answer: bytes) -> None:
    manager = FakeManager(tmp_path)
    manager.exec_capture = lambda *_args, **kwargs: subprocess.CompletedProcess([], 0, answer, b"")  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="unreadable"):
        _apply(manager, _access(1, ("a", 0, [_GPU])), "a")
    assert manager.targets == []
