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
messages, are ordered by that lock. A row taken with ``SKIP LOCKED`` or
``NOWAIT`` never waits for it, but the rows taken after it do.

Three exceptions are known and safe. The idle-only submit locks its newest job
after the group, and only once the group's newest snapshot reads published, so
no publication can be holding that job. Ending a turn fails its queued
descendants after giving its host slot back, and none of them can be claimed
while that turn runs. A publication's lifecycle event share-locks the attempt
of its anchor turn last; that attempt has finished, and whatever locks a
finished attempt takes nothing after it.
"""
