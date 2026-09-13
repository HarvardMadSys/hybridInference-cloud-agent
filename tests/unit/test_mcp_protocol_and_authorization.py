"""The protocol half: framing, SSE, discovery, authorization and invocations.

Everything here is exercised by calling it, because everything here was written
to be callable without a network or a database. The negative cases carry most
of the weight: a repeated pagination cursor, a late approval, a tool whose
description changed after it was approved, and a server that answers on a
stream the POST did not open.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from cloud_agent.mcp import authorization, host, invocations, jsonrpc, oauth, protocol, sse
from cloud_agent.mcp.models import (
    AuthKind,
    Capability,
    CapabilityKind,
    CapabilitySnapshot,
    ConnectionConfig,
    ConnectionStatus,
    ExecutionLocation,
    McpConnection,
    ThreadBinding,
    ToolPolicy,
    ToolRule,
    Transport,
)


def _connection(**overrides: object) -> McpConnection:
    """A usable remote connection, with fields overridable per test."""
    base: dict[str, object] = {
        "connection_id": "7",
        "user_id": "u1",
        "label": "Docs",
        "config": ConnectionConfig(
            transport=Transport.STREAMABLE_HTTP,
            location=ExecutionLocation.REMOTE,
            url="https://mcp.example.com/mcp",
        ),
        "status": ConnectionStatus.ACTIVE,
    }
    base.update(overrides)
    return McpConnection(**base)  # type: ignore[arg-type]


def _capability(name: str = "search", description: str = "Find things") -> Capability:
    """One tool capability."""
    return Capability(kind=CapabilityKind.TOOL, name=name, description=description)


class TestJsonRpc:
    """Correlation, batching, and the errors a peer can act on."""

    def test_correlates_a_string_answer_to_a_numeric_id(self) -> None:
        """``1`` and ``"1"`` are the same request as far as a table is concerned."""
        correlator = jsonrpc.Correlator()
        slot = correlator.open(jsonrpc.id_key(1))
        correlator.route(jsonrpc.decode('{"jsonrpc":"2.0","id":"1","result":{"ok":true}}')[0])
        assert slot.answered
        assert slot.messages[0].result() == {"ok": True}

    def test_a_notification_is_never_answered(self) -> None:
        """A notification has no id and gets no reply."""
        message = jsonrpc.decode('{"jsonrpc":"2.0","method":"notifications/progress"}')[0]
        assert message.is_notification and not message.is_response

    def test_a_server_request_is_not_mistaken_for_a_response(self) -> None:
        """``elicitation/create`` arrives with an id and expects one back."""
        message = jsonrpc.decode(
            '{"jsonrpc":"2.0","id":9,"method":"elicitation/create","params":{}}'
        )[0]
        assert message.is_request and not message.is_response

    def test_an_unmatched_reply_is_kept_rather_than_dropped(self) -> None:
        """A reply that arrives after its waiter gave up is still evidence."""
        correlator = jsonrpc.Correlator()
        correlator.route(jsonrpc.decode('{"jsonrpc":"2.0","id":4,"result":1}')[0])
        assert len(correlator.unmatched()) == 1

    def test_a_batch_is_routed_element_by_element(self) -> None:
        """Batching was removed in 2025-06-18 but older servers still use it."""
        correlator = jsonrpc.Correlator()
        first = correlator.open("1")
        second = correlator.open("2")
        correlator.route_all(
            jsonrpc.decode(
                '[{"jsonrpc":"2.0","id":1,"result":"a"},{"jsonrpc":"2.0","id":2,"result":"b"}]'
            )
        )
        assert first.messages[0].result() == "a"
        assert second.messages[0].result() == "b"

    def test_an_empty_payload_is_a_violation_not_an_empty_result(self) -> None:
        """Accepting it would turn a truncated stream into "nothing to say"."""
        with pytest.raises(jsonrpc.ProtocolViolation):
            jsonrpc.decode(b"")

    def test_an_unsupported_version_error_carries_what_the_server_accepts(self) -> None:
        """That list is the only thing that makes a retry a supported operation."""
        reply = jsonrpc.decode(
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32022,"message":"nope",'
            '"data":{"supported":["2025-11-25"],"requested":"1900-01-01"}}}'
        )[0]
        error = reply.error()
        assert isinstance(error, jsonrpc.UnsupportedProtocolVersion)
        assert error.supported == ["2025-11-25"]


class TestSse:
    """Framing rules a naive split gets wrong."""

    def test_crlf_is_a_terminator_not_data(self) -> None:
        """Servers send CRLF, and treating it as data corrupts every body."""
        decoder = sse.SseDecoder()
        events = decoder.feed(b'event: message\r\ndata: {"a":1}\r\n\r\n')
        assert len(events) == 1
        assert events[0].json() == {"a": 1}

    def test_a_frame_split_across_chunks_is_reassembled(self) -> None:
        """Chunk boundaries fall anywhere, including inside a JSON body."""
        decoder = sse.SseDecoder()
        assert decoder.feed(b'data: {"a"') == []
        events = decoder.feed(b":1}\n\n")
        assert events[0].json() == {"a": 1}

    def test_a_comment_is_neither_data_nor_an_error(self) -> None:
        """Keep-alive comments are how a stream survives an intermediary."""
        decoder = sse.SseDecoder()
        assert decoder.feed(b":\r\n\r\n") == []
        assert decoder.feed(b"data: 1\n\n")[0].json() == 1

    def test_a_cr_at_the_end_of_a_chunk_waits_for_its_lf(self) -> None:
        """Guessing here would emit an empty line and dispatch a half event."""
        decoder = sse.SseDecoder()
        assert decoder.feed(b"data: x\r") == []
        assert decoder.feed(b"\ndata: y\n\n")[0].data == "x\ny"

    def test_the_last_event_id_is_remembered_for_a_reconnect(self) -> None:
        """A resumption handle is only useful if it survives the event."""
        decoder = sse.SseDecoder()
        decoder.feed(b"id: 42\ndata: 1\n\n")
        assert decoder.last_event_id == "42"

    def test_an_over_long_frame_ends_the_stream(self) -> None:
        """An unbounded frame is how one upstream becomes a memory leak."""
        decoder = sse.SseDecoder()
        with pytest.raises(sse.SseError):
            decoder.feed(b"x" * (sse.MAX_EVENT_BYTES + 1))


class _FakeTransport:
    """A protocol transport backed by a table, for discovery tests."""

    def __init__(self, responses: dict[str, object]) -> None:
        """Record the canned replies and the calls that were made."""
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def request(self, method: str, params: dict | None = None) -> object:
        """Return the canned reply for a method, or raise."""
        self.calls.append((method, params))
        reply = self._responses.get(method)
        if callable(reply):
            return reply(params or {})
        if reply is None:
            raise jsonrpc.JsonRpcError(jsonrpc.METHOD_NOT_FOUND, f"no {method}")
        return reply

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Record a notification."""
        self.calls.append((method, params))


class TestProtocol:
    """Version negotiation, discovery and the stateless era's differences."""

    def test_prefers_this_platforms_newest_shared_revision(self) -> None:
        """A server's own ordering states its preference, not ours."""
        assert protocol.negotiate_version(["2024-11-05", "2025-11-25"]) == "2025-11-25"

    def test_refuses_a_server_that_shares_no_revision(self) -> None:
        """Continuing would fail later in a way that looks like our bug."""
        with pytest.raises(protocol.ProtocolError):
            protocol.negotiate_version(["1999-01-01"])

    def test_the_stateless_era_is_a_different_shape(self) -> None:
        """It has no handshake, so a client must not try to initialize it."""
        assert protocol.is_stateless("2026-07-28")
        assert not protocol.PROFILES["2026-07-28"].handshake
        assert not protocol.PROFILES["2026-07-28"].session_header
        assert not protocol.PROFILES["2026-07-28"].server_stream

    def test_a_stateless_request_declares_capabilities_every_time(self) -> None:
        """A server may not infer them from a prior request, so we re-send them."""
        meta = protocol.request_meta("2026-07-28")
        assert meta[protocol.META_PROTOCOL_VERSION] == "2026-07-28"
        assert protocol.META_CLIENT_CAPABILITIES in meta

    def test_a_paginated_catalogue_is_walked_to_its_end(self) -> None:
        """Two pages of tools become one snapshot."""

        async def run() -> CapabilitySnapshot:
            transport = _FakeTransport(
                {
                    protocol.TOOLS_LIST: lambda params: (
                        {"tools": [{"name": "a"}], "nextCursor": "c1"}
                        if not params.get("cursor")
                        else {"tools": [{"name": "b"}]}
                    ),
                }
            )
            handshake = protocol.Handshake(
                protocol_version="2025-11-25", capabilities={"tools": {}}
            )
            return await protocol.discover(
                transport,
                handshake,
                connection_id="7",
                config_revision=1,
                capability_revision=1,
            )

        snapshot = asyncio.run(run())
        assert [tool.name for tool in snapshot.tools] == ["a", "b"]

    def test_a_repeated_cursor_does_not_loop_forever(self) -> None:
        """A server answering the same cursor would otherwise never return."""

        async def run() -> None:
            transport = _FakeTransport(
                {protocol.TOOLS_LIST: {"tools": [{"name": "a"}], "nextCursor": "same"}}
            )
            handshake = protocol.Handshake(
                protocol_version="2025-11-25", capabilities={"tools": {}}
            )
            await protocol.discover(
                transport,
                handshake,
                connection_id="7",
                config_revision=1,
                capability_revision=1,
            )

        with pytest.raises(protocol.ProtocolError):
            asyncio.run(run())

    def test_only_declared_surfaces_are_read(self) -> None:
        """Asking for resources a server never offered is a protocol error."""

        async def run() -> list[str]:
            transport = _FakeTransport({protocol.TOOLS_LIST: {"tools": []}})
            handshake = protocol.Handshake(
                protocol_version="2025-11-25", capabilities={"tools": {}}
            )
            await protocol.discover(
                transport,
                handshake,
                connection_id="7",
                config_revision=1,
                capability_revision=1,
            )
            return [method for method, _ in transport.calls]

        assert asyncio.run(run()) == [protocol.TOOLS_LIST]

    def test_a_presented_name_is_namespaced_and_safe(self) -> None:
        """A CLI's tool-name grammar is narrower than MCP's."""
        assert protocol.presented_name("docs", "git.hub/search") == "docs__git_hub_search"

    def test_two_tools_that_sanitize_alike_get_distinct_names(self) -> None:
        """A collision is resolved rather than one tool being dropped."""
        first = protocol.presented_name("s", "a.b", taken=[])
        second = protocol.presented_name("s", "a/b", taken=[first])
        assert first != second

    def test_an_input_required_result_is_recognized(self) -> None:
        """A stateless server asks by returning, not by calling back."""
        result = {"resultType": "input_required", "inputRequests": {"q": {"method": "x"}}}
        assert protocol.input_required(result)
        assert protocol.input_requests(result) == {"q": {"method": "x"}}

    def test_continuation_echoes_the_state_and_never_invents_one(self) -> None:
        """Adding a state the server did not send is how a client talks it round."""
        params = protocol.continuation_params(input_responses={"q": {}}, request_state=None)
        assert "requestState" not in params

    def test_a_result_without_a_discriminator_is_complete(self) -> None:
        """The specification's own backward-compatibility rule."""
        assert protocol.result_type({"content": []}) == "complete"


class TestOAuth:
    """Discovery, registration precedence and refresh contracts."""

    def test_protected_resource_metadata_tries_the_path_form_first(self) -> None:
        """RFC 9728 puts the resource's path after the well-known segment."""
        urls = oauth.protected_resource_metadata_urls("https://mcp.example.com/public/mcp")
        assert urls[0] == "https://mcp.example.com/.well-known/oauth-protected-resource/public/mcp"
        assert urls[-1] == "https://mcp.example.com/.well-known/oauth-protected-resource"

    def test_an_issuer_with_a_path_keeps_it(self) -> None:
        """Dropping it fetches the tenant-less document and the wrong issuer."""
        urls = oauth.authorization_server_metadata_urls("https://auth.example.com/tenant1")
        assert "https://auth.example.com/.well-known/oauth-authorization-server/tenant1" in urls

    def test_a_metadata_document_that_names_another_issuer_is_refused(self) -> None:
        """That check is what stops the whole flow being redirected."""
        with pytest.raises(oauth.OAuthError):
            oauth.parse_authorization_server_metadata(
                {
                    "issuer": "https://evil.example.net",
                    "authorization_endpoint": "https://evil.example.net/a",
                    "token_endpoint": "https://evil.example.net/t",
                },
                expected_issuer="https://auth.example.com",
            )

    def test_pkce_is_required_and_s256_is_the_only_method_used(self) -> None:
        """An authorization server that will not say it supports S256 is refused."""
        metadata = oauth.parse_authorization_server_metadata(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/a",
                "token_endpoint": "https://auth.example.com/t",
            },
            expected_issuer="https://auth.example.com",
        )
        assert not metadata.supports_pkce_s256()

    def test_a_preregistered_client_wins_over_automatic_registration(self) -> None:
        """It is the only mechanism that needs no trust decision at connect time."""
        metadata = oauth.parse_authorization_server_metadata(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/a",
                "token_endpoint": "https://auth.example.com/t",
                "registration_endpoint": "https://auth.example.com/register",
                "client_id_metadata_document_supported": True,
            },
            expected_issuer="https://auth.example.com",
        )
        identity = oauth.choose_registration(
            metadata,
            preregistered_client_id="our-client",
            cimd_client_id="https://agent.example/client.json",
        )
        assert identity.registration == "preregistered"
        assert identity.delete_on_disconnect is False

    def test_a_server_that_cannot_register_asks_the_user_for_a_client_id(self) -> None:
        """Not broken -- it expects a pasted client id, and the error says so."""
        metadata = oauth.parse_authorization_server_metadata(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/a",
                "token_endpoint": "https://auth.example.com/t",
            },
            expected_issuer="https://auth.example.com",
        )
        with pytest.raises(oauth.OAuthError) as raised:
            oauth.choose_registration(metadata)
        assert raised.value.kind == "client_registration_required"

    def test_the_resource_rides_on_both_the_authorize_and_token_requests(self) -> None:
        """A token minted for one resource must not be replayable at another."""
        flow = oauth.OAuthFlow(
            versions=oauth.FlowVersions("7", 1, 1, 1),
            state="state",
            pkce=oauth.PkcePair.generate(),
            client=oauth.ClientIdentity(client_id="c", registration="dcr"),
            metadata=oauth.AuthorizationServerMetadata(
                issuer="https://auth.example.com",
                authorization_endpoint="https://auth.example.com/authorize",
                token_endpoint="https://auth.example.com/token",
            ),
            resource="https://mcp.example.com/mcp",
            redirect_uri="https://agent.example/callback",
        )
        assert "resource=https%3A%2F%2Fmcp.example.com%2Fmcp" in flow.authorization_url()
        assert flow.token_request("code").data["resource"] == "https://mcp.example.com/mcp"

    def test_a_rotated_refresh_token_that_did_not_arrive_is_an_error(self) -> None:
        """Keeping the old one would look fine until the next refresh."""
        with pytest.raises(oauth.OAuthError):
            oauth.apply_refresh(
                {"refresh_token": "old", "issuer": "i", "resource": "r"},
                oauth.TokenSet(access_token="new"),
                rotates=True,
            )

    def test_a_non_rotating_issuer_keeps_the_refresh_token(self) -> None:
        """Omitting one there means "keep using the old one"."""
        merged = oauth.apply_refresh(
            {"refresh_token": "old", "issuer": "i", "resource": "r"},
            oauth.TokenSet(access_token="new"),
            rotates=False,
        )
        assert merged["refresh_token"] == "old"

    def test_the_challenge_header_pointing_at_metadata_is_read(self) -> None:
        """It is authoritative when the well-known location is not where it lives."""
        header = 'Bearer resource_metadata="https://mcp.example.com/meta", error="invalid_token"'
        assert oauth.resource_metadata_from_challenge(header) == "https://mcp.example.com/meta"

    def test_a_state_value_is_not_stored_in_the_clear(self) -> None:
        """A database read must not hand out a replayable state."""
        state = oauth.new_state()
        assert oauth.state_hash(state) != state


class TestAuthorization:
    """Who may call what, and when the answer is "ask the owner"."""

    def _binding(self, connection: McpConnection, **overrides: object) -> ThreadBinding:
        """A binding whose rules are stamped against one capability."""
        capability = _capability()
        rules = overrides.pop(
            "rules",
            {
                "search": ToolRule(
                    name="search", policy=ToolPolicy.ALLOW, fingerprint=capability.fingerprint()
                )
            },
        )
        base: dict[str, object] = {
            "thread_id": "t1",
            "connection_id": connection.connection_id,
            "alias": "docs",
            "config_revision": connection.config_revision,
            "authorization_epoch": connection.authorization_epoch,
            "capability_revision": connection.capability_revision,
            "rules": rules,
        }
        base.update(overrides)
        return ThreadBinding(**base)  # type: ignore[arg-type]

    def test_an_allowed_capability_is_dispatched(self) -> None:
        """The ordinary case."""
        connection = _connection()
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection),
            capability=_capability(),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.allowed

    def test_a_tool_the_owner_never_enabled_is_denied(self) -> None:
        """Including one the server invented after they last looked."""
        connection = _connection()
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection),
            capability=_capability("delete_everything"),
            kind=CapabilityKind.TOOL,
            name="delete_everything",
            user_id="u1",
        )
        assert decision.code == "capability_not_enabled"

    def test_a_changed_description_sends_the_call_back_to_the_owner(self) -> None:
        """Approving a tool is approving what it says it does."""
        connection = _connection()
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection),
            capability=_capability("search", "Now also deletes things"),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.code == "capability_changed"
        assert decision.needs_confirmation

    def test_a_reconfigured_connection_invalidates_its_bindings(self) -> None:
        """The owner approved a different target, not this one."""
        connection = _connection(config_revision=2, authorization_epoch=2)
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(_connection()),
            capability=_capability(),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.code == "binding_stale"
        assert decision.needs_confirmation

    def test_a_disabled_connection_is_refused_whatever_the_binding_says(self) -> None:
        """Turning a connection off takes effect immediately."""
        connection = _connection(status=ConnectionStatus.DISABLED)
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection),
            capability=_capability(),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.code == "connection_disabled"

    def test_another_users_connection_is_refused(self) -> None:
        """Unreachable through the API, kept because the relay also calls this."""
        connection = _connection(user_id="u2")
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection),
            capability=_capability(),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.code == "wrong_owner"

    def test_a_migrated_binding_asks_once_instead_of_refusing(self) -> None:
        """A session that predates this model keeps working without inheriting."""
        connection = _connection()
        decision = authorization.decide(
            connection=connection,
            binding=self._binding(connection, rules={}, selection_pending=True),
            capability=_capability(),
            kind=CapabilityKind.TOOL,
            name="search",
            user_id="u1",
        )
        assert decision.needs_confirmation

    def test_a_disallowed_tool_is_not_advertised(self) -> None:
        """A model cannot be talked into calling a tool it was never offered."""
        connection = _connection()
        snapshot = (Capability(kind=CapabilityKind.TOOL, name="search"),)
        binding = self._binding(connection)
        visible = authorization.visible_capabilities(binding, snapshot, CapabilityKind.TOOL)
        assert [capability.name for capability in visible] == ["search"]
        assert authorization.visible_capabilities(None, snapshot, CapabilityKind.TOOL) == []


class TestInvocations:
    """The state machine behind a held call, and its timeout contract."""

    def test_a_late_approval_cannot_revive_an_expired_call(self) -> None:
        """What the owner was shown is gone, so approving it means nothing."""
        assert (
            invocations.transition(invocations.InvocationStatus.PENDING, "timeout")
            is invocations.InvocationStatus.EXPIRED
        )
        with pytest.raises(invocations.TransitionRefused):
            invocations.transition(invocations.InvocationStatus.EXPIRED, "approve")

    def test_a_lost_response_becomes_outcome_unknown_not_a_retry(self) -> None:
        """An external side effect may already have landed."""
        assert (
            invocations.transition(invocations.InvocationStatus.DISPATCHING, "dispatch_lost")
            is invocations.InvocationStatus.OUTCOME_UNKNOWN
        )

    def test_the_earliest_deadline_wins(self) -> None:
        """Four layers wait; the platform waits for the shortest of them."""
        created = datetime.now(timezone.utc)
        deadline = invocations.deadline_for(
            created_at=created,
            cli_timeout=timedelta(seconds=30),
            lease_expires_at=created + timedelta(hours=1),
        )
        assert deadline.source == "cli_timeout"

    def test_a_lease_can_be_the_binding_constraint(self) -> None:
        """A job whose lease ends sooner is not held open for its owner."""
        created = datetime.now(timezone.utc)
        deadline = invocations.deadline_for(
            created_at=created, lease_expires_at=created + timedelta(seconds=5)
        )
        assert deadline.source == "lease"

    def test_a_moved_version_refuses_a_dispatch(self) -> None:
        """A narrowing while the call waited must win."""
        invocation = invocations.Invocation(
            invocation_id="1",
            job_id="j",
            attempt_id="a",
            lease_generation=3,
            thread_id="t",
            connection_id="7",
            capability_name="search",
            arguments_digest="d",
            config_revision=1,
            authorization_epoch=1,
            capability_revision=1,
            policy_revision=2,
        )
        fence = invocations.DispatchFence(1, 1, 1, 3, 3, 0)
        assert not invocations.fence_matches(invocation, fence)
        assert invocations.fence_matches(invocation, invocations.DispatchFence(1, 1, 1, 2, 3, 0))

    def test_no_call_is_ever_retried_automatically(self) -> None:
        """A tool annotation claiming idempotency is the server's own claim."""
        invocation = invocations.Invocation(
            invocation_id="1",
            job_id="j",
            attempt_id="a",
            lease_generation=1,
            thread_id="t",
            connection_id="7",
            capability_name="create_issue",
            arguments_digest="d",
        )
        assert not invocations.is_retry_safe(invocation)


class TestHostRendering:
    """What a runtime is told a tool returned."""

    def _host(self) -> host.McpHost:
        """A host with the default bounds."""
        return host.McpHost()

    def test_a_text_result_is_carried_through(self) -> None:
        """The ordinary case."""
        result = self._host().render_result({"content": [{"type": "text", "text": "hi"}]})
        assert result.content[0].text == "hi"
        assert not result.is_error

    def test_an_unknown_block_type_is_named_rather_than_dropped(self) -> None:
        """Reporting an empty result would say the call succeeded with nothing."""
        result = self._host().render_result(
            {"content": [{"type": "quantum", "value": 1}, {"type": "text", "text": "ok"}]}
        )
        assert result.unsupported == ("quantum",)
        assert len(result.content) == 1

    def test_oversized_text_says_it_was_truncated(self) -> None:
        """A model told the truth about a short result behaves better than one lied to."""
        big = "x" * (host.MAX_CONTENT_BYTES + 10)
        result = self._host().render_result({"content": [{"type": "text", "text": big}]})
        assert result.content[0].truncated

    def test_an_error_result_is_not_a_transport_failure(self) -> None:
        """A failed tool call is a thing models handle."""
        result = self._host().render_result(
            {"content": [{"type": "text", "text": "nope"}], "isError": True}
        )
        assert result.is_error

    def test_arguments_have_a_stable_digest(self) -> None:
        """The digest binds an approval to what the owner was shown."""
        left = host.arguments_digest({"b": 1, "a": 2})
        right = host.arguments_digest({"a": 2, "b": 1})
        assert left == right

    def test_arguments_round_trip_through_protected_storage(self) -> None:
        """The body is kept, not just its hash, so an approved call can be sent."""
        arguments = {"query": "is:open label:bug"}
        assert host.decode_arguments(host.encode_arguments(arguments)) == arguments

    @pytest.mark.asyncio
    async def test_a_multi_round_trip_call_is_continued(self) -> None:
        """A stateless server asks by returning; the host answers and retries."""

        class _Transport:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def request(self, method: str, params: dict | None = None) -> object:
                self.calls.append(dict(params or {}))
                if len(self.calls) == 1:
                    return {
                        "resultType": "input_required",
                        "inputRequests": {"q": {"method": protocol.ROOTS_LIST}},
                        "requestState": "st",
                    }
                return {"resultType": "complete", "content": [{"type": "text", "text": "done"}]}

            async def notify(self, method: str, params: dict | None = None) -> None:
                return None

        transport = _Transport()
        session = host.Session(
            key=host.SessionKey(connection_id="7", member="j:1"),
            transport=transport,
            handshake=protocol.Handshake(protocol_version="2026-07-28"),
        )
        result = await self._host().call_tool(session, name="search", arguments={"q": "x"})
        assert result.content[0].text == "done"
        assert transport.calls[1]["requestState"] == "st"
        assert transport.calls[1]["inputResponses"] == {"q": {"roots": []}}

    @pytest.mark.asyncio
    async def test_a_server_asking_for_what_we_cannot_supply_is_reported(self) -> None:
        """Sampling needs model authorization this host does not hold."""

        class _Transport:
            async def request(self, method: str, params: dict | None = None) -> object:
                return {
                    "resultType": "input_required",
                    "inputRequests": {"q": {"method": protocol.SAMPLING_CREATE_MESSAGE}},
                }

            async def notify(self, method: str, params: dict | None = None) -> None:
                return None

        session = host.Session(
            key=host.SessionKey(connection_id="7", member="j:1"),
            transport=_Transport(),
            handshake=protocol.Handshake(protocol_version="2026-07-28"),
        )
        with pytest.raises(host.HostError):
            await self._host().call_tool(session, name="search")

    @pytest.mark.asyncio
    async def test_stdio_without_a_worker_says_which_component_is_missing(self) -> None:
        """Pretending would be worse than refusing."""
        connection = McpConnection(
            connection_id="7",
            user_id="u1",
            label="Local files",
            config=ConnectionConfig(
                transport=Transport.STDIO,
                location=ExecutionLocation.MANAGED_WORKER,
                launch=__import__("cloud_agent.mcp.models", fromlist=["LaunchSpec"]).LaunchSpec(
                    command="npx", args=("s@1.0.0",)
                ),
            ),
        )
        with pytest.raises(host.ConnectorRequired):
            await self._host().open(
                connection, secret=None, key=host.SessionKey(connection_id="7", member="j:1")
            )

    def test_a_session_handle_changes_with_the_owner_lease(self) -> None:
        """Reusing a session across a takeover is what fencing exists to stop."""
        first = host.SessionKey(connection_id="7", member="j:1", owner_lease_generation=1)
        second = host.SessionKey(connection_id="7", member="j:1", owner_lease_generation=2)
        assert first.handle() != second.handle()

    def test_the_eagerly_built_plan_of_a_static_connection_has_no_secret(self) -> None:
        """Whatever the host holds, the public rendering is empty of credentials."""
        from cloud_agent.mcp.models import McpServer

        server = McpServer(
            name="docs",
            url="https://mcp.example.com/mcp",
            connection_id="7",
            transport=Transport.STREAMABLE_HTTP,
            headers={"Authorization": "Bearer secret"},
        )
        assert "secret" not in json.dumps(server.public())


class TestAuthProfile:
    """The auth model's own invariants."""

    def test_oauth_settings_on_a_non_oauth_profile_are_refused(self) -> None:
        """Two sources of truth for how to authenticate is one too many."""
        from cloud_agent.mcp.models import AuthProfile, OAuthProfile

        with pytest.raises(ValueError):
            AuthProfile(kind=AuthKind.STATIC, oauth=OAuthProfile(issuer="https://a.example"))

    def test_an_oauth_profile_must_carry_its_settings(self) -> None:
        """A flow cannot be refreshed from nothing."""
        from cloud_agent.mcp.models import AuthProfile

        with pytest.raises(ValueError):
            AuthProfile(kind=AuthKind.OAUTH)

    def test_a_stdio_connection_needs_a_launch_spec(self) -> None:
        """And a remote one must not pretend to have one."""
        with pytest.raises(ValueError):
            ConnectionConfig(transport=Transport.STDIO, location=ExecutionLocation.USER_CONNECTOR)

    def test_a_remote_connection_may_not_use_the_stdio_transport(self) -> None:
        """The two contradict each other."""
        with pytest.raises(ValueError):
            ConnectionConfig(
                transport=Transport.STDIO,
                location=ExecutionLocation.REMOTE,
                url="https://mcp.example.com/mcp",
            )


class TestSessionRecords:
    """Opening a session records it; superseding one closes what it replaces."""

    def _connection(self, **overrides: object) -> host.McpConnection:
        """A remote connection the host will never actually dial."""
        base: dict[str, object] = {
            "connection_id": "7",
            "user_id": "u1",
            "label": "Docs",
            "config": ConnectionConfig(
                transport=Transport.STREAMABLE_HTTP,
                location=ExecutionLocation.REMOTE,
                url="https://mcp.example.com/mcp",
            ),
        }
        base.update(overrides)
        return host.McpConnection(**base)  # type: ignore[arg-type]

    class _Store:
        """A store that records what the host asked it to remember."""

        def __init__(self) -> None:
            """Start with nothing recorded."""
            self.opened: list[dict[str, object]] = []
            self.closed: list[str] = []
            self.reaped: list[dict[str, object]] = []

        async def open_mcp_transport_session(self, **kwargs: object) -> None:
            self.opened.append(kwargs)

        async def close_mcp_transport_session(self, *, handle: str) -> bool:
            self.closed.append(handle)
            return True

        async def reap_mcp_transport_sessions(self, **kwargs: object) -> list[object]:
            self.reaped.append(kwargs)
            return []

    def _host(self, store: object) -> host.McpHost:
        """A host whose only session-opening step is replaced."""
        built = host.McpHost(store=store)
        return built

    @pytest.mark.asyncio
    async def test_opening_a_session_records_the_versions_it_was_opened_under(self) -> None:
        """The record is what lets another instance find a session it cannot hold."""
        store = self._Store()
        built = self._host(store)

        async def connect(connection: object, *, secret: object) -> tuple:
            # A handshake, because the record names the revision it agreed.
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        key = host.SessionKey(connection_id="7", member="ajob:5", owner_lease_generation=2)
        await built.open(self._connection(), secret=None, key=key)

        assert len(store.opened) == 1
        recorded = store.opened[0]
        assert recorded["handle"] == key.handle()
        assert recorded["connection_id"] == 7
        assert recorded["member"] == "ajob:5"
        assert recorded["job_id"] == "ajob"
        assert recorded["attempt_id"] == 5
        assert recorded["owner_lease_generation"] == 2
        assert store.reaped, "a new session reaps whatever the member left behind"

    @pytest.mark.asyncio
    async def test_superseding_a_session_closes_it_and_forgets_it(self) -> None:
        """Otherwise a superseded owner is one lookup away from its own session."""
        store = self._Store()
        built = self._host(store)

        async def connect(connection: object, *, secret: object) -> tuple:
            # A handshake, because the record names the revision it agreed.
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        first = host.SessionKey(connection_id="7", member="ajob:5", owner_lease_generation=2)
        second = host.SessionKey(connection_id="7", member="ajob:5", owner_lease_generation=3)
        await built.open(self._connection(), secret=None, key=first)
        await built.open(self._connection(), secret=None, key=second)

        assert built.session(first) is None
        assert first.handle() in store.closed
        assert built.session(second) is not None

    @pytest.mark.asyncio
    async def test_a_record_that_cannot_be_written_does_not_fail_the_call(self) -> None:
        """The socket is process-local; losing its row costs a diagnosis, not
        the call."""

        class _Broken(self._Store):
            async def open_mcp_transport_session(self, **kwargs: object) -> None:
                raise RuntimeError("the database is gone")

        built = self._host(_Broken())

        async def connect(connection: object, *, secret: object) -> tuple:
            # A handshake, because the record names the revision it agreed.
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        key = host.SessionKey(connection_id="7", member="ajob:5")
        session = await built.open(self._connection(), secret=None, key=key)
        assert session is not None

    @pytest.mark.asyncio
    async def test_a_rotated_credential_rebuilds_the_session(self) -> None:
        """The key cannot carry the token, so a cache keyed on it must compare.

        A refreshed access token arrives under the same connection, member and
        owner, so the handle is unchanged -- and answering from the cache would
        keep sending the token the refresh just replaced. The same goes for a
        timeout widened on the connection row.
        """
        store = self._Store()
        built = self._host(store)
        opened: list[tuple[object, object]] = []

        async def connect(connection: object, *, secret: object) -> tuple:
            opened.append((connection, secret))
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        key = host.SessionKey(connection_id="7", member="ajob:5")
        first = await built.open(self._connection(), secret="token-v1", key=key)
        assert await built.open(self._connection(), secret="token-v1", key=key) is first
        assert len(opened) == 1, "an unchanged session should be reused"

        second = await built.open(self._connection(), secret="token-v2", key=key)
        assert second is not first
        assert [secret for _, secret in opened] == ["token-v1", "token-v2"]

        # And the timeout, which lives on the connection rather than the token.
        third = await built.open(
            self._connection(request_timeout_s=45.0), secret="token-v2", key=key
        )
        assert third is not second
        assert len(opened) == 3

    @pytest.mark.asyncio
    async def test_a_repointed_connection_rebuilds_the_session(self) -> None:
        """The target is part of what a cached session was opened for.

        Repointing a connection -- a new URL, transport or auth profile -- moves
        ``config_revision`` and deliberately leaves ``credential_version`` where
        it is, because no credential changed. A cache that compares only the
        credential therefore keeps driving a transport that is connected to the
        address the owner just replaced, under an authorization the owner has
        since re-made; both of those are revisions the key cannot carry.
        """
        store = self._Store()
        built = self._host(store)
        opened: list[object] = []

        async def connect(connection: object, *, secret: object) -> tuple:
            opened.append(connection)
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        key = host.SessionKey(connection_id="7", member="ajob:5")
        original = self._connection()
        first = await built.open(original, secret="token", key=key)
        assert await built.open(original, secret="token", key=key) is first

        repointed = self._connection(
            config=ConnectionConfig(
                transport=Transport.STREAMABLE_HTTP,
                location=ExecutionLocation.REMOTE,
                url="https://new.example.com/mcp",
            ),
            config_revision=2,
        )
        second = await built.open(repointed, secret="token", key=key)
        assert second is not first
        assert opened[-1] is repointed

        # A re-made authorization is the other half, and moves its own revision.
        reauthorized = self._connection(config_revision=2, authorization_epoch=2)
        third = await built.open(reauthorized, secret="token", key=key)
        assert third is not second
        # Reused while both revisions still match, so this is a comparison and
        # not an unconditional rebuild.
        assert await built.open(reauthorized, secret="token", key=key) is third
        assert len(opened) == 3

    @pytest.mark.asyncio
    async def test_closing_a_session_removes_its_record(self) -> None:
        """A record nobody can serve is not a session."""
        store = self._Store()
        built = self._host(store)

        async def connect(connection: object, *, secret: object) -> tuple:
            # A handshake, because the record names the revision it agreed.
            return object(), protocol.Handshake(protocol_version="2025-11-25"), None

        built._connect = connect  # type: ignore[method-assign]
        key = host.SessionKey(connection_id="7", member="ajob:5")
        await built.open(self._connection(), secret=None, key=key)
        assert await built.close(key)
        assert key.handle() in store.closed
        assert built.session(key) is None
