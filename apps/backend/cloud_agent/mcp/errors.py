"""Errors the MCP control plane raises when a connection cannot be stored."""

from __future__ import annotations


class McpConnectionCreateConflict(Exception):
    """A connection draft became invalid before its atomic insert."""


class McpCredentialWriteRejected(Exception):
    """A credential write matched no row, so its connection must not remain.

    Raised inside the creation transaction. The insert rolls back with it,
    which is what keeps a rejected secret from leaving a credential-less
    connection behind.
    """
