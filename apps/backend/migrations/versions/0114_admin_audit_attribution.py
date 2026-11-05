"""The admin audit trail says who acted, on what, and with what result.

Until now a row named its actor in one string (``admin:<id>``, ``admin-token``,
``sandbox-deletion-worker`` ...), its target only when the target was a user,
and its result as a boolean that read "succeeded" for work merely queued. This
revision adds what the Audit log needs to answer "which administrator did what
to which user" without guessing:

- ``actor_type``, generated from ``actor_id``, so a row the previous release
  writes during the rollout is classified exactly like one this release
  writes. ``admin`` is an administrator's session; ``user`` an account acting
  on itself; ``anonymous`` a request with no account; ``break_glass`` access
  belonging to no account (``ADMIN_TOKEN``, first-run setup, the bootstrap
  command, a hand-run ``operator:`` change); ``service`` a named automation;
  anything else the control plane acting on its own (``system``).
- ``actor_label``, ``target_label`` and ``subject_label``: the names as they
  were when the row was written. Erasing an account frees its login name for a
  new account, so the page shows a name with its id, never a name alone.
- ``target_type`` / ``target_id``: what was acted on, typed. ``target_user_id``
  keeps meaning "the user affected", which is now often not the target: a
  deleted sandbox's target is the sandbox and its affected user the owner.
- ``outcome`` (``applied``, ``requested``, ``refused``, ``failed``,
  ``replayed``), ``reason`` and ``correlation_id``: the operation or request a
  ``requested`` row's final result is reported against.
- ``agent_admin_audit_outcomes``: that final result, one row per request.
- ``agent_admin_audit_write_failures``: an audit write that failed, so the
  page can say so. Recording an action must never make the action fail.

Existing rows are backfilled from what they already hold. The table below is a
frozen copy of ``cloud_agent.domain.audit.ACTIONS`` as of this revision; a
migration never imports the application.

Three kinds of row are removed, the only rows this trail ever drops: the
Servers page's polling reads (no longer written since 2026-10-09), connection
checks (a read; no longer written), and the sandbox deletion worker's
per-step rows, whose final result moves to ``agent_admin_audit_outcomes``
against the administrator's request. A worker row that names no request is
kept. The steps themselves remain in ``agent_sandbox_events``.

The SQL uses ``starts_with`` rather than ``LIKE``: some callers replay
migrations through drivers that treat ``%`` as a placeholder.

Revision ID: 0114_admin_audit_attribution
Revises: 0113_sandbox_incidents
"""

import sqlalchemy as sa
from alembic import op

revision = "0114_admin_audit_attribution"
down_revision = "0113_sandbox_incidents"
branch_labels = None
depends_on = None

_ACTOR_TYPE = """
    CASE
        WHEN actor_id IN ('admin:staging-cleanup', 'codex-ops-stale-cleanup')
            OR starts_with(actor_id, 'service:') THEN 'service'
        WHEN starts_with(actor_id, 'admin:') THEN 'admin'
        WHEN starts_with(actor_id, 'self:') THEN 'user'
        WHEN actor_id IN ('admin-token', 'deployment-bootstrap', 'first-run-setup')
            OR starts_with(actor_id, 'operator:') THEN 'break_glass'
        WHEN actor_id IN ('anonymous', 'password-reset') THEN 'anonymous'
        ELSE 'system'
    END
"""

#: Reads recorded as actions before they stopped being written.
_NOT_ACTIONS = (
    "agent.compute.providers.list",
    "agent.compute.connections.list",
    "agent.compute.servers.list",
    "agent.compute.operations.list",
    "agent.compute.operation.read",
    "agent.compute.allocation.retirement_impact",
    "agent.compute.connection.validate",
)

_ACCOUNT_ACTIONS = (
    "approve",
    "reject",
    "revoke",
    "suspend",
    "restore",
    "grant_admin",
    "revoke_admin",
    "delete",
    "resume",
    "annotate",
    "erase",
    "transfer_team",
    "setup_admin",
    "signup",
    "password_set",
    "password_change",
    "request_bind_email",
    "bind_phone",
    "reclaim",
    "self_delete",
)

#: (action, target type, details key, fixed target, correlation key, runs later)
_FROZEN: tuple[tuple[str, str, str | None, str | None, str | None, bool], ...] = (
    *((f"agent.local_account.{name}", "account", None, None, None, False) for name in _ACCOUNT_ACTIONS),
    ("agent.local_login_events.purge", "account", None, "all", None, False),
    ("agent.local_broadcast.create", "broadcast", "broadcast_id", None, None, False),
    ("agent.local_broadcast.cancel", "broadcast", "broadcast_id", None, None, False),
    ("agent.local_signup_domain.add", "signup_domain", "domain", None, None, False),
    ("agent.local_signup_domain.remove", "signup_domain", "domain", None, None, False),
    ("agent.settings.update", "setting", None, "application", None, False),
    ("agent.host_settings.set", "setting", None, "host_defaults", None, False),
    ("agent.sandbox_archive_settings.set", "setting", None, "sandbox_archive", None, False),
    ("agent.compute.cloudlab_defaults.set", "setting", None, "cloudlab_defaults", None, False),
    ("agent.compute.cloudlab_autoscaling.set", "setting", None, "cloudlab_autoscaling", None, False),
    ("agent.database.pgadmin.setup", "setting", None, "pgadmin", None, False),
    ("agent.runtime_version.set", "runtime", "runtime", None, None, False),
    ("agent.runtime_version.upgrade", "runtime", "runtime", None, None, False),
    ("agent.runner_host.set_settings", "host", "host", None, None, False),
    ("agent.runner_host.set_active", "host", "new_host", "any", None, False),
    *(
        (f"agent.runner_host.{name}", "host", "host", None, None, False)
        for name in ("drain", "undrain", "remove", "forget", "set_slots", "enroll_token_issued")
    ),
    ("agent.compute.profile.create", "compute_profile", "profile_id", None, None, False),
    ("agent.compute.profile.automatic_create", "compute_profile", "profile_id", None, None, False),
    ("agent.compute.connection.create", "compute_connection", "connection_id", None, None, False),
    ("agent.compute.connection.update", "compute_connection", "connection_id", None, None, False),
    *(
        (f"agent.compute.{name}", "compute_operation", "operation_id", None, "operation_id", True)
        for name in (
            "server.add",
            "server.preflight",
            "allocation.import",
            "operation.retry",
            "operation.cancel",
            "operation.resolve",
            "operation.confirm_absent",
        )
    ),
    ("agent.compute.server.remove", "compute_server", "allocation_id", None, None, False),
    ("agent.compute.allocation.policy", "compute_server", "allocation_id", None, None, False),
    *(
        (f"agent.compute.allocation.{name}", "compute_server", "allocation_id", None, "operation_id", True)
        for name in ("extend", "rotate", "retire")
    ),
    ("agent.sandbox.archive", "sandbox", "sandbox_id", None, None, False),
    ("agent.sandbox.restore", "sandbox", "sandbox_id", None, None, False),
    ("agent.sandbox.stop", "sandbox", "sandbox_id", None, None, False),
    ("agent.sandbox.delete.requested", "sandbox", "sandbox_id", None, "request_id", True),
    ("agent.sandbox.delete.retry_resumed", "sandbox", "sandbox_id", None, "request_id", True),
    ("agent.profiling.start", "measurement", None, "operations", None, False),
    ("agent.profiling.stop", "measurement", None, "operations", None, False),
    ("agent.database.pgadmin.open", "database", None, "pgadmin", None, False),
    ("agent.database.query", "database", "table", "query", None, False),
    ("agent.run.event_content.read", "run", "job_id", None, None, False),
)

_OUTCOME = """
    CASE
        WHEN details->>'outcome' IN ('refused', 'replayed') THEN details->>'outcome'
        WHEN NOT success THEN 'failed'
        WHEN :later THEN 'requested'
        ELSE 'applied'
    END
"""

#: An account's name as the Audit log shows it.
_NAMES = """
    SELECT a.id, COALESCE(n.value, e.value, p.value, a.user_name) AS label
    FROM agent_local_accounts a
    LEFT JOIN agent_local_login_identities n ON n.account_id = a.id AND n.kind = 'login_name'
    LEFT JOIN agent_local_login_identities e ON e.account_id = a.id AND e.kind = 'email'
    LEFT JOIN agent_local_login_identities p ON p.account_id = a.id AND p.kind = 'phone'
"""

_WORKER = "sandbox-deletion-worker"


def _drop_reads() -> None:
    op.execute(
        sa.text("DELETE FROM agent_admin_audit_log WHERE action = ANY(:actions)").bindparams(
            sa.bindparam("actions", list(_NOT_ACTIONS), type_=sa.ARRAY(sa.Text()))
        )
    )


def _add_columns() -> None:
    op.execute(
        f"""
        ALTER TABLE agent_admin_audit_log
            ADD COLUMN actor_type TEXT GENERATED ALWAYS AS ({_ACTOR_TYPE}) STORED,
            ADD COLUMN actor_label TEXT,
            ADD COLUMN target_type TEXT,
            ADD COLUMN target_id TEXT,
            ADD COLUMN target_label TEXT,
            ADD COLUMN subject_label TEXT,
            ADD COLUMN outcome TEXT CHECK (
                outcome IS NULL
                OR outcome IN ('applied', 'requested', 'refused', 'failed', 'replayed')
            ),
            ADD COLUMN reason TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
            ADD COLUMN correlation_id TEXT
        """
    )
    op.execute(
        """
        CREATE TABLE agent_admin_audit_outcomes (
            event_id BIGINT PRIMARY KEY REFERENCES agent_admin_audit_log (id),
            outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'failed', 'cancelled')),
            reported_by TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_admin_audit_write_failures (
            id BIGSERIAL PRIMARY KEY,
            action TEXT NOT NULL,
            actor_id TEXT,
            error TEXT NOT NULL CHECK (length(error) <= 200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    for statement in (
        "CREATE INDEX idx_agent_admin_audit_subject "
        "ON agent_admin_audit_log (target_user_id, id DESC) WHERE target_user_id IS NOT NULL",
        "CREATE INDEX idx_agent_admin_audit_target "
        "ON agent_admin_audit_log (target_type, target_id, id DESC)",
        "CREATE INDEX idx_agent_admin_audit_actor ON agent_admin_audit_log (actor_type, id DESC)",
        "CREATE INDEX idx_agent_admin_audit_correlation "
        "ON agent_admin_audit_log (correlation_id) WHERE correlation_id IS NOT NULL",
        "CREATE INDEX idx_agent_admin_audit_write_failures_created "
        "ON agent_admin_audit_write_failures (created_at)",
    ):
        op.execute(statement)


def _backfill_targets() -> None:
    for action, target_type, key, fixed, correlation, later in _FROZEN:
        account_fallback = "target_user_id" if target_type == "account" else "NULL"
        op.execute(
            sa.text(
                f"""
                UPDATE agent_admin_audit_log
                SET target_type = :target_type,
                    target_id = COALESCE(
                        NULLIF(details->>CAST(:key AS TEXT), ''),
                        {account_fallback},
                        CAST(:fixed AS TEXT),
                        'unknown'
                    ),
                    correlation_id = NULLIF(details->>CAST(:correlation AS TEXT), ''),
                    outcome = {_OUTCOME}
                WHERE action = :action
                """
            ).bindparams(
                action=action,
                target_type=target_type,
                key=key,
                fixed=fixed,
                correlation=correlation,
                later=later,
            )
        )
    op.execute(
        sa.text(
            f"""
            UPDATE agent_admin_audit_log
            SET target_type = 'unknown',
                target_id = COALESCE(target_user_id, 'unknown'),
                outcome = {_OUTCOME}
            WHERE target_type IS NULL
            """
        ).bindparams(later=False)
    )


def _backfill_names() -> None:
    op.execute(
        f"""
        UPDATE agent_admin_audit_log l SET actor_label = names.label
        FROM ({_NAMES}) names
        WHERE l.actor_id IN ('admin:' || names.id, 'self:' || names.id)
        """
    )
    op.execute(
        f"""
        UPDATE agent_admin_audit_log l
        SET subject_label = names.label,
            target_label = CASE
                WHEN l.target_type = 'account' AND l.target_id = names.id THEN names.label
            END
        FROM ({_NAMES}) names
        WHERE l.target_user_id = names.id
        """
    )


def _move_worker_results() -> None:
    """The worker's last word on each request becomes that request's result."""
    op.execute(
        f"""
        WITH steps AS (
            SELECT DISTINCT ON (details->>'request_id')
                details->>'request_id' AS request_id, action, details, created_at
            FROM agent_admin_audit_log
            WHERE (actor_id = '{_WORKER}' OR action = 'agent.sandbox.delete.retry_resumed')
              AND details->>'request_id' IS NOT NULL
            ORDER BY details->>'request_id', id DESC
        ), finals AS (
            SELECT s.request_id, s.details, s.created_at,
                CASE
                    WHEN s.action IN ('agent.sandbox.delete.completed',
                                      'agent.sandbox.delete.discarded') THEN 'applied'
                    WHEN s.action = 'agent.sandbox.delete.retry_exhausted' THEN 'failed'
                    WHEN s.action = 'agent.sandbox.delete.failed' AND EXISTS (
                        SELECT 1 FROM agent_sandboxes x
                        WHERE x.deletion_request_id = s.request_id
                          AND x.deletion_state <> 'deleted'
                          AND (x.deletion_state = 'failed'
                               OR x.deletion_retry_exhausted_at IS NOT NULL)
                    ) THEN 'failed'
                END AS outcome
            FROM steps s
        ), requests AS (
            SELECT DISTINCT ON (correlation_id) id, correlation_id
            FROM agent_admin_audit_log
            WHERE action = 'agent.sandbox.delete.requested' AND correlation_id IS NOT NULL
            ORDER BY correlation_id, id DESC
        )
        INSERT INTO agent_admin_audit_outcomes (event_id, outcome, reported_by, details, created_at)
        SELECT r.id, f.outcome, '{_WORKER}',
               jsonb_strip_nulls(jsonb_build_object(
                   'archive_key', f.details->>'archive_key',
                   'host_reclaimed', (f.details->>'host_reclaimed')::boolean
               )),
               f.created_at
        FROM finals f JOIN requests r ON r.correlation_id = f.request_id
        WHERE f.outcome IS NOT NULL
        """
    )
    op.execute(
        f"""
        DELETE FROM agent_admin_audit_log l
        WHERE l.actor_id = '{_WORKER}'
          AND EXISTS (
              SELECT 1 FROM agent_admin_audit_log r
              WHERE r.action = 'agent.sandbox.delete.requested'
                AND r.correlation_id = l.details->>'request_id'
          )
        """
    )


def upgrade() -> None:
    """Drop reads, add attribution, backfill it, and move worker steps to results."""
    _drop_reads()
    _add_columns()
    _backfill_targets()
    _backfill_names()
    _move_worker_results()


def downgrade() -> None:
    """Drop the attribution columns and the two tables.

    The rows the upgrade removed — reads and the worker's per-step rows — are
    not restored; the steps remain in ``agent_sandbox_events``.
    """
    op.execute("DROP TABLE IF EXISTS agent_admin_audit_write_failures")
    op.execute("DROP TABLE IF EXISTS agent_admin_audit_outcomes")
    for index in (
        "idx_agent_admin_audit_subject",
        "idx_agent_admin_audit_target",
        "idx_agent_admin_audit_actor",
        "idx_agent_admin_audit_correlation",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute(
        """
        ALTER TABLE agent_admin_audit_log
            DROP COLUMN IF EXISTS actor_type,
            DROP COLUMN IF EXISTS actor_label,
            DROP COLUMN IF EXISTS target_type,
            DROP COLUMN IF EXISTS target_id,
            DROP COLUMN IF EXISTS target_label,
            DROP COLUMN IF EXISTS subject_label,
            DROP COLUMN IF EXISTS outcome,
            DROP COLUMN IF EXISTS reason,
            DROP COLUMN IF EXISTS correlation_id
        """
    )
