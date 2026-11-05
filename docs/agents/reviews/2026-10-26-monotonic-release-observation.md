# Release terminal observations and evidence

The previous real-resource acceptance remains failed: the resource did not have
consistent formal termination evidence before its lease deadline. Cleanup and the
retire operation succeeded, but that does not settle the acceptance result or
independently establish the resource's actual final state. This change preserves
that record and does not retry any old operation.

## Reproduction and cause

The normal dispatcher serializes operations for an allocation. A deterministic
PostgreSQL workflow queues retire, then inspect, lets retire finish, and returns
an older pending, ready or unknown provider observation to the queued inspect.
All three cases overwrite terminated on base `37e95975ef927ebf042e81e6a850ccf86290eb3e`.
They preserve the first terminal record with this change. The earlier live
completion timestamps do not independently prove overlapping workers; overlap
is unnecessary to reproduce the defect.

The stale record was also stranded: scheduling skipped release-committed
allocations and storage refused even inspection operations after commitment.
The fix permits only inspection in that state and retains scheduling deduplication.
It does not schedule renewal, upgrade, rotation or another release.

## Guarantees and boundaries

- Storage checks the worker fence and immutable resource identity before keeping
  the first terminal observation. A later read or read error cannot replace its
  state, source or recording time. Local absence without a resource identity can
  still yield to discovery of an identity.
- A release acknowledgement is checkpointed on the operation before persisting
  the allocation observation. Recovery uses it only for the same operation and
  resource identity, without another provider read or DELETE. A crash before the
  acknowledgement checkpoint remains an unknown outcome; this does not make an
  external request and a database write atomic.
- Public `result.release_evidence` distinguishes `delete_acknowledged`,
  `provider_terminal`, `expired_not_found` and `never_created`. It includes only
  the safe identity, source, actual HTTP status when available, and time.
  `observed_at` is the provider observation time; `recorded_at` is the database
  recording time when recovering an older terminal allocation without its
  original operation receipt. Raw responses, configuration and credentials are
  excluded.
- A 404 within the lease and an unknown provider outcome remain inconclusive.
  Expiry-derived termination remains explicitly marked and cannot establish
  active-release acceptance before the deadline.

## Verification

Tests use isolated PostgreSQL and inert provider/host ports; no real resource is
created or deleted by these tests. The three serialized workflow reproductions
fail on the base. Additional regression tests cover acknowledgement recovery,
read-only reconciliation, late read errors, identity mismatch, lost worker
leases, expiry inference and never-created resources. Refusal tests also pass on
the base and are reported separately from new red-to-green regressions.

Independent evidence review found and fixed malformed-source projection errors,
misclassification of terminal observations as DELETE acknowledgements, and the
mislabeling of database recording times as provider observation times.

The existing removal test's setup formerly revived a terminal fixture through
`observe_allocation`; it now initializes its live fixture with SQL. The protection
assertion is unchanged. File-size and complexity limits have not been increased.

Full gate results and immutable tested revision are recorded in the pull request.
The ordinary test tier has seven root-dependent skips; the full database tier
must have zero skipped tests. Successful isolated checks are not live acceptance.

## Next real acceptance gate

Use a new run identity only after exact-revision CI and formal deployment pass.
Retain the original two-agent baseline, cleanup, credential-removal and checkpoint
requirements. Require public successful operation evidence and a stable formal
terminal allocation before expiry. Allow at most five minutes after retire
acceptance for this release check, with no automatic retry or duplicate DELETE;
a hard failure ends the run immediately with evidence. Observe the terminal
record across at least two normal observation intervals. Expiry, not-found or a
post-failure diagnostic scan cannot turn the run into a pass.
