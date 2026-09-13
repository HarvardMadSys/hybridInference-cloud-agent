"""Discovery, registration and token parsing, branch by branch.

`test_mcp_protocol_and_authorization.py` pins the rules a flow depends on -- the
resource parameter, the rotation contract, PKCE. This file covers the parts that
only run when something is *unusual*, and each of them decides whether a user
gets a browser or a diagnosis:

- where the authorization server is found, and what happens when the metadata
  says something other than what was asked for;
- which of the three registration mechanisms is chosen, in the order the design
  specifies;
- what a token response means when it is missing a field the flow would like;
- which scopes are asked for when nobody said.

The metadata fetcher is a stub rather than a mock of httpx, because discovery is
the part under test and the HTTP call under it is already covered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud_agent.mcp import oauth
from cloud_agent.mcp.net_policy import EgressPolicy

_RESOURCE = "https://mcp.example.com/mcp"
_ISSUER = "https://auth.example.com"


class _Fetcher:
    """A metadata server with a scripted answer per URL."""

    def __init__(self, answers: dict[str, Any]) -> None:
        """Answer with ``answers[url]``, or raise when it is an exception."""
        self.answers = answers
        self.requested: list[str] = []

    async def fetch_json(
        self, url: str, *, policy: EgressPolicy, headers: Any = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return the scripted document, or raise what the script says."""
        del policy, headers
        self.requested.append(url)
        answer = self.answers.get(url)
        if answer is None:
            raise RuntimeError(f"nothing scripted for {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer, {}


def _policy() -> EgressPolicy:
    """A policy that allows the metadata hosts this file uses."""
    return EgressPolicy()


def _resource_metadata(*servers: str, resource: str = _RESOURCE) -> dict[str, Any]:
    """A protected-resource document naming ``servers``."""
    return {"resource": resource, "authorization_servers": list(servers)}


def _server_metadata(**overrides: Any) -> dict[str, Any]:
    """An authorization-server document, with everything the flow needs."""
    document: dict[str, Any] = {
        "issuer": _ISSUER,
        "authorization_endpoint": f"{_ISSUER}/authorize",
        "token_endpoint": f"{_ISSUER}/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    document.update(overrides)
    return document


class TestDiscovery:
    """Finding the authorization server, or saying why not."""

    async def test_the_challenge_header_points_at_the_metadata(self) -> None:
        """It is authoritative when the well-known location is not where it is.

        That is the whole reason the header exists, so it is tried before the
        guess.
        """
        # A document at a location no well-known path would have guessed.
        announced = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
        fetcher = _Fetcher(
            {
                announced: _resource_metadata(_ISSUER),
                f"{_ISSUER}/.well-known/oauth-authorization-server": _server_metadata(),
            }
        )
        resource, server = await oauth.discover_authorization_server(
            fetcher,
            resource_url=_RESOURCE,
            policy=_policy(),
            challenge_header=f'Bearer resource_metadata="{announced}"',
        )
        assert resource is not None
        assert server.issuer == _ISSUER
        # And the well-known path was never guessed at.
        assert len(fetcher.requested) == 2

    async def test_the_well_known_path_is_used_when_no_header_names_one(self) -> None:
        """The common case: the server publishes where the specification says."""
        fetcher = _Fetcher(
            {
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": (
                    _resource_metadata(_ISSUER)
                ),
                f"{_ISSUER}/.well-known/oauth-authorization-server": _server_metadata(),
            }
        )
        _resource, server = await oauth.discover_authorization_server(
            fetcher, resource_url=_RESOURCE, policy=_policy()
        )
        assert server.token_endpoint == f"{_ISSUER}/token"

    async def test_a_preconfigured_issuer_skips_the_resource_document(self) -> None:
        """When the deployment already knows the issuer, fetching a document to
        learn it is a round trip that can only produce a different answer."""
        fetcher = _Fetcher(
            {f"{_ISSUER}/.well-known/oauth-authorization-server": _server_metadata()}
        )
        resource, server = await oauth.discover_authorization_server(
            fetcher, resource_url=_RESOURCE, policy=_policy(), preregistered_issuer=_ISSUER
        )
        assert resource is None
        assert server.issuer == _ISSUER
        assert fetcher.requested == [f"{_ISSUER}/.well-known/oauth-authorization-server"]

    async def test_metadata_describing_another_resource_is_refused(self) -> None:
        """A document at this address about somebody else's server is a
        misconfiguration at best, and a redirect at worst."""
        url = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
        fetcher = _Fetcher({url: _resource_metadata(_ISSUER, resource="https://other/mcp")})
        with pytest.raises(oauth.OAuthError):
            await oauth.discover_authorization_server(
                fetcher, resource_url=_RESOURCE, policy=_policy()
            )

    async def test_resource_metadata_naming_no_authorization_server_is_refused(self) -> None:
        """It has to say who authorizes it; an empty list is not a lead."""
        fetcher = _Fetcher(
            {
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": (
                    _resource_metadata()
                )
            }
        )
        with pytest.raises(oauth.OAuthError):
            await oauth.discover_authorization_server(
                fetcher, resource_url=_RESOURCE, policy=_policy()
            )

    async def test_no_discoverable_metadata_is_a_diagnosis_not_a_crash(self) -> None:
        """Every candidate fails, and the user is told that rather than shown a
        stack trace from the last one."""
        fetcher = _Fetcher({})
        with pytest.raises(oauth.OAuthError) as raised:
            await oauth.discover_authorization_server(
                fetcher, resource_url=_RESOURCE, policy=_policy()
            )
        assert raised.value.kind == "protocol_incompatible"

    async def test_a_server_without_s256_pkce_is_refused_before_the_browser(self) -> None:
        """Better at the connection than after a login the platform cannot finish."""
        fetcher = _Fetcher(
            {
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": (
                    _resource_metadata(_ISSUER)
                ),
                f"{_ISSUER}/.well-known/oauth-authorization-server": _server_metadata(
                    code_challenge_methods_supported=["plain"]
                ),
            }
        )
        with pytest.raises(oauth.OAuthError) as raised:
            await oauth.discover_authorization_server(
                fetcher, resource_url=_RESOURCE, policy=_policy()
            )
        assert "S256" in str(raised.value)

    async def test_an_issuer_with_a_path_keeps_it_in_the_metadata_url(self) -> None:
        """Dropping it fetches the tenant-less document, and the issuer check
        then refuses a document that was never about this tenant."""
        urls = oauth.authorization_server_metadata_urls("https://auth.example.com/tenant1")
        assert urls[0] == (
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant1"
        )


class TestRegistration:
    """Which mechanism is chosen, in the order the design specifies."""

    def _metadata(self, **overrides: Any) -> oauth.AuthorizationServerMetadata:
        return oauth.parse_authorization_server_metadata(
            _server_metadata(**overrides), expected_issuer=_ISSUER
        )

    async def test_a_metadata_document_beats_dynamic_registration(self) -> None:
        """CIMD needs no state created on our behalf, so it comes first."""
        metadata = self._metadata(
            registration_endpoint=f"{_ISSUER}/register",
            client_id_metadata_document_supported=True,
        )
        identity = oauth.choose_registration(
            metadata, cimd_client_id="https://agent.example.com/client.json"
        )
        assert identity.registration == "cimd"
        # And it is not ours to delete: the authorization server never created it.
        assert identity.delete_on_disconnect is False

    async def test_dynamic_registration_is_used_when_nothing_better_is_offered(self) -> None:
        """Public clients only: a server that wants a secret needs a person to
        supply one, not a registration we cannot complete."""
        metadata = self._metadata(registration_endpoint=f"{_ISSUER}/register")
        identity = oauth.choose_registration(metadata)
        assert identity.registration == "dcr"
        assert identity.delete_on_disconnect is True

    async def test_a_server_wanting_a_secret_asks_the_user_instead(self) -> None:
        """Rather than registering a client that cannot authenticate."""
        metadata = self._metadata(
            registration_endpoint=f"{_ISSUER}/register",
            token_endpoint_auth_methods_supported=["client_secret_basic"],
        )
        with pytest.raises(oauth.OAuthError) as raised:
            oauth.choose_registration(metadata)
        assert raised.value.kind == "client_registration_required"

    async def test_a_preregistered_secret_makes_it_a_confidential_client(self) -> None:
        """The deployment's own client, which is not deleted on disconnect."""
        identity = oauth.choose_registration(
            self._metadata(),
            preregistered_client_id="our-client",
            preregistered_client_secret="our-secret",
        )
        assert identity.auth_method == "client_secret_basic"
        assert identity.delete_on_disconnect is False

    def test_a_registration_response_without_a_client_id_is_refused(self) -> None:
        """There is nothing to authorize as, so the flow cannot start."""
        with pytest.raises(oauth.OAuthError):
            oauth.parse_registration_response({"client_secret": "s"})

    def test_a_registration_request_names_a_public_client(self) -> None:
        """The platform cannot keep a secret the authorization server invents,
        so it asks for the one method it can complete unattended."""
        body = oauth.registration_request_body(
            client_name="Cloud Agent",
            redirect_uris=["https://agent.example.com/callback"],
            scopes=["read"],
        )
        assert body["token_endpoint_auth_method"] == "none"
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert body["redirect_uris"] == ["https://agent.example.com/callback"]


class TestTokenResponses:
    """What an answer means when a field the flow would like is absent."""

    def test_a_token_without_an_expiry_is_stored_as_having_none(self) -> None:
        """``None`` means "no expiry we were told about", not "never expires"."""
        tokens = oauth.parse_token_response({"access_token": "a"})
        assert tokens.expires_at is None
        assert tokens.needs_refresh() is False

    def test_a_token_with_no_access_token_is_refused(self) -> None:
        """There is nothing to store, so the flow did not succeed."""
        with pytest.raises(oauth.OAuthError):
            oauth.parse_token_response({"expires_in": 60})

    def test_the_scope_the_issuer_returned_replaces_the_one_we_asked_for(self) -> None:
        """An issuer may narrow a request, and the narrower set is what the
        token can actually do."""
        tokens = oauth.parse_token_response(
            {"access_token": "a", "scope": "read write", "expires_in": 60}
        )
        assert tokens.scopes == ("read", "write")

    def test_the_margin_is_what_decides_a_refresh_not_the_expiry(self) -> None:
        """A tool call can take minutes, so renewal is not left to the boundary."""
        now = datetime.now(timezone.utc)
        soon = oauth.TokenSet(access_token="a", expires_at=now + timedelta(seconds=30))
        assert soon.needs_refresh(now=now)
        comfortable = oauth.TokenSet(access_token="a", expires_at=now + timedelta(hours=1))
        assert not comfortable.needs_refresh(now=now)

    def test_a_rotating_issuer_that_sent_a_new_token_records_that_it_rotates(self) -> None:
        """Receiving one is the evidence; the flag has to follow it, or the next
        refresh keeps a token that is already dead."""
        merged = oauth.apply_refresh(
            {"refresh_token": "old", "issuer": _ISSUER, "resource": _RESOURCE},
            oauth.TokenSet(access_token="new", refresh_token="newer"),
            rotates=False,
        )
        assert merged["refresh_token"] == "newer"
        assert merged["refresh_rotates"] is True

    def test_a_refresh_keeps_the_issuer_and_resource_it_was_made_against(self) -> None:
        """Otherwise a later refresh would be sent to wherever discovery now
        points, rather than to the server that issued the token."""
        merged = oauth.apply_refresh(
            {"refresh_token": "old", "issuer": _ISSUER, "resource": _RESOURCE},
            oauth.TokenSet(access_token="new"),
            rotates=False,
        )
        assert merged["issuer"] == _ISSUER
        assert merged["resource"] == _RESOURCE


class TestScopesAndRevocation:
    """What is asked for, and what is given back."""

    def test_an_explicit_request_wins(self) -> None:
        """The owner's choice is not a default to be improved on."""
        server = oauth.parse_authorization_server_metadata(
            _server_metadata(scopes_supported=["a", "b"]), expected_issuer=_ISSUER
        )
        resource = oauth.parse_protected_resource_metadata(
            {**_resource_metadata(_ISSUER), "scopes_supported": ["c"]}, expect=_RESOURCE
        )
        assert oauth.default_scopes(resource, server, requested=["d"]) == ("d",)

    def test_the_resources_own_advertisement_beats_the_servers(self) -> None:
        """The resource knows what it needs; the authorization server knows what
        it can issue, and the narrower of the two is the one that applies."""
        server = oauth.parse_authorization_server_metadata(
            _server_metadata(scopes_supported=["a", "b"]), expected_issuer=_ISSUER
        )
        resource = oauth.parse_protected_resource_metadata(
            {**_resource_metadata(_ISSUER), "scopes_supported": ["c"]}, expect=_RESOURCE
        )
        assert oauth.default_scopes(resource, server) == ("c",)

    def test_asking_for_nothing_is_a_valid_outcome(self) -> None:
        """Several servers read an absent scope as their own default, and
        inventing one would over-ask."""
        server = oauth.parse_authorization_server_metadata(
            _server_metadata(), expected_issuer=_ISSUER
        )
        assert oauth.default_scopes(None, server) == ()

    def test_a_server_without_a_revocation_endpoint_gets_no_call(self) -> None:
        """Best effort by design: the token is dropped locally either way, and
        pretending otherwise would leave a user believing it was destroyed."""
        metadata = oauth.parse_authorization_server_metadata(
            _server_metadata(), expected_issuer=_ISSUER
        )
        identity = oauth.ClientIdentity(client_id="c", registration="dcr")
        assert oauth.revocation_request(metadata, identity, "token") is None

    def test_a_confidential_client_revokes_with_its_secret(self) -> None:
        """The endpoint may require it, and a revocation that is refused leaves
        the credential alive upstream."""
        metadata = oauth.parse_authorization_server_metadata(
            _server_metadata(revocation_endpoint=f"{_ISSUER}/revoke"),
            expected_issuer=_ISSUER,
        )
        identity = oauth.ClientIdentity(
            client_id="c", registration="preregistered", client_secret="s"
        )
        built = oauth.revocation_request(metadata, identity, "token")
        assert built is not None
        url, body = built
        assert url == f"{_ISSUER}/revoke"
        assert body["client_secret"] == "s"

    def test_the_callback_carries_the_provider_rather_than_a_connection_id(self) -> None:
        """The callback arrives before the platform knows which connection it
        belongs to; the state is what says that."""
        assert oauth.redirect_uri("https://agent.example.com", "github") == (
            "https://agent.example.com/v1/agent/mcp/oauth/callback/github"
        )

    def test_a_flow_past_its_lifetime_is_not_resumable(self) -> None:
        """An abandoned login is not a standing offer."""
        flow = oauth.OAuthFlow(
            versions=oauth.FlowVersions("7", 1, 1, 1),
            state="s",
            pkce=oauth.PkcePair.generate(),
            client=oauth.ClientIdentity(client_id="c", registration="dcr"),
            metadata=oauth.parse_authorization_server_metadata(
                _server_metadata(), expected_issuer=_ISSUER
            ),
            resource=_RESOURCE,
            redirect_uri="https://agent.example.com/callback",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert flow.expired

    def test_a_flow_records_the_versions_it_was_started_under(self) -> None:
        """A callback is checked item by item, so the names have to survive."""
        versions = oauth.FlowVersions("7", 2, 3, 4)
        assert versions.as_dict() == {
            "connection_id": "7",
            "config_revision": 2,
            "authorization_epoch": 3,
            "oauth_generation": 4,
        }
        assert versions.matches(
            {"id": "7", "config_revision": 2, "authorization_epoch": 3, "oauth_generation": 4}
        )
        # One moved version is enough to refuse it.
        assert not versions.matches(
            {"id": "7", "config_revision": 2, "authorization_epoch": 9, "oauth_generation": 4}
        )
