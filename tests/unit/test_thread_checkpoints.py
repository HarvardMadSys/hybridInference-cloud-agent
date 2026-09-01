"""The COLD loop's platform pieces (design §6, issue #56).

Three seams, each tested where it lives: the claude driver's export/restore
round trip (files in, files out, traversal refused), the checkpoint seal
(encrypted at rest, defects answered with None), and the claim gate (only the
thread's present, at the version about to run, is ever handed out).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import pathlib

import cloud_agent.checkpoints as checkpoints
from cloud_agent.checkpoints import gate_for_claim, open_checkpoint
from cloud_agent_host.drivers import (
    ClaudeCodeDriver,
    DriverError,
    HarnessState,
    LaunchContext,
    StateExportUnsupported,
    TurnInput,
    driver_for,
    get_driver,
)
from cloud_agent_host.runner import state_from_checkpoint
from cloud_agent_host.runtimes import EMPTY_RUNTIME_MCP_CONFIG, AgentRuntime


class _Process:
    def lines(self):
        yield from ()

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return 0

    def stderr_text(self) -> str:
        return ""


class _ClaudeStub(AgentRuntime):
    """Accepts the claude-specific prepare kwargs and records them."""

    name = "claude-code"

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.prepared: dict | None = None

    def prepare(self, **kwargs):
        self.prepared = kwargs
        return ["claude"], {}

    def parse_event(self, line: str):
        return None


def _context(state_dir: pathlib.Path | None) -> LaunchContext:
    return LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr",
        provides_isolation=True,
        spawn=lambda argv, env: _Process(),
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=None if state_dir is None else str(state_dir),
    )


def _seed_session(state_dir: pathlib.Path, session_id: str) -> pathlib.Path:
    project = state_dir / "claude" / "projects" / "-workspace"
    project.mkdir(parents=True)
    session_file = project / f"{session_id}.jsonl"
    session_file.write_text('{"role":"user"}\n')
    return session_file


def test_claude_export_then_restore_round_trips_the_session(tmp_path):
    """The whole point: the next turn's sandbox sees the same session file."""
    exporter = ClaudeCodeDriver(_ClaudeStub())
    old_dir = tmp_path / "old"
    _seed_session(old_dir, "abc-123")
    session = exporter.launch(_context(old_dir))
    exporter.runtime.session_id = "abc-123"

    state = session.export_state()
    assert state.session_id == "abc-123"

    restorer = ClaudeCodeDriver(_ClaudeStub())
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    restored = restorer.launch(_context(new_dir), state)
    assert (new_dir / "claude" / "projects" / "-workspace" / "abc-123.jsonl").read_text() == (
        '{"role":"user"}\n'
    )

    restored.start_turn(TurnInput(prompt="continue", model="m"))
    prepared = restorer.runtime.prepared
    assert prepared["resume_session_id"] == "abc-123"
    assert prepared["config_dir"] == "/agent-state/claude"
    assert prepared["task_prompt"] == "continue"


def test_export_refuses_partial_answers(tmp_path):
    """No state mount, no announced session, no file: refuse, never guess."""
    driver = ClaudeCodeDriver(_ClaudeStub())
    with pytest.raises(StateExportUnsupported, match="state mount"):
        driver.launch(_context(None)).export_state()

    empty = tmp_path / "s"
    empty.mkdir()
    with pytest.raises(StateExportUnsupported, match="session id"):
        driver.launch(_context(empty)).export_state()

    driver.runtime.session_id = "abc"
    with pytest.raises(StateExportUnsupported, match="no session file"):
        driver.launch(_context(empty)).export_state()


def test_restore_refuses_traversal(tmp_path):
    """A checkpoint is data; a member that names ../ is an attack, not a path."""
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as archive:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"hi"))
    target = tmp_path / "s"
    target.mkdir()

    with pytest.raises(DriverError, match="escapes"):
        ClaudeCodeDriver(_ClaudeStub()).launch(
            _context(target), HarnessState(session_id="abc", blob=evil.getvalue())
        )


def test_the_registry_hands_out_the_claude_driver():
    assert isinstance(get_driver("claude-code"), ClaudeCodeDriver)
    assert get_driver("claude-code").capabilities().continuity == "resume"
    assert get_driver("claude-code").capabilities().checkpoint_export is True
    assert driver_for(_ClaudeStub()).__class__ is ClaudeCodeDriver
    for runtime_id in ("codex", "pi", "opencode", "kilo"):
        capabilities = get_driver(runtime_id).capabilities()
        assert capabilities.continuity == "resume"
        assert capabilities.checkpoint_export is False


@pytest.fixture
def cipher_key(monkeypatch):
    """A configured checkpoint key, with the module cache reset around it."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "test-key-material")
    monkeypatch.setattr(checkpoints, "_cipher", None)
    yield
    monkeypatch.setattr(checkpoints, "_cipher", None)


def _envelope(**overrides):
    blob = b"tar-bytes"
    envelope = {
        "harness_state": {
            "driver": "claude-code",
            "driver_version": "1.2.3",
            "state_format_version": 1,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob),
            "blob": base64.b64encode(blob).decode(),
        },
        "consistency": {"through_turn_id": "job-parent", "attempt_id": 7},
        "session_id": "abc",
    }
    envelope.update(overrides)
    return envelope


def test_sealed_checkpoints_are_ciphertext_and_round_trip(cipher_key):
    cipher = checkpoints.checkpoint_cipher()
    assert cipher is not None
    sealed = cipher.encrypt(json.dumps(_envelope()))

    assert "job-parent" not in sealed  # at rest means at rest
    assert open_checkpoint(sealed) == _envelope()
    assert open_checkpoint("garbage") is None


def test_without_a_key_everything_fails_closed(monkeypatch):
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(checkpoints, "_cipher", None)

    assert checkpoints.checkpoint_cipher() is None
    assert open_checkpoint("anything") is None


def test_the_claim_gate_compares_against_the_attempts_own_stamp():
    """The stamp the claim wrote, never a fresh host read (review finding 3):
    a rolling upgrade's heartbeat moving the host row between claim and gate
    must change nothing about what this attempt may resume."""
    ok = gate_for_claim(
        _envelope(),
        runtime="claude-code",
        parent_job_id="job-parent",
        stamped_runtime_version="1.2.3",
    )
    assert ok is not None

    refusals: list[dict] = [
        {"runtime": "codex", "parent_job_id": "job-parent", "stamped_runtime_version": "1.2.3"},
        {
            "runtime": "claude-code",
            "parent_job_id": "job-OTHER",
            "stamped_runtime_version": "1.2.3",
        },
        {
            "runtime": "claude-code",
            "parent_job_id": "job-parent",
            "stamped_runtime_version": "9.9.9",
        },
        {"runtime": "claude-code", "parent_job_id": "job-parent", "stamped_runtime_version": ""},
        {"runtime": "claude-code", "parent_job_id": "job-parent", "stamped_runtime_version": None},
    ]
    for kwargs in refusals:
        assert gate_for_claim(_envelope(), **kwargs) is None


def test_state_from_checkpoint_verifies_what_it_decodes():
    good = state_from_checkpoint(_envelope())
    assert good is not None
    assert (good.session_id, good.blob) == ("abc", b"tar-bytes")

    tampered = _envelope()
    tampered["harness_state"]["sha256"] = "0" * 64
    assert state_from_checkpoint(tampered) is None
    assert state_from_checkpoint({"harness_state": {}, "session_id": ""}) is None


def test_launching_claude_installs_the_tool_boundary_hooks(tmp_path):
    """The sandbox is given hooks that mark when each tool call actually ran.

    Without them a call's window is only when the platform parsed its events,
    which brackets the execution from outside — and leaves calls announced in
    one assistant message sharing a window they did not really share.
    """
    driver = ClaudeCodeDriver(_ClaudeStub())
    session = driver.launch(_context(tmp_path))
    session.start_turn(TurnInput(prompt="go", model="m"))

    settings = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse"}
    for event in settings["hooks"].values():
        command = event[0]["hooks"][0]["command"]
        assert command.endswith("exit 0"), "a hook that can fail can fail a tool call"
        assert "/agent-state/tool-marks.log" in command


def test_the_hooks_do_not_replace_settings_the_directory_already_holds(tmp_path):
    """A restored state mount may carry the CLI's own configuration."""
    config = tmp_path / "claude"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(json.dumps({"model": "keep-me"}))

    driver = ClaudeCodeDriver(_ClaudeStub())
    session = driver.launch(_context(tmp_path))
    session.start_turn(TurnInput(prompt="go", model="m"))

    settings = json.loads((config / "settings.json").read_text())
    assert settings["model"] == "keep-me"
    assert "PreToolUse" in settings["hooks"]


def test_docker_owned_state_uses_the_retained_hook_writer(tmp_path):
    """A guest path must not be mistaken for a path on the broker host."""
    updates: list[dict] = []
    host_path = tmp_path / "not-the-retained-state"
    context = replace(
        _context(host_path),
        install_tool_hooks=lambda update: updates.append(update),
    )

    ClaudeCodeDriver(_ClaudeStub()).launch(context).start_turn(TurnInput(prompt="go", model="m"))

    assert len(updates) == 1
    assert set(updates[0]["hooks"]) == {"PreToolUse", "PostToolUse"}
    assert not host_path.exists()


def test_a_turn_without_a_state_mount_installs_nothing(tmp_path):
    """No mount, no place to write marks, and no attempt to."""
    driver = ClaudeCodeDriver(_ClaudeStub())
    session = driver.launch(_context(None))
    session.start_turn(TurnInput(prompt="go", model="m"))

    assert not (tmp_path / "claude").exists()
