"""Tests for GitHub App credentials.

An App exists to make the publisher's credential short-lived and scoped, so
the cases below are the ones where getting it wrong quietly restores the
properties a PAT had: a token cached past its life, a repo resolved to the
wrong installation, or a private key leaking into a log line.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from cloud_agent.github_app import (
    AppConfig,
    GitHubAppCredentials,
    GitHubAppError,
    build_app_jwt,
)


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """Generate a throwaway RSA key so signing is exercised for real."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture()
def config(keypair) -> AppConfig:
    """An App config backed by the generated key."""
    return AppConfig(app_id="12345", private_key=keypair[0], api_base="https://api.test")


def _client_returning(handler):
    """Patch httpx.AsyncClient onto a mock transport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return original, Patched


def test_config_absent_is_none_not_an_error():
    """A deployment without the App should not publish, not fail to boot."""
    assert AppConfig.from_env({}) is None
    assert AppConfig.from_env({"AGENT_GITHUB_APP_ID": "1"}) is None


def test_config_unescapes_a_pasted_key(keypair):
    """A key pasted into an env var arrives with literal \\n sequences."""
    private_pem, _ = keypair
    config = AppConfig.from_env(
        {
            "AGENT_GITHUB_APP_ID": "42",
            "AGENT_GITHUB_APP_PRIVATE_KEY": private_pem.replace("\n", "\\n"),
        }
    )
    assert config is not None
    assert "\\n" not in config.private_key
    assert config.private_key.startswith("-----BEGIN")


def test_jwt_is_signed_verifiably_and_short_lived(config, keypair):
    """The JWT must verify against the public key and expire within ten minutes."""
    _, public_pem = keypair
    now = int(time.time())
    token = build_app_jwt(config, now=now)

    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_aud": False})
    assert claims["iss"] == "12345"
    # Backdated so a slightly fast clock is not rejected by GitHub.
    assert claims["iat"] <= now
    # GitHub refuses anything over ten minutes.
    assert 0 < claims["exp"] - now <= 600


def test_unusable_private_key_is_a_clear_error():
    """A malformed key must fail with a message, not a stack trace."""
    with pytest.raises(GitHubAppError):
        build_app_jwt(AppConfig(app_id="1", private_key="not a key"))


def test_user_authorization_uses_oauth_even_when_app_is_already_installed(config):
    oauth_config = AppConfig(
        app_id=config.app_id,
        private_key=config.private_key,
        api_base=config.api_base,
        client_id="Iv1.client",
        client_secret="secret",
    )

    url = urlparse(GitHubAppCredentials(oauth_config).user_authorization_url("signed-state"))

    assert url.path == "/login/oauth/authorize"
    assert parse_qs(url.query) == {
        "client_id": ["Iv1.client"],
        "state": ["signed-state"],
    }


async def test_token_is_scoped_to_the_repository_installation(config):
    """The repo resolves to its installation, and that installation's token."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/o/n/installation":
            return httpx.Response(200, json={"id": 777})
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_scoped", "expires_at": "2099-01-01T00:00:00Z"},
            )
        return httpx.Response(404, json={})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        assert await creds.token_for("o/n") == "ghs_scoped"
    finally:
        httpx.AsyncClient = original

    assert seen == [
        "GET /repos/o/n/installation",
        "POST /app/installations/777/access_tokens",
    ]


async def test_token_is_cached_but_not_past_its_life(config):
    """A live token is reused; an expiring one is re-minted.

    Caching forever would quietly restore the property the App exists to
    remove — a credential that outlives its window.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        calls["n"] += 1
        return httpx.Response(
            201,
            json={
                "token": f"ghs_{calls['n']}",
                # Already inside the refresh margin, so the next call re-mints.
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)),
            },
        )

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        first = await creds.token_for("o/n")
        second = await creds.token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert (first, second) == ("ghs_1", "ghs_2")
    assert calls["n"] == 2


async def test_a_caller_can_ask_for_a_token_that_outlives_its_work(config):
    """A token with twenty minutes left serves a quick caller, not a long publish."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        calls["n"] += 1
        expires = time.gmtime(time.time() + (1200 if calls["n"] == 1 else 3600))
        return httpx.Response(
            201,
            json={
                "token": f"ghs_{calls['n']}",
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", expires),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        assert await credentials.token_for("o/n") == "ghs_1"
        assert await credentials.token_for("o/n") == "ghs_1"
        assert await credentials.token_for("o/n", min_ttl_s=1800) == "ghs_2"
        assert await credentials.token_for("o/n", min_ttl_s=1800) == "ghs_2"

    assert calls["n"] == 2


async def test_a_long_lived_token_is_reused(config):
    """Within its window the token is served from cache, not re-minted."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        calls["n"] += 1
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        await creds.token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert calls["n"] == 1


async def test_uninstalled_repository_says_so(config):
    """A repo without an installation is a clear message, not a retry loop."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        with pytest.raises(GitHubAppError):
            await creds.token_for("o/uninstalled")
    finally:
        httpx.AsyncClient = original


async def test_malformed_repo_is_rejected_before_any_request(config):
    """'owner/name' is required; anything else never reaches GitHub."""
    creds = GitHubAppCredentials(config)
    with pytest.raises(GitHubAppError, match="owner/name"):
        await creds.installation_id_for("not-a-repo")


async def test_forget_drops_cached_state(config):
    """Uninstalling the App must not leave a usable cached token behind."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 5})
        return httpx.Response(201, json={"token": "ghs_y", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        assert creds._tokens
        creds.forget("o/n")
        assert not creds._tokens
    finally:
        httpx.AsyncClient = original


async def test_a_reinstalled_app_is_looked_up_again_rather_than_minted_against_forever(config):
    """Installing the App again gives the repository a new installation id.

    The remembered installation then refuses to mint. One fresh lookup finds
    the new one; before, every claim released with a 503 until a restart.
    """
    installation = {"id": 1}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/o/n/installation":
            return httpx.Response(200, json=installation)
        if request.url.path == f"/app/installations/{installation['id']}/access_tokens":
            return httpx.Response(
                201,
                json={"token": f"ghs_{installation['id']}", "expires_at": "2099-01-01T00:00:00Z"},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        assert await credentials.token_for("o/n", permissions={"contents": "read"}) == "ghs_1"
        installation["id"] = 2
        # Another scope has no cached token, so it mints against the remembered id.
        assert await credentials.token_for("o/n", permissions={"contents": "write"}) == "ghs_2"

    assert seen == [
        "GET /repos/o/n/installation",
        "POST /app/installations/1/access_tokens",
        "POST /app/installations/1/access_tokens",
        "GET /repos/o/n/installation",
        "POST /app/installations/2/access_tokens",
    ]


@pytest.mark.parametrize("still_named", [True, False])
async def test_an_installation_gone_for_good_is_settled_not_an_outage(config, still_named):
    """A mint refused again after a fresh lookup means the App does not cover the repo."""
    from cloud_agent.github_app import AppNotInstalled

    lookups: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/n/installation":
            lookups.append(1)
            if still_named or len(lookups) == 1:
                return httpx.Response(200, json={"id": 1})
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppNotInstalled):
            await GitHubAppCredentials(config, client=client).token_for("o/n")

    assert len(lookups) == 2


async def test_a_repository_s_installation_is_not_remembered_forever(config, monkeypatch):
    import cloud_agent.github_app as github_app

    lookups: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lookups.append(1)
        return httpx.Response(200, json={"id": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        assert await credentials.installation_id_for("o/n") == 1
        assert await credentials.installation_id_for("o/n") == 1
        assert len(lookups) == 1
        monkeypatch.setattr(github_app, "_INSTALLATION_TTL_S", 0.0)
        assert await credentials.installation_id_for("o/n") == 1

    assert len(lookups) == 2


def test_private_key_never_appears_in_an_error(config):
    """A failure message must not carry the key it failed to use."""
    # Assembled rather than written out. A PEM header is a credential shape,
    # and the export audit reads files, not intent — it cannot tell this one
    # from a real key, and it should not have to guess.
    label = "BEGIN " + "PRIVATE KEY"
    broken = AppConfig(app_id="1", private_key=f"-----{label}-----\nbroken\n")
    with pytest.raises(GitHubAppError) as excinfo:
        build_app_jwt(broken)
    assert "broken" not in str(excinfo.value)
    assert label not in str(excinfo.value)


async def test_a_narrowed_token_asks_github_for_less(config):
    """An installation token inherits every App permission unless it asks for less.

    The App holds `contents: write` so the publisher can push. The runner only
    needs to read, and it runs on the host that executes untrusted repository
    code — so its token must be requested narrower, not merely used narrowly.
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        import json as _json

        bodies.append(_json.loads(request.content) if request.content else {})
        return httpx.Response(201, json={"token": "ghs_ro", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n", permissions={"contents": "read"}, repository_scoped=True)
    finally:
        httpx.AsyncClient = original

    assert bodies[0]["permissions"] == {"contents": "read"}
    assert bodies[0]["repositories"] == ["n"]


async def test_a_narrowed_token_is_not_served_from_the_full_token_cache(config):
    """Sharing one cache slot would silently undo the narrowing, in both directions."""
    minted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        import json as _json

        payload = _json.loads(request.content) if request.content else {}
        minted.append(payload)
        label = "ro" if payload.get("permissions") else "rw"
        return httpx.Response(
            201, json={"token": f"ghs_{label}", "expires_at": "2099-01-01T00:00:00Z"}
        )

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        write = await creds.token_for("o/n")
        read = await creds.token_for("o/n", permissions={"contents": "read"})
        # And each is still cached within its own scope.
        read_again = await creds.token_for("o/n", permissions={"contents": "read"})
    finally:
        httpx.AsyncClient = original

    assert (write, read) == ("ghs_rw", "ghs_ro")
    assert read_again == "ghs_ro"
    assert len(minted) == 2, "the second read should have come from cache"


_WRITE = {"contents": "write", "pull_requests": "write"}


def _minting(minted: list[str]):
    """Answer every mint with a new token that lives until 2099."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        minted.append(f"ghs_{len(minted) + 1}")
        return httpx.Response(201, json={"token": minted[-1], "expires_at": "2099-01-01T00:00:00Z"})

    return httpx.MockTransport(respond)


async def test_a_token_handed_to_a_holder_is_its_own(config):
    """A sandbox's token is neither the publisher's nor another conversation's.

    Whoever holds an installation token can revoke it, with no permission at
    all. Served from one slot, a token one sandbox revoked was refused for the
    publisher and every other sandbox while the slot went on serving it.
    """
    minted: list[str] = []
    async with httpx.AsyncClient(transport=_minting(minted)) as client:
        credentials = GitHubAppCredentials(config, client=client)

        async def token(holder: str | None = None) -> str:
            return await credentials.token_for(
                "o/n", permissions=_WRITE, repository_scoped=True, holder=holder
            )

        publisher, first, second = await token(), await token("athr_1"), await token("athr_2")
        # Each stays cached for its own holder, so a follow-up keeps its token.
        assert [await token(), await token("athr_1"), await token("athr_2")] == minted

    assert minted == [publisher, first, second] == ["ghs_1", "ghs_2", "ghs_3"]


async def test_a_token_github_refused_is_not_served_again(config):
    """Dropped by value: a replacement another caller already minted stays."""
    minted: list[str] = []
    async with httpx.AsyncClient(transport=_minting(minted)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        refused = await credentials.token_for("o/n", permissions=_WRITE, repository_scoped=True)
        held = await credentials.token_for(
            "o/n", permissions=_WRITE, repository_scoped=True, holder="athr_1"
        )
        credentials.forget_token(refused)
        replacement = await credentials.token_for("o/n", permissions=_WRITE, repository_scoped=True)
        credentials.forget_token(refused)
        assert (
            await credentials.token_for("o/n", permissions=_WRITE, repository_scoped=True)
            == replacement
        )
        assert (
            await credentials.token_for(
                "o/n", permissions=_WRITE, repository_scoped=True, holder="athr_1"
            )
            == held
        )

    assert minted == [refused, held, replacement]


async def test_an_expired_holder_s_token_is_not_kept(config, monkeypatch):
    """A slot per conversation must not grow with every conversation ever held."""
    import cloud_agent.github_app as github_app

    now = [1_000_000.0]
    monkeypatch.setattr(github_app.time, "time", lambda: now[0])

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(201, json={"token": "ghs_x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        await credentials.token_for("o/n", holder="athr_1")
        now[0] += 3601
        await credentials.token_for("o/n", holder="athr_2")

    assert len(credentials._tokens) == 1


async def test_an_uninstalled_repository_is_distinguishable_from_an_outage(config):
    """404 is a settled answer; 503 is GitHub having a bad minute.

    Collapsing the two is what lets a momentary outage be handled as "no
    credential needed" and terminally fail a private repository's job.
    """
    from cloud_agent.github_app import AppNotInstalled

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "Service Unavailable"})

    original, patched = _client_returning(not_found)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(AppNotInstalled):
            await GitHubAppCredentials(config).token_for("o/n")
    finally:
        httpx.AsyncClient = original

    original, patched = _client_returning(unavailable)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(GitHubAppError) as excinfo:
            await GitHubAppCredentials(config).token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert not isinstance(excinfo.value, AppNotInstalled), (
        "a 503 must not be reported as 'the App is not installed here'"
    )
    assert excinfo.value.status == 503


async def test_repository_contents_uses_read_only_repo_scope_and_pinned_ref(config):
    """The Files view cannot inherit write access or follow a moving branch."""
    token_bodies: list[dict] = []
    content_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 9})
        if request.url.path.endswith("/access_tokens"):
            import json as _json

            token_bodies.append(_json.loads(request.content))
            return httpx.Response(
                201, json={"token": "ghs_read", "expires_at": "2099-01-01T00:00:00Z"}
            )
        content_requests.append(request)
        return httpx.Response(200, json={"type": "file", "content": "aGk=", "encoding": "base64"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        result = await GitHubAppCredentials(config).repository_contents(
            "o/n", path="docs/a #.md", ref="a" * 40
        )
    finally:
        httpx.AsyncClient = original

    assert result["type"] == "file"
    assert token_bodies == [{"permissions": {"contents": "read"}, "repositories": ["n"]}]
    assert len(content_requests) == 1
    assert (
        content_requests[0].url.raw_path == b"/repos/o/n/contents/docs/a%20%23.md?ref=" + b"a" * 40
    )


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "C:/Windows", r"dir\file", ".GIT/config"]
)
async def test_repository_contents_rejects_unsafe_paths_before_minting(config, path):
    """The credential helper remains safe when called without the HTTP route."""
    with pytest.raises(GitHubAppError, match="safe relative path"):
        await GitHubAppCredentials(config).repository_contents("o/n", path=path, ref="a" * 40)


async def test_repository_contents_rejects_a_moving_ref(config):
    """A branch name must never make the Files tab diverge from the agent tree."""
    with pytest.raises(GitHubAppError, match="pinned"):
        await GitHubAppCredentials(config).repository_contents("o/n", ref="dev")


async def test_a_ref_to_resolve_can_only_ever_name_a_ref(config):
    """The requester's ref reaches the commits endpoint encoded, or not at all.

    Interpolated raw, `../../../../installation/repositories` was collapsed by
    the HTTP client into another endpoint and `main?x=1` added a query, both
    sent with an installation token.
    """
    requested: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/n/installation":
            return httpx.Response(200, json={"id": 9})
        if request.url.path == "/app/installations/9/access_tokens":
            return httpx.Response(
                201, json={"token": "ghs_read", "expires_at": "2099-01-01T00:00:00Z"}
            )
        requested.append(request.url.raw_path)
        return httpx.Response(200, json={"sha": "f" * 40})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        for ref in ("../../../../installation/repositories", "main?x=1", "..", "-x", "a b"):
            with pytest.raises(GitHubAppError, match="not a valid Git ref name"):
                await credentials.resolve_ref("o/n", ref)
        assert requested == []
        assert await credentials.resolve_ref("o/n", "release/1.2") == "f" * 40
        assert await credentials.resolve_ref("o/n", "fix#12%") == "f" * 40

    assert requested == [b"/repos/o/n/commits/release/1.2", b"/repos/o/n/commits/fix%2312%25"]


@pytest.mark.parametrize(
    ("ref", "accepted"),
    [
        ("main", True),
        ("release/1.2", True),
        ("v1.0.0", True),
        ("a" * 40, True),
        ("feature/x#1", True),
        ("", False),
        ("@", False),
        ("-main", False),
        ("/main", False),
        ("main/", False),
        ("main.", False),
        ("a..b", False),
        ("a//b", False),
        ("a@{1}", False),
        (".hidden", False),
        ("x/.hidden", False),
        ("x.lock", False),
        ("x.lock/y", False),
        ("a b", False),
        ("a~1", False),
        ("a^", False),
        ("a:b", False),
        ("a?b", False),
        ("a*", False),
        ("a[b", False),
        ("a\\b", False),
        ("a\x7fb", False),
        ("a\nb", False),
    ],
)
def test_ref_names_follow_git_s_own_rules(ref, accepted):
    from cloud_agent.github_app import is_ref_name

    assert is_ref_name(ref) is accepted


def _entitlement_handler(minted: list[dict], listed: list[int]):
    """Answer the three calls repository entitlement makes, counting each."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        if request.url.path.endswith("/access_tokens"):
            import json as _json

            payload = _json.loads(request.content) if request.content else {}
            minted.append(payload)
            label = "ro" if payload.get("permissions") else "full"
            return httpx.Response(
                201, json={"token": f"ghs_{label}", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if request.url.path == "/installation/repositories":
            listed.append(1)
            return httpx.Response(200, json={"repositories": [{"full_name": "o/n"}]})
        raise AssertionError(f"unexpected call to {request.url}")

    return handler


async def test_entitlement_stops_minting_a_token_on_every_call(config):
    """`_installation_token` bypassed the cache its own class already keeps.

    Repository entitlement runs on every workspace file read, every write and
    every terminal create/list/stream, so a mint that never consulted the
    cache put a GitHub round trip on the interactive path and kept it there.
    The repository list itself stays live — only the credential is reused.
    """
    minted: list[dict] = []
    listed: list[int] = []
    original, patched = _client_returning(_entitlement_handler(minted, listed))
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        first = await creds.repositories_for_installation(1)
        second = await creds.repositories_for_installation(1)
    finally:
        httpx.AsyncClient = original

    assert first == second == ["o/n"]
    assert len(minted) == 1, "the second call should have reused the cached token"
    assert len(listed) == 2, "the repository list must not be cached by this change"


async def test_entitlement_shares_the_unnarrowed_slot_with_token_for(config):
    """It is the same request, so it is the same slot — that is why this is safe."""
    minted: list[dict] = []
    listed: list[int] = []
    original, patched = _client_returning(_entitlement_handler(minted, listed))
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        await creds.repositories_for_installation(1)
    finally:
        httpx.AsyncClient = original

    assert len(minted) == 1


async def test_a_narrowed_token_is_never_served_to_the_entitlement_path(config):
    """The scope key still separates them; sharing a slot must stay impossible."""
    minted: list[dict] = []
    listed: list[int] = []
    original, patched = _client_returning(_entitlement_handler(minted, listed))
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        narrowed = await creds.token_for(
            "o/n", permissions={"contents": "read"}, repository_scoped=True
        )
        await creds.repositories_for_installation(1)
    finally:
        httpx.AsyncClient = original

    assert narrowed == "ghs_ro"
    assert len(minted) == 2, "entitlement must not be handed the narrowed token"
    assert minted[1] == {}, "and must still ask for an unnarrowed one"


async def test_forget_drops_the_entitlement_token_too(config):
    """Uninstalling the App must not leave a cached credential behind."""
    minted: list[dict] = []
    listed: list[int] = []
    original, patched = _client_returning(_entitlement_handler(minted, listed))
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        await creds.repositories_for_installation(1)
        creds.forget("o/n")
        await creds.repositories_for_installation(1)
    finally:
        httpx.AsyncClient = original

    assert len(minted) == 2


@pytest.mark.asyncio
async def test_skill_contents_reuse_configured_client_and_bound_symbolic_ref_reads(config):
    from unittest.mock import AsyncMock

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"type": "file", "content": "x" * 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        credentials.token_for = AsyncMock(return_value="scoped-token")
        with pytest.raises(GitHubAppError, match="allowed size"):
            await credentials.repository_contents(
                "owner/repo",
                path="SKILL.md",
                ref="feature/review",
                allow_symbolic_ref=True,
                max_response_bytes=50,
            )
        result = await credentials.repository_contents(
            "owner/repo",
            path="SKILL.md",
            ref="feature/review",
            allow_symbolic_ref=True,
            max_response_bytes=1000,
        )
        assert result["type"] == "file"
        assert not client.is_closed
        assert len(requests) == 2
        assert all(request.url.host == "api.test" for request in requests)
        assert requests[0].url.params["ref"] == "feature/review"
        assert requests[0].headers["authorization"] == "Bearer scoped-token"
        credentials.token_for.assert_awaited_with(
            "owner/repo", permissions={"contents": "read"}, repository_scoped=True
        )


async def test_the_connected_account_is_the_token_s_own_user(config):
    """Who connected is GitHub's answer for the exchanged token, nothing else."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 583231, "login": "octocat", "name": "Mona"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        profile = await GitHubAppCredentials(config, client=client).user_profile("ghu_user")

    assert profile == {"id": 583231, "login": "octocat"}
    assert [request.url.path for request in requests] == ["/user"]
    assert requests[0].headers["authorization"] == "Bearer ghu_user"


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"message": "Bad credentials"}), (200, {"id": 1}), (200, {"login": "octocat"})],
)
async def test_an_unreadable_user_is_an_error_not_a_nameless_account(config, status, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    ) as client:
        with pytest.raises(GitHubAppError):
            await GitHubAppCredentials(config, client=client).user_profile("ghu_user")


async def test_every_page_of_the_user_s_installations_is_read(config):
    """A connection's grants become exactly this list, so a page left unread is
    an installation revoked."""
    pages = {
        "1": [{"id": n, "account": {"login": f"org-{n}", "id": n}} for n in range(1, 101)],
        "2": [{"id": 101, "account": {"login": "octocat", "id": 583231}}, {"id": "not-an-id"}],
    }
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"installations": pages[request.url.params["page"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        found = await GitHubAppCredentials(config, client=client).installations_for_user("ghu")

    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert all(request.url.params["per_page"] == "100" for request in requests)
    assert len(found) == 101
    assert found[-1] == {"installation_id": 101, "account_login": "octocat", "account_id": 583231}


# ── The user's own token ─────────────────────────────────────────────────


def _oauth(config: AppConfig) -> AppConfig:
    return AppConfig(
        app_id=config.app_id,
        private_key=config.private_key,
        api_base=config.api_base,
        client_id="Iv1.client",
        client_secret="secret",
    )


async def test_an_exchanged_token_keeps_how_long_it_lasts_and_how_to_renew_it(config):
    import json

    def respond(request):
        assert parse_qs(request.content.decode())["code"] == ["the-code"]
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "access_token": "ghu_a",
                    "expires_in": 28800,
                    "refresh_token": "ghr_a",
                    "refresh_token_expires_in": 15897600,
                }
            ),
        )

    before = time.time()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tokens = await GitHubAppCredentials(_oauth(config), client=client).exchange_user_code(
            "the-code"
        )

    assert (tokens.access_token, tokens.refresh_token) == ("ghu_a", "ghr_a")
    assert tokens.access_expires_at is not None and tokens.refresh_expires_at is not None
    assert 28800 - 5 <= tokens.access_expires_at.timestamp() - before <= 28800 + 5


async def test_a_token_that_does_not_expire_has_nothing_to_renew(config):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "ghu_forever"})
        )
    ) as client:
        tokens = await GitHubAppCredentials(_oauth(config), client=client).exchange_user_code("c")

    assert tokens.access_token == "ghu_forever"
    assert (tokens.access_expires_at, tokens.refresh_token) == (None, None)


async def test_renewing_spends_the_refresh_token_with_the_app_s_own_secret(config):
    from cloud_agent.github_app import UserAuthorizationRevoked

    sent = []
    request_timeouts = []

    def respond(request):
        sent.append(parse_qs(request.content.decode()))
        request_timeouts.append(request.extensions["timeout"]["read"])
        if sent[-1]["refresh_token"] == ["ghr_spent"]:
            return httpx.Response(200, json={"error": "bad_refresh_token"})
        return httpx.Response(
            200, json={"access_token": "ghu_b", "expires_in": 28800, "refresh_token": "ghr_b"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = GitHubAppCredentials(_oauth(config), client=client)
        tokens = await credentials.refresh_user_token("ghr_a", timeout_s=17)
        with pytest.raises(UserAuthorizationRevoked):
            await credentials.refresh_user_token("ghr_spent")

    assert (tokens.access_token, tokens.refresh_token) == ("ghu_b", "ghr_b")
    assert sent[0] == {
        "client_id": ["Iv1.client"],
        "client_secret": ["secret"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["ghr_a"],
    }
    assert request_timeouts[0] == 17


async def test_github_down_while_renewing_is_not_a_revoked_authorization(config):
    """A 502 must not make the user reconnect: the stored token stays."""
    from cloud_agent.github_app import UserAuthorizationRevoked

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway"))
    ) as client:
        with pytest.raises(GitHubAppError) as raised:
            await GitHubAppCredentials(_oauth(config), client=client).refresh_user_token("ghr_a")

    assert not isinstance(raised.value, UserAuthorizationRevoked)


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (429, {"retry-after": "60"}, {"message": "rate limited"}),
        (403, {"x-ratelimit-remaining": "0"}, {"message": "API rate limit exceeded"}),
        (401, {}, {"error": "invalid_client"}),
        (200, {}, {"error": "incorrect_client_credentials"}),
        (400, {}, "<html>bad request</html>"),
    ],
)
async def test_a_refusal_that_is_not_about_the_refresh_token_keeps_the_connection(
    config, status, headers, body
):
    """A rate limit or the App's own refused secret must not make the user reconnect."""
    from cloud_agent.github_app import UserAuthorizationRevoked

    def respond(request):
        if isinstance(body, str):
            return httpx.Response(status, headers=headers, text=body)
        return httpx.Response(status, headers=headers, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(GitHubAppError) as raised:
            await GitHubAppCredentials(_oauth(config), client=client).refresh_user_token("ghr_a")

    assert not isinstance(raised.value, UserAuthorizationRevoked)
    assert raised.value.status == status


@pytest.mark.parametrize(("status", "error"), [(200, "bad_refresh_token"), (400, "invalid_grant")])
async def test_a_finished_refresh_token_is_a_revoked_authorization(config, status, error):
    """GitHub's own code and the OAuth standard's both settle it: connect again."""
    from cloud_agent.github_app import UserAuthorizationRevoked

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": error}))
    ) as client:
        with pytest.raises(UserAuthorizationRevoked, match=error):
            await GitHubAppCredentials(_oauth(config), client=client).refresh_user_token("ghr_a")


async def test_only_repositories_the_user_can_push_to_are_theirs(config):
    """Asked as the user, every page, and push is the bar — not read."""
    requests = []
    pages = {
        "1": [
            {"full_name": f"acme/r{n}", "permissions": {"pull": True, "push": n % 2 == 0}}
            for n in range(100)
        ],
        "2": [
            {"full_name": "acme/last", "permissions": {"admin": True, "push": True}},
            {"full_name": "acme/no-permissions"},
        ],
    }

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"repositories": pages[request.url.params["page"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        found = await GitHubAppCredentials(config, client=client).writable_repositories_for_user(
            "ghu_user", 77
        )

    assert [request.url.path for request in requests] == [
        "/user/installations/77/repositories",
        "/user/installations/77/repositories",
    ]
    assert all(request.headers["authorization"] == "Bearer ghu_user" for request in requests)
    assert len(found) == 51
    assert "acme/r1" not in found and "acme/no-permissions" not in found
    assert found[-1] == "acme/last"


@pytest.mark.parametrize(("status", "expected"), [(404, []), (403, [])])
async def test_an_installation_the_user_lost_contributes_nothing(config, status, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    ) as client:
        credentials = GitHubAppCredentials(config, client=client)
        assert await credentials.writable_repositories_for_user("ghu_user", 77) == expected


@pytest.mark.parametrize(
    ("status", "headers", "body", "wait"),
    [
        (403, {"retry-after": "30"}, {}, 30.0),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}, {}, 0.0),
        (403, {}, {"message": "You have exceeded a secondary rate limit."}, 0.0),
        (429, {}, {}, 0.0),
    ],
)
async def test_a_rate_limited_listing_is_an_outage_not_a_lost_installation(
    config, status, headers, body, wait
):
    """GitHub answers its rate limits with 403 too, and a limit says nothing about access."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers=headers, json=body)
        )
    ) as client:
        credentials = GitHubAppCredentials(config, client=client)
        with pytest.raises(GitHubAppError) as raised:
            await credentials.writable_repositories_for_user("ghu_user", 77)
    assert (raised.value.status, raised.value.retry_after_s) == (status, wait)


async def test_a_refused_user_token_is_a_revoked_authorization_and_an_outage_is_not(config):
    from cloud_agent.github_app import UserAuthorizationRevoked

    statuses = iter([401, 500])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses), json={}))
    ) as client:
        credentials = GitHubAppCredentials(config, client=client)
        with pytest.raises(UserAuthorizationRevoked):
            await credentials.writable_repositories_for_user("ghu_user", 77)
        with pytest.raises(GitHubAppError) as raised:
            await credentials.writable_repositories_for_user("ghu_user", 77)
    assert not isinstance(raised.value, UserAuthorizationRevoked)


async def test_withdrawing_the_authorization_names_the_token_under_the_app_s_own_secret(config):
    import base64
    import json

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await GitHubAppCredentials(_oauth(config), client=client).revoke_user_authorization(
            "ghu_user"
        )

    [request] = requests
    assert (request.method, request.url.path) == ("DELETE", "/applications/Iv1.client/grant")
    assert (
        request.headers["authorization"]
        == "Basic " + base64.b64encode(b"Iv1.client:secret").decode()
    )
    assert json.loads(request.content) == {"access_token": "ghu_user"}


def test_a_users_repositories_are_remembered_for_a_minute_and_forgotten_on_change(
    config, monkeypatch
):
    import cloud_agent.github_app as github_app

    now = [1000.0]
    monkeypatch.setattr(github_app.time, "monotonic", lambda: now[0])
    credentials = GitHubAppCredentials(config)
    credentials.remember_user_repositories("alice", (77,), ["acme/service"])

    assert credentials.cached_user_repositories("alice", (77,)) == ["acme/service"]
    # Another set of installations is another question.
    assert credentials.cached_user_repositories("alice", (77, 88)) is None
    assert credentials.cached_user_repositories("bob", (77,)) is None
    now[0] += 61
    assert credentials.cached_user_repositories("alice", (77,)) is None

    credentials.remember_user_repositories("alice", (77,), ["acme/service"])
    credentials.forget_user("alice")
    assert credentials.cached_user_repositories("alice", (77,)) is None


@pytest.mark.parametrize("private", [True, False])
async def test_creating_a_personal_repository_uses_the_users_token_and_initializes_it(
    config, private
):
    """A personal repository uses the user's authority and starts with a branch."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "full_name": "octocat/new-project",
                "html_url": "https://github.com/octocat/new-project",
                "default_branch": "main",
                "owner": {"login": "octocat"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GitHubAppCredentials(config, client=client).create_personal_repository(
            "ghu_user", name="new-project", description="New project", private=private
        )
        assert not client.is_closed

    assert result == {
        "full_name": "octocat/new-project",
        "html_url": "https://github.com/octocat/new-project",
        "default_branch": "main",
    }
    [request] = requests
    assert (request.method, str(request.url)) == ("POST", "https://api.test/user/repos")
    assert request.headers["authorization"] == "Bearer ghu_user"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert json.loads(request.content) == {
        "name": "new-project",
        "description": "New project",
        "private": private,
        "auto_init": True,
    }


@pytest.mark.parametrize(
    ("status", "body", "headers", "reason"),
    [
        (403, {"message": "secret-token upstream body"}, {}, "forbidden"),
        (404, {"message": "secret-token upstream body"}, {}, "forbidden"),
        (
            422,
            {
                "message": "secret-token upstream body",
                "errors": [{"resource": "Repository", "field": "name", "code": "already_exists"}],
            },
            {},
            "already_exists",
        ),
        (
            422,
            {
                "message": "secret-token upstream body",
                "errors": [
                    {
                        "resource": "Repository",
                        "field": "name",
                        "code": "custom",
                        "message": "name already exists on this account",
                    }
                ],
            },
            {},
            "already_exists",
        ),
        (422, {"message": "name already exists: secret-token upstream body"}, {}, "invalid"),
        (
            422,
            {
                "message": "secret-token upstream body",
                "errors": [{"resource": "Repository", "field": "name", "code": "invalid"}],
            },
            {},
            "invalid",
        ),
        (
            422,
            {
                "message": "secret-token upstream body",
                "errors": [
                    {"resource": "Repository", "field": "description", "code": "already_exists"}
                ],
            },
            {},
            "invalid",
        ),
        (429, {"message": "secret-token upstream body"}, {}, "unavailable"),
        (
            403,
            {"message": "secret-token upstream body"},
            {"X-RateLimit-Remaining": "0"},
            "unavailable",
        ),
        (
            403,
            {"message": "secret-token upstream body"},
            {"Retry-After": "60"},
            "unavailable",
        ),
        (500, {"message": "secret-token upstream body"}, {}, "uncertain"),
        (502, {"message": "secret-token upstream body"}, {}, "uncertain"),
        (503, {"message": "secret-token upstream body"}, {}, "uncertain"),
    ],
)
async def test_repository_creation_distinguishes_settled_errors_without_retrying_or_echoing(
    config, status, body, headers, reason
):
    """Callers need a safe next step and must never replay an uncertain POST."""
    from cloud_agent.github_app import RepositoryCreationError

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RepositoryCreationError) as raised:
            await GitHubAppCredentials(config, client=client).create_personal_repository(
                "secret-token", name="new-project", description="", private=True
            )

    assert raised.value.reason == reason
    assert raised.value.status == status
    assert len(requests) == 1
    assert "secret-token" not in str(raised.value)
    assert "upstream body" not in str(raised.value)


async def test_repository_creation_rejects_a_revoked_user_authorization(config):
    """An invalid user token must trigger the existing reconnect flow."""
    from cloud_agent.github_app import UserAuthorizationRevoked

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401, json={"message": "secret-token upstream body"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(UserAuthorizationRevoked) as raised:
            await GitHubAppCredentials(config, client=client).create_personal_repository(
                "secret-token", name="new-project", description="", private=True
            )

    assert len(requests) == 1
    assert "secret-token" not in str(raised.value)
    assert "upstream body" not in str(raised.value)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_repository_creation_network_errors_leave_the_result_uncertain(config, error_type):
    """GitHub may have committed a timed-out request, so it cannot be retried."""
    from cloud_agent.github_app import RepositoryCreationError

    requests = []

    def respond(request):
        requests.append(request)
        raise error_type("secret-token upstream body", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RepositoryCreationError) as raised:
            await GitHubAppCredentials(config, client=client).create_personal_repository(
                "secret-token", name="new-project", description="", private=True
            )

    assert raised.value.reason == "uncertain"
    assert raised.value.status is None
    assert len(requests) == 1
    assert "secret-token" not in str(raised.value)
    assert "upstream body" not in str(raised.value)


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON secret-token upstream body",
        b"[]",
        b"null",
        b"{}",
        b'{"full_name":"octocat/new-project","html_url":"https://github.com/octocat/new-project"}',
        b'{"full_name":1,"html_url":"https://github.com/octocat/new-project","default_branch":"main"}',
        b'{"full_name":"octocat/new-project","html_url":"https://github.com/octocat/new-project","default_branch":""}',
    ],
)
async def test_repository_creation_malformed_success_is_uncertain_and_never_retried(
    config, content
):
    """A success that cannot identify the new repository is not safe to replay."""
    from cloud_agent.github_app import RepositoryCreationError

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RepositoryCreationError) as raised:
            await GitHubAppCredentials(config, client=client).create_personal_repository(
                "secret-token", name="new-project", description="", private=True
            )

    assert raised.value.reason == "uncertain"
    assert raised.value.status == 201
    assert len(requests) == 1
    assert "secret-token" not in str(raised.value)
    assert "upstream body" not in str(raised.value)


# ── a base commit has to be in a branch's history ─────────────────────

_TIP = "1" * 40
_OLDER = "2" * 40
_FORK_HEAD = "3" * 40


def _branch_history(
    *,
    branches: dict[str, str],
    default: str = "main",
    compare: dict[tuple[str, str], tuple[int, dict]] | None = None,
    requests: list[httpx.Request],
):
    """Answer the three reads the base check makes, recording each request."""

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        prefix = "/repos/o/n"
        if path == prefix:
            return httpx.Response(200, json={"default_branch": default})
        if path.startswith(f"{prefix}/branches/"):
            name = path.removeprefix(f"{prefix}/branches/")
            if name not in branches:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": name, "commit": {"sha": branches[name]}})
        if path.startswith(f"{prefix}/compare/"):
            base, _, head = path.removeprefix(f"{prefix}/compare/").partition("...")
            status, body = (compare or {}).get((base, head), (404, {"message": "Not Found"}))
            return httpx.Response(status, json=body)
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    return respond


async def _check(config, respond, sha: str, branch: str | None) -> str:
    from unittest.mock import AsyncMock

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = GitHubAppCredentials(config, client=client)
        credentials.token_for = AsyncMock(return_value="scoped-token")
        try:
            return await credentials.require_commit_on_branch("o/n", sha, branch=branch)
        finally:
            credentials.token_for.assert_awaited_with(
                "o/n", permissions={"contents": "read"}, repository_scoped=True
            )


async def test_a_branch_tip_needs_no_comparison(config):
    requests: list[httpx.Request] = []
    respond = _branch_history(branches={"dev": _TIP}, requests=requests)

    assert await _check(config, respond, _TIP, "dev") == "dev"
    assert [request.url.path for request in requests] == ["/repos/o/n/branches/dev"]


@pytest.mark.parametrize("status", ["behind", "identical"])
async def test_an_ancestor_of_the_named_branch_is_accepted(config, status):
    requests: list[httpx.Request] = []
    respond = _branch_history(
        branches={"release/x": _TIP},
        compare={(_TIP, _OLDER): (200, {"status": status})},
        requests=requests,
    )

    assert await _check(config, respond, _OLDER, "release/x") == "release/x"
    # Compared from the tip's hash: the caller's text never names the base.
    assert requests[-1].url.path == f"/repos/o/n/compare/{_TIP}...{_OLDER}"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"status": "diverged"}),
        (200, {"status": "ahead"}),
        (200, {}),
        (404, {"message": "Not Found"}),
        (422, {"message": "No common ancestor"}),
    ],
)
async def test_a_commit_outside_the_branch_history_is_refused(config, status, body):
    """A fork's pull request head fetches by hash but is not the branch's history."""
    from cloud_agent.github_app import CommitNotOnBranch

    requests: list[httpx.Request] = []
    respond = _branch_history(
        branches={"dev": _TIP}, compare={(_TIP, _FORK_HEAD): (status, body)}, requests=requests
    )

    with pytest.raises(CommitNotOnBranch, match="not in the history of branch 'dev' in o/n"):
        await _check(config, respond, _FORK_HEAD, "dev")


@pytest.mark.parametrize("named", [None, _FORK_HEAD, "refs/pull/5/head", "fork-owner:main"])
async def test_without_a_named_branch_the_default_branch_is_the_anchor(config, named):
    """A tag, commit, pull ref or cross-repository name is not a branch of this repository."""
    from cloud_agent.github_app import CommitNotOnBranch

    requests: list[httpx.Request] = []
    respond = _branch_history(
        branches={"main": _TIP},
        compare={(_TIP, _OLDER): (200, {"status": "behind"})},
        requests=requests,
    )

    assert await _check(config, respond, _OLDER, named) == "main"
    assert requests[-1].url.path == f"/repos/o/n/compare/{_TIP}...{_OLDER}"
    with pytest.raises(CommitNotOnBranch, match="branch 'main'"):
        await _check(config, respond, _FORK_HEAD, named)


async def test_failing_to_read_the_branch_is_not_a_refusal(config):
    """An outage must stay distinguishable from a settled 'not on this branch'."""
    from cloud_agent.github_app import CommitNotOnBranch

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(GitHubAppError) as raised:
        await _check(config, respond, _OLDER, "dev")
    assert not isinstance(raised.value, CommitNotOnBranch)
    assert raised.value.status == 502


async def test_a_non_hash_base_is_refused_before_any_request(config):
    with pytest.raises(GitHubAppError, match="commit sha"):
        await GitHubAppCredentials(config).require_commit_on_branch("o/n", "--upload-pack=x")
