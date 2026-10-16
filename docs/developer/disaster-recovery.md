# Backup and recovery

Back up the agent database, its encryption keys, and retained host state as a
coordinated recovery set. A database dump cannot reconstruct a native CLI
session whose Docker writable layer was lost. Restorable checkpoints in S3
cover only groups that stopped cleanly with archives enabled, and CloudLab node
rotation helps move an available source; neither replaces an off-node backup.

The deployment owner must implement backup storage and scheduling. This guide
does not install a backup service, claim a successful restore drill, or establish
measured recovery times.

## Initial operating policy

Use these initial targets until the deployment owner records different ones and
validates them with a restore drill. Retention alone does not meet a recovery
objective: alert on a failed or late backup and on failed integrity checks.

| Recovery material | Frequency and retention | Initial recovery objective |
| --- | --- | --- |
| Agent Postgres database | Hourly encrypted dumps for 24 hours; daily copies for 30 days; monthly copies for 3 months | Database recovery point at most 1 hour old |
| Coordinated database and retained host state | Daily off-node recovery set for 7 days; weekly set for 4 weeks | Native-session recovery point at most 24 hours old |
| Configuration, encryption keys, host credentials, SSH trust, release locks and recipes | Capture with every recovery set and before every change; retain while any dependent backup/release is retained | Recover the exact keys and identities used by the selected set |
| Previous verified artifact release | Keep the current and previous successful release, plus every release referenced by retained recovery sets | Available without rebuilding or relying on mutable image tags |

The initial recovery-time target is 4 hours after choosing a usable set. Large
Docker stores can exceed this; measure the copy/restore time, provision enough
capacity, and revise the target. More frequent database dumps do not imply a
1-hour native-session objective: a full restore that includes retained chats
must use a coherent host/database set. Use Postgres-supported continuous WAL
archiving if the database needs a shorter recovery-point objective.

Keep encrypted backups outside the machines and CloudLab allocation they
protect. Restrict access, separate backup decryption material from the stored
backups, monitor available space, and verify hashes after transfer. Do not
delete an encryption key or registry image still needed by a retained set.

## What belongs in a recovery set

Record a set ID, start/end UTC times, source host machine identities, source
commit, database revision, deployment protocol, Docker/storage-driver versions,
data-root paths, container inventory, file hashes, and the release manifest hash.
This inventory contains identifiers, not prompts, tokens, or credential values.

Preserve all of the following privately:

- A full agent-owned Postgres backup, including settings, identity namespace,
  enrolled hosts, placement/ownership, jobs and attempts, connections, and compute
  lifecycle records. Do not select only user-facing conversation tables.
- Both deployment roots' `.env` and mounted credential files. Preserve
  `AGENT_SETTINGS_ENCRYPTION_KEY`, or the original `AGENT_SESSION_SECRET` when
  used as its fallback; also preserve source-control and compute encryption
  keys, control/session signing secrets, sandbox manager credentials and GitHub App keys.
  Saved settings can hold some of these keys, but they still require the
  settings root key to decrypt. Generating new keys does not recover old data.
- Each host's persisted enrollment credential directory and deployment identity,
  `.deployed-sha`, readiness marker, namespace/project, tunnel SSH credentials
  and verified `known_hosts`, and host runtime/storage configuration. The
  credential must match the restored host row's credential hash.
- The complete retained Docker store and the containerd image store. With the
  supported bind layout those trees are `<disk>/docker` and
  `<disk>/containerd`, while `docker info` still reports
  `DockerRootDir=/var/lib/docker`. Copy both with Docker and containerd
  stopped. Preserve ownership, modes, hard links, extended attributes and
  sparse files. Keep the thread-to-host-to-container identities in the matching
  database; exporting a container filesystem alone omits required metadata.
- The verified `release.json`, its reviewed pinned SHA-256, `recipe.tar.gz`,
  extracted recipe, helper dependencies and image digests for both the current
  and previous serving releases. Keep digest-addressed images available in the
  registry or a tested private archive.

Never place these backups in the repository or attach credential archives to
issues or CI logs. A read-only Docker socket does not make raw overlay copying
safe. Follow [retained Docker storage](operations.md#retained-docker-storage-on-a-host).

## Capture a coordinated set

1. Record current deployment markers, placement, host status, container
   inventory and encryption-key version identifiers. Schedule a maintenance
   window and prevent new admissions and admin configuration changes. Drain
   hosts and wait for active attempts and publications to finish; record which
   hosts were already draining or disabled so their state is preserved.
2. Stop the main API and MCP listener after draining. This also stops compute
   reconciliation and background writers. Stop retained sandbox containers and
   each host stack, then stop `docker.socket`, Docker and containerd before
   taking a host-state snapshot or copy. No source host may keep writing while
   its recovery set is captured.
3. Take a full database dump while the listeners remain stopped. From the
   control-plane deployment root, with the existing generated Compose file and
   project selected, this writes no credentials to the command output:

   ```bash
   umask 077
   export BACKUP_DIRECTORY=/path/on/encrypted-backup-storage/set-id
   mkdir -p "${BACKUP_DIRECTORY:?}"
   docker compose -p "${COMPOSE_PROJECT:?}" --env-file .env \
     -f "${COMPOSE_FILE:?}" exec -T postgres \
     sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
     > "${BACKUP_DIRECTORY}/agent.dump"
   ```

   Capture configuration and the stopped host storage into that same set using
   the approved encrypted backup tooling. Transfer it off-node, record hashes,
   and verify the archive and dump are readable before treating it as usable.
4. Start the original daemons and both listeners, then start host stacks and run
   host checks; their live probes require the API and MCP listener. Verify
   identity, sandbox manager access and readiness. Restore only the
   drain states changed for this maintenance window, then reopen admissions.
   A failed backup does not authorize deleting or replacing the source state.

An hourly database-only dump can be taken while Postgres serves requests;
`pg_dump` supplies a consistent database snapshot. Label it as database-only.
Do not combine it with a host snapshot from another time and claim a coherent
native-session restore.

## Restore and verify before reopening

1. Keep ingress closed and replacement host daemons stopped. Isolate recovered
   compute controllers from provider side effects until allocation/node
   inventory is reconciled: an old database can refer to expired allocations.
   Confirm source hosts are stopped or fenced; two machines must never serve
   the same host credential and retained namespace simultaneously.
2. Verify the selected set's hashes and decryption material. Restore into an
   isolated database first, using compatible Postgres and the selected release.
   Restore the matching configuration/root keys before starting a listener.
   Follow [identity binding](minimal-identity.md) and the existing
   [database recovery procedures](operations.md#preserving-an-old-chain-database)
   for historical schema chains. Do not improvise an Alembic downgrade.
3. Restore the complete stopped host state with a compatible Docker/storage
   driver and the correct mounts. Check mount availability before starting
   Docker. Retain the matching enrollment credential, namespace, and container
   IDs. Start and inspect the owning daemon without admitting new tasks; do
   not run generic prune or orphan cleanup to make mismatched state look empty.
4. Compare every retained group's database owner and recorded container identity
   with the restored Docker labels. Reconcile stale attempts, leases and compute
   operations through their supported recovery paths. A missing container or
   credential mismatch is a blocked recovery, not evidence that another host
   can recreate the native session.
5. Run namespace verification and Kata/candidate checks, then use the phased
   deployment sequence to bootstrap listeners before host live checks. After
   host state and live checks pass, restore only hosts previously eligible for
   work to `online`; the exact-generation readiness fence requires an online
   host and reverse-sandbox-manager probes. This can resume work already queued in the
   restored database. Keep external ingress closed while completing activation
   and checking the restored system. Verify sign-in, file access,
   retained continuation, separate Fork context, a queued turn, cancellation,
   publication, and a host restart with disposable test conversations. Reopen
   production admissions only after these checks and external health/queue
   monitoring pass.

Perform an isolated restore drill quarterly and after changing storage topology,
key handling or deployment protocol. Record the set/release IDs, checks, elapsed
time, observed lost-data window and failures without secrets. A static test,
readable dump, manual node rotation or backup hash is not restore-drill evidence.
Moving retained sessions to another host otherwise depends on compatible full
host-state restoration. The exception is a stopped group whose current
[restorable checkpoint](operations.md#restoring-a-removed-hosts-sandboxes) is
in S3: once its host is removed from the pool, its node is confirmed
terminated, or the host moves out before its lease ends, another host restores
it on the group's next turn. If the source disk, its off-node copy and any checkpoint are
all lost, native context cannot be recovered from Postgres.

## Recover a previous artifact release

The host deployer does not automatically restore an older artifact manifest: it
is handed only one release. The consuming distribution must retain previous
verified release material outside `DEPLOY_ROOT`, alongside the reviewed lock.
Keep each release in a separate immutable directory; do not overwrite the
previous recipe with a candidate that failed.

Before rollback, verify the previous manifest's pinned hash, the recipe hash,
registry digests/platforms/labels and deployment protocol. Check the candidate's
`compatibility.verified_rollback_to` against the previous commit and the previous
release's compatibility with the database now installed. Empty compatibility
records do not establish a verified online rollback. An older recipe must not
run migrations backwards or cross the deployment-protocol fence; use a tested
stopped-writer recovery procedure when online compatibility is unproved.

For a compatible rollback, on **each** machine select its previously verified
directory and stable deployment root:

```bash
export AGENT_DEPLOY_MODE=artifacts
export RECOVERY_RELEASE=/opt/cloud-agent/releases/previous-verified-release
export AGENT_RELEASE_MANIFEST="${RECOVERY_RELEASE}/release.json"
export AGENT_DEPLOY_PYTHON=/opt/cloud-agent/deployer/bin/python
export DEPLOY_ROOT=/path/to/this-machines/persistent-deployment
```

Run the [phased deployment sequence](../../deploy/release/README.md#deploying-the-two-in-the-order-the-design-specifies)
using `"${RECOVERY_RELEASE}/recipe/deploy/control-plane/deploy.sh"` on the
control-plane machine and `"${RECOVERY_RELEASE}/recipe/deploy/host/deploy.sh"`
on its execution host: prepare both, prepare the database, activate the host,
then activate the control plane. All exact-generation and isolation checks
remain in force. Database preparation runs `upgrade head` from the selected
recipe; Alembic will refuse unknown newer revisions, so verify this against the
current schema in isolation before choosing online rollback. Do not delete or
stamp migration history to bypass a refusal. A rollback may therefore require
an explicitly tested compatible recipe or full stopped-writer recovery.

Read both `.deployed-sha` files and host readiness after success. Record the
incident as a failed candidate rollout even when the previous release serves
again. Verify retained continuation and sandbox manager access before reopening traffic.
