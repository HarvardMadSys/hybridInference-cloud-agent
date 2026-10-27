# Release removes the runtime download work its runner left

Stopping a runner that had just started a runtime download left an exited
container labelled `org.freeinference.runtime-download.work=<compose project>`.
Formal quiesce stopped the runner and removed self-test sandboxes only, so the
formal inventory then counted the container as unmanaged and refused release.
The node had no user jobs. The inventory was right to refuse it: an unknown
container is not safe. This change makes quiesce remove that work and leaves
the inventory as it was.

## Producer contract

`RuntimeDownloads` is the only producer of this label with a namespace value,
and only the runner creates a `RuntimeDownloads`, from
`AGENT_SANDBOX_NAMESPACE`. On a managed host that variable is the Compose project
quiesce already uses. It labels four containers `work=<namespace>`: the fetch
and install-script containers (`_container`), the container a kept tree is
committed from (`_keep_tree`), and the candidate check (`runtime_probe`, given
the same label by `_install`). They are made from the sandbox image, a commit
of the fetch container, or the candidate image. So they carry only the sandbox
image's `org.freeinference.runtime.*` and `org.freeinference.helper.*` labels
and the download's own `work`, `namespace`, `runtime` and `version` labels.

The commit sets an empty `work` label on kept versions. A chat's copy source
(`tree_copy`) sets an empty `work` label and its own `source` label. The work
filter matches neither. A version kept by an earlier release can still carry
a non-empty work label, and so can a chat, snapshot or copy source made from
it. Those need the conflict checks below.

## Change

After the runner is proved stopped, which is unchanged, `_QUIESCE`:

1. lists `docker ps -a` with the exact filter
   `label=org.freeinference.runtime-download.work=<project>`;
2. reads each candidate's full label map with one `docker container inspect`
   and requires the inspected IDs to equal the listed IDs, in order;
3. keeps only candidates whose `work` label equals the project exactly, whose
   `namespace` label, if present, equals the project, and which have no
   `com.docker.compose.*` label, no `hybridinference.*` label, and no other
   `org.freeinference.*` label. This rejects chat, probe, snapshot, checkpoint
   and network labels (`chat-*`), `sandbox-namespace`, `runtime-download.source`,
   `storage-attestation`, and any label this platform adds later;
4. removes the kept candidates with `docker rm --force`. A running fetch has
   no client once its runner is gone.

A conflicting candidate is left alone and quiesce still succeeds. The inventory
counts it as retained or unmanaged, as it did before. Any listing, inspect,
parse, ID mismatch or removal error answers `host_quiesce_failed`. The control
plane treats that as retryable, and the inventory does not run.

`probes_removed` still counts self-test sandboxes only. No work count was
added: `SshHostBootstrap.quiesce` returns one integer, and the operation record
and admin view whitelist fields (`domain/compute.py`). Reporting work removed
would mean changing those, which is outside this fix.

## Verification

Regression reproduction used base `5ca72158e64e9dabe339ca1d07af8de6710f7348`.
Final validation ran in a separate isolated worktree on
`ac666ba9e3e69bf8fed676a4598a340abc7a2264`, with its own `.venv`.

- `pytest tests/unit/test_compute_host_bootstrap.py -k quiesce --maxfail=1000`:
  the 11 new cases fail on the base script and pass with the fix. They cover:
  removing this deployment's running, exited and check work; keeping other
  namespaces, longer namespace values, unlabeled, empty-work, chat-, snapshot-,
  `sandbox-namespace`-, source-, Compose-, foreign-namespace-, attestation- and
  `hybridinference.*`-labelled candidates; failing on list, inspect and
  removal errors; and failing on malformed, partial, mismatched or empty
  inspect output. The existing runner-still-running case now also holds a
  work container. It asserts that nothing but `stop` ran, so the container
  stays.
- `pytest -m external tests/external/test_host_release_real_docker.py`
  (Docker 29.1.3, privileged DinD node, `alpine:3.20`): 4 passed with the fix.
  On the base script, the two changed tests fail: the four work containers
  (running fetch, exited scripts, created tree, created check) remain. The new
  test removes them, `quiesce` returns 0, and the inventory is complete with no
  blocked reasons. The extended preservation test keeps another namespace's
  full download, a copy source, a copy source and an owner chat carrying this
  project's work label, and a check naming another namespace. Its inventory is
  `(retained, unknown) == (3, 8)`. Labels come from the producers:
  `RuntimeDownloads._container` and `_keep_tree`, and `tree_copy.staged_tree`,
  run against refusing or no-op Docker stand-ins.
- Final `make check`: formatting, lint and type checks passed; 12,348 tests
  passed, with 7 root/UID prerequisite skips (2026-10-26 04:37 UTC). This run
  includes the final malformed-empty-label-map regression. The final quiesce
  unit suite was also repeated against the original script: 11 failed,
  8 passed, then the fixed script was restored.
- `make test-db-local ARGS="tests/integration/storage/test_superseded_containers.py"`,
  which shares the extended Docker fake: 17 passed, 0 skipped.

## Limitations

- The DinD test cannot keep a runner running through `docker stop`. The guard
  that a running runner prevents cleanup is a unit test only. That ordering
  existed before this change.
- Candidate and kept images (`*-candidate:*`) are not removed. The inventory
  does not count images, and the runner's own `_remove_leftovers` handles them
  when it starts.
- A custom sandbox image with its own `org.freeinference.*` label outside
  `runtime.` and `helper.` would make its work containers look foreign. Release
  then refuses and does not delete them. The current sandbox Dockerfile sets
  only `runtime.*` and `helper.*`.
