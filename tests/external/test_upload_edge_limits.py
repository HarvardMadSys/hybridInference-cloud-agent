"""Exercise shipped nginx routes against local mocks on an isolated Docker network.

Pull nginx:1.27.5-alpine before selecting this external tier, or set
AGENT_UPLOAD_TEST_NGINX_IMAGE to an already available nginx image. No live
deployment, gateway credential, or external HTTP endpoint is used.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from cloud_agent.attachments.models import MAX_ATTACHMENT_FILE_BYTES

pytestmark = pytest.mark.external
REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("AGENT_UPLOAD_TEST_NGINX_IMAGE", "nginx:1.27.5-alpine")
CASES = [
    ("deploy/control-plane/edge.nginx.conf", "/api", 80),
    ("deploy/local/nginx.conf", "/api", 8088),
    ("deploy/reverse-proxy/agents-under-gateway.conf", "/agents/api", 80),
]
MOCKS = r"""
server {
    listen 8000;
    # The mock must not impose its own 1 MiB default behind the edge under test.
    client_max_body_size 16m;
    location = /v1/agent/uploads {
        return 201 '{"path":"$uri","body_bytes":$http_content_length}';
    }
    location / { return 404; }
}
server { listen 3000; return 200 'web'; }
server { listen 3002; return 200 'web'; }
server { listen 8080; return 200 'gateway'; }
"""


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Use only the local daemon, retaining registry/proxy configuration."""
    environment = os.environ.copy()
    for selector in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(selector, None)
    return subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


@pytest.fixture(scope="module")
def nginx_network():
    if not shutil.which("docker") or docker("info", check=False).returncode:
        pytest.skip("the isolated upload test requires the local Docker daemon")
    if docker("image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"pull the test image first: {IMAGE}")
    network = "review-test-upload-" + uuid.uuid4().hex[:12]
    docker("network", "create", network)
    try:
        yield network
    finally:
        docker("network", "rm", network, check=False)


@pytest.fixture(params=CASES, ids=["standalone", "local", "gateway-reference"])
def edge(request, tmp_path: Path, nginx_network):
    source, prefix, port = request.param
    configuration = (REPO_ROOT / source).read_text()
    if source.startswith("deploy/reverse-proxy/"):
        configuration = "server { listen 80;\n" + configuration + "\n}"
    config = tmp_path / "nginx.conf"
    config.write_text("events {}\nhttp { access_log off;\n" + configuration + MOCKS + "\n}")
    name = "review-test-upload-" + uuid.uuid4().hex[:12]
    try:
        docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            nginx_network,
            "--network-alias",
            "control-plane",
            "--network-alias",
            "web",
            "--network-alias",
            "local-gateway",
            "--mount",
            f"type=bind,src={config},dst=/etc/nginx/nginx.conf,readonly",
            "-p",
            f"127.0.0.1::{port}",
            "--entrypoint",
            "nginx",
            IMAGE,
            "-g",
            "daemon off;",
        )
        mapped = docker("port", name, f"{port}/tcp").stdout.strip()
        origin = "http://" + mapped
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=15) as client:
            while True:
                try:
                    client.get(origin)
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail(docker("logs", name, check=False).stderr)
                    time.sleep(0.05)
            syntax = docker("exec", name, "nginx", "-t")
            assert "test is successful" in syntax.stderr
            yield client, origin + prefix
    finally:
        docker("rm", "-f", name, check=False)


def test_shipped_edges_accept_advertised_uploads_and_enforce_bounded_limits(edge):
    """All prefixes preserve the upload path and honor encoded request size."""
    client, base = edge
    for file_bytes in (768 * 1024, MAX_ATTACHMENT_FILE_BYTES):
        body = json.dumps(
            {
                "path": "sample.bin",
                "content_base64": base64.b64encode(bytes(file_bytes)).decode(),
                "content_type": "application/octet-stream",
            }
        ).encode()
        assert len(body) > 1024 * 1024
        response = client.post(
            base + "/v1/agent/uploads",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 201, response.text
        assert response.json() == {"path": "/v1/agent/uploads", "body_bytes": len(body)}

    oversized = client.post(base + "/v1/agent/uploads", content=bytes(8 * 1024 * 1024 + 1))
    assert oversized.status_code == 413
    other_route = client.post(base + "/other", content=bytes(1024 * 1024 + 1))
    assert other_route.status_code == 413, "the larger cap is confined to uploads"
