"""A host environment from before the service rename still passes, and says what to rename."""

from __future__ import annotations

from pathlib import Path

from tests.unit.test_retained_deploy import _add_required_host_paths, _host, _run


def test_existing_host_environment_can_pass_preflight_after_service_rename(tmp_path):
    env = _host(tmp_path)
    _add_required_host_paths(env, tmp_path)
    assert "former name" not in _run(env, "status").stderr
    dotenv = Path(env["APP_DIR"]) / ".env"
    contents = dotenv.read_text()
    for current, previous in (
        ("AGENT_SANDBOX_MANAGER_TOKEN", "AGENT_WORKSPACE_BROKER_TOKEN"),
        ("AGENT_SANDBOX_MANAGER_ADVERTISE_URL", "AGENT_WORKSPACE_BROKER_ADVERTISE_URL"),
        ("AGENT_SANDBOX_MANAGER_REVERSE_BIND", "AGENT_BROKER_REVERSE_BIND"),
        ("AGENT_SANDBOX_MANAGER_REVERSE_PORT", "AGENT_BROKER_REVERSE_PORT"),
    ):
        contents = contents.replace(current, previous)
    dotenv.write_text(contents)

    result = _run(env, "preflight")

    assert result.returncode == 0, result.stderr

    token_log = tmp_path / "manager-token"
    env["MANAGER_TOKEN_LOG"] = str(token_log)
    status = _run(env, "status")
    assert status.returncode == 0, status.stderr
    assert token_log.read_text().splitlines()[-1] == "test-sandbox-manager-token"
    # A former name that supplies a value says so, so the operator knows what
    # to rename; it never prints the value, here the manager's credential.
    assert (
        "[sandbox-host] WARNING: AGENT_WORKSPACE_BROKER_TOKEN is a former name for "
        "AGENT_SANDBOX_MANAGER_TOKEN; set that instead" in status.stderr
    )
    assert "test-sandbox-manager-token" not in status.stderr
