"""Disk admission uses fresh attested capacity and preserves retained resumes."""

from __future__ import annotations

import io
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent.schemas import WorkerReleaseRequest
from cloud_agent_host import runner, storage_admission
from cloud_agent_host.host_capacity import StorageMissing, StorageSample
from cloud_agent_host.host_runtime import HANDOVER_DECLINED, encode_turn
from cloud_agent_host.runner import ClaimedJob
from cloud_agent_host.sandbox import ProcessBackend


@pytest.mark.parametrize("available_mib,declined", [(1023, True), (1024, False), (2048, False)])
def test_new_allocation_reserve_uses_unprivileged_available_bytes(
    monkeypatch, available_mib, declined
):
    sample = StorageSample(
        total_bytes=10 * 1024**3,
        free_bytes=4 * 1024**3,
        available_bytes=available_mib * 1024**2,
        target="/var/lib/docker",
    )
    monkeypatch.setattr(
        storage_admission, "build_storage_probe", lambda: SimpleNamespace(sample=lambda: sample)
    )
    reason = storage_admission.new_sandbox_storage_error(minimum_mib=1024)
    assert bool(reason) is declined
    if reason:
        assert "Free storage" in reason
        assert "existing sessions can still resume" in reason


@pytest.mark.parametrize("sample", [None, StorageMissing(reason="unverified")])
def test_unknown_capacity_does_not_invent_a_storage_pressure_verdict(monkeypatch, sample):
    probe = None if sample is None else SimpleNamespace(sample=lambda: sample)
    monkeypatch.setattr(storage_admission, "build_storage_probe", lambda: probe)
    assert storage_admission.new_sandbox_storage_error(minimum_mib=1024) is None


def test_existing_sandbox_resume_does_not_depend_on_available_disk_space(monkeypatch):
    job = ClaimedJob(
        job_id="ajob_test",
        thread_id="athr_test",
        attempt_id=1,
        attempt_no=1,
        repo="owner/repo",
        base_sha=None,
        task_prompt="resume",
        runtime="codex",
        model="model",
        worker_token="worker",
        sandbox_token="sandbox",
        thread_sandbox={"sandbox_id": "asbx_existing"},
    )
    called = []
    monkeypatch.setattr(
        runner, "new_sandbox_storage_error", lambda **kw: called.append(kw) or "disk full"
    )
    assert runner._new_sandbox_storage_error(job, {"storage_min_available_mib": 2048}) is None
    assert called == []
    assert (
        runner._new_sandbox_storage_error(
            replace(job, thread_sandbox=None), {"storage_min_available_mib": 2048}
        )
        == "disk full"
    )
    assert called == [{"minimum_mib": 2048}]


def test_low_storage_declines_handover_before_the_turn_starts(monkeypatch):
    payload = {
        "job_id": "ajob_test",
        "thread_id": "athr_test",
        "attempt_id": 1,
        "attempt_no": 1,
        "repo": "owner/repo",
        "task_prompt": "work",
        "runtime": "codex",
        "model": "model",
        "worker_token": "worker",
        "sandbox_token": "sandbox",
        "_host_settings": {"storage_min_available_mib": 2048},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    monkeypatch.setattr(
        runner, "build_backend_from_env", lambda: ProcessBackend(acknowledged_unsafe=True)
    )
    monkeypatch.setattr(
        runner, "new_sandbox_storage_error", lambda **_kw: "storage reserve reached"
    )
    started = []
    monkeypatch.setattr(runner, "run_turn", lambda *_a, **_kw: started.append(True))
    read_fd, write_fd = os.pipe()
    try:
        assert (
            runner._serve_turn(
                handover_fd=write_fd,
                base_url="http://control",
                gateway_url="http://gateway",
                worker_id="runner",
                lease_ttl=120,
                agent_timeout_s=3600,
                host="host",
            )
            == 2
        )
        assert (
            os.read(read_fd, 4096)
            .decode()
            .startswith(f"{HANDOVER_DECLINED} storage reserve reached")
        )
        assert started == []
    finally:
        os.close(read_fd)


def test_direct_runner_storage_release_posts_a_valid_audit_reason(monkeypatch):
    job = ClaimedJob.from_response(
        {
            "job_id": "ajob_test",
            "thread_id": "athr_test",
            "attempt_id": 1,
            "attempt_no": 1,
            "repo": "owner/repo",
            "task_prompt": "work",
            "runtime": "codex",
            "model": "model",
            "worker_token": "worker",
            "sandbox_token": "sandbox",
        }
    )
    monkeypatch.setattr(
        runner,
        "_new_sandbox_storage_error",
        lambda *_args: "new sandbox allocation declined: verified Docker storage is below reserve",
    )
    posted = []

    def post(_url, **kwargs):
        posted.append(WorkerReleaseRequest.model_validate(kwargs["json"]))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(runner.httpx, "post", post)
    assert (
        runner._run_turn(
            job,
            base_url="http://control",
            worker_id="runner",
            backend=ProcessBackend(acknowledged_unsafe=True),
        )
        == 0
    )
    assert [body.reason for body in posted] == ["storage_pressure"]
