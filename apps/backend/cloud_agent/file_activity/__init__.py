"""A conversation's File Activity: recorded file operations, grouped per file.

The package holds the read's own pieces, none of which observe a filesystem or
ask a runtime anything: the wire shapes (:mod:`models`), the reducer that turns
one recorded event into the operations it proves (:mod:`normalize`), the
grouping and paging (:mod:`aggregate`), the scan that walks a conversation's
ledger (:mod:`scan`), and the opaque page cursors (:mod:`cursor`).

The Postgres projection that hides each runtime's argument spelling lives in
:mod:`cloud_agent.storage._sql.file_activity`; the owner-scoped read is
:mod:`cloud_agent.storage.repositories.file_activity`, and the routes are
:mod:`cloud_agent.api.file_activity`.
"""

from __future__ import annotations
