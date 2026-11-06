"""Postgres read models behind the admin console, one module per capability.

Bounded, read-only projections across every owner. Each class implements the
port of the same name without its ``Postgres`` prefix in
:mod:`cloud_agent.ports.admin_queries`, and ``storage.assembly`` is the only
production caller that names one.
"""
