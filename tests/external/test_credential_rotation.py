"""Credential behavior of the *real*, pinned claude-code CLI.

Two upstream facts, both measured rather than believed:

1. **In-process rotation never takes effect.** The ``apiKeyHelper`` result is
   fetched once per process; ``CLAUDE_CODE_API_KEY_HELPER_TTL_MS`` (unset, 0
   and 1 alike) changes nothing, and a 401 on a revoked key produces an error
   result with no refetch and no retry. This is why a live session uses one
   residency-lifetime credential instead of attempting per-turn rotation.
2. **A replacement process can resume with a replacement credential.** This
   is the recovery behavior used after a stopped container is started again;
   it is not a one-shot platform execution path.

The pinned version is read from the sandbox Dockerfile, so this suite always
measures the CLI the image actually ships.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil

import pytest

from tests.support.claude_cli import ClaudeCli, FakeAnthropicGateway

pytestmark = pytest.mark.external

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _pinned_cli_version() -> str:
    dockerfile = (_REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    return match.group(1)


def _cli(**kwargs: object) -> ClaudeCli:
    """The pinned version: this suite always measures the CLI the image ships."""
    return ClaudeCli(version=_pinned_cli_version(), **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def gateway():
    fake = FakeAnthropicGateway()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _needs_npx():
    if shutil.which("npx") is None:
        pytest.skip("npx is required to run the pinned CLI")


def _config_with_helper(base: pathlib.Path, credential_value: str) -> pathlib.Path:
    config = base / "claude"
    config.mkdir(parents=True, exist_ok=True)
    credential = config / "credential"
    credential.write_text(credential_value + "\n")
    helper = config / "helper.sh"
    helper.write_text(f'#!/bin/sh\ncat "{credential}"\n')
    helper.chmod(0o755)
    (config / "settings.json").write_text(json.dumps({"apiKeyHelper": str(helper)}))
    return config


def test_in_process_rotation_never_takes_effect(tmp_path, gateway):
    """The disproof this architecture stands on. Allowed to break only when a
    CLI upgrade makes in-process rotation real — then reconsider live sessions."""
    config = _config_with_helper(tmp_path, "grant-TURN-A")
    workdir = tmp_path / "ws"
    workdir.mkdir()
    cli = _cli(workdir=workdir, config_dir=config, gateway_port=gateway.port, credential=None)
    try:
        assert cli.turn("say ok") is not None
        first_turn_requests = len(gateway.keys)
        assert set(gateway.keys) == {"grant-TURN-A"}

        (config / "credential").write_text("grant-TURN-B\n")
        assert cli.turn("say ok again") is not None
    finally:
        cli.close()

    second_turn_keys = set(gateway.keys[first_turn_requests:])
    assert second_turn_keys == {"grant-TURN-A"}, (
        "the pinned CLI re-read the helper mid-process: in-process rotation has "
        "become real — revisit the per-turn-restart decision (design §13)"
    )


def test_replacement_process_resumes_with_a_replacement_grant(tmp_path, gateway):
    """A cold process replacement resumes the same native session with a new grant."""
    config = tmp_path / "claude"
    config.mkdir()
    workdir = tmp_path / "ws"
    workdir.mkdir()

    first = _cli(
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="grant-ATTEMPT-1",
    )
    try:
        result = first.turn("remember the number 41")
        assert result is not None
        session_id = result.get("session_id")
        assert session_id
    finally:
        first.close()
    boundary = len(gateway.keys)
    assert set(gateway.keys[:boundary]) == {"grant-ATTEMPT-1"}

    second = _cli(
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="grant-ATTEMPT-2",
        resume_session_id=session_id,
    )
    try:
        assert second.turn("now add one") is not None
    finally:
        second.close()

    assert gateway.keys[boundary:], "the resumed turn made no model calls"
    assert set(gateway.keys[boundary:]) == {"grant-ATTEMPT-2"}
