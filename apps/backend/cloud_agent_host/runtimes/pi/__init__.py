"""pi: everything the platform knows about this one CLI.

``adapter`` builds the RPC-mode launch and normalizes its events; ``session``
is its live session, including its native Fork; ``member`` defines its launch
steps.

Nothing is imported here: ``session`` and ``member`` build on :mod:`cloud_agent_host.drivers`,
which imports this package's parent, so each module is imported by its own
name.
"""
