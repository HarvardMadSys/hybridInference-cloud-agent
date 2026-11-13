"""Serve one release's control plane for ``scripts/check_mixed_protocol.py``.

Run with that release's own interpreter and checkout: the real ``create_app()``
on a loopback port, against a database that release's Alembic migrated, with
nothing on the internet reachable. Before it answers ``READY`` it writes what
a host needs to find work through the release's own ``Storage``: an approved
account, a verified model connection (the row only; nothing dials the
provider), a temporary-workspace conversation and a host enrollment token.

Every host request is read a second time through the release's own body model
(``mixed_protocol_wire``) and every key that model would drop is recorded, with
the status code the route answered.

After ``READY`` it takes one JSON command per stdin line and answers one JSON
line: the owner and operator actions a scripted turn needs from the control
plane's side, and ``report``. These are the previous release's storage API when
this serves the previous release, so, as in ``rolling_api_worker.py``, a change
to one of their signatures keeps the old spelling working here until a release
with the new one has been published.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from typing import Any

# The controller names the checkout explicitly; see rolling_api_worker.py for
# why importing the wrong tree must fail rather than fall through.
_tree = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(_tree / "apps" / "backend"))

import asyncpg
import uvicorn
from mixed_protocol_wire import HOST_PREFIXES, WireRecorder, host_routes

import cloud_agent

if not Path(cloud_agent.__file__).resolve().is_relative_to(_tree):
    raise SystemExit(f"{cloud_agent.__file__} is not from {_tree}")

from cloud_agent.accounts import tokens
from cloud_agent.accounts.passwords import hash_password
from cloud_agent.app import create_app
from cloud_agent.application.model_connections import (
    model_connection_cipher,
    new_connection_id,
)
from cloud_agent.storage.repositories.conversation_commands import insert_follow_up
from cloud_agent_shared.model_connections import PROTOCOLS

_OWNER_EMAIL = "mixed-protocol@example.org"
_MODEL = "mixed-protocol-model"


async def _account(storage: Any) -> str:
    """An approved, verified account: the owner the claim asks the directory about."""
    account_id = tokens.new_account_id()
    assert await storage.local_accounts.create_account(
        account_id=account_id,
        email=_OWNER_EMAIL,
        email_normalized=_OWNER_EMAIL,
        password_hash=await hash_password(secrets.token_urlsafe(16)),
    )
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="verify_email",
        token_hash=tokens.email_token_hash(token),
        ttl_s=60,
    )
    assert await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(token))
    await storage.local_accounts.submit_application(account_id=account_id, purpose="test")
    record = await storage.local_accounts.get_account(account_id)
    await storage.local_accounts.apply_admin_action(
        account_id=account_id,
        action="approve",
        reason=None,
        expected_version=record.version,
        actor_id="mixed-protocol",
        actor_account_id=None,
        admin_ip="test",
    )
    return account_id


async def _fixtures(storage: Any, host: str) -> dict[str, Any]:
    owner = await _account(storage)
    connection = await storage.model_connections.create_model_connection(
        user_id=owner,
        values={
            "id": new_connection_id(),
            "name": "Mixed protocol provider",
            "base_url": "https://provider.example/v1",
            "models": [_MODEL],
            "api_key_ciphertext": model_connection_cipher().encrypt("sk-test-mixed-protocol"),
            "model_checks": {
                _MODEL: {"protocols": {protocol: {"status": "supported"} for protocol in PROTOCOLS}}
            },
        },
    )
    job = await storage.conversation_commands.create_job(
        user_id=owner,
        repo=None,
        task_prompt="Mixed protocol: first turn",
        runtime="codex",
        model=_MODEL,
        model_connection_id=connection["id"],
    )
    enroll_token = secrets.token_urlsafe(32)
    await storage.host_pool.issue_enroll_token(
        token_hash=hashlib.sha256(enroll_token.encode()).hexdigest(),
        host=host,
        ttl_seconds=3600,
        issued_by="mixed-protocol",
    )
    return {"owner": owner, "job_id": job["id"], "enroll_token": enroll_token}


async def _command(
    storage: Any, conn: Any, owner: str, recorder: WireRecorder, command: dict[str, Any]
) -> Any:
    op = command["op"]
    if op == "follow_up":
        # The way the API writes one: ``insert_follow_up`` in a transaction.
        async with conn.transaction():
            job = await insert_follow_up(
                conn,
                parent_job_id=command["parent_job_id"],
                user_id=owner,
                prompt=command["prompt"],
            )
        return {"job_id": job["id"]}
    if op == "cancel":
        return {
            "state": await storage.attempt_commands.request_cancel(
                job_id=command["job_id"], user_id=owner
            )
        }
    if op == "release_residency":
        # What an eviction does: the next beat naming this generation is lost.
        released = await storage.residency.release_residency(
            thread_id=command["thread_id"], generation=command["generation"], host=command["host"]
        )
        return {"released": released is not None}
    if op == "report":
        return {"calls": recorder.calls}
    raise ValueError(f"unknown command {op!r}")


async def main() -> None:
    """Serve until stdin closes or says ``quit``."""
    host = os.environ["MIXED_PROTOCOL_HOST"]
    app = create_app()
    recorder = WireRecorder(app, prefixes=HOST_PREFIXES, routes=host_routes())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(recorder, log_level="warning", access_log=False, lifespan="on")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise SystemExit("the control plane stopped before it started")
        await asyncio.sleep(0.05)
    storage = app.state.storage
    fixtures = await _fixtures(storage, host)
    print(
        "READY " + json.dumps({**fixtures, "port": listener.getsockname()[1]}),
        flush=True,
    )
    conn = await asyncpg.connect(os.environ["AGENT_DATABASE_URL"])
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "quit":
                break
            try:
                result = await _command(storage, conn, fixtures["owner"], recorder, command)
                answer = {"ok": True, "result": result}
            except Exception as exc:
                answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            # Prefixed: the release's own logging may share this stream.
            print("ANSWER " + json.dumps(answer, default=str), flush=True)
    finally:
        await conn.close()
        server.should_exit = True
        await serving


if __name__ == "__main__":
    asyncio.run(main())
