"""The local dev script reads a token set under its former name, and says so."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from tests.unit.test_deploy_config import _LOCAL_DEV_SCRIPT

if TYPE_CHECKING:
    from pathlib import Path


def test_local_dev_script_keeps_a_token_set_under_its_former_name_and_says_so(
    tmp_path: Path,
) -> None:
    """The run that reads the former name copies its value to the current one.

    It names the former variable on stderr, never the value, which is the
    sandbox manager's credential. A later run reads the current name, silently.
    """
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    dotenv = script.parent / ".env.local"
    dotenv.write_text("AGENT_WORKSPACE_BROKER_TOKEN=keep-this-token\nLOCAL_EDGE_PORT=13002\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # This docker fails every command, so the script falls back to standalone
    # docker-compose even where the Compose plugin is installed, and no engine
    # answers its queries.
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 1\n")
    fake_docker.chmod(0o755)
    fake_compose = fake_bin / "docker-compose"
    compose_tokens = tmp_path / "compose-tokens"
    fake_compose.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$AGENT_SANDBOX_MANAGER_TOKEN" >>"$COMPOSE_TOKEN_LOG"\n'
    )
    fake_compose.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "COMPOSE_TOKEN_LOG": str(compose_tokens),
    }
    env.pop("AGENT_SANDBOX_MANAGER_TOKEN", None)
    env.pop("AGENT_WORKSPACE_BROKER_TOKEN", None)

    def config() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(script), "config"], env=env, capture_output=True, text=True, check=False
        )

    first = config()
    second = config()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (
        "AGENT_WORKSPACE_BROKER_TOKEN is a former name for AGENT_SANDBOX_MANAGER_TOKEN"
        in first.stderr
    )
    assert "keep-this-token" not in first.stderr + first.stdout
    assert "former name" not in second.stderr
    assert "AGENT_SANDBOX_MANAGER_TOKEN=keep-this-token" in dotenv.read_text().splitlines()
    assert compose_tokens.read_text().splitlines() == ["keep-this-token", "keep-this-token"]
