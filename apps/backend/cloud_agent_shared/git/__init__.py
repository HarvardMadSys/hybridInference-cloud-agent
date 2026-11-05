"""Cloud Agent's shared Git algorithms.

The rules the control plane and the host agent both apply to Git: the name of
a conversation's local branch, the archive builders that turn a prepared
checkout into bytes a sandbox can import, and a temporary workspace's fixed
first commit. Both sides run them — the control plane when it admits a turn
and names its branch, the host's runner and source preparation when they
build a workspace — so the rules have one home instead of one side owning
them.

It is the one subpackage of :mod:`cloud_agent_shared` allowed to touch the
outside world — ``subprocess`` for Git and ``tarfile`` for the archives — which
is why it is a subpackage with a name of its own rather than three modules
beside the vocabulary. It imports its parent and never ``cloud_agent`` or
``cloud_agent_host``, so either image can ship it.
"""
