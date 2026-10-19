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
when their consumer exits. Native CLI records have an 8 MiB byte limit; sandbox manager
NDJSON frames have a 32 MiB limit to account for normalized content, the raw
record, and JSON escaping. An oversized native record stops its member process
through the same scoped cleanup path. Setup and runner diagnostic tails are
also bounded by bytes.

Each normalized event is bounded to 256 KiB of JSON where the host creates it,
and again where the control plane receives it. Text Postgres cannot store is
replaced first: a NUL, which a tool printing binary or NUL-separated output
emits, and an unpaired surrogate, which a CLI leaves when it cuts a string
inside a surrogate pair, each become U+FFFD. The event is kept rather than
refused, because a runner that cannot record an event ends its turn. The same
replacement applies to everything the runner posts to the control plane, and
again to the reason it gives when it releases an activation or ends a turn,
which quotes what the CLI printed: a refused reason would leave the turn on its
lease. The runner also cuts a release's reason to the 1000 characters the
control plane accepts (`ABANDON_DETAIL_MAX_CHARS`), as it cuts a heartbeat's
launch error, because a longer one would have the whole release refused.

Resource checkpoints are forwarded independently of native CLI events. A
silent model or tool invocation can therefore persist its latest sampled
checkpoint before emitting another message. The existing final report remains
the authoritative closing measurement.

Disk space is guarded only at admission: the runner declines a turn that
would create a sandbox when free space is below the
[storage reserve](runtime-settings.md). Nothing bounds what a running sandbox
writes except an optional per-container disk limit; see
[sandbox settings](operations.md#sandbox-settings).
