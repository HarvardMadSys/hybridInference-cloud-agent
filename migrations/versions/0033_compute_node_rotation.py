"""Let a CloudLab Host move to a replacement node before its lease ends.

A CloudLab experiment always expires and its disks are wiped. To keep retained
chats, the whole Host — its name, its sandbox namespace and its Docker state —
moves to a newly allocated node first. Retained sandboxes stay keyed by the
Host name, so nothing about them changes.

* ``agent_compute_node_registrations``: the control plane must know a new
  node's host key before its first SSH connection, and a replacement node has
  nobody to verify it by hand. The node's boot script receives a one-time
  token through the provider and posts its host keys over HTTPS; only the
  token's hash is stored. A registration belongs to one allocation, expires,
  and accepts one set of keys: the same keys again get the same answer,
  different keys are refused.
* ``slot_host`` and ``slot_namespace`` name the Host identity a node deploys.
  Both stay NULL for an allocation that starts a Host, which keeps the
  original ``compute-<node>`` / ``ca-<node>`` naming.
* ``predecessor_id`` links a replacement node to the allocation it replaces.
* A ``rotation`` admission hold stops claims while the Host changes machine,
  and at most one move of an allocation's Host is in progress.
"""

from __future__ import annotations

from alembic import op

revision = "0033_compute_node_rotation"
down_revision = "0032_push_usage_and_workers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the registration table, the Host identity columns and the hold purpose."""
    op.execute(
        """
        CREATE TABLE agent_compute_node_registrations (
            id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES agent_compute_allocations(id),
            operation_id TEXT NOT NULL REFERENCES agent_compute_operations(id),
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            host_keys JSONB,
            registered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK ((host_keys IS NULL) = (registered_at IS NULL)),
            CHECK (host_keys IS NULL OR jsonb_typeof(host_keys) = 'array')
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_compute_node_registrations_allocation "
        "ON agent_compute_node_registrations(allocation_id, created_at)"
    )
    op.execute(
        """
        ALTER TABLE agent_compute_allocations
            ADD COLUMN slot_host TEXT,
            ADD COLUMN slot_namespace TEXT,
            ADD COLUMN predecessor_id TEXT REFERENCES agent_compute_allocations(id)
        """
    )
    op.execute(
        "CREATE INDEX idx_compute_allocations_predecessor "
        "ON agent_compute_allocations(predecessor_id) WHERE predecessor_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_compute_operations_allocation ON agent_compute_operations(allocation_id)"
    )
    # Two moves of one Host at once would race for its name and state.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_compute_one_active_rotation
            ON agent_compute_operations(allocation_id)
            WHERE kind = 'rotate' AND state NOT IN ('succeeded', 'failed', 'cancelled')
        """
    )
    op.execute(
        """
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement','rotation'))
        """
    )


def downgrade() -> None:
    """Drop them; a Host stays on the node that started it."""
    op.execute("DELETE FROM agent_host_admission_holds WHERE purpose = 'rotation'")
    op.execute(
        """
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement'))
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_compute_one_active_rotation")
    op.execute("DROP INDEX IF EXISTS idx_compute_operations_allocation")
    op.execute("DROP INDEX IF EXISTS idx_compute_allocations_predecessor")
    op.execute(
        """
        ALTER TABLE agent_compute_allocations
            DROP COLUMN IF EXISTS predecessor_id,
            DROP COLUMN IF EXISTS slot_namespace,
            DROP COLUMN IF EXISTS slot_host
        """
    )
    op.execute("DROP TABLE IF EXISTS agent_compute_node_registrations")
