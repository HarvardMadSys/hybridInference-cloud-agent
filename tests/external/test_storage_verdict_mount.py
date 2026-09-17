"""Exercise the Compose verdict bind against a real, local Linux Docker daemon.

Set AGENT_STORAGE_TEST_IMAGE to an existing runner image. The current probe is
mounted into a disposable container; no host stack or real verdict is changed.
Run on the Docker host, since bind source paths must exist on that machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from cloud_agent_host.host_capacity import DEFAULT_STORAGE_VERDICT_PATH

pytestmark = pytest.mark.external
REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=20).strip()


def test_a_running_replica_observes_verdict_unlink_recreation_and_replacement(tmp_path) -> None:
    """A file bind pins the old inode; this test must use the real Compose bind."""
    image = os.environ.get("AGENT_STORAGE_TEST_IMAGE")
    if not image:
        pytest.skip("set AGENT_STORAGE_TEST_IMAGE to an existing runner image on this Docker host")
    compose = yaml.safe_load((REPO_ROOT / "deploy/host/docker-compose.sandbox.yml").read_text())
    mount = next(
        value
        for value in compose["services"]["runner"]["volumes"]
        if isinstance(value, dict)
        and str(value.get("source", "")).startswith("../../.storage-probe")
    )
    source = (tmp_path / "deploy/host" / mount["source"]).resolve()
    target = Path(mount["target"])
    relative = Path(DEFAULT_STORAGE_VERDICT_PATH).relative_to(target)
    verdict_file = source / relative
    verdict_file.parent.mkdir(parents=True)
    verdict_file.write_text("{}")
    name = f"agent-storage-verdict-test-{uuid.uuid4().hex[:12]}"
    command = (
        "import dataclasses,json; from cloud_agent_host.host_capacity import build_storage_probe; "
        "print(json.dumps(dataclasses.asdict(build_storage_probe().sample())))"
    )

    def sample() -> dict:
        return json.loads(_docker("exec", name, "python", "-c", command))

    try:
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=bind,src={source},dst={target},readonly",
            "--mount",
            f"type=bind,src={REPO_ROOT}/cloud_agent_host/host_capacity.py,"
            "dst=/app/cloud_agent_host/host_capacity.py,readonly",
            "--entrypoint",
            "sleep",
            image,
            "300",
        )
        description = json.loads(
            _docker("exec", name, "python", "-m", "cloud_agent_host.host_capacity", "describe")
        )
        assert description["ok"], description
        entry = {
            "verdict": "verified",
            "size_bytes": description["total_bytes"],
            "mount_root": "/diagnostic-only",
        }
        document = {"schema_version": 1, "entries": {description["upperdir"]: entry}}
        verdict_file.write_text(json.dumps(document))
        assert sample()["total_bytes"] == description["total_bytes"]

        verdict_file.unlink()
        assert sample() == {"reason": "unverified"}
        verdict_file.write_text('{"schema_version":1,"entries":{}}')
        assert sample() == {"reason": "unverified"}
        verdict_file.write_text(json.dumps(document))
        assert sample()["total_bytes"] == description["total_bytes"]

        entry["verdict"] = "unsupported"
        replacement = verdict_file.with_name("replacement.json")
        replacement.write_text(json.dumps(document))
        replacement.replace(verdict_file)
        assert sample() == {"reason": "unsupported"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False)
