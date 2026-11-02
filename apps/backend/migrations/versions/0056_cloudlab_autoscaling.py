"""Persist the singleton CloudLab autoscaling policy and fenced runtime state.

Revision ID: 0056_cloudlab_autoscaling
Revises: 0055_send_now_handed_off
"""

from alembic import op

revision = "0056_cloudlab_autoscaling"
down_revision = "0055_send_now_handed_off"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add policy, hysteresis, ownership and deployment-profile reservations."""
    op.execute(
        """
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement','rotation',
                                   'autoscaling_slots'));

        CREATE TABLE agent_cloudlab_autoscaling_policy (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            min_hosts INTEGER NOT NULL DEFAULT 0 CHECK (min_hosts BETWEEN 0 AND 8),
            max_hosts INTEGER NOT NULL DEFAULT 0 CHECK (max_hosts BETWEEN 0 AND 8),
            connection_id TEXT REFERENCES agent_compute_connections (id) ON DELETE RESTRICT,
            ssh_connection_id TEXT REFERENCES agent_compute_connections (id) ON DELETE RESTRICT,
            configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
            deployment_profiles TEXT[] NOT NULL DEFAULT '{}'::text[],
            sandbox_storage_path TEXT,
            renewal_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
            slot_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
            scale_up_sustain_seconds INTEGER NOT NULL DEFAULT 120
                CHECK (scale_up_sustain_seconds BETWEEN 0 AND 86400),
            scale_down_idle_seconds INTEGER NOT NULL DEFAULT 3600
                CHECK (scale_down_idle_seconds BETWEEN 60 AND 604800),
            cooldown_seconds INTEGER NOT NULL DEFAULT 600
                CHECK (cooldown_seconds BETWEEN 0 AND 86400),
            scale_down_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_cloudlab_autoscaling_policy_singleton CHECK (id),
            CONSTRAINT agent_cloudlab_autoscaling_host_order CHECK (min_hosts <= max_hosts),
            CONSTRAINT agent_cloudlab_autoscaling_configuration_object
                CHECK (jsonb_typeof(configuration) = 'object'),
            CONSTRAINT agent_cloudlab_autoscaling_renewal_object
                CHECK (jsonb_typeof(renewal_policy) = 'object'),
            CONSTRAINT agent_cloudlab_autoscaling_slot_object
                CHECK (jsonb_typeof(slot_policy) = 'object')
        );

        CREATE TABLE agent_cloudlab_autoscaling_runtime (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE,
            generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
            pressure_since TIMESTAMPTZ,
            idle_since TIMESTAMPTZ,
            cooldown_until TIMESTAMPTZ,
            queue_pressure INTEGER NOT NULL DEFAULT 0 CHECK (queue_pressure >= 0),
            slot_status JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_decision_at TIMESTAMPTZ,
            blocker_code TEXT,
            blocker_message TEXT,
            observed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_cloudlab_autoscaling_runtime_singleton CHECK (id),
            CONSTRAINT agent_cloudlab_autoscaling_slot_status_array
                CHECK (jsonb_typeof(slot_status) = 'array'),
            CONSTRAINT agent_cloudlab_autoscaling_decision_object
                CHECK (jsonb_typeof(last_decision) = 'object')
        );

        CREATE TABLE agent_cloudlab_autoscaling_members (
            allocation_id TEXT PRIMARY KEY
                REFERENCES agent_compute_allocations (id) ON DELETE RESTRICT,
            deployment_profile TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'provisioning'
                CHECK (state IN ('provisioning','active','retiring','released','blocked')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE agent_cloudlab_autoscaling_profile_reservations (
            deployment_profile TEXT PRIMARY KEY,
            profile_snapshot JSONB NOT NULL,
            allocation_id TEXT UNIQUE
                REFERENCES agent_compute_allocations (id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT agent_cloudlab_autoscaling_profile_snapshot_object
                CHECK (jsonb_typeof(profile_snapshot) = 'object')
        );

        INSERT INTO agent_cloudlab_autoscaling_policy (id) VALUES (TRUE);
        INSERT INTO agent_cloudlab_autoscaling_runtime (id) VALUES (TRUE);
        """
    )


def downgrade() -> None:
    """Drop autoscaling state without changing provider resources or allocations."""
    op.execute(
        """
        DELETE FROM agent_host_admission_holds WHERE purpose='autoscaling_slots';
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement','rotation'));

        DROP TABLE agent_cloudlab_autoscaling_profile_reservations;
        DROP TABLE agent_cloudlab_autoscaling_members;
        DROP TABLE agent_cloudlab_autoscaling_runtime;
        DROP TABLE agent_cloudlab_autoscaling_policy;
        """
    )
