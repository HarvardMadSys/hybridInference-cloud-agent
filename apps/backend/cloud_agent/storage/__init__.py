"""The control plane's own database: job, thread, attempt and host state.

Owned outright by this service. The gateway holds none of it after the split —
which is why the attempt fence lives here and grant renewal there consults no
attempt state.
"""
