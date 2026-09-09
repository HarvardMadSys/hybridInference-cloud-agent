"""The immutable in-sandbox boundary for per-turn background processes."""

from __future__ import annotations

import importlib.util
import signal
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "deploy" / "sandbox" / "member-process-reaper"
DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"


def _load_helper():
    loader = SourceFileLoader("member_process_reaper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_helper_is_root_owned_image_code_not_a_member_writable_baseline():
    dockerfile = DOCKERFILE.read_text()

    assert "COPY deploy/sandbox/member-process-reaper" in dockerfile
    assert "chmod 0555 /usr/local/bin/member-process-reaper" in dockerfile
    assert ".baseline" not in HELPER.read_text()


def test_baseline_parser_rejects_pid_aliasing_and_non_numeric_identity():
    helper = _load_helper()

    assert helper._parse_baseline(b"41 400\n42 500\n") == {(41, 400), (42, 500)}
    with pytest.raises(ValueError, match="identity fence"):
        helper._parse_baseline(b"41 400\n41 500\n")
    with pytest.raises(ValueError, match="malformed"):
        helper._parse_baseline(b"41 not-a-start-time\n")


def test_scan_to_signal_race_never_kills_a_reused_pid(monkeypatch):
    helper = _load_helper()
    killed: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(helper, "_extras", lambda *_args: {(42, 500)})
    monkeypatch.setattr(helper, "_identity", lambda *_args: (42, 501))
    monkeypatch.setattr(helper.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert helper._signal_extras(11001, set(), signal.SIGKILL) is False
    assert killed == []


def test_reaper_escalates_and_fails_closed_on_a_persistent_forker(monkeypatch):
    helper = _load_helper()
    signals: list[signal.Signals] = []

    def still_forking(_uid, _baseline, sig):
        signals.append(sig)
        return False

    monkeypatch.setattr(helper, "_signal_extras", still_forking)
    monkeypatch.setattr(helper, "_extras", lambda *_args: {(77, 700)})
    monkeypatch.setattr(helper.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="survived cleanup"):
        helper._reap(11001, set())
    assert signals.count(signal.SIGTERM) == 20
    assert signals.count(signal.SIGKILL) == 64
