"""Every agent CLI is told how GitHub works here, each the way it takes instructions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_agent_host.runtimes.live import member_launch

REPO = Path(__file__).resolve().parents[2]


def with_platform_instructions(
    runtime_id: str, argv: list[str], env: dict[str, str], instructions: str
) -> tuple[list[str], dict[str, str]]:
    """The runtime's own placement, as the transport factory calls it."""
    return member_launch(runtime_id).with_platform_instructions(argv, env, instructions)


_TEXT = 'Open the PR yourself.\nUse `gh api` and "REST".'


@pytest.mark.parametrize("runtime_id", ["claude-code", "pi"])
def test_claude_and_pi_append_to_their_system_prompt(runtime_id):
    argv, env = with_platform_instructions(runtime_id, ["cli", "--model", "m"], {"A": "1"}, _TEXT)
    assert argv == ["cli", "--model", "m", "--append-system-prompt", _TEXT]
    assert env == {"A": "1"}


def test_codex_gets_developer_instructions_before_its_subcommand():
    argv, _env = with_platform_instructions(
        "codex", ["launcher", "codex", "-c", "x=1", "app-server"], {}, _TEXT
    )
    assert argv[-1] == "app-server"
    assert argv[-3] == "-c"
    key, _, value = argv[-2].partition("=")
    assert key == "developer_instructions"
    assert json.loads(value) == _TEXT  # a JSON string is a TOML basic string


@pytest.mark.parametrize("runtime_id", ["opencode", "kilo"])
def test_the_opencode_family_gets_it_through_its_wrapper_environment(runtime_id):
    argv, env = with_platform_instructions(runtime_id, ["bridge"], {}, _TEXT)
    assert argv == ["bridge"]
    assert env == {f"{runtime_id.upper()}_FREEINFERENCE_INSTRUCTIONS": _TEXT}


def test_no_instructions_changes_nothing():
    assert with_platform_instructions("codex", ["codex", "app-server"], {}, "") == (
        ["codex", "app-server"],
        {},
    )


@pytest.mark.parametrize("name", ["opencode", "kilo"])
def test_the_wrapper_lists_the_instructions_file_in_the_config_it_writes(tmp_path, name):
    """Run the image's real wrapper with a stand-in CLI that prints its config."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / name
    fake.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"sys.stdout.write(open(os.environ['{name.upper()}_CONFIG']).read())\n"
    )
    fake.chmod(0o755)
    config_dir = tmp_path / "config"
    prefix = name.upper()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": "https://models.example/v1",
        "OPENAI_API_KEY": "key",
        f"{prefix}_GATEWAY_MODEL": "model",
        f"{prefix}_FREEINFERENCE_CONFIG_DIR": str(config_dir),
    }
    wrapper = REPO / "deploy" / "sandbox" / f"{name}-freeinference"

    def run(extra: dict[str, str]) -> dict:
        done = subprocess.run(
            [sys.executable, str(wrapper), "serve"],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(done.stdout)

    config = run({f"{prefix}_FREEINFERENCE_INSTRUCTIONS": _TEXT})
    (listed,) = config["instructions"]
    assert Path(listed).read_text() == _TEXT
    assert Path(listed).parent == config_dir
    # A later run without instructions leaves neither the entry nor the file.
    assert "instructions" not in run({})
    assert not Path(listed).exists()


def test_the_instructions_say_facts_and_no_way_of_working(monkeypatch):
    """How to work, and how to open a pull request, is the user's and the repository's to say."""
    from types import SimpleNamespace

    from cloud_agent_host.platform_instructions import platform_instructions

    monkeypatch.setenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", "/ca.pem")
    launch = SimpleNamespace(
        github_credential="agh.placeholder", github_repo="owner/repo", git_coauthor="Bot <b@x>"
    )
    text = platform_instructions(launch, coauthor_trailer=True)

    assert text.startswith("# Working in this Cloud Agent sandbox\n")
    assert "a checkout of `owner/repo`" in text
    assert "`Co-Authored-By: Bot <b@x>`" in text
    assert "gh api repos/owner/repo/pulls -f title=" in text
    assert "Deleting branches and pushing tags are refused" in text
    assert "`git branch` lists it" in text
    assert "it is not always `main`" in text
    for policy in (
        "open it yourself",
        "do not ask",
        "use a button",
        "as a draft unless",
        "never push to the default branch",
        "Read the repository's own instructions",
        "-F draft=true -f body",
    ):
        assert policy not in text
    without_repo = SimpleNamespace(github_credential=None, github_repo="o/r", git_coauthor=None)
    assert platform_instructions(without_repo, coauthor_trailer=True) == ""

    monkeypatch.delenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", raising=False)
    local = platform_instructions(launch, coauthor_trailer=True)
    assert "GitHub credentials are not available in this environment" in local
    assert "`git branch` lists it" in local
    assert "gh api repos/owner/repo/pulls" not in local
    assert "`Co-Authored-By: Bot <b@x>`" in local
