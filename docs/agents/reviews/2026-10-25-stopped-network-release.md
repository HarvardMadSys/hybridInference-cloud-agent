# Stopped network intent blocked active release

The October 25 staging acceptance failed before expiry. Its saved evidence
showed two stopped containers with empty endpoint IDs whose Docker network
settings still named bridges that no longer existed. Manager refusals named
the final archive and lifecycle-check barriers. This record covers a repair
and isolated reproduction; it does not reclassify that failed acceptance.

## Reproduction

On a disposable Docker 29.9.0 daemon, stopping a container removes it from
`network inspect`'s active `Containers` map. Its own `NetworkSettings.Networks`
retains the bridge name and network ID with an empty endpoint ID. Removing
the bridge succeeds, leaving that saved container setting behind. Disconnect
by the missing network's ID fails; disconnect by its name clears the setting.

At base `35c4dbf4`, the actual manager release path failed both normal-stop and
already-removed-bridge regressions. The supervisor regression repeated final
archive recovery on every listing: three listings called finalization three
times. The hook in that regression represents an already-completed checkpoint;
Docker inspection, network release, recovery and discard use product code.
No inference call or archive upload is part of this isolated reproduction.

The cause is the combination of an incomplete endpoint enumeration and an
early return: release disconnected only active endpoints before removing the
bridge, then returned successfully on retries when the bridge was absent.
The supervisor correctly retained the saved bridge name as unfinished archive
intent, repeatedly reserving recovery and blocking discard.

## Repair and safety

Release now includes the exact stopped container's saved network intent in
the disconnect set, even when Docker omits its endpoint. An already-missing
bridge is recovered by clearing the exact container's old setting by name.
Both paths retain container, name, generation, stopped-state and network-ID
checks. An unreadable network, changed identity or known same-name replacement
fails closed. Opportunistic pruning does not gain authority to detach stopped
sandboxes.

A fresh container inspection must show the group network setting gone before
release reports success. Real archive or network failure still prevents
discard. New refusal categories are fixed safe values in text and JSON logs;
no Docker error text or credentials are added to logs.

## Validation

- Remote isolated `make check`: 12,325 passed, 7 skipped (UID/root prerequisites),
  90.17 seconds for pytest; formatter, lint and type checks passed.
- Real Docker regression: both old-code failures reproduced. The final suite
  passed all 9 tests in 153.78 seconds, including one-time recovery followed
  by actual discard, preservation of a replacement network's running peer,
  host-release fencing and checkpoint restore.
- Dedicated unit regressions cover silent ineffective disconnects, read
  failures, identity/generation/record changes, restart races, pruning,
  repeated listing and genuine archive/network failures.
- Independent Claude Code review used `claude-opus-5-5`, high effort. Its
  normal-path Docker question was verified by the real-Docker test, and the
  suggested fresh network-ID check was included.
- The real-Docker regressions are added to the release job in CI.

An exact deployed staging revision and a new active-release acceptance are
still required. Passing isolated tests does not establish that the runner
stops, all receipts survive, the formal inventory passes, or resource release
finishes before expiry on a real allocated node.
