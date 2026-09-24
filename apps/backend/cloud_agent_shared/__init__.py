"""Cloud Agent contracts.

The values the control plane and the host agent must agree on: capability
strings, protocol versions, the pins a runner attests, and the operation
measurement record both sides produce. Both sides import this package, and it
imports neither implementation package, so the agreement has one home instead
of one side owning it.
"""
