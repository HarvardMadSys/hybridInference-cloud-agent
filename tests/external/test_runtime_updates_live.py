"""An agent update end to end, against real Docker, npm and the built sandbox image.

What the unit tests fake, run for real: a host downloads an exact npm release
in the four steps of ``runtime_downloads``, checks it with one turn through
the production driver against the loopback model, keeps it as a local image,
offers it in its version stamp, and copies it into a running container where a
member can run it and cannot change it.

Needs the built sandbox image (``AGENT_RUNTIME_UPDATE_TEST_IMAGE``, or
``MCP_TEST_SANDBOX_IMAGE``) and a network that reaches the npm registry. Pick
the versions with ``AGENT_RUNTIME_UPDATE_TEST_VERSIONS`` (``runtime=version``,
comma-separated); the default is one release of each runtime that differs from
the image's pins.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE
from cloud_agent_host.runtime_downloads import RuntimeDownloads
from cloud_agent_host.runtime_images import discover
from cloud_agent_host.runtime_install import ensure_downloaded_runtime
from cloud_agent_host.runtime_probe import probe_runtime
from cloud_agent_host.sandbox import ContainerBackend, runtime_bin_dir

pytestmark = pytest.mark.external

_DEFAULT_VERSIONS = "claude-code=2.1.300,codex=0.162.1,opencode=1.19.0"


def _image() -> str:
    image = os.getenv("AGENT_RUNTIME_UPDATE_TEST_IMAGE") or os.getenv("MCP_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("set AGENT_RUNTIME_UPDATE_TEST_IMAGE to the built sandbox image")
    return image


def _versions() -> list[tuple[str, str]]:
    spec = os.getenv("AGENT_RUNTIME_UPDATE_TEST_VERSIONS", _DEFAULT_VERSIONS)
    return [tuple(item.split("=", 1)) for item in spec.split(",") if item]  # type: ignore[misc]


@pytest.fixture
def namespace():
    name = f"runtime-update-test-{uuid.uuid4().hex[:8]}"
    yield name
    for image in discover("docker", name):
        subprocess.run(["docker", "rmi", image.ref], capture_output=True, check=False)


@pytest.mark.parametrize(("runtime", "version"), _versions())
def test_an_update_downloads_checks_offers_and_runs_in_a_chat(namespace, runtime, version):
    image = _image()
    backend = ContainerBackend(image=image, sandbox_namespace=namespace)
    downloads = RuntimeDownloads(
        docker="docker", image=image, namespace=namespace, image_menu=backend.image_menu
    )
    downloads.start()
    try:
        downloads.apply(
            [
                {
                    "runtime": runtime,
                    "version": version,
                    "before": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "nonce": uuid.uuid4().hex,
                }
            ]
        )
        deadline = time.monotonic() + 1200
        report: dict = {}
        while time.monotonic() < deadline:
            report = next((item for item in downloads.report() if item["version"] == version), {})
            if report.get("state") in {"ready", "failed"}:
                break
            time.sleep(1)
    finally:
        downloads.stop()
    assert report.get("state") == "ready", report
    backend.downloaded_versions.invalidate()
    assert version in backend.version_stamp()["runtime_versions_available"][runtime]

    container = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "sleep",
            image,
            "600",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:

        def exec_capture(
            _spec, argv, *, workdir="/", timeout_s=60, user_uid=None, user_gid=None, **_
        ):
            return subprocess.run(
                ["docker", "exec", "-u", f"{user_uid}:{user_gid}", "-w", workdir, container, *argv],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )

        manager = SimpleNamespace(
            backend=backend,
            exec_capture=exec_capture,
            _require=lambda _spec: SimpleNamespace(container_id=container),
            _run=ChatSandboxManager._run,
            _run_bytes=ChatSandboxManager._run_bytes,
        )
        ensure_downloaded_runtime(manager, None, runtime, version)  # type: ignore[arg-type]

        member = f"{MEMBER_UID_BASE}:10001"
        binary = f"{runtime_bin_dir(runtime, version)}"
        ran = subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                member,
                container,
                "sh",
                "-c",
                f'ls "{binary}" && ! touch "{binary}/written-by-a-member" 2>/dev/null',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == 0, ran.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def test_a_version_that_is_not_installed_fails_its_check():
    """A missing directory must not let the image's default answer for it."""
    result = probe_runtime(
        docker="docker",
        image=_image(),
        runtime_id="claude-code",
        version="9.9.9",
        label="org.freeinference.runtime-download.work=runtime-update-test",
    )
    assert not result.ok
    assert "does not report version 9.9.9" in result.detail
