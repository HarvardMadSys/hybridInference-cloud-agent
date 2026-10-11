"""SQL projections for the retained sandbox read shape.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

import json

from cloud_agent_shared.shared_protocol import (
    FORK_HELPER,
    MID_TURN_INPUT_CAPABILITY_PREFIX,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
)

NATIVE_FORK_CAPABILITY_JSON = json.dumps([NATIVE_FORK_AT_ANCHOR_CAPABILITY], separators=(",", ":"))

# Host ``h`` ships the exact Agent CLI version group ``g``'s container runs:
# the version its drivers were built and tested against. A release that only
# rebuilds the sandbox image leaves this true. Every release does rebuild it —
# the image is labelled with its source commit — so asking whether the host
# still runs the group's very image would be false after every deployment.
HOST_SHIPS_GROUP_CLI = "(h.runtime_versions_available -> g.runtime_id) ? g.runtime_version"

# Whether the host holding group ``g`` can fork one of its members at a
# historical anchor. The host must advertise it, and the fork must run the way
# it was tested: the container's own fork helper understands the anchor (the
# original helper ignored it and silently forked the latest message), and the
# host drives the CLI version the container runs. A host running the very image
# the group was created from satisfies both, and is the only proof for a group
# whose helper version is unrecorded or outside the reviewed pin. A helper pin
# change therefore requires an explicit review of retained-group compatibility.
NATIVE_FORK_CAPABLE = f"""EXISTS (
    SELECT 1 FROM agent_runner_hosts h
    WHERE h.host = g.host
      AND h.status = 'online'
      AND h.capabilities @> '{NATIVE_FORK_CAPABILITY_JSON}'::jsonb
      AND (
          h.sandbox_image_digest = g.sandbox_image_digest
          OR (
              g.fork_helper_version = '{SHARED_SANDBOX_HELPER_PINS[FORK_HELPER]}'
              AND {HOST_SHIPS_GROUP_CLI}
          )
      )
)"""

# Every runnable chat is a member of a retained shared-live group: one
# ``agent_sandboxes`` row per container, one membership row per chat. This is
# the read shape the worker and owner routes see for a thread's sandbox.
THREAD_SANDBOX_SOURCE = f"""
(
    SELECT
        m.thread_id, g.id AS sandbox_id, g.generation, g.host, g.container_id,
        g.container_name, g.runtime_id, g.runtime_version,
        g.sandbox_image_digest, g.execution_profile, g.state,
        g.memory_limit, g.cpu_limit, g.disk_limit_mib, g.stop_timeout_seconds,
        m.context_status, m.activation_generation, m.process_generation,
        m.active_attempt_id, m.admitted_job_id, m.admitted_at,
        m.last_settled_job_id, m.native_session_id, g.idle_deadline,
        COALESCE(m.last_error, g.last_error) AS last_error,
        m.created_at, m.updated_at, m.fork_operation_id,
        TRUE AS shared_live, g.origin_thread_id, m.resident_state,
        m.residency_generation, m.resident_slot_id,
        g.resident_slot_capacity, m.native_state,
        m.forked_from_thread_id, m.fork_anchor_job_id,
        g.last_activity_at AS group_last_activity_at,
        (
            SELECT snapshot.state
            FROM agent_sandbox_snapshots snapshot
            WHERE snapshot.sandbox_id = g.id
            ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
            LIMIT 1
        ) AS workspace_publication_state,
        g.workspace_epoch,
        {NATIVE_FORK_CAPABLE} AS native_fork_capable,
        -- The host holding this group can hand a message to a running turn of
        -- the group's runtime: its broker and driver say so, per runtime, and
        -- the driver speaks the CLI version this container runs. No helper in
        -- the container takes part; the message goes to the CLI itself.
        EXISTS (
            SELECT 1 FROM agent_runner_hosts h
            WHERE h.host = g.host
              AND h.status = 'online'
              AND h.capabilities @> jsonb_build_array(
                  '{MID_TURN_INPUT_CAPABILITY_PREFIX}' || g.runtime_id
              )
              AND (
                  h.sandbox_image_digest = g.sandbox_image_digest
                  OR {HOST_SHIPS_GROUP_CLI}
              )
        ) AS mid_turn_input_capable,
        COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'thread_id', peer.thread_id,
                    'native_session_id', peer.native_session_id,
                    'native_state', peer.native_state,
                    'context_status', peer.context_status,
                    'activation_generation', peer.activation_generation,
                    'process_generation', peer.process_generation,
                    'residency_generation', peer.residency_generation,
                    'resident_state', peer.resident_state,
                    'resident_slot_id', peer.resident_slot_id,
                    'active_attempt_id', peer.active_attempt_id,
                    'admitted_job_id', peer.admitted_job_id,
                    'admitted_at', peer.admitted_at,
                    'last_settled_job_id', peer.last_settled_job_id,
                    'forked_from_thread_id', peer.forked_from_thread_id,
                    'fork_anchor_job_id', peer.fork_anchor_job_id
                ) ORDER BY peer.created_at, peer.thread_id
            )
            FROM agent_thread_sandbox_memberships peer
            WHERE peer.sandbox_id = g.id
              -- Failed Fork placeholders keep their lineage but release their
              -- slots. Only current slot owners belong in the host manifest.
              AND peer.resident_slot_id IS NOT NULL
        ), '[]'::jsonb) AS group_members
    FROM agent_thread_sandbox_memberships m
    JOIN agent_sandboxes g ON g.id = m.sandbox_id
) AS s
"""
