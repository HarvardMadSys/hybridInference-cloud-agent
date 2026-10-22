"""The bounded history contract at the HTTP boundary."""

from unittest.mock import AsyncMock

import pytest
from tests.servers.test_agent_jobs_api import (
    _api_key_secret as _api_key_secret,
    _create_job,
    client as client,
    store as store,
)

pytestmark = pytest.mark.asyncio


async def test_page_parameters_are_forwarded_and_rejected_before_work(client, store, monkeypatch):
    job_id = await _create_job(client)
    row = await store.get_thread_for_job(job_id=job_id, user_id="user-owner")
    read = AsyncMock(return_value=row)
    monkeypatch.setattr(store, "get_thread_for_job", read)
    result = await client.get(f"/v1/agent/jobs/{job_id}/thread?cursor=opaque&limit=7&anchor=true")
    assert result.status_code == 200
    assert read.call_args.kwargs == {
        "job_id": job_id,
        "user_id": "user-owner",
        "cursor": "opaque",
        "after": None,
        "limit": 7,
        "anchor": True,
    }
    read.reset_mock()
    for query in ("limit=0", "limit=101", "cursor=" + "a" * 513):
        assert (await client.get(f"/v1/agent/jobs/{job_id}/thread?{query}")).status_code == 422
    read.assert_not_awaited()
    read.side_effect = ValueError("Invalid thread history cursor")
    assert (await client.get(f"/v1/agent/jobs/{job_id}/thread?cursor=bad")).status_code == 400


async def test_chunk_contract_keeps_character_offsets_and_explicit_completion(
    client, store, monkeypatch
):
    job_id = await _create_job(client)
    read = AsyncMock(
        return_value={
            "id": 3,
            "job_id": job_id,
            "role": "assistant",
            "content": "🙂ab",
            "content_length": 10,
            "created_at": None,
        }
    )
    monkeypatch.setattr(store, "get_thread_message_chunk", read, raising=False)
    result = await client.get(f"/v1/agent/jobs/{job_id}/thread/messages/3?offset=4&limit=3")
    assert result.status_code == 200
    assert result.json()["next_offset"] == 7
    assert result.json()["content_truncated"] is True
    for query in ("offset=-1", "offset=2147483647", "limit=0", "limit=16385"):
        assert (
            await client.get(f"/v1/agent/jobs/{job_id}/thread/messages/3?{query}")
        ).status_code == 422
    for message_id in (0, 2**63):
        assert (
            await client.get(f"/v1/agent/jobs/{job_id}/thread/messages/{message_id}")
        ).status_code == 422
    read.return_value = None
    assert (await client.get(f"/v1/agent/jobs/{job_id}/thread/messages/3")).status_code == 404
