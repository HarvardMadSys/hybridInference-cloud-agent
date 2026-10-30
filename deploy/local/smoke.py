"""End-to-end smoke test for ``deploy/local``.

Crosses the public boundary a fresh deployment starts from: the edge, first-run
setup with the code from the control plane's log, the session cookie, the
account and configuration APIs, and the refusal of a turn that has no model
connection. Once the deployment has its administrator, first-run setup cannot
run again, and the smoke says so and checks only that no session exists.

A local stack has no model provider: sandboxes reach public addresses only, so
turns are exercised against a real provider with a model connection, not here.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from typing import Any

SESSION_COOKIES = {"cloud_agent_session", "__Host-cloud_agent_session"}


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """The status and JSON answer: a refusal is a result here, not an error."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Origin": self.base}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, json.loads(payload) if payload else {}

    def signed_in(self) -> bool:
        return any(cookie.name in SESSION_COOKIES for cookie in self.cookies)


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:13002"
    client = Client(base)

    print("[1/5] The account system offers sign-in, with no mode to choose", flush=True)
    status, config = client.request("/api/v1/auth/config")
    require(status == 200 and "mode" not in config, f"unexpected auth config: {config}")

    print("[2/5] First-run setup creates the administrator", flush=True)
    code = os.environ.get("LOCAL_SETUP_CODE", "")
    login_name = f"smoke-admin-{secrets.token_hex(3)}"
    status, user = client.request(
        "/api/v1/auth/setup/admin",
        method="POST",
        body={
            "setup_code": code or "XXXX-XXXX-XXXX",
            "login_name": login_name,
            "password": secrets.token_urlsafe(24),
        },
    )
    if status == 409 or not code:
        print("  already set up: the first administrator exists", flush=True)
        status, _ = client.request("/api/v1/session/me")
        require(status == 401, f"an unauthenticated session read answered {status}")
        return 0
    require(status == 200, f"setup failed: {status} {user}")
    require(client.signed_in(), "setup set no session cookie")

    print("[3/5] The session is the administrator's", flush=True)
    status, me = client.request("/api/v1/session/me")
    require(status == 200 and me.get("id"), f"session read failed: {status} {me}")
    status, settings = client.request("/api/admin/agent/settings")
    require(status == 200, f"the administrator could not read settings: {status}")
    require("gateway_base_url" not in settings.get("values", {}), "a Gateway setting survived")

    print("[4/5] Nothing can run until the owner connects a model", flush=True)
    status, agent = client.request("/api/v1/agent/config")
    require(status == 200 and agent.get("model_connection_required"), f"config: {agent}")
    require("local/demo" in (agent.get("repos") or []), f"config offers no repository: {agent}")
    status, refused = client.request(
        "/api/v1/agent/jobs",
        method="POST",
        body={"repo": "local/demo", "task_prompt": "hello", "runtime": "codex", "model": "m"},
    )
    require(status == 409, f"a turn with no connection answered {status}: {refused}")
    require(
        refused.get("detail", {}).get("error", {}).get("type") == "model_connection_required",
        f"unexpected refusal: {refused}",
    )

    print("[5/5] Sign-out ends the session", flush=True)
    status, _ = client.request("/api/v1/session/logout", method="POST")
    require(status in {200, 204}, f"sign-out answered {status}")
    status, _ = client.request("/api/v1/session/me")
    require(status == 401, f"a signed-out session read answered {status}")

    print("Local smoke passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
