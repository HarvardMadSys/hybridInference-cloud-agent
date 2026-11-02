"""Cloud Agent control plane.

Owns the job/thread/attempt lifecycle, the agent database, source-control
integrations, publishing, host-pool scheduling, and its own accounts. Every
turn runs on a model connection its owner added; no other service signs in its
users or serves its inference.
"""

__version__ = "0.1.0"
