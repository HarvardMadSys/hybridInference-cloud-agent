"""End-to-end smoke test for ``deploy/local``.

Crosses the public boundary a fresh deployment starts from: the edge, first-run
setup with the code from the control plane's log, the session cookie, the
account and configuration APIs, and the refusal of a turn that has no model
connection. Once the deployment has its administrator, first-run setup cannot
run again: the smoke passes that step only on the server's own answer that
setup is complete, and fails when setup is still pending and the control
plane's log offered no code -- a stack with no administrator never passes.

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

#: The account session cookie, as ``cloud_agent.accounts.config`` names it:
#: ``__Host-`` where cookies are Secure, the plain name on a local HTTP stack.
SESSION_COOKIES = {"__Host-cloud_agent_local_session", "cloud_agent_local_session"}

#: What a successful run of first-run setup proves, as ``setup_admin`` reports it.
CREATED = "created"
#: The server said the first administrator already exists.
COMPLETE = "complete"


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


def _error_type(answer: dict[str, Any]) -> str | None:
    detail = answer.get("detail")
    error = detail.get("error") if isinstance(detail, dict) else None
    return error.get("type") if isinstance(error, dict) else None


def setup_admin(client: Client, code: str) -> str:
    """Run first-run setup, or prove it already ran; anything else fails.

    Returns ``CREATED`` when setup made an administrator and signed it in, or
    ``COMPLETE`` when the server answered that one already exists. Pending
    setup without a code is a failure, never a pass: nothing was proven.
    """
    status, options = client.request("/api/v1/auth/options")
    require(status == 200, f"auth options answered {status}: {options}")
    pending = options.get("setup_required")
    require(
        isinstance(pending, bool), f"auth options do not say whether setup is pending: {options}"
    )
    if pending:
        require(
            code,
            "first-run setup is pending but the control plane's log has no setup code; "
            "the deployment has no administrator and setup was not proven",
        )
    status, answer = client.request(
        "/api/v1/auth/setup/admin",
        method="POST",
        body={
            # A code the server never issued, when only its refusal is asked for.
            "setup_code": code or "XXXX-XXXX-XXXX",
            "login_name": f"smoke-admin-{secrets.token_hex(3)}",
            "password": secrets.token_urlsafe(24),
        },
    )
    if not pending:
        require(
            status == 409 and _error_type(answer) == "setup_complete",
            f"setup is not pending, but the server did not say it is complete: {status} {answer}",
        )
        return COMPLETE
    require(status == 200, f"setup failed: {status} {answer}")
    require(client.signed_in(), "setup set no account session cookie")
    return CREATED


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:13002"
    client = Client(base)

    print("[1/5] The account system offers sign-in, with no mode to choose", flush=True)
    status, config = client.request("/api/v1/auth/config")
    require(status == 200 and "mode" not in config, f"unexpected auth config: {config}")

    print("[2/5] First-run setup creates the administrator", flush=True)
    if setup_admin(client, os.environ.get("LOCAL_SETUP_CODE", "")) == COMPLETE:
        print("  already set up: the server says the first administrator exists", flush=True)
        status, _ = client.request("/api/v1/session/me")
        require(status == 401, f"an unauthenticated session read answered {status}")
        print("Local smoke passed (setup already complete).", flush=True)
        return 0

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
