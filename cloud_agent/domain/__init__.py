"""Domain values the control plane shares: state names, ids, and pure checks.

Nothing here imports ``cloud_agent.storage``. The store imports these, not the
other way round, so a caller naming a job state does not thereby depend on
Postgres.
"""
