"""Port: the runner-host pool and its enrollment.

Enrollment, heartbeats, status transitions, and the placement pool the
scheduler draws from.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID


class HostPoolRepository(Protocol):
    """Port: the runner-host pool and its enrollment."""

    async def autoscaling_observation(self, *, expected_deployment_sha: str) -> dict[str, Any]:
        """Observe new-group demand and capacity without claiming work.

        The queue count deliberately excludes jobs whose thread already owns
        a retained sandbox: another machine cannot serve those turns. Free
        slots include only fresh, enrolled hosts that may create a new group
        under the same durable compute fences as the claim path. Unmatched
        demand is the exact capability/profile matching against those slots;
        once an owned fresh template exists, work that no such template can
        run is omitted from scale-up pressure. No job identifiers or job
        requirements leave this projection. The result also carries raw host
        telemetry for conservative per-host slot sizing; callers must not
        treat a stale sample as evidence for an increase.
        """
        ...

    async def queue_health(self, *, expected_deployment_sha: str) -> dict[str, int]:
        """Observe configured queue stalls without claiming or changing jobs.

        Occupied slots are ordinary backpressure. Missing/stale/disabled hosts,
        lost deployment markers and a wrong placement pin are not. Retained
        members use their owner, independent of the pin for new conversations.
        No user content, job identifiers or credentials leave this projection.
        """
        ...

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        """Record a one-time enrollment token by hash and return its expiry.

        Only the hash is stored. An operator who loses the token issues another
        one; nobody — including anyone who can read this table — can recover
        the value that was handed out.

        ``host`` is the name this token may register, and it is stored in
        clear: it is not a secret, it is the authenticated decision the token
        carries. See :meth:`consume_enroll_token`.
        """
        ...

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        """Spend an enrollment token, exactly once and only under its own name.

        Single-use is the UPDATE's own ``consumed_at IS NULL`` predicate rather
        than a read followed by a write: two machines booting from the same
        pasted token race here, and only one row update can win.

        **The name is part of the predicate, not a check after the fact.**
        ``register_host`` upserts, so registering under a live host's name
        revokes that host's credential and resets its row to ``enrolling`` —
        one typo during a setup takes a working machine out of the pool. The
        name therefore comes from the token an admin issued rather than from
        the machine presenting it. Because it is a predicate, a mismatch also
        does not *spend* the token: the typo is retried, not re-issued.

        A row whose ``host`` is NULL — issued before the binding existed —
        matches nothing, because ``NULL = $2`` is never true. That is the
        fail-closed direction: such a token was redeemable under any name.
        """
        ...

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        """Return one host row, without its credential hash."""
        ...

    async def register_host(
        self,
        *,
        host: str,
        credential_hash: str,
        enrollment_token_hash: str | None = None,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        sandbox_manager_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enrol a machine, or re-enrol one that was rebuilt.

        Starts at ``enrolling``, not ``online``: registration proves somebody
        held a valid enrollment token, not that the machine is up. The first
        heartbeat is what says that, and it is one round trip away.

        Re-enrollment resets ``slots_used`` to the number of attempts actually
        running on this host rather than to zero. A machine that was rebuilt
        mid-job has a stale counter, and zero would over-subscribe it while the
        old attempts are still leased; counting is the only answer that is
        right in both the rebuilt and the merely-re-registered case.

        The upsert is what makes a rebuild possible, and it is also what makes
        a mistyped name destructive — it revokes whatever credential the name
        currently holds. It stays an upsert; what changed is who chooses the
        name. :meth:`consume_enroll_token` will only spend a token under the
        name an admin bound to it, so reaching this method under a live host's
        name is an operator's decision rather than a machine's.

        Re-enrollment also **rotates the capacity epoch and clears the stored
        observation**. A rebuilt machine keeps its name, so nothing else
        distinguishes the new box from the one it replaced; without the
        rotation the admin chart would join the two machines' histories into
        one line that no machine ever drew.
        """
        ...

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        """Return the host iff this credential is the one it currently holds.

        Fails closed on every other reading: an unknown host, a revoked
        credential (``credential_hash`` nulled), a superseded one, or a
        disabled row all answer ``None``. Compared in SQL so a revocation that
        commits mid-request is seen by the next call rather than by the next
        restart.
        """
        ...

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        launch_error: str | None = None,
        runner_version: str | None = None,
        sandbox_manager_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
        settings_applied: Mapping[str, int | float] | None = None,
        settings_local: Mapping[str, int | float] | None = None,
        sandbox_archive_settings_applied_revision: int | None = None,
        sandbox_archive_settings_revision_reported: bool = False,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may.

        ``enrolling → online`` and ``offline → online`` both happen here: a
        machine that comes back after a network partition needs no operator to
        put it back to work. ``draining`` and ``disabled`` deliberately do
        **not** — a heartbeat is the host saying "I am here", not "I should be
        taking work", and letting it re-promote itself would make drain a
        suggestion.

        Returns ``None`` for an unknown host. Every field is optional and only
        overwrites when supplied, so a heartbeat that carries nothing but
        liveness does not blank out what enrollment declared.

        **The capacity block is written by a second statement, deliberately.**
        Its acceptance conditions are not the heartbeat's: the credential must
        still be the one this request authenticated with, the sample must not
        be one already stored, and an older observation must not overwrite a
        younger one. They are enforced in SQL rather than here because the
        sequence they guard against is a race — credential A authenticates, an
        operator re-enrolls the name under credential B, and A's delayed
        request arrives afterwards. Checking "is this still the right
        credential" outside the write would let exactly that request through.
        That second statement runs in its own savepoint, so a block the
        database refuses costs the host nothing but the block: the liveness
        half of the same call still commits. A third, in a savepoint of its
        own again, keeps an accepted observation in the history the chart
        reads — this request is the only place that sample is known to exist,
        because the row it was just written into holds one observation and the
        next heartbeat replaces it.

        Args:
            host: The authenticated host name.
            credential_hash: Digest the request authenticated with. The
                archive revision, capacity write, and legacy settings import
                are refused unless the row still holds it.
            capacity: The validated, normalized capacity block to store.
            capacity_sample_age_ms: How old the observation was when sent.
            physical_host: Admin grouping label; never an identity.
            slots_total: The concurrency limit this host currently applies.
            launch_error: Why it is not claiming, or ``""`` when nothing is
                wrong. ``None`` is a host that does not report this at all and
                leaves whatever is stored in place, the same way an unreported
                runtime does.
            runner_version: Runner build the host is running.
            sandbox_manager_url: Address the control plane reaches this host's sandbox manager.
            capabilities: What this host can run.
            sandbox_runtime: Attested container runtime.
            sandbox_image_digest: Digest of the sandbox image it launches.
            runtime_versions: Default CLI version per runtime.
            runtime_versions_available: Every CLI version its image holds.
            settings_applied: The runtime settings the host is applying now.
            settings_local: Legacy environment observations, imported once as
                per-host overrides. Either ``None`` leaves the report in place.

        Returns:
            The updated host row, or ``None`` when the host no longer exists.
        """
        ...

    async def set_host_status(self, *, host: str, status: str, force: bool = False) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"``, ``"invalid"`` or ``"fenced"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.
        """
        ...

    async def acquire_host_drain_fence(
        self,
        *,
        host: str,
        expected_fence_token: UUID | None,
        expected_fence_epoch: UUID | None,
        fence_token: UUID,
    ) -> tuple[str, str | None, UUID | None]:
        """CAS an expected deployment-drain owner and epoch to ``fence_token``.

        Exact requested-token retries are resolved before the expected owner
        and epoch are compared. The row lock covers observation and mutation.
        ``status_before`` is returned only for ``"ok"``; other outcomes
        are ``"unknown"``, ``"invalid"``, ``"manual"``, ``"stale"``
        and ``"released"``. The epoch is absent only for an unknown host.
        """
        ...

    async def release_host_drain_fence(
        self, *, host: str, fence_token: UUID
    ) -> tuple[str, UUID | None]:
        """Put the exact active drain owner online, retaining its receipt.

        Returns the outcome and current post-update epoch. Outcomes are
        ``"ok"``, ``"unknown"``, ``"invalid"`` or ``"stale"``.
        """
        ...

    async def set_host_slots_desired(self, *, host: str, slots: int | None) -> str:
        """Ask a host to run ``slots`` turns at once, or with ``None`` its default.

        Returns ``"ok"``, ``"unknown"`` or ``"autoscaling_managed"``. Nothing
        here starts or stops anything: the host reads the value off its next heartbeat answer,
        serves it as its limit, and reports it back as ``slots_total``. The
        claim honours the lower of the two from the next poll on, so a number
        the host has not confirmed admits nothing extra and a lowered one takes
        effect at once. Turns already running are never touched.

        The value survives heartbeats and re-enrollment on purpose. It is a
        decision about the named machine, and neither the heartbeat's
        ``COALESCE`` nor the registration upsert names this column. Bounds are
        the request schema's business; the column refuses only a non-positive
        size, which is not a size.
        """
        ...

    async def set_runner_host_settings(
        self, *, host: str, changes: Mapping[str, int | None]
    ) -> dict[str, Any] | None:
        """Set or clear runtime settings for one server, over the global values.

        ``changes`` names only the settings to change, and ``None`` clears one
        so the global value, or the built-in default, applies again. Settings it does
        not name keep their values. The host applies the result from its next
        heartbeat answer.

        Like ``slots_desired``, the values survive heartbeats and
        re-enrollment: they are a decision about the named machine. Validation
        is the caller's (``cloud_agent_shared.host_settings``). The row is
        locked for the read-modify-write, so two admins changing different
        settings on the same server both land.

        Returns the server's values before the change, or ``None`` for an
        unknown host.
        """
        ...

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Revoke a host's credential and take it out of the pool.

        Returns ``"ok"``, ``"unknown"``, ``"fenced"``, ``"draining"`` or
        ``"busy"``. A host still holding slots is drained instead of revoked:
        it claims nothing more, the request is recorded, and
        :meth:`finish_host_removals` revokes it once its running attempts end.
        ``"busy"`` is the busy host whose status cannot move to ``draining``.
        ``force`` revokes now and ends that work — the lease expiry *is* the
        recovery path. A retained activation cannot be replayed safely after
        its host disappears, so its expiry marks that chat LOST and fails
        already-queued descendants instead.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
        """
        ...

    async def finish_host_removals(self) -> list[str]:
        """Revoke every host whose requested removal has no running work left.

        Returns the hosts it revoked.
        """
        ...

    async def reap_offline_hosts(
        self, *, interval_seconds: float, missed_intervals: int
    ) -> list[str]:
        """Mark enrolled hosts that stopped heartbeating as ``offline``.

        Only *enrolled* hosts — ones holding a credential — are candidates.
        A host that joined by polling never promised to heartbeat, and marking
        it offline would stop the claims of a machine that is working
        perfectly, which is the whole pre-G deployment.

        ``COALESCE(last_heartbeat_at, first_seen_at)`` so a machine that
        registered and then never came up ages out of ``enrolling`` too,
        instead of sitting there looking like it is mid-setup forever.

        **``draining`` is not a candidate, and that is the point.** It was, and
        the result was that an operator's drain undid itself: this pass moved
        ``draining → offline``, and the next heartbeat moved ``offline →
        online``, because a machine returning from a partition is supposed to
        come back without an operator. A drained host that goes quiet and
        returns would silently rejoin the claim pool — quietly taking work the
        operator had just decided it must not take. Drain is an instruction,
        not an observation, so nothing here overwrites it; ``remove`` is what
        ends a drained host's life.

        Returns the hosts it changed.
        """
        ...

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """The host row an attempt ran on, or ``None`` when it had none.

        ``None`` covers both "this attempt predates host reporting" and "the
        host it ran on was forgotten" — the FK is ``ON DELETE SET NULL``, so
        both present as the same absence, and both mean the caller falls back
        to the single-host configuration.
        """
        ...

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The job attempt's host, or its retained thread's durable host."""
        ...

    async def active_runner_host(self) -> str | None:
        """The new-chat placement host, or ``None`` when any may materialize."""
        ...

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        """The pool, the pinned host and the version selection, read together.

        One transaction because all three are shown side by side: read
        separately, a switch landing between them renders a page whose list and
        whose header name different hosts, which reads as a bug in the thing
        the operator is using to fix a bug. The version selection joined that
        for the same reason — a page offering a menu that no longer contains
        the selection it displays is the same failure wearing a different hat.

        **Repeatable read, and read-only.** The pool is the page an operator
        reads while changing things, so a claim, a pin and a drain landing
        between the policy read and the host read used to be able to produce a
        list that never existed: slots already spent shown against a pin not
        yet switched. One snapshot for all of it removes that class of answer,
        and ``observed_at`` is the database's own instant for the same
        snapshot, so the ages the page renders are measured from the moment the
        data was taken rather than from whenever the client's clock says.

        Returns:
            ``(hosts, active_host, selected_versions, observed_at)``.
        """
        ...

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Read placement policy, hosts, and retained owners for rollout fencing.

        The admin pin governs only chats with no sandbox. A control-plane
        rollout must also know which other hosts still own shared-live Docker
        layers so it can reverse-probe their sandbox managers. Read the policy under a
        shared lock and the owners in the same transaction: a concurrent host
        switch cannot make the snapshot describe two different placement
        policies, and any newly claimed group while this lock is held belongs
        to one of the placement hosts this snapshot already includes.
        """
        ...

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Return every host seen polling, most recent poll first."""
        ...

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        """Choose the CLI version this deployment runs for one runtime.

        ``version=None`` clears the choice, which returns that runtime to
        whatever its claiming host reports as the image default.

        Returns ``False`` when no host in the pool offers ``version``. Storing a
        selection nothing can serve is not harmless: the claim would fall back
        silently, so the admin page would show a version that never runs — the
        pin's "switching to a typo" failure, one layer along.

        Same lock order as ``set_active_runner_host`` — policy row first, then
        the host rows it is checked against — so the two admin writes and the
        claim's shared read serialize with no cycle to deadlock on.
        """
        ...

    async def host_settings(self) -> dict[str, Any]:
        """The runtime settings set for every server, as stored.

        A server's own values win over these, and a setting set nowhere falls
        back to the built-in default. Read through
        ``cloud_agent_shared.host_settings.admin_values``, which drops anything
        a release can no longer apply.
        """
        ...

    async def set_host_settings(self, *, changes: Mapping[str, int | None]) -> dict[str, Any]:
        """Set or clear runtime settings for every server.

        ``changes`` names only the settings to change, and ``None`` clears one.
        Every host applies the result from its next heartbeat answer, except
        where a value set for that server wins. The policy row is locked for
        the read-modify-write, the same first lock every other policy writer
        takes, so this serializes with them and with a concurrent change to
        another setting. Returns the values before the change, for the audit
        record.
        """
        ...

    async def sandbox_archive_settings(self) -> dict[str, Any]:
        """Return the normalized deployment-wide archive policy and its timestamp."""
        ...

    async def set_sandbox_archive_settings(
        self,
        *,
        expected_revision: int,
        settings: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically replace the archive policy, or return ``None`` on a stale revision."""
        ...

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Pin new-chat placement to one host, or unpin with ``None``.

        Returns ``False`` when ``host`` names a machine that has never polled —
        pinning to one would park every chat without a sandbox on a host that
        may not exist. Existing groups remain on their owning hosts.

        Takes the policy row exclusively *first*, then the host row it is about
        to point at. Every writer here uses that order, and the claim takes the
        policy row shared, so the paths serialize with no cycle to deadlock on:
        a claim past its own read finishes before this commits, one that has
        not reached it waits and then sees this value, and a concurrent removal
        of the host being pinned cannot slip in between the check and the write.
        """
        ...

    async def forget_runner_host(self, *, host: str) -> str:
        """Drop a host from the pool. A runner still polling re-adds itself.

        Returns ``"deleted"`` or ``"unknown"``; refuses ``"active"``,
        ``"fenced"``,
        ``"retained_groups_present"``, ``"resource_bound"``,
        ``"active_attempts_present"`` and ``"pending_compute_operations"``.
        A sandbox whose deletion has completed is an audit row: it keeps the
        host name and does not block forgetting. A live group, or one whose
        deletion failed or is still in progress, does. A disabled attached
        machine without external release capability may be deregistered after
        dependency checks; this never shuts it down.

        The active-host refusal lives here rather than in the caller because
        checking there is a read the switch can outrun: the endpoint sees the
        host idle, an admin pins it, and the delete then removes the row the
        new-chat placement gate is pointing at — silently unpinning that
        policy. Same policy row, same order as the switch, so the two serialize.
        """
        ...
