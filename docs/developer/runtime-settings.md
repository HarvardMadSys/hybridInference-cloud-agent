# Host runtime settings

Admin Settings defines defaults for every host; an individual server's settings
can override them. The effective values arrive on its next heartbeat. The runner
hands each turn a validated snapshot, and pushes sandbox manager settings to its separate
sandbox manager. Clearing an admin override restores the built-in default.
Environment variables supply bootstrap values only until the first settings
answer arrives.

The complete registry, labels, units, scopes, and validation ranges live in
[`cloud_agent_shared/host_settings.py`](../../apps/backend/cloud_agent_shared/host_settings.py).
The admin UI reads that registry rather than maintaining another list.

**Storage reserve for new sandboxes** (`storage_min_available_mib`) is a runner
setting with a default of 1024 MiB and a range of 1–1,048,576 MiB. Its bootstrap
variable is `AGENT_STORAGE_MIN_AVAILABLE_MIB`. Before accepting a turn that
would allocate a sandbox, the runner reads fresh available space through the
existing attested Docker writable-layer storage probe. A reading below the
reserve declines the untouched turn; the server's launch error describes the
pressure and suggests freeing storage or adjusting this setting. Existing
retained sandboxes can resume and cleanup stays available.

This guard is not a quota or space reservation. Unknown, disabled, unavailable,
unsupported, or unverified capacity remains explicitly unmeasured and does not
establish a storage-pressure verdict. See [storage measurement and attestation](operations.md#how-full-is-the-sandbox-storage)
and [host execution limits](host-execution-limits.md) for operational boundaries.
