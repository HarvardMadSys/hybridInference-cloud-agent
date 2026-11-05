# First DELETE after the release commit

The release commit follows the inventory, authorization, quiescence and fencing
checks. `ComputeRelease.committed` nevertheless performed another provider read
before its first DELETE. A transient read failure could therefore prevent an
already authorized deletion, even when DELETE itself could succeed.

The first DELETE now proceeds directly against the committed immutable resource
identity. Its durable submission marker is still written before the request. A
previous submission with an unknown outcome retains the existing observation
before replay; a pending resource is polled without another DELETE. A known
rejection clears the marker, while a failed observation after DELETE does not.
Saved acknowledgements and terminal observations retain their existing behavior.
The pre-commit checks, TLS verification and interpretation of deletion evidence
are unchanged.

## Isolated verification

Base: `b3b92d1ff6ad96c4c74006b50f809e6596593f26`.

- Seven new regression cases fail on the base, with only those tests added.
- The candidate passes all 126 focused reconciler and release-evidence cases.
- `make check`: 12,355 passed; seven existing cases require root and UID switching
  and are skipped in this unprivileged test environment. Formatting, lint and
  type checking pass.
- Database-backed release-evidence and terminal-observation tests: 12 passed,
  zero skipped, on fresh databases allocated to the isolated checkout.

The first full check inherited SSH environment markers that caused two workflow
tests' nested shell to resolve the real command instead of their temporary test
substitute. Both unchanged tests pass after clearing those markers for the test
process; the complete check above was rerun with that environment. No shell
configuration or test assertions were changed.

The regressions cover a missing or explicitly false submission marker, confirmed,
pending and unknown receipts without observations, known pre-submission rejection,
and a lost DELETE response. Existing replay and pending-resource tests remain
unchanged. These are isolated validation results; they do not claim deployment or
live acceptance.
