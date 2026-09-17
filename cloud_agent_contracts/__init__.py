"""Cloud Agent contracts.

The values the control plane and the host agent must agree on: capability
strings, protocol versions, and the pins a runner attests. Both sides import
this package, and it imports neither implementation package, so the agreement
has one home instead of one side owning it.
"""
