"""GitHub authentication belongs to the owning resident, never the host fallback.

What a member holds is its residency's GitHub relay placeholder (``agh.``),
never a token GitHub accepts; and its commits are the owner's.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.member_loopback import HELPER_IN_PLACE
from tests.support.owners import launch_connection

from cloud_agent_host.container.chat_sandbox import MEMBER_UID_BASE, ChatSandboxSpec
from cloud_agent_host.member_setup.compat_helper import LAUNCHER
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory

_PLACEHOLDER = "agh.payload.signature"


@pytest.fixture(autouse=True)
def _relay_configured(monkeypatch):
    """These tests are the relay-on path, unless one turns the relay off."""
    monkeypatch.setenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", "/ca.pem")


def _context(runtime_id="claude-code", **changes):
    calls = []
    inherited = {"GH_TOKEN": "host-account-token", "GITHUB_TOKEN": "host-other-token"}
    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=lambda: inherited),
        reap_member_processes=lambda *_args, **_kwargs: None,
        # The sandbox's shared browser service, started before every CLI.
        exec_capture=lambda _spec, argv, **_kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        ensure_mcp_helpers=lambda *_args: None,
        ensure_kilo_mcp_runtime=lambda *_args: None,
        spawn=lambda spec, argv, **kwargs: calls.append({"argv": argv, **kwargs}),
        await_member_launch=lambda spec, **kwargs: None,
        install_tool_hooks=lambda spec, update, **kwargs: calls.append({"hooks": update}),
        exact=HELPER_IN_PLACE,
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
            credential="inference-grant",
            github_credential=_PLACEHOLDER,
            git_author_name="octo",
            git_author_email="7+octo@users.noreply.github.com",
            git_coauthor="FreeInference <9+freeinference[bot]@users.noreply.github.com>",
            github_repo="owner/repo",
            **launch_connection(runtime_id),
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
    assert calls[0]["env"]["GH_TOKEN"] == _PLACEHOLDER
    assert calls[0]["env"]["GH_REPO"] == "owner/repo"
    assert calls[0]["env"]["GH_HOST"] == "github.com"
    assert calls[0]["env"]["GH_PROMPT_DISABLED"] == "1"
    assert "GITHUB_TOKEN" not in context.base_env
    assert inherited == {"GH_TOKEN": "host-account-token", "GITHUB_TOKEN": "host-other-token"}
    # The owner authors and commits; the agent is named as co-author.
    assert calls[0]["env"]["GIT_AUTHOR_NAME"] == "octo"
    assert calls[0]["env"]["GIT_COMMITTER_EMAIL"] == "7+octo@users.noreply.github.com"
    assert calls[0]["env"]["FREEINFERENCE_GIT_COAUTHOR"].startswith("FreeInference <")


def test_without_a_relay_the_placeholder_is_not_presented_to_github(monkeypatch):
    """A placeholder sent to the public github.com is rejected, so it is withheld."""
    monkeypatch.delenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", raising=False)
    context, calls, _ = _context()
    context.spawn_live(["claude", "-p"], context.base_env)

    env = calls[0]["env"]
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["GIT_AUTHOR_NAME"] == "octo"
    assert env["GIT_CONFIG_COUNT"] == "1"
    text = calls[0]["argv"][-1]
    assert "GitHub credentials are not available in this environment" in text
    assert "`git branch` lists it" in text
    assert "gh api repos/owner/repo/pulls" not in text


def test_a_sibling_or_credentialless_launch_never_reuses_a_hosts_or_peers_token():
    owner, _, _ = _context()
    sibling, _, _ = _context(
        thread_id="athr_sibling", slot_id=1, github_credential="agh.sibling.signature"
    )
    plain, _, _ = _context(github_credential="")

    assert owner.base_env["GH_TOKEN"] == _PLACEHOLDER
    assert sibling.base_env["GH_TOKEN"] == "agh.sibling.signature"
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
    assert f"password={_PLACEHOLDER}\n" in result.stdout
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
    assert _PLACEHOLDER not in result.stdout + result.stderr


def test_relay_trust_is_the_sandboxs_not_a_members(monkeypatch, tmp_path):
    # Every process on the bridge reaches the relay, credential or not, so the
    # trust is installed for the whole container when a process is spawned
    # (tests/unit/test_github_relay_trust.py), not carried by a member's launch.
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", str(ca))
    context, _, _ = _context(slot_id=3)
    for name in ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"):
        assert name not in context.base_env


def test_claude_is_told_how_github_works_here_and_names_the_co_author():
    context, calls, _ = _context()
    context.spawn_live(["claude", "-p"], context.base_env)
    context.install_tool_hooks({"hooks": {}})

    # A direct plan's CLI starts behind its request meter; the CLI's own argv follows.
    assert calls[0]["argv"][: len(LAUNCHER)] == list(LAUNCHER)
    argv = calls[0]["argv"][len(LAUNCHER) :]
    assert argv[:2] == ["claude", "-p"]
    assert argv[-2] == "--append-system-prompt"
    assert "`gh api repos/owner/repo/pulls" in argv[-1]
    # Claude's own attribution adds the trailer; the instructions do not ask twice.
    assert "Co-Authored-By:" not in argv[-1]
    pi, pi_calls, _ = _context("pi")
    pi.spawn_live(["pi-freeinference"], pi.base_env)
    assert "Co-Authored-By: FreeInference <" in pi_calls[0]["argv"][-1]
    assert calls[1]["hooks"]["attribution"] == {
        "commit": "Co-Authored-By: FreeInference <9+freeinference[bot]@users.noreply.github.com>",
        "pr": "",
    }

    plain, plain_calls, _ = _context(github_credential="", git_coauthor="")
    plain.spawn_live(["claude", "-p"], plain.base_env)
    plain.install_tool_hooks({"hooks": {}})
    assert plain_calls[0]["argv"] == [*LAUNCHER, "claude", "-p"]
    assert plain_calls[1]["hooks"] == {"hooks": {}}
