"""The sandbox manager's whole-workspace download.

``GET /workspaces/{id}/archive`` copies ``/workspace`` out through Docker's
archive boundary, compresses it, and answers only once the archive is complete,
so a workspace over the host's download limit is refused instead of arriving
truncated. The open workspace operation brackets the copy either way.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.test_agent_sandbox_manager import _app, _SharedSupervisor

from cloud_agent_host.chat_sandbox import ArchiveExportTooLarge

_HEADERS = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}


def _workspace_tar() -> bytes:
    """What ``docker cp <container>:/workspace -`` would write: the directory, named."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("workspace")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        content = b"print('hello')\n"
        entry = tarfile.TarInfo("workspace/hello.py")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_the_workspace_is_sent_as_one_gzipped_tar():
    supervisor = _SharedSupervisor()
    exported: list[dict[str, Any]] = []

    def export_archive(spec, path, destination, *, max_bytes):
        exported.append({"spec": spec, "path": path, "max_bytes": max_bytes})
        destination.write(_workspace_tar())
        return 0

    supervisor.workspace_backend.manager.export_archive = export_archive
    async with AsyncClient(
        transport=ASGITransport(app=_app(supervisor)), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get("/workspaces/athr_source/archive", headers=_HEADERS)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.headers["cache-control"] == "no-store"
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(response.content))) as archive:
        hello = archive.extractfile("workspace/hello.py")
        assert hello is not None
        assert hello.read() == b"print('hello')\n"
    assert exported == [
        {
            "spec": supervisor.workspace_backend.spec,
            "path": "/workspace",
            # The built-in limit, in bytes, before compression.
            "max_bytes": 1024 * 1024 * 1024,
        }
    ]
    assert ("operation_opened", "athr_source") in supervisor.calls
    assert ("operation_closed", "athr_source", "operation-token") in supervisor.calls


@pytest.mark.asyncio
async def test_a_workspace_over_the_download_limit_is_refused_whole():
    supervisor = _SharedSupervisor()
    limits: list[int] = []

    def export_archive(_spec, _path, destination, *, max_bytes):
        limits.append(max_bytes)
        destination.write(b"partial")
        raise ArchiveExportTooLarge("chat sandbox archive for '/workspace' exceeds its byte limit")

    supervisor.workspace_backend.manager.export_archive = export_archive
    async with AsyncClient(
        transport=ASGITransport(app=_app(supervisor)), base_url="http://sandbox_manager"
    ) as client:
        configured = await client.post(
            "/host-settings", headers=_HEADERS, json={"settings": {"workspace_download_max_mib": 3}}
        )
        refused = await client.get("/workspaces/athr_source/archive", headers=_HEADERS)

    assert configured.status_code == 200
    assert refused.status_code == 413
    assert (
        refused.json()["detail"]
        == "The workspace is larger than this server's 3 MiB download limit."
    )
    assert limits == [3 * 1024 * 1024]
    assert ("operation_closed", "athr_source", "operation-token") in supervisor.calls


@pytest.mark.asyncio
async def test_the_download_needs_the_sandbox_manager_credential():
    supervisor = _SharedSupervisor()
    async with AsyncClient(
        transport=ASGITransport(app=_app(supervisor)), base_url="http://sandbox_manager"
    ) as client:
        refused = await client.get("/workspaces/athr_source/archive")

    assert refused.status_code in {401, 403}
    assert ("operation_opened", "athr_source") not in supervisor.calls
