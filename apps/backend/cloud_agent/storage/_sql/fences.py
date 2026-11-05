"""The fence a relay reads before it speaks for a sandbox: one definition for every relay.

A relay outside the sandbox holds a credential and acts for the turn a
residency placeholder names: the MCP relay
(:class:`~cloud_agent.storage.repositories.credential_queries.PostgresCredentialQueries`)
and the GitHub relay
(:class:`~cloud_agent.storage.repositories.github_relay.PostgresGitHubRelayRepository`).
Both ask the same question, so both build their statements from these
fragments: a change to the fence reaches every relay, or none.

The fence has two halves, and they compose:

* :func:`hot_residency_sql` -- the residency is the chat's current, hot,
  unexpired one, the chat is not deleted, and the attempt runs on the
  residency's own host. :func:`residency_caller_sql` adds which chat and
  ties the job to it.
* :data:`LIVE_ATTEMPT` -- the attempt is its job's current, running one with
  an unexpired lease, the job is running and not asked to stop, and its host
  is still enrolled. It extends :data:`CURRENT_RUNNING_ATTEMPT`.

The GitHub relay reads both halves in one statement
(:func:`live_residency_caller_sql`). The MCP relay reads them in two: the
residency names its attempt under :data:`CURRENT_RUNNING_ATTEMPT` only, and
the attempt is then checked under :data:`LIVE_ATTEMPT` with the residency
re-read beside it (:func:`~cloud_agent.storage._sql.mcp_residency.residency_fence_sql`,
which is built from :func:`hot_residency_sql` too). The split is deliberate:
"this chat has no running turn" and "this turn's credential no longer
works" are different refusals, and the second statement is the one the
relay re-reads after an asynchronous wait.

Every fragment binds the aliases its docstring names; the caller supplies
them. Placeholders are passed in as trusted SQL expressions, never values.
"""

from __future__ import annotations

from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST

#: The residency, its chat, its group, the attempt it is serving and that
#: attempt's job, as ``m``, ``t``, ``g``, ``a`` and ``j``. The attempt is the one
#: admitted to the membership (``m.active_attempt_id``), never another turn.
RESIDENCY_CALLER_FROM = """agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                JOIN agent_attempts a ON a.id = m.active_attempt_id
                JOIN agent_jobs j ON j.id = a.job_id"""

#: Attempt ``a`` is its job ``j``'s current one and is running.
CURRENT_RUNNING_ATTEMPT = """a.status = 'running'
                  AND j.current_attempt_id = a.id"""

#: Attempt ``a`` may still act for job ``j``: it is the job's current, running
#: attempt, its lease has not expired by the wall clock, the job is running and
#: has not been asked to stop, and the attempt's host is still enrolled. Stop
#: and host removal change neither the lease nor the job's state at once, which
#: is why both are read here rather than left to the lease.
LIVE_ATTEMPT = f"""{CURRENT_RUNNING_ATTEMPT}
                  AND a.lease_expires_at > clock_timestamp()
                  AND j.state = 'running'
                  AND NOT j.cancel_requested
                  AND {LIVE_ATTEMPT_HOST}"""


def hot_residency_sql(generation: str) -> str:
    """Membership ``m`` is the hot, unexpired residency ``generation`` names, on ``a``'s host.

    Binds ``m``, its chat ``t``, its group ``g`` and the attempt ``a``. The chat
    is not deleted, and the attempt runs on the group's own host, so a
    credential copied out of one sandbox cannot speak for a turn the chat runs
    anywhere else.
    """
    return f"""m.residency_generation = {generation}
                  AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND t.deleted_at IS NULL
                  AND a.host_id = g.host"""


def residency_caller_sql(thread_id: str, generation: str) -> str:
    """Chat ``thread_id``'s residency ``generation`` is hot, and job ``j`` is that chat's.

    Over :data:`RESIDENCY_CALLER_FROM`. Says nothing of the attempt's own
    liveness: add :data:`CURRENT_RUNNING_ATTEMPT` or :data:`LIVE_ATTEMPT`.
    """
    return f"""m.thread_id = {thread_id}
                  AND {hot_residency_sql(generation)}
                  AND j.thread_id = m.thread_id"""


def live_residency_caller_sql(thread_id: str, generation: str) -> str:
    """The whole relay fence in one predicate, over :data:`RESIDENCY_CALLER_FROM`.

    The residency is hot and serving its admitted attempt on its own host, and
    that attempt is :data:`LIVE_ATTEMPT`.
    """
    return f"""{residency_caller_sql(thread_id, generation)}
                  AND {LIVE_ATTEMPT}"""
