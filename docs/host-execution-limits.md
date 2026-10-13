# Host execution limits

Bounded workspace commands run in their own process groups inside the retained
sandbox. Their PID records include the Linux start time, and cleanup verifies
both the UID and process identity before signalling that group. A command's
timeout or output overflow therefore preserves concurrent native sessions and
terminals in the shared container. Zombie processes awaiting the container's
init reap do not count as surviving command writers.

Host readers drain stdout and stderr in 64 KiB chunks. Command stdout limits
apply while those chunks arrive; stderr keeps at most 256 KiB of diagnostic
bytes. Queues hold at most four items and apply backpressure, with cancellation
when their consumer exits. Native CLI records have an 8 MiB byte limit; broker
NDJSON frames have a 32 MiB limit to account for normalized content, the raw
record, and JSON escaping. An oversized native record stops its member process
through the same scoped cleanup path. Setup and runner diagnostic tails are
also bounded by bytes.

Resource checkpoints are forwarded independently of native CLI events. A
silent model or tool invocation can therefore persist its latest sampled
checkpoint before emitting another message. The existing final report remains
the authoritative closing measurement.

The runner's **Storage reserve for new sandboxes** setting defaults to 1024 MiB.
Before accepting a turn that would allocate a new sandbox, the runner takes a
fresh reading through the existing attested Docker writable-layer storage
probe. If unprivileged available space is below the reserve, it declines the
handover and the host returns the untouched turn to the queue. Its launch-error
status identifies the available space and suggests freeing storage or adjusting
the reserve. Existing retained sessions may resume and cleanup remains available.

This is an admission guard, not a disk quota or a reservation. Concurrent or
already-running sandboxes can consume space after the check. Disabled,
unsupported, unavailable, or unverified capacity does not establish a pressure
verdict: the guard does not substitute an arbitrary filesystem measurement and
those hosts retain the existing explicit unmeasured capacity status. Operators
should keep the storage attestation current and monitor available space;
storage-driver-specific writable-layer quotas require separate deployment work.
