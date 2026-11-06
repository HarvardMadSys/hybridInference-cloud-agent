"""Postgres implementation of the retained-sandbox port, one module per aggregate.

Each module holds a mixin with one aggregate's methods, and
:mod:`.repository` composes them into :class:`PostgresSandboxRepository`.
Shared helpers live in :mod:`._common`.
"""
