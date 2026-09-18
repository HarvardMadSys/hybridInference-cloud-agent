"""The agent API's single mount point: eight surface routers behind one prefix.

The handlers live in the surface modules this mounts — ``jobs``, ``turns``,
``threads``, ``workspace``, ``worker``, ``source_control``, ``catalog`` and
``config`` — each of which owns its own prefix, its own dependencies and the
capabilities it reaches the store for. This module exists so that ``app.py``
mounts one router, and so that the surfaces have a fixed order.

The prefix is composed by FastAPI rather than by string concatenation: this
router carries none and each surface carries ``/v1/agent``, so the effective
path is ``/v1/agent/...`` exactly once — two prefixes would compose into
``/v1/agent/v1/agent/...``, which is what the first attempt at this split did.
``SURFACE_ROUTERS`` is the tuple to walk; ``router.routes`` holds lazy include
wrappers rather than routes and is the wrong thing to introspect.

Until R5 this module also re-exported every helper the split moved, so that a
test, a ``Depends`` key or an importer still saying ``routes.X`` resolved to the
identity it always had. Every caller imports the new home now: nothing imports a
symbol from this module any more, at any path, and the only names here are the
router, the tuple, and the modules the tuple mounts.
"""

from __future__ import annotations

from fastapi import APIRouter

from cloud_agent.api import (
    catalog,
    config,
    jobs,
    source_control,
    threads,
    turns,
    worker,
    workspace,
)

router = APIRouter()

#: The leaf routers this aggregator mounts, in the order the concerns first
#: appear. Each carries ``/v1/agent`` and this router carries none, so FastAPI
#: composes one prefix and not two. Tests walk these rather than
#: ``router.routes``, which holds lazy include wrappers rather than routes.
SURFACE_ROUTERS = (
    config.router,
    source_control.router,
    jobs.router,
    catalog.router,
    turns.router,
    threads.router,
    workspace.router,
    worker.router,
)

for _surface in SURFACE_ROUTERS:
    router.include_router(_surface)
