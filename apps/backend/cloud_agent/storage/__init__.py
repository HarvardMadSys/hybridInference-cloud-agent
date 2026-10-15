"""The control plane's own database: job, thread, attempt and host state.

Owned outright by this service. The gateway holds none of it after the split —
which is why the attempt fence lives here and grant renewal there consults no
attempt state.

**Lock order.** A transaction that locks more than one of these rows takes
them in this order, skipping the ones it does not need. Two writers that take
the same two rows in opposite orders deadlock, and Postgres aborts one of them:

1. the runner policy row, then the compute topology fence;
2. a Fork operation, or the snapshot a capture, publication or base import has
   claimed;
3. the attempt;
4. the conversation (``agent_threads``), several in id order;
5. its jobs, several in id order;
6. the conversation's sandbox membership, several in thread id order;
7. the sandbox group;
8. the group's pending snapshot;
9. the runner host row.

A foreign key counts: inserting a row that references a conversation or a job,
or pointing a row's reference at another one, share-locks it. So a transcript
message is written after its conversation is locked, a chat's first
reservation takes its conversation before its turn, and a capture handed back
moves its turns' rows to the pending snapshot rather than writing new ones. Rows
that only ever change under their conversation's lock, such as its waiting
messages, are ordered by that lock. A lock taken with ``SKIP LOCKED`` or
``NOWAIT`` never waits, so it may come out of order; the locks after it may not.

An owner's environments, their installation snapshots and the environment runs
that build them have an order of their own: the owner's environment lock, then
their snapshot lock, then the snapshots, then the runs. A write that locks more
than one snapshot, or a snapshot and its run, takes the snapshot lock first
(``lock_environment_snapshots``): a statement over several snapshots locks them
in whatever order its plan scans them. Publishing a configuration holds the
environment lock when it retires the snapshots of installations it changed.
A team environment's snapshots belong to whichever members built them: a write
over several of them takes every one of those owners' snapshot locks, ordered
by account (``lock_team_environment_snapshots``), before any of the rows.

Known exceptions, each safe for the reason given. The idle-only submit locks
its newest job after the group, and only once the group's newest snapshot reads
published, so no publication can be holding that job. Turns still queued
behind another are failed late, after the group or the host slot: ending,
releasing, reaping or abandoning a turn fails the ones queued behind it, and
completing an admin deletion fails the ones in its group. Nothing holds those
turns then -- the turn ahead of them is still running, or their group is being
deleted -- and a claim, the one writer that holds a queued turn while it waits
for a group and a host, takes neither kind. Deleting a conversation does hold
its queued turns on the way to their membership, so each late failure first
takes the turns' conversation, where deletion starts. A publication's outcome event
key-share-locks the attempt of its anchor turn last; that attempt has finished,
and every lock that conflicts with a key share is taken on running attempts
only.

A cycle is fixed by fixing the order. An event append, whose loss its runner
takes for a lost turn, is also run again when Postgres aborts it as a
deadlock's victim; see :mod:`cloud_agent.storage.deadlocks`. A publication
record lost that way is left to the publisher's own retry.
"""
