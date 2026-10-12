"""Cloud Agent's shared Git algorithms.

The rules the control plane and the host agent both apply to Git objects: the
patch gate that decides whether an agent-produced diff may become a branch,
and the archive builders that turn a prepared checkout into bytes a sandbox
can import. Both sides run them — the control plane's publisher and publish
worker, the host's runner and sandbox manager — so the rules have one home
instead of one side owning them.

It is the one subpackage of :mod:`cloud_agent_shared` allowed to touch the
outside world — ``subprocess`` for Git and ``tarfile`` for the archives — which
is why it is a subpackage with a name of its own rather than three modules
beside the vocabulary. It imports its parent and never ``cloud_agent`` or
``cloud_agent_host``, so either image can ship it.
"""
