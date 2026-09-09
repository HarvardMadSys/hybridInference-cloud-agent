"""Cloud Agent control plane.

Owns the job/thread/attempt lifecycle, the agent database, source-control
integrations, publishing, and host-pool scheduling. Talks to the HybridInference
gateway over HTTP only — identity verification and inference grants — and never
imports gateway code or shares its signing secrets.
"""

__version__ = "0.1.0"
