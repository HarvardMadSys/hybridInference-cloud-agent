"""Peer failure cancels only the readiness probe, never a healthy cold build."""

from __future__ import annotations

import io
import json
import sys

import pytest

from deploy import watch_peer


def test_a_failed_peer_stops_and_removes_the_one_off(tmp_path, monkeypatch, capsys):
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(watch_peer, "peer_failure", lambda peer: f"{peer}: failure (job-url)")
    result = watch_peer.supervise(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        peer="Deploy runner host",
        container="this-run-readiness",
        interval=0.01,
    )
    assert result == 1
    assert calls.read_text().strip() == "rm -f this-run-readiness"
    assert "Deploy runner host: failure" in capsys.readouterr().out


@pytest.mark.parametrize("api_down", [False, True])
def test_running_host_or_unavailable_actions_api_leaves_the_fence_in_charge(monkeypatch, api_down):
    def status(peer):
        if api_down:
            raise TimeoutError("private response must not be logged")
        return None

    monkeypatch.setattr(watch_peer, "peer_failure", status)
    result = watch_peer.supervise(
        [sys.executable, "-c", "import time; time.sleep(.3); raise SystemExit(17)"],
        peer="Deploy runner host",
        container="this-run-readiness",
        interval=0.01,
    )
    assert result == 17


def test_only_this_run_attempt_is_read_and_pagination_finds_the_peer(monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_TOKEN": "test-token",
    }.items():
        monkeypatch.setenv(key, value)
    urls = []

    def open_request(request, **kwargs):
        urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer test-token"
        jobs = (
            [{"name": "different", "status": "completed"}] * 100
            if len(urls) == 1
            else [
                {
                    "name": "Deploy runner host",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "html_url": "job-url",
                }
            ]
        )
        return io.BytesIO(json.dumps({"jobs": jobs}).encode())

    monkeypatch.setattr(watch_peer.urllib.request, "urlopen", open_request)
    assert "cancelled" in watch_peer.peer_failure("Deploy runner host")
    assert len(urls) == 2
    assert all("/runs/123/attempts/2/jobs?" in url for url in urls)
    assert urls[1].endswith("page=2")
