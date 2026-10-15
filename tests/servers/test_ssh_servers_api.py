"""SSH servers are registered, checked and selected only by their owner.

The repository is an in-memory stand-in with the port's semantics; the network
checks are replaced at the API's seams, so these tests pin what the routes do
with their results: what is stored, what is shown, what is refused, and when a
change is pushed to running groups.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.support.ssh_keys import host_key, parses_as_ed25519

from cloud_agent import identity
from cloud_agent.api import ssh_servers as api
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.application.ssh_servers import CheckFailed, ssh_key_cipher
from cloud_agent.domain.ssh_servers import (
    CHECK_EVENTS,
    SshServerConflict,
    SshServerUnavailable,
)

PATH = "/v1/agent/ssh-servers"
_HOST_KEY = host_key()
_SCAN = [{"type": "ssh-ed25519", "key": _HOST_KEY, "fingerprint": "SHA256:abcdefgh"}]


class FakeSshServers:
    """The port, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.bindings: dict[str, dict[str, bool]] = {}
        self.threads = {"athr_mine": "owner-1", "athr_theirs": "owner-2"}

    def _event(self, user_id, server_id, kind, thread_id=None, detail=None):
        self.events.append(
            {
                "user_id": user_id,
                "server_id": server_id,
                "kind": kind,
                "thread_id": thread_id,
                "detail": detail or {},
            }
        )

    def _live(self, user_id, server_id):
        row = self.rows.get(server_id)
        if row is None or row["user_id"] != user_id or row["archived_at"] is not None:
            return None
        return row

    async def create_ssh_server(self, *, user_id, values):
        if any(
            row["user_id"] == user_id and row["name"] == values["name"] and not row["archived_at"]
            for row in self.rows.values()
        ):
            raise SshServerConflict(values["name"])
        now = datetime.now(UTC)
        row = {
            "user_id": user_id,
            "pinned_ipv4": None,
            "host_key_type": None,
            "host_key": None,
            "scanned_ipv4": None,
            "scanned_host_keys": None,
            "pending_public_key": None,
            "pending_private_key_ciphertext": None,
            "status": "pending",
            "last_check_at": None,
            "last_check_error": None,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
            **values,
        }
        self.rows[values["id"]] = row
        self._event(user_id, values["id"], "created")
        return copy.deepcopy(row)

    async def list_ssh_servers(self, *, user_id):
        return [
            copy.deepcopy(row)
            for row in self.rows.values()
            if row["user_id"] == user_id and row["archived_at"] is None
        ]

    async def get_ssh_server(self, *, user_id, server_id):
        row = self._live(user_id, server_id)
        return None if row is None else copy.deepcopy(row)

    async def update_ssh_server(self, *, user_id, server_id, revision, values, event, detail=None):
        row = self._live(user_id, server_id)
        if row is None or row["revision"] != revision:
            return None
        if "name" in values and any(
            other is not row and other["user_id"] == user_id and other["name"] == values["name"]
            for other in self.rows.values()
        ):
            raise SshServerConflict(values["name"])
        row.update(values)
        row["revision"] += 1
        self._event(user_id, server_id, event, detail=detail)
        return copy.deepcopy(row)

    async def archive_ssh_server(self, *, user_id, server_id):
        row = self._live(user_id, server_id)
        if row is None:
            return None
        row.update(
            archived_at=datetime.now(UTC),
            status="pending",
            private_key_ciphertext=None,
            pending_public_key=None,
            pending_private_key_ciphertext=None,
        )
        self._event(user_id, server_id, "archived")
        return copy.deepcopy(row)

    async def record_ssh_check(self, *, user_id, server_id, kind, detail):
        assert kind in CHECK_EVENTS
        self._event(user_id, server_id, kind, detail=detail)

    async def count_recent_checks(self, *, user_id, seconds):
        return sum(
            1
            for event in self.events
            if event["user_id"] == user_id and event["kind"] in CHECK_EVENTS
        )

    async def list_thread_ssh_bindings(self, *, user_id, thread_id):
        if self.threads.get(thread_id) != user_id:
            return None
        return [
            {
                "server_id": server_id,
                "enabled": enabled,
                "name": self.rows[server_id]["name"],
                "status": self.rows[server_id]["status"],
            }
            for server_id, enabled in self.bindings.get(thread_id, {}).items()
            if self.rows[server_id]["archived_at"] is None
        ]

    async def replace_thread_ssh_bindings(self, *, user_id, thread_id, server_ids):
        if self.threads.get(thread_id) != user_id:
            return None
        current = self.bindings.get(thread_id, {})
        refused = [
            server_id
            for server_id in server_ids
            if (row := self._live(user_id, server_id)) is None
            or (row["status"] != "ready" and not current.get(server_id))
        ]
        if refused:
            raise SshServerUnavailable(refused)
        current = self.bindings.setdefault(thread_id, {})
        for server_id in current:
            current[server_id] = server_id in server_ids
        for server_id in server_ids:
            current[server_id] = True
        return await self.list_thread_ssh_bindings(user_id=user_id, thread_id=thread_id)


@pytest.fixture()
def repository() -> FakeSshServers:
    return FakeSshServers()


@pytest.fixture()
def pushes(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def push(store, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(api, "push_ssh_access", push)
    return seen


@pytest.fixture()
def app(monkeypatch, repository, pushes):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "ssh-test-session-secret-long-enough")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "ssh-test-encryption-key")
    api._check_locks.clear()
    application = FastAPI()
    application.include_router(api.router)

    class Gateway:
        async def agent_access(self, owner):
            return {"user_id": owner, "allowed": True, "permissions": ["agent.use"]}

    application.dependency_overrides[get_gateway_client] = Gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        ssh_servers=repository, host_pool=object()
    )
    return application


def headers(user_id: str = "owner-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {identity.mint_session(user_id=user_id)}"}


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    )


def _checks(monkeypatch, *, scan=None, login=None, resolve="93.184.216.34") -> list[dict]:
    """Replace the network checks; return the logins attempted."""
    logins: list[dict] = []

    async def fake_resolve(address):
        if isinstance(resolve, Exception):
            raise resolve
        return resolve

    async def fake_scan(ipv4, port):
        if isinstance(scan, Exception):
            raise scan
        return scan if scan is not None else copy.deepcopy(_SCAN)

    async def fake_login(**kwargs):
        logins.append(kwargs)
        if login is not None:
            raise login

    monkeypatch.setattr(api, "resolve_public_ipv4", fake_resolve)
    monkeypatch.setattr(api, "scan_host_keys", fake_scan)
    monkeypatch.setattr(api, "check_login", fake_login)
    return logins


async def _create(http, **fields) -> dict[str, Any]:
    body = {"name": "gpu", "address": "gpu.example.org", "username": "ubuntu", **fields}
    response = await http.post(PATH, json=body, headers=headers())
    assert response.status_code == 201, response.text
    return response.json()


async def _ready(http, monkeypatch) -> dict[str, Any]:
    server = await _create(http)
    _checks(monkeypatch)
    scanned = await http.post(
        f"{PATH}/{server['id']}/scan", json={"revision": server["revision"]}, headers=headers()
    )
    assert scanned.status_code == 200, scanned.text
    verified = await http.post(
        f"{PATH}/{server['id']}/verify",
        json={"revision": scanned.json()["revision"], "fingerprint": "SHA256:abcdefgh"},
        headers=headers(),
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


async def test_a_new_server_shows_its_public_key_and_never_the_private_one(app, repository):
    async with client(app) as http:
        server = await _create(http, description="A100 box")
        listed = await http.get(PATH, headers=headers())

    assert server["status"] == "pending"
    assert server["public_key"].startswith("ssh-ed25519 ")
    assert server["public_key"].endswith(f"cloud-agent:gpu-{server['id'][:8]}")
    assert server["public_key"] in server["install_command"]
    assert "authorized_keys" in server["install_command"]
    stored = repository.rows[server["id"]]
    private = ssh_key_cipher().decrypt(stored["private_key_ciphertext"])
    assert parses_as_ed25519(private)
    assert "PRIVATE KEY" not in listed.text and "ciphertext" not in listed.text
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {"configured": True, "servers": [server]}


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "github.com"},
        {"name": "GPU"},
        {"address": "2001:db8::1"},
        {"address": "bad host!"},
        {"username": "-oProxyCommand=x"},
        {"description": "line one\nline two"},
        {"description": "x" * 201},
        {"port": 0},
    ],
)
async def test_values_that_could_change_the_ssh_configuration_are_refused(app, fields):
    body = {"name": "gpu", "address": "gpu.example.org", "username": "ubuntu", **fields}
    async with client(app) as http:
        response = await http.post(PATH, json=body, headers=headers())
    assert response.status_code == 422


async def test_names_are_unique_per_owner_not_per_deployment(app):
    async with client(app) as http:
        await _create(http)
        taken = await http.post(
            PATH,
            json={"name": "gpu", "address": "1.2.3.4", "username": "u"},
            headers=headers(),
        )
        other = await http.post(
            PATH,
            json={"name": "gpu", "address": "1.2.3.4", "username": "u"},
            headers=headers("owner-2"),
        )
    assert taken.status_code == 409
    assert taken.json()["detail"]["error"]["type"] == "ssh_server_name_taken"
    assert other.status_code == 201


async def test_another_owner_cannot_see_or_touch_a_server(app):
    async with client(app) as http:
        server = await _create(http)
        sid = server["id"]
        listed = await http.get(PATH, headers=headers("owner-2"))
        changed = await http.patch(
            f"{PATH}/{sid}", json={"revision": 1, "name": "mine"}, headers=headers("owner-2")
        )
        deleted = await http.delete(f"{PATH}/{sid}", headers=headers("owner-2"))
    assert listed.json()["servers"] == []
    assert changed.status_code == 404
    assert deleted.status_code == 404


async def test_a_check_pins_the_confirmed_key_and_address_and_pushes(
    app, monkeypatch, repository, pushes
):
    async with client(app) as http:
        server = await _create(http)
        logins = _checks(monkeypatch)
        scanned = await http.post(
            f"{PATH}/{server['id']}/scan", json={"revision": 1}, headers=headers()
        )
        assert scanned.json()["scan"] == {
            "address": "93.184.216.34",
            "host_keys": [{"type": "ssh-ed25519", "fingerprint": "SHA256:abcdefgh"}],
        }
        assert scanned.json()["status"] == "pending"
        unknown = await http.post(
            f"{PATH}/{server['id']}/verify",
            json={"revision": scanned.json()["revision"], "fingerprint": "SHA256:somethingelse"},
            headers=headers(),
        )
        verified = await http.post(
            f"{PATH}/{server['id']}/verify",
            json={"revision": scanned.json()["revision"], "fingerprint": "SHA256:abcdefgh"},
            headers=headers(),
        )

    assert unknown.status_code == 422
    body = verified.json()
    assert body["status"] == "ready"
    assert body["pinned_address"] == "93.184.216.34"
    assert body["host_key"]["type"] == "ssh-ed25519"
    assert body["scan"] is None
    (login,) = logins
    assert login["ipv4"] == "93.184.216.34" and login["host_key"] == _HOST_KEY
    assert parses_as_ed25519(login["private_key"])
    assert pushes == [{"server_id": server["id"]}]
    kinds = [event["kind"] for event in repository.events]
    assert "scanned" in kinds and "verified" in kinds


async def test_a_failed_check_is_recorded_and_explained_without_the_remotes_output(
    app, monkeypatch, repository
):
    async with client(app) as http:
        server = await _create(http)
        _checks(monkeypatch, resolve=CheckFailed("not_public"))
        refused = await http.post(
            f"{PATH}/{server['id']}/scan", json={"revision": 1}, headers=headers()
        )
        listed = await http.get(PATH, headers=headers())

    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["type"] == "ssh_check_not_public"
    (shown,) = listed.json()["servers"]
    assert shown["last_check_error"] == "not_public"
    assert "public IPv4" in shown["last_check_message"]
    assert [event["kind"] for event in repository.events].count("check_failed") == 1


async def test_checks_are_limited_per_owner_per_hour(app, monkeypatch, repository):
    async with client(app) as http:
        server = await _create(http)
        _checks(monkeypatch)
        for _ in range(api.CHECKS_PER_HOUR):
            repository._event("owner-1", server["id"], "scanned")
        limited = await http.post(
            f"{PATH}/{server['id']}/scan", json={"revision": 1}, headers=headers()
        )
        await _create(http, name="other")
        elsewhere = await http.get(PATH, headers=headers("owner-2"))
    assert limited.status_code == 429
    assert limited.json()["detail"]["error"]["type"] == "ssh_check_limit"
    assert elsewhere.status_code == 200


async def test_changing_how_to_reach_a_server_sends_it_back_to_checking(app, monkeypatch, pushes):
    async with client(app) as http:
        ready = await _ready(http, monkeypatch)
        renamed = await http.patch(
            f"{PATH}/{ready['id']}",
            json={"revision": ready["revision"], "description": "now with notes"},
            headers=headers(),
        )
        moved = await http.patch(
            f"{PATH}/{ready['id']}",
            json={"revision": renamed.json()["revision"], "port": 2222},
            headers=headers(),
        )
        stale = await http.patch(
            f"{PATH}/{ready['id']}",
            json={"revision": ready["revision"], "name": "late"},
            headers=headers(),
        )
    assert renamed.json()["status"] == "ready"
    body = moved.json()
    assert (body["status"], body["port"], body["pinned_address"], body["host_key"]) == (
        "pending",
        2222,
        None,
        None,
    )
    assert stale.status_code == 409
    assert pushes[-1] == {"server_id": ready["id"]}


async def test_a_rotated_key_replaces_the_old_one_only_after_it_logs_in(
    app, monkeypatch, repository
):
    async with client(app) as http:
        ready = await _ready(http, monkeypatch)
        old_public = ready["public_key"]
        rotating = await http.post(
            f"{PATH}/{ready['id']}/rotate-key",
            json={"revision": ready["revision"]},
            headers=headers(),
        )
        body = rotating.json()
        assert body["public_key"] == old_public
        assert body["pending_public_key"] not in (None, old_public)
        new_public = body["pending_public_key"]
        logins = _checks(monkeypatch, login=CheckFailed("auth_failed"))
        failed = await http.post(
            f"{PATH}/{ready['id']}/verify",
            json={"revision": body["revision"], "fingerprint": ready["host_key"]["fingerprint"]},
            headers=headers(),
        )
        assert failed.status_code == 422
        current = (await http.get(PATH, headers=headers())).json()["servers"][0]
        assert current["public_key"] == old_public and current["status"] == "ready"
        logins = _checks(monkeypatch)
        promoted = await http.post(
            f"{PATH}/{ready['id']}/verify",
            json={"revision": current["revision"], "fingerprint": ready["host_key"]["fingerprint"]},
            headers=headers(),
        )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["public_key"] == new_public
    assert promoted.json()["pending_public_key"] is None
    (login,) = logins
    stored = repository.rows[ready["id"]]
    assert login["private_key"] == ssh_key_cipher().decrypt(stored["private_key_ciphertext"])


async def test_deleting_erases_the_key_and_cuts_access_at_once(
    app, monkeypatch, repository, pushes
):
    async with client(app) as http:
        ready = await _ready(http, monkeypatch)
        deleted = await http.delete(f"{PATH}/{ready['id']}", headers=headers())
        listed = await http.get(PATH, headers=headers())
    assert deleted.status_code == 204
    assert listed.json()["servers"] == []
    assert repository.rows[ready["id"]]["private_key_ciphertext"] is None
    assert pushes[-1] == {"server_id": ready["id"]}


async def test_a_chat_selects_only_its_owners_ready_servers(app, monkeypatch, repository, pushes):
    async with client(app) as http:
        ready = await _ready(http, monkeypatch)
        pending = await _create(http, name="lab")
        chosen = await http.put(
            f"{PATH}/threads/athr_mine/bindings",
            json={"server_ids": [ready["id"]]},
            headers=headers(),
        )
        refused = await http.put(
            f"{PATH}/threads/athr_mine/bindings",
            json={"server_ids": [pending["id"]]},
            headers=headers(),
        )
        foreign = await http.put(
            f"{PATH}/threads/athr_theirs/bindings",
            json={"server_ids": [ready["id"]]},
            headers=headers(),
        )
        malformed = await http.put(
            f"{PATH}/threads/athr_mine/bindings",
            json={"server_ids": ["not-a-uuid"]},
            headers=headers(),
        )
        cleared = await http.put(
            f"{PATH}/threads/athr_mine/bindings", json={"server_ids": []}, headers=headers()
        )
        listed = await http.get(f"{PATH}/threads/athr_mine/bindings", headers=headers())

    assert chosen.json() == {
        "bindings": [{"server_id": ready["id"], "name": "gpu", "status": "ready", "enabled": True}]
    }
    assert refused.status_code == 422
    assert foreign.status_code == 404
    assert malformed.status_code == 422
    assert cleared.json()["bindings"][0]["enabled"] is False
    assert listed.json() == cleared.json()
    assert pushes.count({"thread_ids": ["athr_mine"]}) == 2


async def test_without_an_encryption_key_servers_cannot_be_added(app, monkeypatch):
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY")
    async with client(app) as http:
        listed = await http.get(PATH, headers=headers())
        created = await http.post(
            PATH,
            json={"name": "gpu", "address": "1.2.3.4", "username": "u"},
            headers=headers(),
        )
    assert listed.json() == {"configured": False, "servers": []}
    assert created.status_code == 503


def test_server_ids_are_uuids() -> None:
    assert api.SshServerSelection(server_ids=[str(uuid.uuid4()).upper()]).server_ids[0].islower()
