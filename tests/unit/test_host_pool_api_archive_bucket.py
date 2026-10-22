"""The archive bucket a heartbeat sends a host, with and without the destination capability."""

from __future__ import annotations

from typing import Any

import pytest

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - the shared pytest fixture
    _enabled_archive,
    _enroll,
    client,
)

from cloud_agent_host.runtime_settings import SandboxArchiveRuntimeSettings


@pytest.mark.parametrize("capable", [True, False])
async def test_only_a_capable_host_is_sent_another_bucket(client, capable):
    """An older manager refuses any other bucket; archiving to the default would be wrong.

    So while archives are enabled it is sent no policy at all. It confirms no
    revision, so it takes no work at all until it is redeployed, and its manager
    keeps the last policy it applied: it goes on archiving the groups it holds
    into the previous bucket.
    """
    async_client, store = client
    credential = await _enroll(async_client, store)
    _enabled_archive(store, s3_uri="s3://example-archives/cloud-agent/sandbox")
    capabilities = ["sandbox-archive-settings-v1", "sandbox-checkpoints-v1"]
    if capable:
        capabilities.append("sandbox-archive-destination-v1")

    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"capabilities": capabilities},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200
    delivered = response.json()["sandbox_archive_settings"]
    if capable:
        assert delivered["s3_uri"] == "s3://example-archives/cloud-agent/sandbox"
        assert delivered["revision"] == 5
    else:
        assert delivered is None


async def test_an_older_host_is_sent_a_disabled_policy_in_the_default_bucket(client):
    """A bucket saved ahead of enabling archives must not stop an older host.

    Its runner claims nothing until its manager confirms a revision, and that
    manager refuses any bucket but the default. A disabled policy creates no S3
    service and starts no new archive, so the host is sent it with the default
    bucket, as an opt-out is reduced for a host without checkpoints.
    """
    async_client, store = client
    credential = await _enroll(async_client, store)
    _enabled_archive(store, s3_uri="s3://example-archives/cloud-agent/sandbox")
    store.archive_settings.update(enabled=False)

    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"capabilities": ["sandbox-archive-settings-v1", "sandbox-checkpoints-v1"]},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200
    delivered = response.json()["sandbox_archive_settings"]
    assert delivered["enabled"] is False
    assert delivered["s3_uri"] == "s3://freeinference/cloud-agent/sandbox"
    assert delivered["revision"] == 5

    def older_manager(settings: dict[str, Any]) -> dict[str, Any]:
        # What a manager from before the bucket was a setting accepts.
        if settings["s3_uri"] != "s3://freeinference/cloud-agent/sandbox":
            raise ValueError("s3_uri must be exactly s3://freeinference/cloud-agent/sandbox")
        return {"applied_revision": settings["revision"]}

    archive = SandboxArchiveRuntimeSettings(older_manager)
    archive.apply(delivered)
    # The gate the runner claims behind, environment runs included.
    assert archive.ready
    assert archive.report() == 5
