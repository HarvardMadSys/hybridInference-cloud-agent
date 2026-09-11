"""Port: the runner-host pool and its enrollment.

Enrollment, heartbeats, status transitions, and the placement pool the
scheduler draws from.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class HostPoolRepository(Protocol):
    """Port: the runner-host pool and its enrollment."""

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
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
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
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
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
        """
        ...

    async def set_host_status(self, *, host: str, status: str) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"`` or ``"invalid"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.
        """
        ...

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Revoke a host's credential and take it out of the pool.

        Returns ``"ok"``, ``"unknown"`` or ``"busy"``. Busy means the host is
        still holding slots: removing it then would leave running attempts
        whose machine has officially stopped existing, and nothing would notice
        until their leases lapsed. ``force`` accepts exactly that — the lease
        expiry *is* the recovery path. A retained activation cannot be replayed
        safely after its host disappears, so its expiry marks that chat LOST
        and fails already-queued descendants instead.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
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

    async def runner_pool(self) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
        """The pool, the pinned host and the version selection, read together.

        One transaction because all three are shown side by side: read
        separately, a switch landing between them renders a page whose list and
        whose header name different hosts, which reads as a bug in the thing
        the operator is using to fix a bug. The version selection joined that
        for the same reason — a page offering a menu that no longer contains
        the selection it displays is the same failure wearing a different hat.
        """
        ...

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Read placement policy, hosts, and retained owners for rollout fencing.

        The admin pin governs only chats with no sandbox. A control-plane
        rollout must also know which other hosts still own shared-live Docker
        layers so it can reverse-probe their brokers. Read the policy under a
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

        Returns ``"deleted"``, ``"active"`` (refused) or ``"unknown"``.

        The active-host refusal lives here rather than in the caller because
        checking there is a read the switch can outrun: the endpoint sees the
        host idle, an admin pins it, and the delete then removes the row the
        new-chat placement gate is pointing at — silently unpinning that
        policy. Same policy row, same order as the switch, so the two serialize.
        """
        ...
