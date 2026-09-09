"""Cloud Agent host agent.

Runs on every machine in the host pool: claims attempts from the control plane,
prepares workspaces, drives agent runtimes inside per-task sandboxes, and brokers
terminal and file access back to the control plane.
"""

__version__ = "0.1.0"
