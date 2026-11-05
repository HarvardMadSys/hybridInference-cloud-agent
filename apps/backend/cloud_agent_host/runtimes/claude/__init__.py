"""Claude Code: everything the platform knows about this one CLI.

``adapter`` builds the resident launch and normalizes its stream-json events;
``session`` is its live session and turn; ``background`` tracks the background
tasks a turn leaves running; ``member`` prepares a member's private config and
materializes a native Fork.

Nothing is imported here: ``session`` and ``member`` build on
:mod:`cloud_agent_host.drivers`, which imports this package's parent, so each
module is imported by its own name.
"""
