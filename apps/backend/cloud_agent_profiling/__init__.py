"""Cloud Agent operation profiling.

The measurement vocabulary both sides agree on: the opt-in run descriptor, the
bounds a record is held to, the fragment protocol one producer answers another
with, and the delivery policy a producer applies to what it collected.

Both the control plane and the host agent import this package, and it imports
neither implementation package. The collector was rooted in the runner and the
selection in the control plane, but the record they exchange is one artifact --
so its shape belongs to neither side, for the same reason the capability
strings next door do.
"""
