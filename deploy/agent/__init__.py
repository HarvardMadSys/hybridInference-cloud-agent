"""The deployment agent: one per self-managed machine, outside the store it bootstraps.

It keeps this machine's deployment configuration, release lock and operation
journal under ``<DEPLOY_ROOT>/agent/`` and executes only the closed set of
operations in ``deploy/agent/deployment_contract.py``, a byte-for-byte copy of
``cloud_agent_shared.deployment_contract`` that the control plane uses. It pulls intents
from the control plane with its own credential, and every operation also has a
local command, so the machine can be recovered while the control plane (or its
database) is unavailable. See ``deploy/agent/README.md``.
"""

from __future__ import annotations
