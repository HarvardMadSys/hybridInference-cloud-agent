"""Exercise the production normalizer with real Linux UID/DAC boundaries.

Set AGENT_PERMISSION_TEST_IMAGE to a locally available Python image. Containers
have no network or mounts, and normalization runs without Linux capabilities.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE, ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend

pytestmark = pytest.mark.external


_LINUX_CHECK = r"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

payload = json.load(sys.stdin)
owner = payload["owner_uid"]
member = payload["member_uid"]
gid = payload["gid"]
registry = Path("/agent-state/claude/sessions")
history = Path("/agent-state/claude/projects/project")
workspace = Path("/workspace/repo")
directories = [workspace, registry, history, Path("/agent-state/.platform")]
for directory in directories:
    directory.mkdir(parents=True, exist_ok=True)
(registry / "123.json").write_text('{"pid":123,"sessionId":"process-only"}')
(history / "child.jsonl").write_text('native conversation stays unchanged\n')
(workspace / "result.txt").write_text('shared workspace output\n')
if payload["deny_history"]:
    blocked = history / "inaccessible-native-history"
    blocked.mkdir()
    (blocked / "parent.jsonl").write_text('must not silently skip real history\n')
    os.chown(blocked / "parent.jsonl", owner, gid)
    os.chown(blocked, owner, gid)
    blocked.chmod(0o700)
for root in [Path("/workspace"), workspace, Path("/agent-state"),
             Path("/agent-state/claude"), history.parent]:
    os.chown(root, owner, gid)
    root.chmod(0o2770)
for path in [history / "child.jsonl", workspace / "result.txt", history]:
    os.chown(path, member, gid)
    path.chmod(0o700 if path.is_dir() else 0o600)
os.chown(registry / "123.json", owner, gid)
(registry / "123.json").chmod(0o600)
os.chown(registry, owner, gid)
registry.chmod(0o700)  # Claude repeats this on each process start/resume.
platform = Path("/agent-state/.platform")
os.chown(platform, owner, gid)
platform.chmod(0o700)

def as_uid(uid, command):
    return subprocess.run(command, user=uid, group=gid, extra_groups=[],
                          capture_output=True, text=True, timeout=20)

capabilities = as_uid(member, ["python3", "-c",
    "from pathlib import Path; print(next(x for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:')))"])
assert capabilities.returncode == 0, capabilities.stderr
assert capabilities.stdout.split()[-1] == "0000000000000000", capabilities.stdout
result = as_uid(member, payload["command"])
registry_read = as_uid(owner, ["python3", "-c",
    "from pathlib import Path; print(Path('/agent-state/claude/sessions/123.json').read_text())"])
assert registry_read.returncode == 0, registry_read.stderr
history_read = as_uid(member, ["python3", "-c",
    "from pathlib import Path; print(Path('/agent-state/claude/projects/project/child.jsonl').read_text(), end='')"])
print(json.dumps({
    "returncode": result.returncode,
    "stderr": result.stderr,
    "registry_mode": stat.S_IMODE(registry.stat().st_mode),
    "registry_content": registry_read.stdout.strip(),
    "history_mode": stat.S_IMODE((history / "child.jsonl").stat().st_mode),
    "workspace_mode": stat.S_IMODE((workspace / "result.txt").stat().st_mode),
    "history_content": history_read.stdout,
}))
"""


@pytest.mark.parametrize("slot_id,deny_history", [(1, False), (0, False), (1, True)])
def test_claude_process_registry_does_not_break_member_settlement(
    monkeypatch: pytest.MonkeyPatch, slot_id: int, deny_history: bool
) -> None:
    image = os.environ.get("AGENT_PERMISSION_TEST_IMAGE")
    if not image:
        pytest.skip("set AGENT_PERMISSION_TEST_IMAGE to enable the Linux UID regression")
    manager = ChatSandboxManager(ContainerBackend(image="unused"))
    spec = ChatSandboxSpec(
        thread_id="athr_permissions",
        sandbox_id="asbx_permissions",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    commands: list[list[str]] = []

    def capture(_spec, command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture)
    manager.normalize_member_files(
        spec,
        slot_id=slot_id,
        user_uid=MEMBER_UID_BASE + slot_id,
        user_gid=manager.backend.gid,
    )
    assert len(commands) == 1
    payload = {
        "command": commands[0],
        "owner_uid": MEMBER_UID_BASE,
        "member_uid": MEMBER_UID_BASE + slot_id,
        "gid": manager.backend.gid,
        "deny_history": deny_history,
    }
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "-i",
            "--network=none",
            "--user=0:0",
            f"--group-add={manager.backend.gid}",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--cap-add=SETUID",
            "--cap-add=SETGID",
            "--security-opt=no-new-privileges",
            "--entrypoint=python3",
            image,
            "-c",
            _LINUX_CHECK,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["registry_mode"] == 0o700, observed
    assert json.loads(observed["registry_content"]) == {"pid": 123, "sessionId": "process-only"}
    assert observed["history_content"] == "native conversation stays unchanged\n", observed
    assert observed["workspace_mode"] & 0o060 == 0o060, observed
    if deny_history:
        assert observed["returncode"] != 0, observed
        assert "inaccessible-native-history" in observed["stderr"], observed
    else:
        assert observed["returncode"] == 0, observed
        assert observed["history_mode"] & 0o060 == 0o060, observed
