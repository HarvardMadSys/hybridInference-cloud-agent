"""The branches of a decision, one case each.

`test_mcp_protocol_and_authorization.py` covers the shapes a person meets in
normal use -- an allowed tool, a denied one, a changed one. This file covers the
*refusals*, because those are the ones nobody exercises by hand and the ones
whose exact reason is the whole message the owner reads:

    "you turned this off" and "this needs re-approving" and "the server no longer
    offers that" are three different screens, and a decision that collapsed them
    into one boolean would leave an owner with a tool that does not work and no
    way to find out why.

The same goes for the timeout contract: four layers wait on a held call, and the
platform has to pick the earliest of whichever ones are present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cloud_agent.mcp import authorization, invocations
from cloud_agent.mcp.models import (
    Capability,
    CapabilityKind,
    ConnectionConfig,
    ConnectionErrorKind,
    ConnectionStatus,
    ExecutionLocation,
    McpConnection,
    ThreadBinding,
    ToolPolicy,
    ToolRule,
    Transport,
)

_OWNER = "u1"


def _connection(**overrides: object) -> McpConnection:
    """A usable remote connection, with fields overridable per case."""
    base: dict[str, object] = {
        "connection_id": "7",
        "user_id": _OWNER,
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


def _binding(connection: McpConnection, **overrides: object) -> ThreadBinding:
    """A binding fenced to ``connection`` as it stands."""
    base: dict[str, object] = {
        "thread_id": "t1",
        "connection_id": connection.connection_id,
        "alias": "docs",
        "config_revision": connection.config_revision,
        "authorization_epoch": connection.authorization_epoch,
        "capability_revision": connection.capability_revision,
    }
    base.update(overrides)
    return ThreadBinding(**base)  # type: ignore[arg-type]


def _decide(connection: McpConnection | None, binding: ThreadBinding | None, **overrides: object):
    """Decide one call, with the boring arguments filled in."""
    settings: dict[str, object] = {
        "capability": None,
        "kind": CapabilityKind.TOOL,
        "name": "search",
        "user_id": _OWNER,
    }
    settings.update(overrides)
    return authorization.decide(connection=connection, binding=binding, **settings)  # type: ignore[arg-type]


class TestRefusals:
    """Every way a call is refused, and what each one tells the owner."""

    async def test_a_binding_with_no_connection_says_so(self) -> None:
        """The row is gone, which is not the owner's mistake and not a secret."""
        decision = _decide(None, _binding(_connection()), capability=_capability())
        assert decision.code == "connection_missing"
        assert "no longer exists" in decision.message

    async def test_a_failed_connection_repeats_its_own_diagnosis(self) -> None:
        """The reason the connection recorded is the reason the caller gets.

        A generic "not working" would send the owner to a page that already says
        which thing is wrong.
        """
        connection = _connection(
            status=ConnectionStatus.ERROR,
            error_kind=ConnectionErrorKind.NEEDS_AUTHORIZATION,
            error_detail="This connection needs to be authorized.",
        )
        decision = _decide(connection, _binding(connection), capability=_capability())
        assert decision.code == "connection_failed"
        assert decision.message == "This connection needs to be authorized."

    async def test_a_failed_connection_with_no_detail_still_says_something(self) -> None:
        """A row can be errored before anyone wrote a sentence on it."""
        connection = _connection(status=ConnectionStatus.ERROR)
        decision = _decide(connection, _binding(connection), capability=_capability())
        assert decision.code == "connection_failed"
        assert decision.message

    async def test_a_binding_that_is_switched_off_is_not_bound(self) -> None:
        """The session turned the connection off rather than the owner."""
        connection = _connection()
        decision = _decide(
            connection, _binding(connection, enabled=False), capability=_capability()
        )
        assert decision.code == "not_bound"
        assert "not enabled for this session" in decision.message

    async def test_a_name_the_server_no_longer_offers_is_refused_not_held(self) -> None:
        """There is nothing to show the owner, so there is nothing to approve.

        A call to a tool discovery never saw is either a stale client or an
        attempt to reach something the catalogue filtered out; prompting for it
        would be asking somebody to approve a thing the platform cannot describe.
        """
        connection = _connection()
        decision = _decide(
            connection,
            _binding(
                connection,
                rules={"search": ToolRule(name="search", policy=ToolPolicy.ALLOW)},
            ),
            capability=None,
        )
        assert decision.code == "capability_unknown"
        assert "no longer offers" in decision.message

    async def test_a_rule_that_says_deny_reads_differently_from_a_missing_rule(self) -> None:
        """Turned off by the owner, versus never enabled: one screen has a toggle."""
        connection = _connection()
        denied = _decide(
            connection,
            _binding(
                connection,
                rules={"search": ToolRule(name="search", policy=ToolPolicy.DENY)},
            ),
            capability=_capability(),
        )
        assert denied.code == "capability_not_enabled"
        assert "turned off" in denied.message
        assert denied.refused

    async def test_a_rule_that_says_confirm_is_held(self) -> None:
        """The third policy, and the only one that stops for a person."""
        connection = _connection()
        decision = _decide(
            connection,
            _binding(
                connection,
                rules={"search": ToolRule(name="search", policy=ToolPolicy.CONFIRM)},
            ),
            capability=_capability(),
        )
        assert decision.code == "needs_confirmation"
        assert decision.needs_confirmation
        assert not decision.allowed and not decision.refused


class TestWhatIsVisible:
    """A capability is only offered when the session may actually call it."""

    async def test_a_denied_capability_is_not_offered(self) -> None:
        """A model cannot be talked into calling what it was never shown."""
        connection = _connection()
        snapshot = (_capability("search"), _capability("delete"))
        binding = _binding(
            connection,
            rules={
                "search": ToolRule(name="search", policy=ToolPolicy.ALLOW),
                "delete": ToolRule(name="delete", policy=ToolPolicy.DENY),
            },
        )
        visible = authorization.visible_capabilities(binding, snapshot, CapabilityKind.TOOL)
        assert [capability.name for capability in visible] == ["search"]

    async def test_a_migrated_session_is_shown_everything_and_approved_for_nothing(self) -> None:
        """The prompt is the point: the owner is asked, not silently granted.

        A binding migrated from the old provider-name list has no rules and must
        not read as "nothing is enabled" either -- that would hide the tools from
        the person who has to choose among them.
        """
        connection = _connection()
        snapshot = (_capability("search"), _capability("delete"))
        binding = _binding(connection, rules={}, selection_pending=True)
        visible = authorization.visible_capabilities(binding, snapshot, CapabilityKind.TOOL)
        assert [capability.name for capability in visible] == ["search", "delete"]
        decision = _decide(connection, binding, capability=_capability("delete"))
        assert decision.needs_confirmation

    async def test_a_binding_with_an_empty_selection_offers_nothing(self) -> None:
        """``selection_pending`` false and no rules means the owner chose nothing."""
        connection = _connection()
        snapshot = (_capability("search"),)
        visible = authorization.visible_capabilities(
            _binding(connection, rules={}), snapshot, CapabilityKind.TOOL
        )
        assert visible == []

    async def test_a_capability_approved_against_another_schema_stays_visible(self) -> None:
        """It is not silent access -- the call is still held -- and hiding it
        would make the change invisible to the person who has to approve it."""
        connection = _connection()
        changed = _capability("search", "Now also deletes things")
        binding = _binding(
            connection,
            rules={
                "search": ToolRule(
                    name="search",
                    policy=ToolPolicy.ALLOW,
                    fingerprint=_capability("search").fingerprint(),
                )
            },
        )
        visible = authorization.visible_capabilities(binding, (changed,), CapabilityKind.TOOL)
        assert [capability.name for capability in visible] == ["search"]


class TestSelecting:
    """Turning a session's choices into a binding."""

    async def test_fingerprints_are_stamped_from_the_snapshot_they_were_chosen_from(self) -> None:
        """So a later schema change is detected rather than diffed by nobody."""
        connection = _connection()
        capability = _capability("search")
        binding = authorization.binding_from_selection(
            thread_id="t1",
            connection=connection,
            alias="docs",
            rules={"search": ToolPolicy.ALLOW},
            capabilities=(capability,),
        )
        assert binding.rules["search"].fingerprint == capability.fingerprint()
        assert binding.enabled

    async def test_a_name_with_no_advertised_schema_is_still_stamped_with_nothing(self) -> None:
        """An empty fingerprint means "not compared", which is the honest state
        for a rule whose capability discovery never returned."""
        connection = _connection()
        binding = authorization.binding_from_selection(
            thread_id="t1",
            connection=connection,
            alias="docs",
            rules={"ghost": ToolPolicy.ALLOW},
            capabilities=(),
        )
        assert binding.rules["ghost"].fingerprint == ""

    async def test_choosing_nothing_produces_a_disabled_binding(self) -> None:
        """ "I chose nothing" and "I chose everything, which is nothing" differ."""
        binding = authorization.binding_from_selection(
            thread_id="t1",
            connection=_connection(),
            alias="docs",
            rules={},
        )
        assert binding.enabled is False

    async def test_resources_and_prompts_alone_still_enable_the_binding(self) -> None:
        """A connection may be enabled for a resource and no tool at all."""
        binding = authorization.binding_from_selection(
            thread_id="t1",
            connection=_connection(),
            alias="docs",
            rules={},
            resources={"repo://notes": ToolPolicy.ALLOW},
            prompts={"summarize": ToolPolicy.CONFIRM},
        )
        assert binding.enabled
        assert binding.policy_for(CapabilityKind.RESOURCE, "repo://notes") is ToolPolicy.ALLOW
        assert binding.policy_for(CapabilityKind.PROMPT, "summarize") is ToolPolicy.CONFIRM
        # And an absent one still denies, whatever the other two surfaces say.
        assert binding.policy_for(CapabilityKind.TOOL, "search") is ToolPolicy.DENY


class TestTheTimeoutContract:
    """Four layers wait on a held call; the platform waits for the shortest."""

    def test_the_decision_window_is_the_answer_when_nothing_else_waits(self) -> None:
        """There is always a deadline, even with no other clock configured."""
        created = datetime.now(timezone.utc)
        deadline = invocations.deadline_for(created_at=created)
        assert deadline.source == "decision_window"
        assert deadline.expires_at == created + invocations.DEFAULT_DECISION_WINDOW

    def test_the_relay_timeout_wins_when_it_is_tighter_than_the_cli(self) -> None:
        """Both are configured, and the shorter one is what the caller lives
        with."""
        created = datetime.now(timezone.utc)
        deadline = invocations.deadline_for(
            created_at=created,
            cli_timeout=timedelta(minutes=5),
            relay_timeout=timedelta(seconds=30),
        )
        assert deadline.source == "relay_timeout"

    def test_a_runner_deadline_is_an_instant_not_a_duration(self) -> None:
        """The whole job's wall clock, not a per-call slice of it."""
        created = datetime.now(timezone.utc)
        deadline = invocations.deadline_for(
            created_at=created, runner_deadline=created + timedelta(seconds=10)
        )
        assert deadline.source == "runner_deadline"

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """The store writes tz-aware values; a fixture may not, and reading one
        as local time would move a deadline by hours."""
        created = datetime.now(timezone.utc)
        naive = created.replace(tzinfo=None) + timedelta(seconds=5)
        deadline = invocations.deadline_for(created_at=created, lease_expires_at=naive)
        assert deadline.source == "lease"

    def test_a_passed_deadline_reports_no_time_left_rather_than_a_negative(self) -> None:
        """A caller subtracting it should not have to clamp."""
        deadline = invocations.deadline_for(
            created_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        assert deadline.passed
        assert deadline.remaining() == timedelta(0)


class TestTheStateMachine:
    """Every transition a held call can take, and the ones it cannot."""

    def test_the_happy_path_runs_pending_to_completed(self) -> None:
        """Approved, claimed by exactly one dispatcher, and finished."""
        status = invocations.InvocationStatus.PENDING
        for event, expected in (
            ("approve", invocations.InvocationStatus.APPROVED),
            ("claim", invocations.InvocationStatus.DISPATCHING),
            ("dispatch_ok", invocations.InvocationStatus.COMPLETED),
        ):
            status = invocations.transition(status, event)  # type: ignore[arg-type]
            assert status is expected

    def test_a_denied_call_stays_denied(self) -> None:
        """A decision is a decision; nothing revives it."""
        status = invocations.transition(invocations.InvocationStatus.PENDING, "deny")
        assert status is invocations.InvocationStatus.DENIED
        assert status in invocations.TERMINAL_STATUSES

    def test_a_cancelled_call_expires_rather_than_failing(self) -> None:
        """The runtime gave up, which is not the same as the call going wrong."""
        assert (
            invocations.transition(invocations.InvocationStatus.PENDING, "cancel")
            is invocations.InvocationStatus.EXPIRED
        )
        assert (
            invocations.transition(invocations.InvocationStatus.APPROVED, "cancel")
            is invocations.InvocationStatus.EXPIRED
        )

    def test_a_failed_dispatch_is_terminal_and_answered_by_a_person(self) -> None:
        """No automatic retry: the external effect may already have landed."""
        assert (
            invocations.transition(invocations.InvocationStatus.DISPATCHING, "dispatch_error")
            is invocations.InvocationStatus.FAILED
        )

    @pytest.mark.parametrize(
        "status",
        [
            invocations.InvocationStatus.DENIED,
            invocations.InvocationStatus.EXPIRED,
            invocations.InvocationStatus.COMPLETED,
            invocations.InvocationStatus.FAILED,
            invocations.InvocationStatus.OUTCOME_UNKNOWN,
        ],
    )
    def test_a_terminal_call_refuses_every_event(
        self, status: invocations.InvocationStatus
    ) -> None:
        """Including the ones that would look harmless, like a late denial.

        Rewriting a finished call is how a record stops being a record.
        """
        for event in ("approve", "deny", "timeout", "cancel", "claim", "dispatch_ok"):
            assert not invocations.can_transition(status, event)  # type: ignore[arg-type]
            with pytest.raises(invocations.TransitionRefused):
                invocations.transition(status, event)  # type: ignore[arg-type]


class TestTheFence:
    """One moved version is enough to refuse a dispatch."""

    def _invocation(self, **overrides: object) -> invocations.Invocation:
        base: dict[str, object] = {
            "invocation_id": "1",
            "job_id": "j",
            "attempt_id": "a",
            "lease_generation": 3,
            "thread_id": "t",
            "connection_id": "7",
            "capability_name": "search",
            "arguments_digest": "d",
            "config_revision": 1,
            "authorization_epoch": 1,
            "capability_revision": 1,
            "policy_revision": 1,
        }
        base.update(overrides)
        return invocations.Invocation(**base)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "moved",
        [
            {"config_revision": 2},
            {"authorization_epoch": 2},
            {"capability_revision": 2},
            {"policy_revision": 2},
            {"lease_generation": 4},
        ],
    )
    def test_any_moved_version_refuses_the_dispatch(self, moved: dict[str, int]) -> None:
        """The catalogue grew, the session narrowed, the job was taken over --
        each is a reason the approval no longer describes this call."""
        invocation = self._invocation()
        fence = invocations.DispatchFence(1, 1, 1, 1, 3, 0)
        for field, value in moved.items():
            moved_fence = invocations.DispatchFence(
                **{**fence.__dict__, field: value}  # type: ignore[arg-type]
            )
            assert not invocations.fence_matches(invocation, moved_fence), field

    def test_the_dispatch_generation_is_not_part_of_the_fence(self) -> None:
        """It fences the *dispatcher*, not the approval: the claim that bumps it
        is the same transition that makes the call dispatchable."""
        invocation = self._invocation()
        assert invocations.fence_matches(invocation, invocations.DispatchFence(1, 1, 1, 1, 3, 9))


class TestWhatTheOwnerIsTold:
    """The sentence beside a call that is not waiting any more."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (invocations.InvocationStatus.PENDING, ""),
            (invocations.InvocationStatus.APPROVED, ""),
            (invocations.InvocationStatus.COMPLETED, ""),
            (invocations.InvocationStatus.OUTCOME_UNKNOWN, invocations.RECOVERY_GUIDANCE),
        ],
    )
    def test_a_call_with_nothing_to_explain_says_nothing(
        self, status: invocations.InvocationStatus, expected: str
    ) -> None:
        """An empty string, not a sentence that means "fine"."""
        invocation = invocations.Invocation(
            invocation_id="1",
            job_id="j",
            attempt_id="a",
            lease_generation=1,
            thread_id="t",
            connection_id="7",
            capability_name="search",
            arguments_digest="d",
            status=status,
        )
        assert invocations.recovery_note(invocation) == expected

    def test_an_unknown_outcome_tells_the_owner_to_check_the_service(self) -> None:
        """Because the platform will not retry, somebody has to look."""
        invocation = invocations.Invocation(
            invocation_id="1",
            job_id="j",
            attempt_id="a",
            lease_generation=1,
            thread_id="t",
            connection_id="7",
            capability_name="create_issue",
            arguments_digest="d",
            status=invocations.InvocationStatus.OUTCOME_UNKNOWN,
        )
        assert "will not be retried" in invocations.recovery_note(invocation)

    def test_an_expired_approval_tells_the_owner_to_ask_again(self) -> None:
        """The call is gone, so the action is to re-request it."""
        invocation = invocations.Invocation(
            invocation_id="1",
            job_id="j",
            attempt_id="a",
            lease_generation=1,
            thread_id="t",
            connection_id="7",
            capability_name="search",
            arguments_digest="d",
            status=invocations.InvocationStatus.EXPIRED,
        )
        assert "expired" in invocations.recovery_note(invocation)


@pytest.mark.parametrize("kind", list(CapabilityKind))
@pytest.mark.parametrize(
    "revision", ["config_revision", "authorization_epoch", "capability_revision"]
)
@pytest.mark.parametrize("scope", ["denied", "unselected", "unknown"])
def test_a_stale_binding_never_prompts_for_an_out_of_scope_capability(kind, revision, scope):
    """Connection changes must not turn a refusal into a one-call approval."""
    connection = _connection(**{revision: 2})
    old = _connection()
    policy = ToolPolicy.DENY if scope == "denied" else ToolPolicy.ALLOW
    rules = {} if scope == "unselected" else {"search": policy}
    binding = _binding(
        old,
        rules={name: ToolRule(name=name, policy=value) for name, value in rules.items()},
        resource_rules=rules,
        prompt_rules=rules,
    )
    decision = _decide(
        connection,
        binding,
        kind=kind,
        capability=None if scope == "unknown" else Capability(kind=kind, name="search"),
    )
    assert decision.refused
    assert decision.code == (
        "capability_unknown" if scope == "unknown" else "capability_not_enabled"
    )
