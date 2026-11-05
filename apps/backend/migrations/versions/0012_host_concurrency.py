"""Let an operator set how many turns a host runs at once, from the admin page.

``slots_total`` is the host's own statement of the limit it is serving, and the
heartbeat rewrites it every thirty seconds, so nothing an operator typed into
that column would survive a minute. Changing concurrency meant editing the
host's environment and redeploying — the wrong tool both for "this machine is
struggling, give it less this afternoon" and for "the queue is long, give it
more".

Two columns, for the two halves of that conversation:

* ``slots_desired`` is what the operator asked for. The host reads it off its
  next heartbeat answer and serves it as its limit, reporting it back as
  ``slots_total``. The claim admits against the **lower** of the two, so a
  number the host has not confirmed can only ever admit less work than it can
  run, never more — and a number that was lowered stops admitting before the
  host has even heard about it. Null means no operator opinion, which is what
  every existing row gets and what makes the host's own ``AGENT_HOST_SLOTS``
  apply.
* ``launch_error`` is why a host with free capacity is nonetheless claiming
  nothing: it could not start the process a turn runs in, repeatedly. Without
  it, the admin page shows an idle machine with room and no explanation, which
  is the state an operator most needs a reason for. Empty is a positive
  statement that nothing is wrong; null is a host that has never said.

The CHECK is structural — a non-positive count is not a count. The shared-live
floor of two is policy, enforced by the request schema on one side and the
host's own bounds on the other, because a floor is a protocol decision and this
column outlives any particular one.

Revision ID: 0012_host_concurrency
Revises: 0011_mcp_rolling_compat
"""

from __future__ import annotations

from alembic import op

revision = "0012_host_concurrency"
down_revision = "0011_mcp_rolling_compat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the operator's requested limit, and the host's reason for idling."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            ADD COLUMN slots_desired INTEGER
                CHECK (slots_desired IS NULL OR slots_desired > 0),
            ADD COLUMN launch_error TEXT
                CHECK (launch_error IS NULL OR length(launch_error) <= 500)
        """
    )


def downgrade() -> None:
    """Drop both; the host's own reported limit is the only bound again."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP COLUMN IF EXISTS slots_desired,
            DROP COLUMN IF EXISTS launch_error
        """
    )
