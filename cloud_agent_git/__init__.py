"""Cloud Agent's shared Git algorithms.

The rules the control plane and the host agent both apply to Git objects: the
patch gate that decides whether an agent-produced diff may become a branch,
and the archive builders that turn a prepared checkout into bytes a sandbox
can import. Both sides run them — the control plane's publisher and publish
worker, the host's runner and workspace broker — so the rules have one home
instead of one side owning them.

This package imports :mod:`cloud_agent_contracts` and never
``cloud_agent`` or ``cloud_agent_host``, so either image can ship it.
"""
