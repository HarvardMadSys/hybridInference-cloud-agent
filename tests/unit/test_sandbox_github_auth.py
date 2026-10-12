"""GitHub authentication belongs to the owning resident, never the host fallback."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE, ChatSandboxSpec
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory


def _context(runtime_id="claude-code", **changes):
    calls = []
    inherited = {"GH_TOKEN": "host-account-token", "GITHUB_TOKEN": "host-other-token"}
    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=lambda: inherited),
        reap_member_processes=lambda *_args, **_kwargs: None,
        ensure_mcp_helpers=lambda *_args: None,
        ensure_kilo_mcp_runtime=lambda *_args: None,
        spawn=lambda spec, argv, **kwargs: calls.append(kwargs),
    )
    spec = ChatSandboxSpec(
        thread_id="athr_owner",
        sandbox_id="asbx_group",
        generation=1,
        runtime_id=runtime_id,
        shared_live=True,
    )
    launch = replace(
        ResidentLaunch(
            thread_id="athr_owner",
            runtime_id=runtime_id,
            runtime_version=None,
            native_session_id=None,
            model="model",
            reasoning_effort=None,
            gateway_base_url="http://gateway",
            credential="inference-grant",
            github_token="sandbox-write-token",
            github_repo="owner/repo",
        ),
        **changes,
    )
    context = ResidentTransportFactory(manager)._launch_context(
        spec, launch, credential="inference-grant"
    )
    return context, calls, inherited


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_github_authentication_is_in_the_members_process_environment(runtime_id):
    context, calls, inherited = _context(runtime_id, slot_id=2)
    context.spawn_live(["agent"], context.base_env)

    assert calls[0]["user_uid"] == MEMBER_UID_BASE + 2
    assert calls[0]["env"]["GH_TOKEN"] == "sandbox-write-token"
    assert calls[0]["env"]["GH_REPO"] == "owner/repo"
    assert calls[0]["env"]["GH_HOST"] == "github.com"
    assert calls[0]["env"]["GH_PROMPT_DISABLED"] == "1"
    assert "GITHUB_TOKEN" not in context.base_env
    assert inherited == {"GH_TOKEN": "host-account-token", "GITHUB_TOKEN": "host-other-token"}


def test_a_sibling_or_credentialless_launch_never_reuses_a_hosts_or_peers_token():
    owner, _, _ = _context()
    sibling, _, _ = _context(thread_id="athr_sibling", slot_id=1, github_token="sibling-token")
    plain, _, _ = _context(github_token=None)

    assert owner.base_env["GH_TOKEN"] == "sandbox-write-token"
    assert sibling.base_env["GH_TOKEN"] == "sibling-token"
    assert "GH_TOKEN" not in plain.base_env
    assert "GITHUB_TOKEN" not in plain.base_env
    assert plain.base_env["GIT_CONFIG_COUNT"] == "1"


def test_git_https_uses_gh_without_writing_token_or_helper_configuration(tmp_path):
    context, _, _ = _context()
    helper = tmp_path / "gh"
    helper.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "auth git-credential get" ] || exit 1\n'
        'printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"\n'
    )
    helper.chmod(0o755)
    env = {
        **context.base_env,
        "HOME": str(tmp_path),
        "PATH": f"{tmp_path}:{context.base_env['PATH']}",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )

    assert "username=x-access-token\n" in result.stdout
    assert "password=sandbox-write-token\n" in result.stdout
    assert not (tmp_path / ".gitconfig").exists()
    assert not (tmp_path / ".config" / "gh" / "hosts.yml").exists()


def test_github_helper_does_not_offer_the_token_to_other_https_hosts(tmp_path):
    context, _, _ = _context()
    helper = tmp_path / "gh"
    helper.write_text('#!/bin/sh\nprintf "unexpected helper call\\n" >&2\nexit 1\n')
    helper.chmod(0o755)
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=other.example\n\n",
        cwd=tmp_path,
        env={
            **context.base_env,
            "HOME": str(tmp_path),
            "PATH": f"{tmp_path}:{context.base_env['PATH']}",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "unexpected helper call" not in result.stderr
    assert "sandbox-write-token" not in result.stdout + result.stderr
