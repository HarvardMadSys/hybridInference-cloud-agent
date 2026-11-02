"""GitHub connection and repository route behavior.

Split from the core job-router suite so GitHub ownership, installation, and
branch semantics form an independently schedulable test module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    _OWNER,
    UTC,
    Any,
    ASGITransport,
    AsyncClient,
    FakeStorage,
    FastAPI,
    _api_key_secret,
    _build_app,
    _create_job,
    _create_job_raw,
    asyncio,
    client,
    datetime,
    pytest,
    store,
)
from tests.support.owners import connection_claim, on_connection

pytestmark = pytest.mark.asyncio


class _FakeGitHubApp:
    """Stands in for the App: a user token maps to installations, which cover repos.

    ``repos_by_installation`` is what each installation covers; ``pushable``,
    where given, is the part of it this user may push to — what GitHub answers
    when asked as the user. Absent, the user may push to all of it.
    """

    def __init__(
        self,
        installations,
        repos_by_installation,
        *,
        exchange_fails=False,
        login="octocat",
        pushable=None,
    ):
        from cloud_agent.source_control import SourceControlCipher

        self.installations = installations
        self.repos = repos_by_installation
        self.pushable = pushable
        self.exchange_fails = exchange_fails
        self.login = login
        self.exchanged: list[str] = []
        self.listed_with: list[str] = []
        self.revoked: list[str] = []
        self.refused_tokens: set[str] = set()
        self.forgotten: list[str] = []
        self.cipher = SourceControlCipher("test-source-control-key")

    async def exchange_user_code(self, code: str) -> Any:
        from cloud_agent.github_app import UserTokens

        if self.exchange_fails:
            raise RuntimeError("GitHub refused the code exchange")
        self.exchanged.append(code)
        return UserTokens(
            access_token=f"user-token-for-{code}",
            access_expires_at=datetime.now(UTC) + timedelta(hours=8),
            refresh_token=f"refresh-for-{code}",
            refresh_expires_at=datetime.now(UTC) + timedelta(days=180),
        )

    async def user_profile(self, user_token: str) -> dict[str, Any]:
        assert user_token.startswith("user-token-for-"), "the profile is the token's own"
        return {"id": 583231, "login": self.login}

    async def writable_repositories_for_user(self, user_token: str, installation_id: int):
        from cloud_agent.github_app import UserAuthorizationRevoked

        if user_token in self.refused_tokens:
            raise UserAuthorizationRevoked("GitHub refused the user's token")
        self.listed_with.append(user_token)
        if self.pushable is not None:
            return list(self.pushable.get(installation_id, []))
        return list(self.repos.get(installation_id, []))

    # The fake answers fresh every time; the minute-long memory is the real
    # class's, and tested there.
    def cached_user_repositories(self, user_id, installation_ids):
        return None

    def remember_user_repositories(self, user_id, installation_ids, repositories):
        pass

    def forget_user(self, user_id):
        self.forgotten.append(user_id)

    async def revoke_user_authorization(self, access_token: str) -> None:
        self.revoked.append(access_token)

    user_authorization_configured = True

    def user_authorization_url(self, state: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}"

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        return self.installations

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        return self.repos.get(installation_id, [])

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        return "ghs_x"

    async def require_commit_on_branch(
        self, repo: str, sha: str, *, branch: str | None = None
    ) -> str:
        """Every base is on its branch unless a test says otherwise."""
        return branch or "main"


def _store_with_grants(store: FakeStorage) -> FakeStorage:
    """Give the fake store the grant surface the router uses."""
    store.grants = {}
    store.consumed_oauth_states = set()

    async def record_repo_grant(*, user_id, installation_id, account_login=None):
        store.grants.setdefault(user_id, {})[installation_id] = account_login

    async def list_repo_grants(*, user_id):
        return [
            {"installation_id": iid, "account_login": login}
            for iid, login in store.grants.get(user_id, {}).items()
        ]

    async def revoke_repo_grant(*, user_id, installation_id):
        return store.grants.get(user_id, {}).pop(installation_id, "missing") != "missing"

    async def consume_oauth_state(*, state_hash, user_id, provider):
        assert user_id == "user-owner"
        assert provider == "github"
        if state_hash in store.consumed_oauth_states:
            return None
        store.consumed_oauth_states.add(state_hash)
        return {"code_verifier_ciphertext": None}

    store.issued_oauth_states = []

    async def create_oauth_state(**kwargs):
        store.issued_oauth_states.append(kwargs)

    store.github_accounts = {}
    store.github_account_locks = {}
    store.github_account_lock_attempts = []

    @asynccontextmanager
    async def serialize_github_account(*, user_id):
        lock = store.github_account_locks.setdefault(user_id, asyncio.Lock())
        task = asyncio.current_task()
        store.github_account_lock_attempts.append(
            task.get_name() if task is not None else "unknown"
        )
        async with lock:
            yield store

    store.serialize_github_account = serialize_github_account

    async def connect_github_account(
        *,
        user_id,
        github_user_id,
        login,
        installations,
        access_token_ciphertext=None,
        access_expires_at=None,
        refresh_token_ciphertext=None,
        refresh_expires_at=None,
    ):
        previous = store.github_accounts.get(user_id)
        refresh_generation = 0 if previous is None else previous["refresh_generation"] + 1
        store.github_accounts[user_id] = {
            "github_user_id": github_user_id,
            "login": login,
            "access_token_ciphertext": access_token_ciphertext,
            "access_expires_at": access_expires_at,
            "refresh_token_ciphertext": refresh_token_ciphertext,
            "refresh_expires_at": refresh_expires_at,
            "refresh_generation": refresh_generation,
        }
        store.grants[user_id] = {
            item["installation_id"]: item.get("account_login") for item in installations
        }

    async def get_github_account(*, user_id):
        account = store.github_accounts.get(user_id)
        return dict(account) if account else None

    async def claim_github_token_refresh(*, user_id, lease_seconds, margin_seconds):
        return None

    async def store_github_tokens(**kwargs):
        return False

    async def clear_github_tokens(*, user_id, access_token_ciphertext):
        account = store.github_accounts.get(user_id)
        if not account or account.get("access_token_ciphertext") != access_token_ciphertext:
            return False
        account.update(access_token_ciphertext=None, refresh_token_ciphertext=None)
        return True

    async def disconnect_github_account(*, user_id):
        account = store.github_accounts.pop(user_id, None)
        if account is None:
            return None
        store.grants.pop(user_id, None)
        return dict(account)

    store.record_repo_grant = record_repo_grant
    store.list_repo_grants = list_repo_grants
    store.revoke_repo_grant = revoke_repo_grant
    store.consume_oauth_state = consume_oauth_state
    store.create_oauth_state = create_oauth_state
    store.connect_github_account = connect_github_account
    store.get_github_account = get_github_account
    store.claim_github_token_refresh = claim_github_token_refresh
    store.store_github_tokens = store_github_tokens
    store.clear_github_tokens = clear_github_tokens
    store.disconnect_github_account = disconnect_github_account
    return store


async def test_connecting_records_only_what_github_attests(store: FakeStorage, monkeypatch):
    """The entitlement must come from GitHub's answer, not the browser's claim.

    The browser sends a code; the platform exchanges it for a token that speaks
    as that user and asks GitHub which installations they can reach. An
    installation id posted directly would be exactly the confused deputy this
    path exists to prevent.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service", "acme/web"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 200
    assert app_creds.exchanged == ["abc123"], "the code must be exchanged, not trusted"
    assert connected.json()["connections"][0]["installation_id"] == 77
    # The picker now offers what this user actually connected.
    assert config.json()["repos"] == ["acme/service", "acme/web"]
    assert config.json()["github_connected"] is True


async def test_a_connected_user_may_run_only_their_own_repos(store: FakeStorage, monkeypatch):
    """Connecting one owner must not entitle someone else's repository."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        mine = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )
        theirs = await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"}
            ),
        )

    assert mine.status_code == 201
    assert theirs.status_code == 403
    assert "connected" in theirs.text


async def test_a_failed_exchange_grants_nothing(store: FakeStorage, monkeypatch):
    """A code that does not verify must leave the user with no entitlement."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
        exchange_fails=True,
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "stolen", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 400
    assert config.json()["repos"] == []


async def test_disconnecting_removes_the_entitlement(store: FakeStorage, monkeypatch):
    """Revoking here is half of it; uninstalling on GitHub is the other half."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    # The config and disconnect routes answer while the App is unavailable,
    # so they read it through the lenient dependency.
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        await client.delete("/v1/agent/github/connect/77")
        after = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )

    assert after.status_code == 403


_INSTALL_URL = "https://github.com/apps/agent-test/installations/new"


def _connectable_app(store: FakeStorage, app_creds: _FakeGitHubApp) -> FastAPI:
    """An app where users can connect their own GitHub: App, OAuth and install URL."""
    from cloud_agent.api.deps import get_agent_app_credentials, get_github_app_state
    from cloud_agent.source_control_runtime import GitHubAppState

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    app.dependency_overrides[get_github_app_state] = lambda: GitHubAppState(
        credentials=app_creds, load_status="ready"
    )
    return app


async def test_connecting_records_the_account_even_before_any_installation(
    store: FakeStorage, monkeypatch
):
    """Having connected and having repository access are two different states.

    A user with no installation yet used to leave no row at all, and the
    composer — which read "connected" as "has a repository" — sent them back to
    a Connect button they had just pressed.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")
        integrations = await client.get("/v1/agent/integrations")

    assert connected.status_code == 200
    assert connected.json()["connections"] == []
    assert connected.json()["install_url"].startswith(_INSTALL_URL)
    # Who connected is GitHub's answer for the exchanged token, and the token
    # itself is kept only encrypted.
    account = grants_store.github_accounts[_OWNER]
    assert (account["github_user_id"], account["login"]) == (583231, "octocat")
    assert "user-token-for-abc123" not in str(account)
    assert app_creds.cipher.decrypt(account["access_token_ciphertext"]) == "user-token-for-abc123"
    assert app_creds.cipher.decrypt(account["refresh_token_ciphertext"]) == "refresh-for-abc123"

    assert config.json()["github_connected"] is True
    assert config.json()["repos"] == []

    github_row = integrations.json()["providers"][0]
    assert github_row["connected"] is True
    assert github_row["accounts"] == []
    # Where repository access is added — including a personal account the App
    # is not installed on yet — is offered without an installation to manage.
    assert github_row["manage_url"].startswith(_INSTALL_URL)


async def test_connecting_offers_the_install_page_until_the_app_is_on_the_users_own_account(
    store: FakeStorage, monkeypatch
):
    """An organization's installation is not the user's own.

    The install page used to be offered only when connecting found no
    installation at all. A member of an organization that already had the App
    found that one, skipped the page, and could not tell that their own
    repositories needed an installation of their own.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"], 88: ["octocat/dotfiles"]},
    )
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        org_only = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "first", "state": "test-state-value-first-1"},
        )
        # The user installs the App on their own account and comes back through
        # the same callback.
        app_creds.installations = [
            {"installation_id": 77, "account_login": "acme", "account_id": 1001},
            {"installation_id": 88, "account_login": "octocat", "account_id": 583231},
        ]
        with_own = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "second", "state": "test-state-value-second-2"},
        )

    assert [row["installation_id"] for row in org_only.json()["connections"]] == [77]
    assert org_only.json()["repos"] == ["acme/service"]
    assert org_only.json()["install_url"].startswith(_INSTALL_URL)
    assert with_own.json()["install_url"] is None
    assert with_own.json()["repos"] == ["acme/service", "octocat/dotfiles"]


async def test_a_user_is_offered_only_the_repositories_they_can_push_to(
    store: FakeStorage, monkeypatch
):
    """The installation covers more than this user was given; only theirs are offered.

    Asked with the installation's token, GitHub lists every repository the
    installation covers — for an organization, repositories an outside
    collaborator on one of them was never given, which the platform would then
    clone into their sandbox. Asked as the user, it lists theirs, with their
    permission on each: the agent pushes a branch, so push is the bar.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/secret", "acme/service", "acme/readonly"]},
        pushable={77: ["acme/service"]},
    )
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")
        mine = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )
        secret = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/secret", "task_prompt": "read it", "model": "m"}),
        )
        branches = await client.get("/v1/agent/branches", params={"repo": "acme/secret"})

    assert config.json()["repos"] == ["acme/service"]
    assert mine.status_code == 201
    assert secret.status_code == 403
    assert branches.status_code == 403
    # Asked as the user, with the token the connection exchanged.
    assert set(app_creds.listed_with) == {"user-token-for-abc123"}


async def test_a_token_github_refuses_is_forgotten_and_the_user_connects_again(
    store: FakeStorage, monkeypatch
):
    """A revoked authorization closes everything, and the composer asks to reconnect."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        app_creds.refused_tokens.add("user-token-for-abc123")
        refused = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )
        config = await client.get("/v1/agent/config")

    assert refused.status_code == 403
    assert "Reconnect GitHub" in refused.text
    assert grants_store.github_accounts[_OWNER]["access_token_ciphertext"] is None
    assert config.json()["github_connected"] is False
    assert config.json()["repos"] == []


async def test_disconnecting_github_forgets_the_token_and_withdraws_the_authorization(
    store: FakeStorage, monkeypatch
):
    """The stored token is why this exists: removing installations one by one left it."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        removed = await client.delete("/v1/agent/integrations/github")
        config = await client.get("/v1/agent/config")
        after = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )

    assert removed.status_code == 200
    github_row = removed.json()["providers"][0]
    assert (github_row["connected"], github_row["accounts"]) == (False, [])
    assert removed.json()["providers"][1]["provider"] == "gitlab", "the page's whole state"
    assert app_creds.revoked == ["user-token-for-abc123"]
    assert grants_store.github_accounts == {}
    assert config.json()["github_connected"] is False
    assert after.status_code == 403


async def test_github_connect_first_is_revoked_by_later_disconnect(store: FakeStorage, monkeypatch):
    """The whole-grant lock makes a later disconnect win after OAuth completes."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    original_exchange = app_creds.exchange_user_code
    exchange_started = asyncio.Event()
    release_exchange = asyncio.Event()

    async def blocked_exchange(code: str):
        exchange_started.set()
        await release_exchange.wait()
        return await original_exchange(code)

    app_creds.exchange_user_code = blocked_exchange
    app = _connectable_app(grants_store, app_creds)
    connect: asyncio.Task | None = None
    disconnect: asyncio.Task | None = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        try:
            connect = asyncio.create_task(
                client.post(
                    "/v1/agent/integrations/github/connect",
                    json={"code": "connect-first", "state": "connect-first-state-value"},
                ),
                name="github-connect-first",
            )
            await asyncio.wait_for(exchange_started.wait(), timeout=1)
            disconnect = asyncio.create_task(
                client.delete("/v1/agent/integrations/github"),
                name="github-disconnect-second",
            )
            for _ in range(100):
                if "github-disconnect-second" in grants_store.github_account_lock_attempts:
                    break
                await asyncio.sleep(0.01)
            assert "github-disconnect-second" in grants_store.github_account_lock_attempts
            assert not disconnect.done(), "disconnect bypassed the in-flight OAuth exchange"

            release_exchange.set()
            connected, removed = await asyncio.gather(connect, disconnect)
        finally:
            release_exchange.set()
            pending = [
                task for task in (connect, disconnect) if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    assert connected.status_code == 200
    assert removed.status_code == 200
    assert app_creds.revoked == ["user-token-for-connect-first"]
    assert grants_store.github_accounts == {}
    assert grants_store.grants.get(_OWNER, {}) == {}


async def test_github_disconnect_first_blocks_exchange_then_reconnect_wins(
    store: FakeStorage, monkeypatch
):
    """A reconnect mints its grant only after an earlier full revoke completes."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme", "account_id": 1001}],
        repos_by_installation={77: ["acme/service"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    revoke_started = asyncio.Event()
    release_revoke = asyncio.Event()
    exchange_started = asyncio.Event()
    original_revoke = app_creds.revoke_user_authorization
    original_exchange = app_creds.exchange_user_code

    async def blocked_revoke(access_token: str) -> None:
        revoke_started.set()
        await release_revoke.wait()
        await original_revoke(access_token)

    async def observed_exchange(code: str):
        exchange_started.set()
        return await original_exchange(code)

    disconnect: asyncio.Task | None = None
    reconnect: asyncio.Task | None = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        old = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "old", "state": "old-state-value-123456"},
        )
        assert old.status_code == 200
        app_creds.revoke_user_authorization = blocked_revoke
        app_creds.exchange_user_code = observed_exchange
        try:
            disconnect = asyncio.create_task(
                client.delete("/v1/agent/integrations/github"),
                name="github-disconnect-first",
            )
            await asyncio.wait_for(revoke_started.wait(), timeout=1)
            reconnect = asyncio.create_task(
                client.post(
                    "/v1/agent/integrations/github/connect",
                    json={"code": "new", "state": "new-state-value-123456"},
                ),
                name="github-connect-second",
            )
            for _ in range(100):
                if "github-connect-second" in grants_store.github_account_lock_attempts:
                    break
                await asyncio.sleep(0.01)
            assert "github-connect-second" in grants_store.github_account_lock_attempts
            assert not exchange_started.is_set(), "reconnect exchanged before revoke completed"

            release_revoke.set()
            removed, connected = await asyncio.gather(disconnect, reconnect)
        finally:
            release_revoke.set()
            pending = [
                task for task in (disconnect, reconnect) if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    assert removed.status_code == 200
    assert connected.status_code == 200
    assert app_creds.revoked == ["user-token-for-old"]
    account = grants_store.github_accounts[_OWNER]
    assert app_creds.cipher.decrypt(account["access_token_ciphertext"]) == "user-token-for-new"
    assert grants_store.grants[_OWNER] == {77: "acme"}


async def test_refresh_asks_github_again_rather_than_reusing_the_last_answer(
    store: FakeStorage, monkeypatch
):
    """The composer's Refresh is for a repository just given to the App on GitHub."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/v1/agent/config")
        assert app_creds.forgotten == []
        fresh = await client.get("/v1/agent/config", params={"fresh": "true"})

    assert fresh.status_code == 200
    assert app_creds.forgotten == [_OWNER]


async def test_reconnecting_keeps_only_the_installations_github_still_reports(
    store: FakeStorage, monkeypatch
):
    """An installation the user lost stays granted only until they reconnect.

    Grants were only ever added, so leaving an organization kept its
    repositories until someone removed the connection by hand.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(
        installations=[
            {"installation_id": 77, "account_login": "acme"},
            {"installation_id": 88, "account_login": "octocat"},
        ],
        repos_by_installation={77: ["acme/service"], 88: ["octocat/dotfiles"]},
    )
    grants_store = _store_with_grants(store)
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "first", "state": "test-state-value-first-1"},
        )
        before = await client.get("/v1/agent/config")
        app_creds.installations = [{"installation_id": 88, "account_login": "octocat"}]
        await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "second", "state": "test-state-value-second-2"},
        )
        after = await client.get("/v1/agent/config")
        lost = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )

    assert before.json()["repos"] == ["acme/service", "octocat/dotfiles"]
    assert after.json()["repos"] == ["octocat/dotfiles"]
    assert grants_store.grants[_OWNER] == {88: "octocat"}
    assert lost.status_code == 403


async def test_an_allowlisted_repository_does_not_stand_in_for_connecting(
    store: FakeStorage, monkeypatch
):
    """Where users can connect GitHub, they connect before choosing anything.

    The allowlist is still an entitlement — the create endpoint honours it — but
    it no longer makes the composer skip the connection: that is how a user ran
    their first task without knowing it would change a GitHub repository.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "acme/public")
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _connectable_app(_store_with_grants(store), app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")

    assert config.json()["repos"] == ["acme/public"]
    assert config.json()["github_connected"] is False


async def test_with_nothing_to_connect_to_the_allowlist_is_the_whole_entitlement(
    store: FakeStorage, monkeypatch
):
    """The dogfood shape: no connectable App, so an allowlisted repository runs."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "acme/public")
    monkeypatch.delenv("AGENT_GITHUB_APP_INSTALL_URL", raising=False)
    app = _build_app(_store_with_grants(store))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")

    assert config.json()["repos"] == ["acme/public"]
    assert config.json()["github_connected"] is True


async def test_a_connection_with_no_token_is_made_again_before_anything_runs(
    store: FakeStorage, monkeypatch
):
    """Grants from before users' tokens were kept: GitHub cannot be asked as them.

    Such a connection used to be the whole entitlement, read with the
    installation's token — every repository the installation covered. It now
    grants nothing until the user connects again, and says so.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    monkeypatch.setenv("AGENT_GITHUB_APP_INSTALL_URL", _INSTALL_URL)
    grants_store = _store_with_grants(store)
    await grants_store.record_repo_grant(user_id=_OWNER, installation_id=77, account_login="acme")
    app_creds = _FakeGitHubApp(installations=[], repos_by_installation={77: ["acme/service"]})
    app = _connectable_app(grants_store, app_creds)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/v1/agent/config")
        integrations = await client.get("/v1/agent/integrations")
        refused = await client.post(
            "/v1/agent/jobs",
            json=on_connection({"repo": "acme/service", "task_prompt": "fix it", "model": "m"}),
        )

    assert config.json()["github_connected"] is False
    assert config.json()["repos"] == []
    assert integrations.json()["providers"][0]["connected"] is False
    assert refused.status_code == 403
    assert "Reconnect GitHub" in refused.text


# ── Issue #47: a GitHub App this process could not load ────────────────────


@pytest.fixture(scope="module")
def app_private_pem() -> str:
    """A real key: recovery signs with it, as the running service would."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class _StepClock:
    """The runtime's monotonic clock, moved by the test instead of waited for."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _app_with_unloaded_github(
    store: FakeStorage, key_path: Any, clock: _StepClock, minter: Any
) -> FastAPI:
    """An app whose GitHub App is configured but whose key is not there yet.

    The real dependencies over a real runtime. ``_build_app`` answers ``None``
    for the credentials, which is exactly the misreading issue #47 was about,
    so that override is removed rather than kept beside the runtime. Once the
    key can be read the runtime signs with it for real and then hands back
    ``minter``, so recovery needs no network.
    """
    from cloud_agent.api.deps import get_agent_app_credentials
    from cloud_agent.source_control_runtime import (
        GitHubAppProvider,
        GitLabOAuthLoad,
        SourceControlRuntime,
    )

    app = _build_app(store)
    del app.dependency_overrides[get_agent_app_credentials]
    provider = GitHubAppProvider(
        {"AGENT_GITHUB_APP_ID": "12345", "AGENT_GITHUB_APP_PRIVATE_KEY_PATH": str(key_path)},
        clock=clock,
        credentials_factory=lambda config: minter,
    )
    app.state.source_control = SourceControlRuntime(github=provider, gitlab=GitLabOAuthLoad({}))
    return app


async def test_an_unloaded_github_app_is_temporarily_unavailable_until_its_key_loads(
    store: FakeStorage, tmp_path, app_private_pem: str
):
    """What a user meets during the outage, and after it, in one process.

    Not "admin setup required" and not "no access": the App is configured and
    this process has not read its key yet. The pages that describe state keep
    answering, with the user's connection intact; the ones that act on GitHub
    answer 503 with a retry hint. Once the key can be read, the same process
    serves all of it again — no restart.
    """
    grants_store = _store_with_grants(store)
    key = tmp_path / "github-app.pem"
    clock = _StepClock()
    minter = _FakeGitHubApp(installations=[], repos_by_installation={77: ["acme/service"]})
    await grants_store.connect_github_account(
        user_id=_OWNER,
        github_user_id=583231,
        login="octocat",
        installations=[{"installation_id": 77, "account_login": "acme"}],
        access_token_ciphertext=minter.cipher.encrypt("user-token-for-earlier"),
    )
    app = _app_with_unloaded_github(grants_store, key, clock, minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_job_raw(client)
        branches = await client.get("/v1/agent/branches", params={"repo": "owner/name"})
        integrations = await client.get("/v1/agent/integrations")
        config = await client.get("/v1/agent/config")

        key.write_text(app_private_pem)
        clock.now += 30
        recreated = await _create_job_raw(client)
        integrations_after = await client.get("/v1/agent/integrations")
        config_after = await client.get("/v1/agent/config")

    for refused in (created, branches):
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "30"
        assert refused.json()["detail"]["error"]["type"] == "source_control_unavailable"
        assert refused.json()["detail"]["error"]["provider"] == "github"

    assert integrations.status_code == 200
    github_row, gitlab_row = integrations.json()["providers"]
    assert github_row["load_status"] == "unavailable"
    assert (github_row["configured"], github_row["connect_url"]) == (False, None)
    assert github_row["connected"] is True, "an outage is not a lost connection"
    assert [account["label"] for account in github_row["accounts"]] == ["acme"]
    assert github_row["repositories"] == []
    assert gitlab_row["load_status"] == "unconfigured"

    assert config.status_code == 200
    assert config.json()["github_load_status"] == "unavailable"
    assert config.json()["github_connected"] is False
    assert config.json()["runtimes"], "the rest of the composer's answers still hold"

    assert recreated.status_code == 201
    github_after = integrations_after.json()["providers"][0]
    assert github_after["load_status"] == "ready"
    assert [repo["name"] for repo in github_after["repositories"]] == ["acme/service"]
    assert config_after.json()["github_load_status"] == "ready"
    assert config_after.json()["github_connected"] is True


async def test_a_connection_can_be_removed_while_the_app_is_unloaded(store: FakeStorage, tmp_path):
    """The grant is this service's own row; removing it never needed GitHub."""
    grants_store = _store_with_grants(store)
    await grants_store.record_repo_grant(user_id=_OWNER, installation_id=77, account_login="acme")
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(grants_store, tmp_path / "github-app.pem", _StepClock(), minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        removed = await client.delete("/v1/agent/integrations/github/connections/77")

    assert removed.status_code == 200
    assert removed.json()["connections"] == []
    assert grants_store.grants[_OWNER] == {}


async def test_a_claim_while_the_app_is_unloaded_leaves_the_job_queued(
    store: FakeStorage, tmp_path, app_private_pem: str
):
    """The runner is told to come back: no attempt spent, nothing minted.

    With ``None`` for credentials the claim checked only the allowlist and
    handed the job over without a clone token, so a private repository failed
    for good at checkout. The refusal comes before the claim itself.
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://test"
    ) as client:
        job_id = await _create_job(client)
    key = tmp_path / "github-app.pem"
    clock = _StepClock()
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(store, key, clock, minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
        while_down = dict(store.jobs[job_id])

        key.write_text(app_private_pem)
        clock.now += 30
        claimed = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )

    assert refused.status_code == 503
    assert refused.json()["detail"]["error"]["type"] == "source_control_unavailable"
    assert (while_down["state"], while_down["current_attempt_id"]) == ("queued", None)
    assert claimed.status_code == 200
    assert claimed.json()["job_id"] == job_id
    assert claimed.json()["clone_token"] == "ghs_x"


async def test_a_turn_finishes_while_the_app_is_unloaded_and_its_wakeup_waits(
    store: FakeStorage, tmp_path
):
    """The finish is recorded and acknowledged; only the next goal round waits.

    A finish the runner could not get acknowledged would be retried and then
    lost with the lease, so it must not depend on the App. The goal consumer it
    drains stops before claiming anything, which is what leaves a wakeup's
    attempts unspent (see ``test_goal_controller``).
    """
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://test"
    ) as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"})
        )
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    claimed_wakeups: list[int] = []
    original_claim = store.goal_wakeups.claim_wakeups

    async def counting_claim(
        *, limit: int = 1, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        claimed_wakeups.append(limit)
        return await original_claim(limit=limit, thread_id=thread_id)

    store.goal_wakeups.claim_wakeups = counting_claim
    minter = _FakeGitHubApp(installations=[], repos_by_installation={})
    app = _app_with_unloaded_github(store, tmp_path / "github-app.pem", _StepClock(), minter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        finish = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )

    assert finish.status_code == 200
    assert store.jobs[job_id]["state"] == "succeeded"
    assert claimed_wakeups == [], "no wakeup may be claimed while the App is unloaded"


async def test_a_finish_drains_only_its_own_conversations_goal_wakeups(
    client: AsyncClient, store: FakeStorage
):
    """The goal drain a finish runs names the finished turn's conversation.

    Unnamed, the claim took the deployment's oldest due wakeups, and this
    runner's request decided other owners' goals before it was acknowledged.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    claims: list[tuple[int, str | None]] = []
    original_claim = store.goal_wakeups.claim_wakeups

    async def recording_claim(
        *, limit: int = 1, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        claims.append((limit, thread_id))
        return await original_claim(limit=limit, thread_id=thread_id)

    store.goal_wakeups.claim_wakeups = recording_claim
    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    assert finish.status_code == 200
    assert claims == [(8, store.jobs[job_id]["thread_id"])]


async def test_a_branch_is_pinned_to_a_commit_at_creation(store: FakeStorage, monkeypatch):
    """A job records the sha, never the branch name.

    A branch moves. A job that stored "dev" would silently mean a different
    tree by the time it ran, and the publisher applies its patch onto a pinned
    commit — so resolving late would mean generating a diff against one tree
    and pushing it onto another.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            assert ref == "dev"
            return "f" * 40

    app_creds = BranchApp(installations=[], repos_by_installation={})
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {
                    "repo": "owner/name",
                    "task_prompt": "fix it",
                    "model": "m",
                    "base_ref": "dev",
                    "metadata": {"_agent_base_ref": "spoofed", "ticket": "ABC-1"},
                }
            ),
        )

    assert created.status_code == 201
    assert created.json()["base_sha"] == "f" * 40
    assert created.json()["base_ref"] == "dev"
    assert created.json()["output_branch"].startswith("agent/")
    assert created.json()["metadata"] == {"ticket": "ABC-1"}


async def test_reserved_base_ref_metadata_cannot_invent_a_branch(client: AsyncClient):
    """Without base_ref, caller metadata must not forge the Git panel's base branch."""
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "metadata": {"_agent_base_ref": "forged"},
            }
        ),
    )

    assert created.status_code == 201
    assert created.json()["base_ref"] is None
    assert created.json()["metadata"] is None


async def test_reserved_oncall_mark_cannot_be_claimed_by_an_owner(
    client: AsyncClient, store: FakeStorage
):
    """Only the on-call door stamps the mark; an owner's copy is dropped."""
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "metadata": {"oncall": True, "ticket": "ABC-1"},
            }
        ),
    )

    assert created.status_code == 201
    assert created.json()["metadata"] == {"ticket": "ABC-1"}
    assert store.jobs[created.json()["id"]]["metadata"] == {"ticket": "ABC-1"}


async def test_an_unresolvable_branch_is_refused_at_creation(store: FakeStorage, monkeypatch):
    """Better a clear 400 than a job queued against a ref that does not exist."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BrokenBranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            raise RuntimeError("no such branch")

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BrokenBranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {
                    "repo": "owner/name",
                    "task_prompt": "fix it",
                    "model": "m",
                    "base_ref": "nope",
                }
            ),
        )

    assert created.status_code == 400
    assert "nope" in created.text


@pytest.mark.parametrize(
    "base_ref", ["../../../../installation/repositories", "main?x=1", "-main", "a..b"]
)
async def test_a_base_ref_git_would_refuse_never_reaches_github(
    store: FakeStorage, monkeypatch, base_ref
):
    """The ref is sent inside a GitHub API path, so only a real ref name is accepted."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    resolved: list[str] = []

    class RecordingApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            resolved.append(ref)
            return "f" * 40

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: RecordingApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {
                    "repo": "owner/name",
                    "task_prompt": "fix it",
                    "model": "m",
                    "base_ref": base_ref,
                }
            ),
        )

    assert created.status_code == 422
    assert "base_ref" in created.text
    assert resolved == []


class _HistoryApp(_FakeGitHubApp):
    """Resolves every ref to ``resolved``; only ``on_branch`` is in branch history."""

    def __init__(self, *, resolved: str, on_branch: set[str]) -> None:
        super().__init__(installations=[], repos_by_installation={})
        self.resolved = resolved
        self.on_branch = on_branch
        self.checked: list[tuple[str, str, str | None]] = []

    async def resolve_ref(self, repo: str, ref: str) -> str:
        return self.resolved

    async def require_commit_on_branch(
        self, repo: str, sha: str, *, branch: str | None = None
    ) -> str:
        from cloud_agent.github_app import CommitNotOnBranch

        self.checked.append((repo, sha, branch))
        if sha not in self.on_branch:
            raise CommitNotOnBranch(f"commit {sha[:12]} is not in the history of branch 'main'")
        return branch or "main"


async def _create_with_history_app(
    store: FakeStorage, monkeypatch, app_creds: _HistoryApp, **fields: str
) -> Any:
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {"repo": "owner/name", "task_prompt": "fix it", "model": "m", **fields}
            ),
        )


@pytest.mark.parametrize(
    "fields",
    [
        # A named commit, with and without the branch it claims to come from.
        {"base_sha": "c" * 40},
        {"base_sha": "c" * 40, "base_ref": "dev"},
        # A ref that resolves to such a commit is the same request.
        {"base_ref": "refs/pull/5/head"},
    ],
)
async def test_a_base_outside_the_repository_history_is_refused_at_creation(
    store: FakeStorage, monkeypatch, fields
):
    """GitHub serves a fork's pull request head by hash; that is not this repository's history.

    The publisher fetches the base by hash and pushes a branch built on it, so
    a base the repository's branches do not contain would have the platform
    push someone else's commits under its own name.
    """
    app_creds = _HistoryApp(resolved="c" * 40, on_branch={"f" * 40})
    jobs_before = dict(store.jobs)

    created = await _create_with_history_app(store, monkeypatch, app_creds, **fields)

    assert created.status_code == 400, created.text
    error = created.json()["detail"]["error"]
    assert error["type"] == "invalid_request"
    assert "not in the history of branch" in error["message"]
    assert "base_ref" in error["message"]
    assert app_creds.checked == [("owner/name", "c" * 40, fields.get("base_ref"))]
    assert store.jobs == jobs_before


@pytest.mark.parametrize(
    "fields", [{"base_sha": "f" * 40}, {"base_sha": "f" * 40, "base_ref": "dev"}, {}]
)
async def test_a_base_on_its_branch_is_accepted_at_creation(
    store: FakeStorage, monkeypatch, fields
):
    """Only a base that exists is checked, and one on its branch is kept as named."""
    app_creds = _HistoryApp(resolved="f" * 40, on_branch={"f" * 40})

    created = await _create_with_history_app(store, monkeypatch, app_creds, **fields)

    assert created.status_code == 201, created.text
    assert created.json()["base_sha"] == fields.get("base_sha")
    expected = [("owner/name", "f" * 40, fields.get("base_ref"))] if fields else []
    assert app_creds.checked == expected


async def test_branches_are_only_listed_for_an_entitled_repo(store: FakeStorage, monkeypatch):
    """This reads through the platform's installation, so it needs the same gate.

    Without it, the endpoint enumerates branches of any repository the App
    happens to cover — which is the confused deputy again, one level down.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from cloud_agent.api.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def branches_for_repo(self, repo: str) -> dict[str, Any]:
            return {"default": "main", "branches": ["main", "dev"]}

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mine = await client.get("/v1/agent/branches", params={"repo": "owner/name"})
        theirs = await client.get("/v1/agent/branches", params={"repo": "someone/private"})

    assert mine.status_code == 200
    assert mine.json()["default"] == "main"
    assert theirs.status_code == 403
