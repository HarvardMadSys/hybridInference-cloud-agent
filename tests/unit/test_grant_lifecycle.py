"""Keeping a grant alive, and killing it — the fence.

After the split the gateway holds **no attempt or lease state at all**. It
cannot tell a live attempt from one the reaper superseded thirty seconds ago,
so it cannot refuse a renewal on those grounds. The only place that knows is
this service's store, which is why ``_renew_live_grants`` re-reads it every
pass instead of remembering what it minted.

That is the property this file exists for, and
``test_a_superseded_attempt_stops_being_renewed`` is the case that would be
worth writing even if every other one here were deleted: a renewal loop that
skipped the store would keep a zombie sandbox spending someone's quota, and
nothing anywhere — here or at the gateway — would notice.

The store's own SQL predicates are pinned separately, against a real database,
in ``tests/integration/storage/test_agent_grant_store.py``.

Two further properties are pinned here because nothing else can see them:

- **The cadence and the TTL are compatible.** They are separate constants in
  separate files and mean nothing apart, and a TTL shorter than the renewal
  interval loses a running attempt its capability with no symptom but a refused
  model call inside a sandbox.
- **Renewal gives up — and only on the grant.** A grant the gateway has
  permanently refused must stop being asked about, and giving up has to end the
  attempt rather than leave it running toward a credential that is about to
  die. But the same give-up, fed by an unreachable gateway or a rotated
  dispatch token, would abandon every running attempt in the fleet at once, so
  half the cases here are about what must *not* trip it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from tests.support.storage import AllCapabilities

from cloud_agent import grants as grant_client
from cloud_agent.gateway import GatewayError
from cloud_agent.grants import GRANT_TTL_S, RENEWAL_INTERVAL_S
from cloud_agent.workers import attempts as control_plane
from cloud_agent.workers.attempts import (
    _cleanup_reaped_shared_residencies,
    _reap_lapsed_residencies,
    _renew_live_grants,
    _revoke_dead_grants,
)

pytestmark = pytest.mark.asyncio


class _AttemptStore(AllCapabilities):
    """A store modelled on the two predicates the real queries use.

    An attempt is renewable while it is ``running``; anything else — superseded
    by the reaper, finished by its worker — is revocable. ``take_revocable_``
    detaches as it hands over, exactly as the ``UPDATE ... RETURNING`` does, so
    a grant is never offered twice.

    ``release_claim`` is the real one's contract too: it aborts the attempt and
    requeues the job, and refuses when the fence no longer matches.
    """

    def __init__(self) -> None:
        self.attempts: dict[int, dict[str, Any]] = {}
        self.released: list[tuple[str, int, int]] = []

    def add(self, attempt_id: int, grant_id: str | None, status: str = "running") -> None:
        self.attempts[attempt_id] = {
            "job_id": f"ajob_{attempt_id}",
            "grant_id": grant_id,
            "status": status,
            "lease_generation": attempt_id,
        }

    def supersede(self, attempt_id: int) -> None:
        self.attempts[attempt_id]["status"] = "superseded"

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        row = self.attempts.get(attempt_id)
        if row is None or row["status"] != "running" or lease_generation != row["lease_generation"]:
            return False
        self.released.append((job_id, attempt_id, lease_generation))
        row["status"] = "aborted"
        return True

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "attempt_id": aid,
                "job_id": row["job_id"],
                "grant_id": row["grant_id"],
                "lease_generation": row["lease_generation"],
            }
            for aid, row in sorted(self.attempts.items())
            if row["grant_id"] is not None and row["status"] == "running"
        ]

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        taken = []
        for aid, row in sorted(self.attempts.items()):
            if row["grant_id"] is not None and row["status"] != "running":
                taken.append(
                    {"attempt_id": aid, "job_id": row["job_id"], "grant_id": row["grant_id"]}
                )
                row["grant_id"] = None
        return taken


def _refusal(error_type: str = "grant_not_live", status: int | None = 404) -> GatewayError:
    """The shape the grant client raises when the gateway refuses a renewal.

    ``status=None`` is the shape it raises when there was no answer at all —
    unreachable, timed out — which is a different fact and must stay so.
    """
    return GatewayError(
        f"gateway answered {status} for renew" if status else "gateway unreachable: timed out",
        status_code=status,
        error_type=error_type if status else None,
    )


class _Grants:
    def __init__(
        self,
        *,
        renew_fails: bool = False,
        revoke_ok: bool = True,
        renew_error: GatewayError | None = None,
    ) -> None:
        self.renewed: list[str] = []
        self.revoked: list[str] = []
        self.renew_fails = renew_fails
        self.revoke_ok = revoke_ok
        # Defaults to a refusal that names the grant, because that is what the
        # give-up tests mean by "refused". A bare exception here would model a
        # gateway blip instead, and pass the give-up tests for the wrong reason.
        self.renew_error = renew_error or _refusal()

    async def renew(self, grant_id: str) -> dict[str, Any]:
        self.renewed.append(grant_id)
        if self.renew_fails:
            raise self.renew_error
        return {"grant_id": grant_id}

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return self.revoke_ok


# ── The cadence and the TTL ────────────────────────────────────────────
#
# Two constants in two files, describing one arrangement. Nothing at runtime
# compares them: a TTL shorter than the interval that renews it produces a
# sandbox whose capability dies mid-run, and the only place that failure
# surfaces is a refused model call inside a container.


async def test_the_renewal_cadence_is_the_reapers_pass() -> None:
    """Renewal runs on the reaper's loop, so there is exactly one number.

    If this ever becomes two, the TTL below is being sized against a cadence
    that is not the one scheduling it.
    """
    assert control_plane.REAP_INTERVAL_S == grant_client.RENEWAL_INTERVAL_S


async def test_a_grant_outlives_several_renewal_passes() -> None:
    """The invariant, stated as the thing that would break.

    Change ``REAP_INTERVAL_S`` (or the headroom) without changing the other and
    this fails here rather than in a sandbox three deployments later.
    """
    assert grant_client.GRANT_TTL_S >= control_plane.REAP_INTERVAL_S * 3
    assert (
        int(grant_client.RENEWAL_INTERVAL_S * grant_client.RENEWAL_HEADROOM_PASSES)
        == grant_client.GRANT_TTL_S
    )


async def test_the_requested_ttl_survives_the_gateways_clamp() -> None:
    """Asking for more than the gateway allows is answered by *silent*
    shortening, so the ceiling has to be respected here rather than discovered
    from the grant that comes back."""
    assert grant_client.GRANT_TTL_S <= grant_client.GATEWAY_MAX_TTL_S


async def test_renewal_keeps_up_with_the_step_the_gateway_grants() -> None:
    """A renewal buys the gateway's step from *now*, whatever was asked for at
    mint — so after the first renewal it is that number, not ours, that the
    cadence has to stay inside."""
    assert control_plane.REAP_INTERVAL_S * grant_client.RENEWAL_HEADROOM_PASSES <= (
        grant_client.GATEWAY_RENEWAL_STEP_S
    )


async def test_giving_up_happens_before_the_capability_dies() -> None:
    """Otherwise the give-up is not a give-up: the attempt would already be
    running with a dead token by the time anything stopped it."""
    give_up_after_s = control_plane.GRANT_RENEWAL_GIVE_UP_AFTER * control_plane.REAP_INTERVAL_S

    assert give_up_after_s < grant_client.GATEWAY_RENEWAL_STEP_S
    assert give_up_after_s < grant_client.GRANT_TTL_S


async def test_mint_asks_for_the_derived_ttl_without_being_told() -> None:
    """The claim path does not pass one, so the default is the whole control.

    ``None`` — "whatever the gateway defaults to" — is a number this service
    does not know and cannot keep in step with its own loop.
    """
    import inspect

    default = inspect.signature(grant_client.GrantClient.mint).parameters["ttl_seconds"].default
    assert default == grant_client.GRANT_TTL_S


# ── Renewal ────────────────────────────────────────────────────────────


async def test_a_superseded_attempt_stops_being_renewed() -> None:
    """**The fence.**

    The gateway cannot make this decision — it has no attempt state — so a loop
    that renewed on a timer would keep the superseded attempt's sandbox alive
    and spending until its own TTL ran out, and every renewal would push that
    moment further away. Consulting the store every pass is what makes
    superseding an attempt actually end it.
    """
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.add(2, "agr_2")
    failures: dict[str, int] = {}

    await _renew_live_grants(store, grants, failures=failures)
    assert grants.renewed == ["agr_1", "agr_2"]

    store.supersede(1)
    await _renew_live_grants(store, grants, failures=failures)

    assert grants.renewed == ["agr_1", "agr_2", "agr_2"], "the superseded attempt was renewed"


async def test_an_attempt_with_no_grant_is_not_renewed() -> None:
    """A deployment whose gateway cannot mint grants yet is a working one."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, None)

    await _renew_live_grants(store, grants, failures={})

    assert grants.renewed == []


async def test_a_refused_renewal_does_not_stop_the_pass() -> None:
    """One grant the gateway has forgotten must not strand every other job."""
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    store.add(2, "agr_2")

    await _renew_live_grants(store, grants, failures={})

    assert grants.renewed == ["agr_1", "agr_2"]


# ── Giving up ──────────────────────────────────────────────────────────
#
# Revocation detaches as it hands over, so a refused revoke costs one call and
# never repeats. Renewal needed the equivalent: without it a grant the gateway
# has permanently refused — revoked out of band, its subject suspended — is
# asked about every 30 seconds for as long as the attempt lives, and the
# attempt runs on toward the moment its capability expires with nothing
# anywhere saying so.


async def test_a_permanently_refused_grant_is_eventually_given_up_on() -> None:
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER + 4):
        await _renew_live_grants(store, grants, failures=failures)

    assert len(grants.renewed) == control_plane.GRANT_RENEWAL_GIVE_UP_AFTER


async def test_giving_up_stops_the_attempt_rather_than_the_renewals() -> None:
    """**The half that matters.**

    Quietly stopping the calls would leave the sandbox running until its
    capability expired and then failing at a model call, minutes and one
    process away from the cause. Releasing the claim aborts the attempt — the
    worker's next heartbeat says stop — and returns the job to the queue
    without spending a retry, where the next claim asks for a fresh grant.
    """
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER):
        await _renew_live_grants(store, grants, failures=failures)

    assert store.released == [("ajob_1", 1, 1)], "the attempt was left running without capability"
    assert store.attempts[1]["status"] == "aborted"


async def test_the_abandoned_grant_is_then_revoked_exactly_once() -> None:
    """Nothing revokes inline: the release makes the attempt non-current, which
    is precisely what the revocation pass selects."""
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER):
        await _renew_live_grants(store, grants, failures=failures)
    await _revoke_dead_grants(store, grants)
    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_one_bad_pass_does_not_count_toward_a_give_up() -> None:
    """Consecutive, not cumulative. A gateway having a bad minute every hour
    must not eventually end a long job that is otherwise fine."""
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER * 3):
        await _renew_live_grants(store, grants, failures=failures)
        grants.renew_fails = not grants.renew_fails

    assert store.released == []
    assert store.attempts[1]["status"] == "running"


# ── ...and not giving up ───────────────────────────────────────────────
#
# The give-up above is a demolition charge wired to a counter, and everything
# here is about what must never trip it. Every one of these failures arrives as
# the same exception type as a revoked grant; the difference is only in what
# the gateway said, and if that difference is not read, then one gateway
# restart, one rotated token, or one missing environment variable abandons
# every running attempt in the fleet simultaneously — each of them a user's
# work, most of the way done.


async def test_a_gateway_outage_does_not_abandon_the_fleet() -> None:
    """**The case this whole section exists for.**

    An unreachable gateway has said nothing about anyone's grant. Counting its
    silence as refusal turns a minute of downtime — three passes — into a
    fleet-wide abort, and one that destroys in-flight work precisely when
    nobody can look at the gateway to see why.
    """
    store = _AttemptStore()
    grants = _Grants(renew_fails=True, renew_error=_refusal(status=None))
    for attempt_id in (1, 2, 3):
        store.add(attempt_id, f"agr_{attempt_id}")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER * 5):
        await _renew_live_grants(store, grants, failures=failures)

    assert store.released == [], "a gateway outage abandoned running attempts"
    assert all(row["status"] == "running" for row in store.attempts.values())
    assert failures == {}, "an outage was recorded as evidence against the grant"


@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        # A rotated or mistyped GATEWAY_GRANT_DISPATCH_TOKEN.
        ("unauthorized", 401),
        # The gateway deployed without its internal API configured. Note the
        # status: identical to unknown_grant, which is why the type is read.
        ("internal_api_not_configured", 404),
        # The gateway unwell, or behind a proxy that is.
        ("internal_error", 500),
        (None, 502),
        # Backpressure, and a slow answer. Both are "ask again".
        ("rate_limited", 429),
        (None, 408),
    ],
)
async def test_a_failure_that_is_not_about_the_grant_never_gives_up(
    error_type: str | None, status: int
) -> None:
    """Our own misconfiguration is not grounds to destroy a user's job.

    Each of these is a statement about the gateway or about this service.
    Reading only the status class cannot separate the 404 here from a grant
    that genuinely no longer exists.
    """
    store = _AttemptStore()
    grants = _Grants(renew_fails=True, renew_error=_refusal(error_type, status))
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER * 3):
        await _renew_live_grants(store, grants, failures=failures)

    assert store.released == []
    assert store.attempts[1]["status"] == "running"


async def test_an_outage_neither_advances_nor_forgives_the_count() -> None:
    """Silence is not evidence in either direction.

    Forgiving would let a grant refused every other pass be renewed forever;
    advancing is the fleet-wide abort. It has to do neither, so a real refusal
    that resumes after an outage picks up where it left off.
    """
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER - 1):
        await _renew_live_grants(store, grants, failures=failures)
    assert store.released == []

    grants.renew_error = _refusal(status=None)
    for _ in range(20):
        await _renew_live_grants(store, grants, failures=failures)
    assert store.released == [], "an outage advanced a count it knows nothing about"

    grants.renew_error = _refusal()
    await _renew_live_grants(store, grants, failures=failures)

    assert store.released == [("ajob_1", 1, 1)], "the outage forgave a refusal already counted"


async def test_a_suspended_subject_is_given_up_on() -> None:
    """The other side of the same coin: a verdict on the grant's subject is a
    verdict on the grant. A user suspended mid-job must stop spending, and the
    attempt must be stopped rather than left running toward a dying token."""
    store = _AttemptStore()
    grants = _Grants(renew_fails=True, renew_error=_refusal("subject_unavailable", 403))
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    for _ in range(control_plane.GRANT_RENEWAL_GIVE_UP_AFTER):
        await _renew_live_grants(store, grants, failures=failures)

    assert store.attempts[1]["status"] == "aborted"


async def test_the_refusals_that_destroy_work_are_named_deliberately() -> None:
    """Adding a type to this set is a decision to abandon running attempts on
    it, so it should require editing this line and saying why.

    These strings are the gateway's, from ``routers/agent_grants.py``. They are
    a cross-repo contract: renaming one there without changing it here does not
    fail anything, it silently stops the give-up from ever firing — which is
    the safe direction, but a quiet one.
    """
    assert {
        "unknown_grant",
        "grant_not_live",
        "subject_unavailable",
    } == control_plane.DEAD_GRANT_REFUSALS


async def test_a_finished_attempt_leaves_no_failure_count_behind() -> None:
    """The tracker lives for the life of the process, so anything it keeps for
    a job that has ended is a leak — and a stale count that could give up on
    the *next* grant sooner than it should."""
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    failures: dict[str, int] = {}

    await _renew_live_grants(store, grants, failures=failures)
    assert failures == {"agr_1": 1}

    store.supersede(1)
    await _renew_live_grants(store, grants, failures=failures)

    assert failures == {}


# ── Revocation ─────────────────────────────────────────────────────────


async def test_a_finished_attempts_grant_is_revoked() -> None:
    """The terminal case: the worker reported, the attempt is over, and the
    capability it was carrying should not outlive it by its whole TTL."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1", status="finished")

    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_a_live_attempts_grant_is_never_revoked() -> None:
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")

    await _revoke_dead_grants(store, grants)

    assert grants.revoked == []


async def test_a_grant_is_only_offered_for_revocation_once() -> None:
    """The store detaches as it hands over, so a gateway that refuses does not
    cost a call every pass for the rest of the deployment's life. The TTL is
    the backstop — revocation only accelerates it."""
    store, grants = _AttemptStore(), _Grants(revoke_ok=False)
    store.add(1, "agr_1")
    store.supersede(1)

    await _revoke_dead_grants(store, grants)
    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_a_revoked_grant_is_not_then_renewed() -> None:
    """The two passes compose: revocation detaches the link, and renewal reads
    the same column, so nothing can be in both sets."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.supersede(1)

    await _revoke_dead_grants(store, grants)
    await _renew_live_grants(store, grants, failures={})

    assert grants.revoked == ["agr_1"]
    assert grants.renewed == []


# ── The pass has to fit, not merely be parallel ────────────────────────


async def test_the_pass_deadline_leaves_room_for_two_of_them() -> None:
    """The arithmetic, stated where a change to either number breaks it.

    A grant renewed at the end of one pass is next reached at the end of the
    following one, so the worst gap between two renewals of one grant is
    ``duration + interval + duration``. That has to stay under the TTL, or a
    capability dies under a running sandbox — the failure the concurrency
    limit was added for and did not fully close.
    """
    worst_gap = 2 * control_plane.GRANT_RENEWAL_PASS_DEADLINE_S + RENEWAL_INTERVAL_S
    assert worst_gap <= GRANT_TTL_S, (
        f"two passes plus an interval is {worst_gap}s against a {GRANT_TTL_S}s "
        "TTL: a grant can expire under a running attempt"
    )


async def test_a_pass_that_runs_long_gives_up_its_tail_and_says_so(monkeypatch, caplog) -> None:
    """What is cut is the least urgent, and it is not cut quietly.

    `list_renewable_grants` returns soonest-expiry first, so the rows that lose
    their turn are the ones with the most life left, and they are retried a
    whole interval before they could expire. Silence would be the problem: a
    pass that keeps timing out means the page is too large or the gateway too
    slow, and the failure it eventually produces points nowhere near here.
    """
    store = _AttemptStore()
    for attempt_id in range(1, 4):
        store.add(attempt_id, f"grant_{attempt_id}")

    class _SlowGrants(_Grants):
        async def renew(self, grant_id: str) -> dict[str, Any]:
            if grant_id != "grant_1":
                await asyncio.sleep(10)
            return await super().renew(grant_id)

    monkeypatch.setattr(control_plane, "GRANT_RENEWAL_PASS_DEADLINE_S", 0.05)
    grants = _SlowGrants()

    with caplog.at_level(logging.WARNING):
        await _renew_live_grants(store, grants, failures={})

    assert grants.renewed == ["grant_1"], grants.renewed
    timed_out = [r for r in caplog.records if "timed_out" in getattr(r, "event", "")]
    assert timed_out, "the pass dropped renewals without saying so"
    assert timed_out[0].outstanding == 2


async def test_a_cancelled_renewal_does_not_forget_its_refusal_count(monkeypatch) -> None:
    """Give-up is consecutive, and a cancelled pass said nothing either way.

    `failures` is pruned to the grants still live at the end of each pass. A
    grant whose renewal the deadline cancelled is still live, so dropping its
    entry would reset a count that the give-up rule depends on being
    consecutive — and a genuinely dead grant would then be retried forever.
    """
    store = _AttemptStore()
    store.add(1, "grant_1")
    store.add(2, "grant_2")

    class _SlowGrants(_Grants):
        async def renew(self, grant_id: str) -> dict[str, Any]:
            await asyncio.sleep(10)
            return await super().renew(grant_id)

    monkeypatch.setattr(control_plane, "GRANT_RENEWAL_PASS_DEADLINE_S", 0.05)
    failures = {"grant_1": 2, "grant_2": 1}

    await _renew_live_grants(store, _SlowGrants(), failures=failures)

    assert failures == {"grant_1": 2, "grant_2": 1}, (
        "a cancelled renewal reset a refusal count the give-up rule needs"
    )


# ── The residency's grant dies with its lease (retained design §6) ─────


class _ResidencyStore:
    def __init__(self, lapsed: list[dict[str, Any]]) -> None:
        self.lapsed = lapsed

    async def reap_expired_residencies(self) -> list[dict[str, Any]]:
        rows, self.lapsed = self.lapsed, []
        return rows


async def test_a_lapsed_residency_takes_its_grant_with_it() -> None:
    """A host that stopped beating must starve promptly: the reaper revokes
    the lapsed residency's credential rather than letting it spend until its
    own TTL. Workspace-only residencies lapse with nothing to revoke."""
    grants = _Grants()
    store = _ResidencyStore(
        [
            {"thread_id": "t1", "generation": 2, "grant_id": "agr-1"},
            {"thread_id": "t2", "generation": 1, "grant_id": None},
        ]
    )

    await _reap_lapsed_residencies(store, grants)

    assert grants.revoked == ["agr-1"]


async def test_a_deployment_without_grants_still_lapses_residencies() -> None:
    store = _ResidencyStore([{"thread_id": "t1", "generation": 1, "grant_id": "agr-1"}])

    await _reap_lapsed_residencies(store, None)

    assert store.lapsed == []  # the reap itself happened


async def test_reaped_shared_members_close_processes_and_grants_independently(
    monkeypatch,
) -> None:
    """One unreachable sandbox manager cannot block another cleanup or grant revoke."""
    sandbox_manager_calls: list[tuple[str, int]] = []

    class _SandboxManager:
        async def shared_residency_lost(self, thread_id: str, *, generation: int) -> None:
            sandbox_manager_calls.append((thread_id, generation))

    async def route(_store, job_id: str):
        if job_id == "job-unreachable":
            raise RuntimeError("host unavailable")
        return _SandboxManager()

    from cloud_agent import sandbox_manager_routing

    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_job", route)
    grants = _Grants()
    actions = [
        {
            "job_id": "job-unreachable",
            "cleanup_residency": {
                "thread_id": "thread-one",
                "generation": 3,
                "grant_id": "grant-one",
            },
        },
        {
            "job_id": "job-reachable",
            "cleanup_residency": {
                "thread_id": "thread-two",
                "generation": 4,
                "grant_id": "grant-two",
            },
        },
    ]

    await _cleanup_reaped_shared_residencies(object(), actions, grants)

    assert sandbox_manager_calls == [("thread-two", 4)]
    assert grants.revoked == ["grant-one", "grant-two"]
