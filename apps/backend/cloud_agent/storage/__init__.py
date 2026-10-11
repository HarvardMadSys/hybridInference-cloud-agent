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

A foreign key counts: inserting a row that references a job share-locks that
job, so a transcript message is written after its conversation is locked. Rows
that only ever change under their conversation's lock, such as its waiting
messages, are ordered by that lock. A lock taken with ``SKIP LOCKED`` or
``NOWAIT`` never waits, so it may come out of order; the locks after it may not.

Known exceptions, each safe for the reason given. The idle-only submit locks
its newest job after the group, and only once the group's newest snapshot reads
published, so no publication can be holding that job. Turns still queued
behind another are failed after the group, and after the host slot is given
back: that happens while the turn ahead of them is still running or their group
is being deleted, and a claim takes neither kind. A publication's lifecycle
event key-share-locks the attempt of its anchor turn last; that attempt has
finished, and the one writer a finished attempt can still block, an event that
arrives late, takes nothing after it.
"""
