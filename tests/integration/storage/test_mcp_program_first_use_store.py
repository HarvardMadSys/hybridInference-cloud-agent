"""A program in the chat's sandbox, turned on in a new chat before it listed tools.

It lists its tools only once a chat has started it, so the composer turns it
on with ``use_defaults``. Real Postgres, because the binding's
``selection_pending`` is written by the conversation's own insert.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_mcp_connection_store import (
    _OWNER,
    pool as pool,
    store as store,
)

from cloud_agent.mcp.registry import resolve_for_thread
from cloud_agent.mcp.sandbox_servers import split_bindings

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_a_program_turned_on_before_it_listed_tools_starts_with_the_chat(store):
    """A new chat's program asks first for every tool, and its launch reaches the host.

    It lists its tools only once a chat has started it, so the composer turns
    it on with ``use_defaults``. The binding must keep that, or it is stored
    with no tool allowed and the chat never starts the program that would
    list them.
    """
    program = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Fetch",
        template=None,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec={"command": "uvx", "args": ["mcp-server-fetch==2025.4.7"], "env": {}},
        auth_kind="none",
        auth_config={},
    )
    job = await store.conversation_commands.create_job(
        user_id=_OWNER,
        repo="example-org/example-repo",
        task_prompt="fetch example.com",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="deadbeef",
        mcp_bindings=[
            {
                "connection_id": int(program["id"]),
                "alias": "fetch",
                "enabled": True,
                "selection_pending": True,
                "config_revision": int(program["config_revision"]),
                "authorization_epoch": int(program["authorization_epoch"]),
                "capability_revision": int(program["capability_revision"]),
                "rules": {},
            }
        ],
    )

    (row,) = await store.mcp.list_mcp_bindings(thread_id=job["thread_id"])
    assert row["enabled"] and row["selection_pending"]
    _, servers = split_bindings(await resolve_for_thread(store.mcp, thread_id=job["thread_id"]))
    assert [(server.alias, server.command) for server in servers] == [("fetch", "uvx")]
