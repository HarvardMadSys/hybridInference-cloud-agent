"""The statements repositories write ``agent_sandboxes`` and memberships with.

A sandbox group (``agent_sandboxes``) and its chats' memberships
(``agent_thread_sandbox_memberships``) were written from eight repository
modules. Each statement those modules execute against either table is here,
moved unchanged: the same text, the same bound parameters, in a section named
after its module. The repository method named above each one still executes
it, under the locks it takes and in the transaction it owns, so moving a
statement here changed no lock order and no transaction boundary. The text
keeps the indentation it had in its method, so it is byte-for-byte the SQL
that method sent before.

The values these statements write are the ones ``domain/sandbox_group.py``
names, and that module's transition tables record which changes of
``state`` and ``deletion_state`` they make.

The ``_sql`` helpers that already wrote these tables keep their statements
(``hosts.py``, ``sandbox.py``, ``sandbox_observations.py``): the statements
here are composed from their predicates, so those modules cannot import this
one back. ``tests/unit/test_sandbox_write_modules.py`` counts every module
that still writes either table, and the count only goes down.
"""

from __future__ import annotations

from cloud_agent.domain.sandbox_group import DeletionState, GroupState, sql_list
from cloud_agent.storage._sql.accounts import local_access_ended
from cloud_agent.storage._sql.fences import leased_attempt_sql
from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST, UNDELETED_GROUP_SQL
from cloud_agent.storage._sql.mcp_residency import mint_residency_sql
from cloud_agent.storage._sql.sandbox import (
    FENCE_START_SQL,
    group_has_live_peer,
    group_is_quiet,
    member_turn_running,
    quiet_idle_deadline,
)
from cloud_agent_shared.sandbox_restore import MAX_RESTORE_ATTEMPTS

#: A beat extends nothing for a local owner whose access has ended; the host
#: then closes the member, whether or not the termination pass reached it.
_OWNER_ACCESS_ENDED = local_access_ended(
    "(SELECT t.user_id FROM agent_threads t WHERE t.id = m.thread_id)"
)

#: A connection a residency may still run on: the owner's, live, enabled, and
#: at the revision the residency was started with. ``b`` is its generation-bound binding.
_STILL_CURRENT = """EXISTS (
    SELECT 1 FROM agent_model_connections c
    WHERE c.id = b.connection_id
      AND c.deleted_at IS NULL AND c.enabled
      AND c.credential_revision = b.credential_revision
)"""

# -- repositories/sandbox.py --------------------------------------------------

#: Executed by ``set_admin_archived``.
SET_ADMIN_ARCHIVED_GROUP_SQL = (
    "UPDATE agent_sandboxes "
    "SET archived_at = CASE WHEN $2 THEN NOW() ELSE NULL END, updated_at = NOW() "
    "WHERE id = $1"
)

#: Executed by ``request_admin_delete``.
RESUME_ADMIN_DELETION_SQL = """
                    UPDATE agent_sandboxes
                    SET deletion_state = $2, deletion_retry_started_at = NOW(),
                        deletion_retry_count = 0,
                        deletion_retry_exhausted_at = NULL, deletion_retry_at = NOW(),
                        deletion_error = NULL, deletion_claim_token = NULL,
                        deletion_claim_expires_at = NULL, updated_at = NOW()
                    WHERE id = $1
                    """

#: Executed by ``request_admin_delete``.
MEMBERS_CONFIRMED_STOPPED_SQL = """
                    UPDATE agent_thread_sandbox_memberships
                    SET resident_state = CASE
                            WHEN native_state = 'lost' THEN 'lost' ELSE 'stopped' END,
                        updated_at = NOW()
                    WHERE sandbox_id = $1
                    """

#: Executed by ``request_admin_delete``.
REQUEST_ADMIN_DELETION_SQL = f"""
                UPDATE agent_sandboxes
                SET state = CASE WHEN $3 THEN {GroupState.STOPPED.sql} ELSE state END,
                    idle_deadline = CASE WHEN $3 THEN NULL ELSE idle_deadline END,
                    stop_requested_at = CASE WHEN $3 THEN NULL ELSE stop_requested_at END,
                    resource_snapshot = CASE WHEN $3 THEN NULL ELSE resource_snapshot END,
                    resource_sampled_at = CASE WHEN $3 THEN NULL ELSE resource_sampled_at END,
                    resource_received_at = CASE WHEN $3 THEN NULL ELSE resource_received_at END,
                    terminal_count = CASE WHEN $3 THEN NULL ELSE terminal_count END,
                    terminal_count_observed_at = CASE WHEN $3 THEN NULL ELSE terminal_count_observed_at END,
                    deletion_state = {DeletionState.PENDING.sql}, deletion_request_id = $2,
                    deletion_requested_at = NOW(), deletion_retry_at = NOW(),
                    deletion_retry_started_at = NOW(), deletion_retry_count = 0,
                    deletion_retry_exhausted_at = NULL,
                    deletion_generation = generation, deletion_container_id = container_id,
                    deletion_last_activity_at = last_activity_at,
                    deletion_claim_token = NULL, deletion_claim_expires_at = NULL,
                    deletion_cleanup_required = FALSE, deletion_cleanup_archive_key = NULL,
                    deletion_discard = $4,
                    deletion_archive_key = NULL, deletion_archive_version_id = NULL,
                    deletion_sha256 = NULL, deletion_size_bytes = NULL, deletion_error = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """

#: Executed by ``claim_due_admin_sandbox_deletion``.
EXHAUST_SPENT_DELETIONS_SQL = f"""
                WITH spent AS (
                    SELECT * FROM agent_sandboxes
                    WHERE deletion_state IN {sql_list(DeletionState.PENDING, DeletionState.ARCHIVED)}
                      AND deletion_retry_exhausted_at IS NULL
                      AND (
                          deletion_retry_count >= $1
                          OR deletion_retry_started_at IS NULL
                          OR deletion_retry_started_at <= clock_timestamp()
                              - make_interval(secs => $2)
                      )
                      AND (deletion_claim_expires_at IS NULL
                           OR deletion_claim_expires_at <= clock_timestamp())
                    ORDER BY deletion_requested_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $3
                )
                UPDATE agent_sandboxes g
                SET deletion_retry_exhausted_at = NOW(), deletion_retry_at = NULL,
                    deletion_claim_token = NULL, deletion_claim_expires_at = NULL,
                    deletion_error = 'Automatic deletion retries stopped. An administrator can resume this request.',
                    updated_at = NOW()
                FROM spent s WHERE g.id = s.id
                RETURNING g.*
                """

#: Executed by ``claim_due_admin_sandbox_deletion``.
CLAIM_DUE_DELETION_SQL = f"""
                WITH due AS (
                    SELECT id FROM agent_sandboxes
                    WHERE deletion_state IN {sql_list(DeletionState.PENDING, DeletionState.ARCHIVED)}
                      AND deletion_retry_exhausted_at IS NULL
                      AND deletion_retry_count < $3
                      AND deletion_retry_started_at > clock_timestamp()
                          - make_interval(secs => $4)
                      AND generation = deletion_generation
                      AND container_id = deletion_container_id
                      AND last_activity_at = deletion_last_activity_at
                      AND (deletion_retry_at IS NULL OR deletion_retry_at <= NOW())
                      AND (deletion_claim_expires_at IS NULL OR deletion_claim_expires_at <= clock_timestamp())
                    ORDER BY deletion_retry_at, deletion_requested_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE agent_sandboxes g
                SET deletion_claim_token = $1,
                    deletion_claim_expires_at = clock_timestamp() + make_interval(secs => $2),
                    deletion_retry_count = deletion_retry_count + 1,
                    updated_at = NOW()
                FROM due WHERE g.id = due.id
                RETURNING g.id, g.user_id, g.origin_thread_id, g.host, g.generation,
                          g.container_id, g.last_activity_at, g.deletion_request_id,
                          g.deletion_state, g.deletion_discard, g.deletion_claim_token,
                          g.deletion_cleanup_required, g.deletion_cleanup_archive_key,
                          g.deletion_archive_key, g.deletion_archive_version_id,
                          g.deletion_sha256, g.deletion_size_bytes, g.deletion_retry_count,
                          g.deletion_retry_started_at
                """

#: Executed by ``record_admin_sandbox_archive``.
RECORD_ADMIN_SANDBOX_ARCHIVE_GROUP_SQL = f"""
                UPDATE agent_sandboxes
                SET deletion_state = {DeletionState.ARCHIVED.sql}, deletion_archive_key = $2,
                    deletion_sha256 = $3, deletion_size_bytes = $4,
                    deletion_archive_version_id = $5, deletion_error = NULL, updated_at = NOW()
                WHERE id = $1
                """

#: Executed by ``renew_admin_sandbox_deletion_claim``.
RENEW_ADMIN_SANDBOX_DELETION_CLAIM_GROUP_SQL = (
    "UPDATE agent_sandboxes "
    "SET deletion_claim_expires_at=clock_timestamp()+make_interval(secs => $2), "
    "deletion_cleanup_required=deletion_cleanup_required OR $3, "
    "deletion_cleanup_archive_key=CASE WHEN $3 "
    "THEN COALESCE(deletion_cleanup_archive_key,$4) "
    "ELSE deletion_cleanup_archive_key END WHERE id=$1"
)

#: Executed by ``complete_admin_sandbox_deletion``.
COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL = f"""
                UPDATE agent_sandboxes
                SET state = {GroupState.LOST.sql}, deletion_state = {DeletionState.DELETED.sql}, deleted_at = NOW(),
                    archived_at = COALESCE(archived_at, NOW()), deletion_error = NULL,
                    idle_deadline = NULL, stop_requested_at = NULL,
                    deletion_retry_at = NULL, deletion_claim_expires_at = NULL,
                    deletion_retry_exhausted_at = NULL,
                    resource_snapshot = NULL, resource_sampled_at = NULL,
                    resource_received_at = NULL, terminal_count = NULL,
                    terminal_count_observed_at = NULL, updated_at = NOW()
                WHERE id = $1
                """

#: Executed by ``complete_admin_sandbox_deletion``.
COMPLETE_ADMIN_SANDBOX_DELETION_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET native_state = 'lost', context_status = 'lost', resident_state = 'lost',
                    updated_at = NOW()
                WHERE sandbox_id = $1
                """

#: Executed by ``_complete_admin_sandbox_discard_in_transaction``.
COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL = f"""
            UPDATE agent_sandboxes
            SET state = {GroupState.LOST.sql}, deletion_state = {DeletionState.DELETED.sql}, deleted_at = NOW(),
                deletion_error = NULL, idle_deadline = NULL, stop_requested_at = NULL,
                deletion_retry_at = NULL, deletion_claim_expires_at = NULL,
                deletion_retry_exhausted_at = NULL,
                resource_snapshot = NULL, resource_sampled_at = NULL,
                resource_received_at = NULL, terminal_count = NULL,
                terminal_count_observed_at = NULL, updated_at = NOW()
            WHERE id = $1
            """

#: Executed by ``_complete_admin_sandbox_discard_in_transaction``.
COMPLETE_ADMIN_SANDBOX_DISCARD_MEMBERS_SQL = """
            UPDATE agent_thread_sandbox_memberships
            SET native_state = 'lost', context_status = 'lost', resident_state = 'lost',
                updated_at = NOW()
            WHERE sandbox_id = $1
            """

#: Executed by ``fail_admin_sandbox_deletion``.
FAIL_ADMIN_SANDBOX_DELETION_GROUP_SQL = """
                UPDATE agent_sandboxes
                SET deletion_state = CASE WHEN $2 OR $5 THEN deletion_state ELSE 'failed' END,
                    deletion_retry_exhausted_at = CASE WHEN $5 THEN NOW() ELSE NULL END,
                    deletion_error = $3, deletion_claim_token = NULL,
                    deletion_claim_expires_at = NULL,
                    deletion_retry_at = CASE WHEN $2
                        THEN clock_timestamp() + make_interval(secs => $4) END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING deletion_retry_at
                """

#: Executed by ``record_terminal_observation``.
RECORD_TERMINAL_OBSERVATION_GROUP_SQL = f"""
                    UPDATE agent_sandboxes
                    SET terminal_count = $5, terminal_count_observed_at = NOW()
                    WHERE id = $1 AND host = $2 AND generation = $3
                      AND container_id = $4 AND state = {GroupState.RUNNING.sql}
                    RETURNING TRUE
                    """

#: Executed by ``request_admin_stop``, ``request_deleted_sessions_stop`` and
#: ``request_move_out_stop``.
REQUEST_GROUP_STOP_SQL = (
    f"UPDATE agent_sandboxes SET state = {GroupState.STOPPING.sql}, "
    "stop_requested_at = NOW(), idle_deadline = NULL, updated_at = NOW() WHERE id = $1"
)

#: Executed by ``mark_thread_sandbox_running``.
MARK_THREAD_SANDBOX_RUNNING_GROUP_AND_MEMBERS_SQL = f"""
                WITH updated_group AS (
                    UPDATE agent_sandboxes g
                    SET state = {GroupState.RUNNING.sql}, container_id = $6, archived_at = NULL,
                        restore_checkpoint_id = NULL, restore_attempts = 0,
                        restore_attempt_id = NULL,
                        last_activity_at = NOW(), updated_at = NOW()
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_attempts a ON a.id = m.active_attempt_id
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                      AND g.generation = $4 AND g.stop_requested_at IS NULL
                      AND {UNDELETED_GROUP_SQL}
                      AND m.activation_generation = $5
                      AND m.active_attempt_id = $2
                      AND (g.container_id IS NULL OR g.container_id = $6)
                      AND {leased_attempt_sql("$2", "$3")}
                      AND j.current_attempt_id = a.id AND j.thread_id = m.thread_id
                    RETURNING g.id
                )
                UPDATE agent_thread_sandbox_memberships m
                SET context_status = $7,
                    resident_state = CASE
                        WHEN m.resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM updated_group u
                WHERE m.thread_id = $1 AND m.sandbox_id = u.id
                RETURNING m.thread_id
                """

#: Executed by ``record_thread_sandbox_process``.
RECORD_THREAD_SANDBOX_PROCESS_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships m
                SET process_generation = $7, resident_state = 'live', updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4 AND g.stop_requested_at IS NULL
                  AND {UNDELETED_GROUP_SQL}
                  AND m.activation_generation = $5
                  AND m.residency_generation = $6 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND m.active_attempt_id = $2
                  AND m.process_generation <= $7
                  AND m.native_state <> 'lost' AND m.context_status <> 'lost'
                  AND (
                      m.process_generation < $7
                      OR m.resident_state IN ('starting', 'restarting', 'live')
                  )
                  AND {leased_attempt_sql("$2", "$3")}
                RETURNING m.thread_id
                """

#: Executed by ``record_shared_thread_residency``.
RECORD_SHARED_THREAD_RESIDENCY_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships m
                SET resident_state = CASE
                        WHEN resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND m.sandbox_id = $4
                  AND g.id = m.sandbox_id
                  AND m.residency_generation = $5 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND m.active_attempt_id = $2
                  AND {leased_attempt_sql("$2", "$3")}
                RETURNING m.thread_id
                """

#: Executed by ``admit_thread_sandbox_turn``.
ADMIT_THREAD_SANDBOX_TURN_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships m
                SET admitted_job_id = $7,
                    admitted_at = COALESCE(m.admitted_at, NOW()),
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a, agent_jobs j, agent_threads t
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND t.id = m.thread_id AND t.deleted_at IS NULL
                  AND g.generation = $4 AND g.stop_requested_at IS NULL
                  AND {UNDELETED_GROUP_SQL}
                  AND m.activation_generation = $5
                  AND m.residency_generation = $6 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND m.active_attempt_id = $2
                  AND {leased_attempt_sql("$2", "$3")}
                  AND j.id = $7 AND j.current_attempt_id = a.id
                  AND j.thread_id = m.thread_id AND j.state = 'running'
                  AND (m.admitted_job_id IS NULL OR m.admitted_job_id = $7)
                RETURNING m.thread_id
                """

#: Executed by ``admit_thread_sandbox_turn``.
ADMIT_THREAD_SANDBOX_TURN_GROUP_SQL = """
                UPDATE agent_sandboxes g
                SET idle_deadline = NULL, last_activity_at = NOW(), updated_at = NOW()
                FROM agent_thread_sandbox_memberships m
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                """

#: Executed by ``settle_thread_sandbox``.
SETTLE_THREAD_SANDBOX_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL, last_settled_job_id = $7,
                    context_status = $8,
                    native_session_id = COALESCE($9, m.native_session_id),
                    native_state = 'ready', resident_state = 'live',
                    last_error = NULL, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.residency_generation = $6 AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND m.active_attempt_id = $2
                  AND m.admitted_job_id = $7
                  AND {leased_attempt_sql("$2", "$3")}
                  AND a.job_id = $7
                RETURNING m.sandbox_id
                """

#: Executed by ``settle_thread_sandbox``.
SETTLE_THREAD_SANDBOX_GROUP_SQL = f"""
                    UPDATE agent_sandboxes g
                    SET state = {GroupState.RUNNING.sql}, last_activity_at = NOW(),
                        idle_timeout_seconds = $2,
                        idle_deadline = {quiet_idle_deadline("g", "$2")},
                        workspace_epoch = workspace_epoch + 1,
                        published_base_sha = COALESCE(g.published_base_sha, $3),
                        updated_at = NOW()
                    WHERE g.id = $1
                    RETURNING g.workspace_epoch, g.published_base_sha
                    """

#: Executed by ``record_sandbox_checkpoint``.
RECORD_SANDBOX_CHECKPOINT_GROUP_SQL = f"""
                    UPDATE agent_sandboxes
                    SET restore_checkpoint_id = $2, updated_at = NOW()
                    WHERE id = $1 AND state = {GroupState.STOPPED.sql} AND start_seq = $3
                    """

#: Executed by ``abandon_thread_sandbox``.
ABANDON_THREAD_SANDBOX_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL,
                    context_status = CASE WHEN $6 THEN 'lost' ELSE m.context_status END,
                    native_state = CASE WHEN $6 THEN 'lost' ELSE m.native_state END,
                    resident_state = CASE
                        WHEN $6 THEN 'lost'
                        WHEN m.native_session_id IS NULL
                             OR m.native_session_id !~ '[^[:space:]]'
                            THEN 'stopped'
                        ELSE 'restarting'
                    END,
                    last_error = $7, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND (
                        ($6 AND (m.admitted_job_id IS NULL OR m.admitted_job_id = a.job_id))
                        OR (NOT $6 AND m.admitted_job_id IS NULL)
                  )
                  AND a.id = $2 AND a.lease_generation = $3
                RETURNING m.sandbox_id
                """

#: Executed by ``abandon_thread_sandbox``.
ABANDON_THREAD_SANDBOX_GROUP_SQL = f"""
                UPDATE agent_sandboxes g
                SET state = CASE
                        -- A failed restore leaves the group in its checkpoint,
                        -- for another claim, until the attempts run out.
                        WHEN g.state = {GroupState.RESTORING.sql}
                         AND ($3 OR g.restore_attempts >= {MAX_RESTORE_ATTEMPTS}) THEN {GroupState.LOST.sql}
                        WHEN g.state = {GroupState.RESTORING.sql} THEN {GroupState.COLD.sql}
                        WHEN $3 AND g.container_id IS NULL THEN {GroupState.LOST.sql}
                        WHEN g.container_id IS NULL THEN {GroupState.PROVISIONING.sql}
                        WHEN g.active_operation_count > 0 OR {member_turn_running("g")} THEN {GroupState.RUNNING.sql}
                        WHEN {group_has_live_peer("g", excluding="$5")} THEN {GroupState.RUNNING.sql}
                        ELSE {GroupState.STOPPING.sql}
                    END,
                    last_activity_at = NOW(),
                    idle_timeout_seconds = $2,
                    idle_deadline = CASE
                        WHEN g.container_id IS NOT NULL
                         AND {group_is_quiet("g")}
                         AND {group_has_live_peer("g", excluding="$5")}
                        THEN clock_timestamp() + make_interval(secs => $2)
                        ELSE NULL
                    END,
                    restore_attempt_id = CASE
                        WHEN g.state = {GroupState.RESTORING.sql} THEN NULL ELSE g.restore_attempt_id
                    END,
                    last_error = CASE
                        WHEN $3 OR g.state = {GroupState.RESTORING.sql} THEN $4 ELSE g.last_error
                    END,
                    updated_at = NOW()
                WHERE g.id = $1
                RETURNING g.state
                """

#: Executed by ``_reserve_shared_thread_sandbox``.
INSERT_SANDBOX_GROUP_SQL = """
                INSERT INTO agent_sandboxes (
                    id, origin_thread_id, user_id, host, container_name,
                    runtime_id, runtime_version, sandbox_image_digest,
                    execution_profile, resident_slot_capacity, memory_limit,
                    cpu_limit, stop_timeout_seconds, fork_helper_version, disk_limit_mib
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """

#: Executed by ``_reserve_shared_thread_sandbox``.
INSERT_SANDBOX_MEMBERSHIP_SQL = """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id
                ) VALUES ($1, $2, $3, 0)
                """

#: Executed by ``_reserve_shared_thread_sandbox``.
RESERVE_ACTIVATE_MEMBER_SQL = """
            UPDATE agent_thread_sandbox_memberships
            SET active_attempt_id = $2, activation_generation = $3,
                context_status = $4, resident_state = CASE
                    WHEN resident_state = 'live' THEN resident_state ELSE 'starting' END,
                last_error = NULL, updated_at = NOW()
            WHERE thread_id = $1
            """

#: Executed by ``_reserve_shared_thread_sandbox``.
RESERVE_ACTIVATE_GROUP_SQL = f"""
            UPDATE agent_sandboxes
            SET state = CASE
                    WHEN state IN {sql_list(GroupState.RUNNING, GroupState.RESTORING)} THEN state
                    ELSE {GroupState.STARTING.sql}
                END,
                {FENCE_START_SQL},
                idle_deadline = NULL, archived_at = NULL,
                last_activity_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """

#: Executed by ``_reconcile_shared_sandbox_observation``.
STOP_ORPHANED_PROVISIONING_GROUP_SQL = f"""
                    UPDATE agent_sandboxes
                    SET state = CASE WHEN $2 THEN {GroupState.STOPPING.sql} ELSE {GroupState.STOPPED.sql} END,
                        idle_deadline = NULL, updated_at = NOW()
                    WHERE id = $1
                    RETURNING state
                    """

#: Executed by ``_reconcile_shared_sandbox_observation``.
STOP_LOST_ONLY_GROUP_SQL = f"""
                    UPDATE agent_sandboxes
                    SET state = CASE
                            WHEN state IN {sql_list(GroupState.LOST, GroupState.STOPPED_DIRTY)} THEN state
                            ELSE {GroupState.STOPPING.sql}
                        END,
                        idle_deadline = NULL, updated_at = NOW()
                    WHERE id = $1
                      AND (state NOT IN {sql_list(GroupState.LOST, GroupState.STOPPED_DIRTY, GroupState.STOPPING)}
                           OR idle_deadline IS NOT NULL)
                    RETURNING state
                    """

#: Executed by ``_record_terminal_observation``.
RECORD_TERMINAL_COUNT_SQL = """
            UPDATE agent_sandboxes
            SET terminal_count = $2,
                terminal_count_observed_at = CASE WHEN $2::integer IS NULL THEN NULL ELSE NOW() END
            WHERE id = $1
            """

#: Executed by ``_record_resource_observation``.
CLEAR_RESOURCE_SAMPLE_SQL = """
                UPDATE agent_sandboxes
                SET resource_snapshot = NULL, resource_sampled_at = NULL,
                    resource_received_at = NULL
                WHERE id = $1
                """

#: Executed by ``_record_resource_observation``.
RECORD_RESOURCE_SAMPLE_SQL = """
            WITH previous AS (
                SELECT resource_sampled_at FROM agent_sandboxes WHERE id = $1
            )
            UPDATE agent_sandboxes
            SET resource_snapshot = $2::jsonb,
                resource_sampled_at = NOW() - make_interval(secs => $3),
                resource_received_at = NOW()
            WHERE id = $1
              AND resource_snapshot->>'sample_id' IS DISTINCT FROM $4
              AND (resource_sampled_at IS NULL
                   OR resource_sampled_at < NOW() - make_interval(secs => $3))
            RETURNING user_id, resource_sampled_at,
                      (SELECT resource_sampled_at FROM previous) AS previous_sampled_at
            """


# -- repositories/session_fork.py ---------------------------------------------

#: Executed by ``prepare_session_fork``.
PREPARE_SESSION_FORK_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET resident_slot_id = NULL, updated_at = NOW()
                WHERE thread_id IN (
                    SELECT m.thread_id
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_threads t ON t.id = m.thread_id
                    WHERE m.sandbox_id = $1
                      AND m.resident_slot_id IS NOT NULL
                      AND t.deleted_at IS NOT NULL
                      AND m.resident_state IN ('stopped', 'lost')
                      AND m.residency_status = 'none'
                      AND m.active_attempt_id IS NULL
                      AND m.fork_operation_id IS NULL
                    FOR UPDATE OF m SKIP LOCKED
                )
                """

#: Executed by ``prepare_session_fork``.
PREPARE_SESSION_FORK_MEMBERS_2_SQL = """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id,
                    forked_from_thread_id, fork_anchor_job_id, fork_operation_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """

#: Executed by ``prepare_session_fork``.
PREPARE_SESSION_FORK_MEMBERS_3_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET native_state = 'fork_preparing', fork_operation_id = $2,
                    updated_at = NOW()
                WHERE thread_id = $1
                """

#: Executed by ``prepare_session_fork``.
PREPARE_SESSION_FORK_GROUP_SQL = f"""
                UPDATE agent_sandboxes
                SET active_operation_count = active_operation_count + 1,
                    {FENCE_START_SQL},
                    idle_deadline = NULL, archived_at = NULL,
                    last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """

#: Executed by ``establish_session_fork_residencies``.
ESTABLISH_SESSION_FORK_RESIDENCIES_MEMBERS_SQL = f"""
                    UPDATE agent_thread_sandbox_memberships m
                    SET {mint_residency_sql("$3")},
                        updated_at = NOW()
                    FROM agent_sandboxes g
                    WHERE m.thread_id = $1 AND m.sandbox_id = $2
                      AND g.id = m.sandbox_id AND g.generation = $4
                    RETURNING m.residency_generation AS generation
                    """

#: Executed by ``establish_session_fork_residencies``.
ESTABLISH_SESSION_FORK_RESIDENCIES_MEMBERS_2_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET resident_state = 'restarting', updated_at = NOW()
                WHERE thread_id = $1 AND residency_generation = $2
                  AND fork_operation_id = $3
                """

#: Executed by ``record_session_fork_materialized``.
RECORD_SESSION_FORK_MATERIALIZED_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET process_generation = $2, residency_generation = $3,
                    resident_state = 'live', updated_at = NOW()
                WHERE thread_id = $1 AND fork_operation_id = $4
                """

#: Executed by ``record_session_fork_materialized``.
RECORD_SESSION_FORK_MATERIALIZED_GROUP_SQL = f"""
                UPDATE agent_sandboxes
                SET state = {GroupState.RUNNING.sql}, archived_at = NULL,
                    last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND generation = $2 AND container_id = $3
                """

#: Executed by ``finalize_session_fork``.
FINALIZE_SESSION_FORK_MEMBERS_SQL = """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_session_id = $3, native_state = 'ready',
                        context_status = 'live', process_generation = $4,
                        residency_generation = $5, resident_state = 'live',
                        resident_slot_id = $6, last_settled_job_id = $7,
                        fork_operation_id = NULL, last_error = NULL,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND sandbox_id = $2 AND fork_operation_id = $8
                    """

#: Executed by ``finalize_session_fork``.
FINALIZE_SESSION_FORK_MEMBERS_2_SQL = """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = 'ready', fork_operation_id = NULL,
                        process_generation = $2, residency_generation = $3,
                        resident_state = 'live', updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $4
                    """

#: Executed by ``finalize_session_fork``.
FINALIZE_SESSION_FORK_GROUP_SQL = f"""
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        last_activity_at = NOW(),
                        idle_deadline = {quiet_idle_deadline("g", "idle_timeout_seconds", operations=1)},
                        updated_at = NOW()
                    WHERE g.id = $1
                    """

#: Executed by ``reap_stalled_session_forks``.
REAP_STALLED_SESSION_FORKS_MEMBERS_SQL = """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = CASE
                            WHEN context_status = 'lost' THEN 'lost' ELSE 'ready' END,
                        fork_operation_id = NULL,
                        resident_state = CASE
                            WHEN context_status = 'lost' THEN 'lost'
                            WHEN native_session_id IS NULL
                                 OR native_session_id !~ '[^[:space:]]'
                                THEN 'stopped'
                            ELSE 'restarting'
                        END,
                        last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """

#: Executed by ``reap_stalled_session_forks``.
REAP_STALLED_SESSION_FORKS_GROUP_SQL = f"""
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        idle_deadline = {quiet_idle_deadline("g", "idle_timeout_seconds", operations=1)},
                        updated_at = NOW()
                    WHERE id = $1
                    """

#: Executed by ``reap_stalled_session_forks``.
REAP_STALLED_SESSION_FORKS_MEMBERS_2_SQL = """
                        UPDATE agent_thread_sandbox_memberships m
                        SET residency_status = 'none', residency_last_active_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $1 AND m.residency_generation = $2
                          AND g.id = m.sandbox_id AND g.host = $3
                          AND m.residency_status <> 'none'
                        RETURNING m.thread_id, m.residency_generation AS generation,
                                  m.residency_grant_id AS grant_id
                        """

#: Executed by ``reap_stalled_session_forks``.
REAP_STALLED_SESSION_FORKS_MEMBERS_3_SQL = """
                    UPDATE agent_thread_sandbox_memberships
                    SET resident_slot_id = NULL, native_state = 'lost',
                        context_status = 'lost', resident_state = 'lost',
                        fork_operation_id = NULL, last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """


# -- repositories/attempt_commands.py -----------------------------------------

#: Executed by ``claim_job``.
CLAIM_COLD_GROUP_FOR_RESTORE_SQL = f"""
                    UPDATE agent_sandboxes
                    SET state = {GroupState.RESTORING.sql}, host = $2, generation = generation + 1,
                        container_id = NULL, restore_attempts = restore_attempts + 1,
                        idle_deadline = NULL, last_error = NULL, updated_at = NOW()
                    WHERE id = $1 AND state = {GroupState.COLD.sql}
                    """

#: Executed by ``claim_job``.
SET_RESTORE_ATTEMPT_SQL = "UPDATE agent_sandboxes SET restore_attempt_id = $2 WHERE id = $1"

#: Executed by ``release_claim``.
RELEASE_CLAIM_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL,
                    resident_state = CASE
                        WHEN m.native_session_id IS NULL
                             OR m.native_session_id !~ '[^[:space:]]'
                            THEN 'stopped'
                        ELSE 'restarting'
                    END,
                    updated_at = NOW()
                FROM agent_sandboxes g
                WHERE m.active_attempt_id = $1 AND g.id = m.sandbox_id
                  AND g.restore_attempt_id IS DISTINCT FROM $1
                """

#: Executed by ``transition``.
TRANSITION_MEMBERS_SQL = """
                        UPDATE agent_thread_sandbox_memberships m
                        SET context_status = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.context_status
                            END,
                            native_state = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.native_state
                            END,
                            resident_state = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.resident_state
                                WHEN m.admitted_job_id = $1 OR g.container_id IS NULL
                                    THEN 'lost'
                                WHEN m.native_session_id IS NULL
                                     OR m.native_session_id !~ '[^[:space:]]'
                                    THEN 'stopped'
                                ELSE 'restarting'
                            END,
                            active_attempt_id = CASE
                                WHEN m.active_attempt_id = $2 THEN NULL
                                ELSE m.active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_at
                            END,
                            last_error = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.last_error
                                ELSE COALESCE($4, 'retained turn ended before settlement')
                            END,
                            updated_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $3 AND g.id = m.sandbox_id
                          AND (
                                m.active_attempt_id = $2
                                OR m.admitted_job_id = $1
                          )
                        RETURNING m.context_status, m.sandbox_id,
                                  m.last_settled_job_id = $1 AS settled
                        """

#: Executed by ``transition``.
TRANSITION_GROUP_SQL = f"""
                            UPDATE agent_sandboxes g
                            SET state = CASE
                                    WHEN $3 AND g.container_id IS NULL THEN {GroupState.LOST.sql}
                                    WHEN $2 THEN {GroupState.RUNNING.sql}
                                    WHEN g.active_operation_count > 0 OR {member_turn_running("g")} THEN {GroupState.RUNNING.sql}
                                    WHEN {group_has_live_peer("g", excluding="$4")} THEN {GroupState.RUNNING.sql}
                                    ELSE {GroupState.STOPPING.sql}
                                END,
                                last_activity_at = NOW(),
                                idle_deadline = CASE
                                    WHEN (
                                        $2 OR {group_has_live_peer("g", excluding="$4")}
                                     )
                                     AND {group_is_quiet("g")}
                                    THEN COALESCE(
                                        g.idle_deadline,
                                        clock_timestamp() + make_interval(secs => g.idle_timeout_seconds)
                                    )
                                    ELSE NULL
                                END,
                                updated_at = NOW()
                            WHERE g.id = $1
                            """

#: Executed by ``_reap_attempt``.
REAP_ATTEMPT_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET native_state = 'lost', context_status = 'lost',
                    resident_state = 'lost',
                    active_attempt_id = CASE
                        WHEN active_attempt_id = $2 THEN NULL
                        ELSE active_attempt_id
                    END,
                    admitted_job_id = CASE
                        WHEN admitted_job_id = $1 THEN NULL
                        ELSE admitted_job_id
                    END,
                    admitted_at = CASE
                        WHEN admitted_job_id = $1 THEN NULL
                        ELSE admitted_at
                    END,
                    last_error = CASE
                        WHEN admitted_job_id = $1
                            THEN 'attempt lease expired after prompt admission'
                        ELSE 'retained activation lease expired'
                    END,
                    updated_at = NOW()
                WHERE thread_id = $3 AND sandbox_id = $4
                  AND last_settled_job_id IS DISTINCT FROM $1
                  AND (active_attempt_id = $2 OR admitted_job_id = $1)
                RETURNING residency_generation
                """

#: Executed by ``_reap_attempt``.
REAP_ATTEMPT_GROUP_SQL = f"""
                    UPDATE agent_sandboxes g
                    SET state = CASE
                            WHEN g.container_id IS NULL THEN {GroupState.LOST.sql}
                            WHEN g.active_operation_count > 0 OR {member_turn_running("g")} THEN {GroupState.RUNNING.sql}
                            WHEN {group_has_live_peer("g")} THEN {GroupState.RUNNING.sql}
                            ELSE {GroupState.STOPPING.sql}
                        END,
                        last_activity_at = NOW(),
                        idle_deadline = CASE
                            WHEN g.container_id IS NOT NULL
                             AND {group_is_quiet("g")}
                             AND {group_has_live_peer("g")}
                            THEN clock_timestamp() + make_interval(
                                secs => g.idle_timeout_seconds
                            )
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE g.id = $1
                    """

#: Executed by ``_reap_attempt``.
REAP_ATTEMPT_MEMBERS_2_SQL = """
                        UPDATE agent_thread_sandbox_memberships m
                        SET residency_status = 'none',
                            residency_last_active_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $1 AND m.residency_generation = $2
                          AND m.residency_status <> 'none'
                          AND g.id = m.sandbox_id
                        RETURNING m.thread_id,
                                  m.residency_generation AS generation,
                                  g.host, m.residency_grant_id AS grant_id
                        """

#: Executed by ``_reap_attempt``.
REAP_ATTEMPT_MEMBERS_3_SQL = """
                UPDATE agent_thread_sandbox_memberships
                SET active_attempt_id = CASE
                        WHEN active_attempt_id = $2 THEN NULL
                        ELSE active_attempt_id
                    END,
                    admitted_job_id = CASE
                        WHEN admitted_job_id = $1 THEN NULL
                        ELSE admitted_job_id
                    END,
                    admitted_at = CASE
                        WHEN admitted_job_id = $1 THEN NULL
                        ELSE admitted_at
                    END,
                    last_error = 'job finalization expired after context settlement',
                    updated_at = NOW()
                WHERE thread_id = $3
                  AND last_settled_job_id = $1
                """


# -- repositories/residency.py ------------------------------------------------

#: Executed by ``establish_residency``.
ESTABLISH_RESIDENCY_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships AS m
                SET {mint_residency_sql("$2")},
                    updated_at = NOW()
                FROM agent_jobs j, agent_attempts a, agent_sandboxes g, agent_threads t
                WHERE m.thread_id = $1 AND m.active_attempt_id = $3
                  AND t.id = m.thread_id AND t.deleted_at IS NULL
                  AND g.id = m.sandbox_id AND g.host = $4
                  AND ($6::text IS NULL OR m.sandbox_id = $6)
                  AND j.thread_id = m.thread_id
                  AND j.current_attempt_id = a.id
                  AND {leased_attempt_sql("$3", "$5")}
                  AND a.job_id = j.id AND a.host_id = $4
                  AND {LIVE_ATTEMPT_HOST}
                  AND NOT {local_access_ended("j.user_id")}
                RETURNING m.thread_id, m.sandbox_id,
                          m.residency_generation AS generation,
                          m.residency_status AS status,
                          m.residency_lease_expires_at AS lease_expires_at,
                          m.residency_grant_id AS grant_id,
                          m.residency_binding_id AS ledger_binding_id,
                          m.residency_established_at AS established_at,
                          m.residency_last_active_at AS last_active_at
                """

#: Executed by ``extend``.
EXTEND_MEMBERS_SQL = f"""
                    UPDATE agent_thread_sandbox_memberships m
                    SET residency_lease_expires_at =
                            clock_timestamp() + make_interval(secs => $3),
                        residency_last_active_at = NOW()
                    FROM unnest($1::text[], $2::bigint[]) AS beat(thread_id, generation),
                         agent_sandboxes g
                    WHERE m.thread_id = beat.thread_id
                      AND m.residency_generation = beat.generation
                      AND m.residency_status = 'hot'
                      AND m.residency_lease_expires_at > clock_timestamp()
                      AND g.id = m.sandbox_id AND ($4::text IS NULL OR g.host = $4)
                      AND NOT {_OWNER_ACCESS_ENDED}
                    RETURNING m.thread_id, m.residency_generation,
                              COALESCE(m.residency_grant_id, '') AS grant_id
                    """

#: Executed by ``release_residency``.
RELEASE_RESIDENCY_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id AND ($3::text IS NULL OR g.host = $3)
                RETURNING COALESCE(m.residency_grant_id, '')
                """

#: Executed by ``release_shared_residency_if_quiescent``.
RELEASE_SHARED_RESIDENCY_IF_QUIESCENT_MEMBERS_SQL = """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.sandbox_id = $4 AND g.id = m.sandbox_id AND g.host = $3
                  AND m.residency_status <> 'none'
                  AND m.process_generation <= $5
                  AND m.resident_state IN ('stopped', 'lost')
                  AND m.active_attempt_id IS NULL
                  AND g.active_operation_count = 0
                RETURNING COALESCE(m.residency_grant_id, '')
                """

#: Executed by ``reap_expired_residencies``.
REAP_EXPIRED_RESIDENCIES_MEMBERS_SQL = """
                WITH expired AS (
                    SELECT m.thread_id, g.host
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_sandboxes g ON g.id = m.sandbox_id
                    WHERE m.residency_status = 'hot'
                      AND m.residency_lease_expires_at <= clock_timestamp()
                    ORDER BY m.residency_lease_expires_at, m.thread_id
                    LIMIT $1
                    FOR UPDATE OF m SKIP LOCKED
                )
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none'
                FROM expired
                WHERE m.thread_id = expired.thread_id
                RETURNING m.thread_id, m.residency_generation AS generation, expired.host,
                          m.residency_grant_id AS grant_id
                """


# -- repositories/compute/move_out.py -----------------------------------------

#: Executed by ``_lose_reclaimed_group``.
LOSE_RECLAIMED_GROUP_SQL = f"""UPDATE agent_sandboxes
                   SET state={GroupState.LOST.sql}, idle_deadline=NULL, stop_requested_at=NULL,
                       last_error='the server holding this sandbox was reclaimed by its provider',
                       updated_at=NOW() WHERE id=$1"""

#: Executed by ``_lose_reclaimed_group``.
LOSE_RECLAIMED_GROUP_MEMBERS_SQL = """UPDATE agent_thread_sandbox_memberships
                   SET native_state='lost', context_status='lost', resident_state='lost',
                       updated_at=NOW()
                   WHERE sandbox_id=$1"""


# -- repositories/conversation_commands.py ------------------------------------

#: Executed by ``delete_sessions`` and by ``owner_work.py``'s ``end_owner_work``.
RELEASE_THREADS_RESIDENCIES_SQL = """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = ANY($1::text[]) AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          COALESCE(m.residency_grant_id, '') AS grant_id
                """


# -- repositories/model_connections.py ----------------------------------------

#: Executed by ``release_stale_residencies``.
RELEASE_STALE_RESIDENCIES_MEMBERS_SQL = f"""
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g, agent_threads t, agent_model_connection_residencies b
                WHERE b.thread_id = m.thread_id
                  AND b.residency_generation = m.residency_generation
                  AND b.connection_id = $1
                  AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id
                  AND t.id = m.thread_id AND t.user_id = $2
                  AND NOT {_STILL_CURRENT}
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          COALESCE(m.residency_grant_id, '') AS grant_id
                """


__all__ = [
    "ABANDON_THREAD_SANDBOX_GROUP_SQL",
    "ABANDON_THREAD_SANDBOX_MEMBERS_SQL",
    "ADMIT_THREAD_SANDBOX_TURN_GROUP_SQL",
    "ADMIT_THREAD_SANDBOX_TURN_MEMBERS_SQL",
    "CLAIM_COLD_GROUP_FOR_RESTORE_SQL",
    "CLAIM_DUE_DELETION_SQL",
    "CLEAR_RESOURCE_SAMPLE_SQL",
    "COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL",
    "COMPLETE_ADMIN_SANDBOX_DELETION_MEMBERS_SQL",
    "COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL",
    "COMPLETE_ADMIN_SANDBOX_DISCARD_MEMBERS_SQL",
    "ESTABLISH_RESIDENCY_MEMBERS_SQL",
    "ESTABLISH_SESSION_FORK_RESIDENCIES_MEMBERS_2_SQL",
    "ESTABLISH_SESSION_FORK_RESIDENCIES_MEMBERS_SQL",
    "EXHAUST_SPENT_DELETIONS_SQL",
    "EXTEND_MEMBERS_SQL",
    "FAIL_ADMIN_SANDBOX_DELETION_GROUP_SQL",
    "FINALIZE_SESSION_FORK_GROUP_SQL",
    "FINALIZE_SESSION_FORK_MEMBERS_2_SQL",
    "FINALIZE_SESSION_FORK_MEMBERS_SQL",
    "INSERT_SANDBOX_GROUP_SQL",
    "INSERT_SANDBOX_MEMBERSHIP_SQL",
    "LOSE_RECLAIMED_GROUP_MEMBERS_SQL",
    "LOSE_RECLAIMED_GROUP_SQL",
    "MARK_THREAD_SANDBOX_RUNNING_GROUP_AND_MEMBERS_SQL",
    "MEMBERS_CONFIRMED_STOPPED_SQL",
    "PREPARE_SESSION_FORK_GROUP_SQL",
    "PREPARE_SESSION_FORK_MEMBERS_2_SQL",
    "PREPARE_SESSION_FORK_MEMBERS_3_SQL",
    "PREPARE_SESSION_FORK_MEMBERS_SQL",
    "REAP_ATTEMPT_GROUP_SQL",
    "REAP_ATTEMPT_MEMBERS_2_SQL",
    "REAP_ATTEMPT_MEMBERS_3_SQL",
    "REAP_ATTEMPT_MEMBERS_SQL",
    "REAP_EXPIRED_RESIDENCIES_MEMBERS_SQL",
    "REAP_STALLED_SESSION_FORKS_GROUP_SQL",
    "REAP_STALLED_SESSION_FORKS_MEMBERS_2_SQL",
    "REAP_STALLED_SESSION_FORKS_MEMBERS_3_SQL",
    "REAP_STALLED_SESSION_FORKS_MEMBERS_SQL",
    "RECORD_ADMIN_SANDBOX_ARCHIVE_GROUP_SQL",
    "RECORD_RESOURCE_SAMPLE_SQL",
    "RECORD_SANDBOX_CHECKPOINT_GROUP_SQL",
    "RECORD_SESSION_FORK_MATERIALIZED_GROUP_SQL",
    "RECORD_SESSION_FORK_MATERIALIZED_MEMBERS_SQL",
    "RECORD_SHARED_THREAD_RESIDENCY_MEMBERS_SQL",
    "RECORD_TERMINAL_COUNT_SQL",
    "RECORD_TERMINAL_OBSERVATION_GROUP_SQL",
    "RECORD_THREAD_SANDBOX_PROCESS_MEMBERS_SQL",
    "RELEASE_CLAIM_MEMBERS_SQL",
    "RELEASE_RESIDENCY_MEMBERS_SQL",
    "RELEASE_SHARED_RESIDENCY_IF_QUIESCENT_MEMBERS_SQL",
    "RELEASE_STALE_RESIDENCIES_MEMBERS_SQL",
    "RELEASE_THREADS_RESIDENCIES_SQL",
    "RENEW_ADMIN_SANDBOX_DELETION_CLAIM_GROUP_SQL",
    "REQUEST_ADMIN_DELETION_SQL",
    "REQUEST_GROUP_STOP_SQL",
    "RESERVE_ACTIVATE_GROUP_SQL",
    "RESERVE_ACTIVATE_MEMBER_SQL",
    "RESUME_ADMIN_DELETION_SQL",
    "SETTLE_THREAD_SANDBOX_GROUP_SQL",
    "SETTLE_THREAD_SANDBOX_MEMBERS_SQL",
    "SET_ADMIN_ARCHIVED_GROUP_SQL",
    "SET_RESTORE_ATTEMPT_SQL",
    "STOP_LOST_ONLY_GROUP_SQL",
    "STOP_ORPHANED_PROVISIONING_GROUP_SQL",
    "TRANSITION_GROUP_SQL",
    "TRANSITION_MEMBERS_SQL",
]
