# Deferred initial archive upload

Status: implemented on `murphy/perf/initial-archive-async`.

## Problem

A new chat's first prompt waited for two sandbox manager calls before the
resident CLI started (staging, `attempt.startup`, path `new_group`, n=35):

| Stage | p50 | Tail |
|---|---|---|
| `initial_archive_status` | 0.75-0.9 s | 2-3 s under concurrency, max 7.1 s |
| `initial_archive` | 3.4 s | p90 4.5 s, one 62 s sample |

`initial_archive` froze every writer, exported `/workspace`, wrote a gzip-1
bundle, wrote the in-container marker, uploaded the bundle to S3, and thawed.
Only the capture has to precede the prompt: it is what fixes the bytes.

A local measurement with the real `spool_sandbox_archive` code (plain Docker,
a 100 MB clone of this repository) put the capture at about 1.6 s: `docker cp`
0.35 s and the gzip-1 bundle write 1.24 s, 90% of it in `zlib.compress`. The
bundle was 72 MB, which the upload (a `GetBucketVersioning` and a 72 MB
`PutObject` with SSE-KMS) then sent on the critical path. The level cannot
drop below 1 without a two-release rollout: a probe rebuilds the levels in
`INITIAL_ARCHIVE_COMPRESSION_LEVELS`, and a level is rebuilt one release
before it is written.

## Design

1. **The upload leaves the critical path.** With `defer_upload` (sent by new
   runners), the Sandbox Manager writes the bundle into a hand-off directory,
   `AGENT_SANDBOX_ARCHIVE_STATE_DIR/initial-uploads/<sha256(namespace)>/<entry>/`,
   named by the one S3 key it writes (namespace, sandbox, generation). The
   order inside the frozen boundary is:
   1. write and fsync `bundle.tar.gz`; write and fsync `marker.json`;
   2. rename the entry into place (published);
   3. write the in-container marker, as before;
   4. create and fsync `committed`;
   5. return `{"enabled": true, "stored": false, "upload": "pending"}`.

   A worker thread in the archive controller uploads committed entries, under
   the policy the marker pinned (reconstructing a retired service exactly as
   probe recovery does), retries after 5 s, 30 s, 2 min, then every 10 min, and
   removes the entry once S3 holds the object.
2. **The status probe is skipped for a container this acquire created.**
   `container_action == "create"` is reported only by the call that ran
   `docker create`. Such a container holds no marker it wrote: one a
   checkpoint carried reads as absent, and the capture deletes it; any other
   is refused by the capture as it would have been by the probe. Every other
   action, or none, still probes.
3. **The duplicate marker read is gone.** The controller already read the
   marker inside the same frozen boundary; the service is handed that payload
   instead of running a second guest-root exec.

## Why the invariants hold

- **Exact pre-prompt bytes.** The bundle is written while every writer is
  frozen, exactly as before; the hand-off is a Sandbox Manager-owned file that
  nothing in the container can reach. Each upload re-hashes it against the
  marker's size and SHA-256 first.
- **Stored once.** The key is unchanged and the write is still conditional
  (`If-None-Match`); a 412 is accepted only for identical metadata. A probe
  and the worker may race on one entry: an in-process per-entry claim
  serializes them, and S3 settles a cross-process race.
- **Marker semantics.** A marker in the container now means: S3 holds the
  bytes, or the frozen workspace can rebuild them (synchronous path, or a
  deferred capture that failed before committing), or a committed hand-off
  holds them. An entry is published before the marker is written and
  committed only after that write returned, so a committed entry always has
  its marker. A capture that fails before committing drops its entry and
  leaves the supervisor's existing frozen-writer preservation in charge,
  exactly as a failed synchronous upload did. Start drops uncommitted entries
  for the same reason: their capture returned no receipt, so no prompt ran.
- **Recovery.** A later attempt that probes a marker without an S3 object
  uploads the committed hand-off instead of rebuilding a workspace the prompt
  has since changed. Without one (another host, a lost disk), the probe still
  rebuilds and fails closed on drift, as before.
- **Credentials.** The upload runs in the Sandbox Manager with its own
  uploader; nothing new reaches the runner or a sandbox. The hand-off holds no
  credentials (the marker omits them).
- **Policy changes.** A deferred upload pins the marker's policy like an
  unresolved initial baseline always did; disabling archives does not abandon
  it.
- **Final archive, deletion, export.** The final archive is unchanged and
  synchronous with the stop. Nothing reads the initial object (it is an
  export/forensics boundary), so a final stored before its initial is not an
  ordering problem. The hand-off does not need the container: a group deleted
  or stopped while its upload is pending still gets its initial object.

## Compatibility

- A new runner on an older Sandbox Manager: the field is ignored (the request
  models do not forbid extra fields) and the receipt is `stored: true`.
- An older runner on a new Sandbox Manager: no field, synchronous as before.
- The runner accepts `stored: true`, or `stored: false` with
  `upload: "pending"` only when archives are enabled.

## Bounds and fallbacks

- Pending bundles plus one new bundle's worst case may not exceed 8 GiB; past
  that, or when the directory cannot be created or written, the capture
  stores before returning (`sandbox_initial_archive_handoff_unavailable`).
- A bundle that no longer matches its marker is dropped
  (`sandbox_initial_archive_handoff_corrupt`); a probe then rebuilds.

## What is deliberately left out

- **Host removal with pending entries.** Until the upload succeeds, the
  hand-off is the only copy of the pre-prompt workspace. A host removed (or a
  state disk wiped) before then loses that initial object; a synchronous
  capture would have failed the turn instead. The operations guide tells
  operators to drain the directory first. A drain gate in host removal is
  possible follow-up work.
- **Fail-closed on S3 outages.** Previously an S3 outage failed new chats'
  first turns. Now they run while bundles accumulate locally, and fall back to
  synchronous (failing) captures only once the 8 GiB budget is used. The
  operations guide records the retry event to alert on.
- **Shutdown.** Closing the controller stops the worker after the attempt in
  flight; it does not wait for the queue. Entries left are uploaded after the
  restart.
- **Compression level and the spool itself.** Unchanged (see Problem).
- **Freeze, thaw and inspection round trips.** The capture still freezes and
  thaws writers and inspects the container through the supervisor, which this
  change does not touch.
- **`initial_archive_status` for reused or restarted containers.** It still
  runs there: a marker may exist, and nothing the runner holds proves
  otherwise.
