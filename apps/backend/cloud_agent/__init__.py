"""Cloud Agent control plane.

Owns the job/thread/attempt lifecycle, the agent database, source-control
integrations, the GitHub relay, host-pool scheduling, and its own accounts. Every
turn runs on a model connection its owner added; no other service signs in its
users or serves its inference.
"""

import os as _os

from cloud_agent.deployment.key_files import fill_environment as _fill_environment

__version__ = "0.1.0"

# Before any module reads the environment: the deployment keys the environment
# leaves unset come from the agent-keys volume (see deployment/key_files.py).
_fill_environment(_os.environ)
