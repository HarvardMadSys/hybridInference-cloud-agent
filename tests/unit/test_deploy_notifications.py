"""Deploy notification errors must not expose the receiver's secret URL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from deploy import notify_failure


@pytest.fixture(autouse=True)
def run_context(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_JOB", "deploy-host")


def test_actions_annotation_is_available_without_a_receiver(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_ALERT_WEBHOOK_URL", raising=False)
    notify_failure.main()
    output = capsys.readouterr().out
    assert "::error" in output
    assert "https://github.com/owner/repo/actions/runs/123" in output


def test_webhook_failure_prints_no_secret(monkeypatch, capsys):
    secret_url = "https://alerts.example.invalid/SECRET"
    monkeypatch.setenv("AGENT_ALERT_WEBHOOK_URL", secret_url)

    def send(*args, **kwargs):
        raise RuntimeError(secret_url)

    monkeypatch.setattr(
        notify_failure.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=send)
    )
    with pytest.raises(SystemExit, match="1"):
        notify_failure.main()
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
    assert "Alert delivery failed" in output.out
