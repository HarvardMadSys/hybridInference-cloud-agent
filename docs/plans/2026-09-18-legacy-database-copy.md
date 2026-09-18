# Preserve a surviving pre-baseline database

The September 10 baseline rewrite assumed all old-chain databases were rebuilt.
A production audit found a database still at `0025_retained_followup_gate` with
jobs, messages, credentials and retained state. The existing rebuild procedure
preserves only selected account/host state and is not suitable for this database.

## Conversion contract

- Operate only on a separately restored database whose name starts with
  `agent_legacy_copy_`. Require its exact name and a dedicated DSN variable;
  never fall back to `AGENT_DATABASE_URL`. Refuse other connections, active jobs,
  unsupported revisions, or a previous conversion. Default to read-only audit.
- Keep the complete source schema as `legacy_archive`. Create the public schema
  from the repository's real baseline, copy compatible rows, then apply the real
  additive migrations. Do not soften constraints or fake an Alembic stamp.
- Derive attempt thread IDs and membership owners from their existing parents.
  Restore circular current-attempt pointers after inserting attempts. Expand
  snapshot membership JSON into the new association table. Restore serial
  sequence high-water marks so new writes cannot collide with historical IDs.
- Preserve retired tables/columns in the archive. Old `replay` telemetry is kept
  there; the new continuity field is NULL for those historical attempts because
  neither supported native mode describes them. Refuse unknown mode values.
- Verify every source row still exists unchanged in the archive; compare common
  live fields and explicit transformations, owners, credentials, relationships,
  content hashes, table counts and sequences. Run both valid and negative cases
  against PostgreSQL, including transaction rollback on a constraint failure.
- Namespace adoption remains a separate, explicit operation after verifying the
  original Gateway issuer. The converter must not guess it or automatically bind
  it. A converted but unbound database is not ready to serve traffic.

## Rehearsal and cutover

The first rehearsal uses a full production dump restored into a PostgreSQL
container with no network and no published port. No application or runner is
attached to the copy. Exercise current-version queries against the copy after
conversion. Prove a fresh write can allocate IDs beyond historical values.

At cutover, take a fresh consistent snapshot after draining and stopping writers,
repeat the rehearsed conversion into a new database, explicitly bind the verified
issuer, and point only the Agent service at the new database. Preserve the old
live database and full dump. Before admitting new work, validate the service.
After new writes arrive, rollback cannot discard them or point old code at the
new schema; a recovery must account for the delta. This document does not execute
or authorize a production cutover.
