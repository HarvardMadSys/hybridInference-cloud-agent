"""The runner's resident-sandbox pool (HOT, design §7).

One entry per resident thread on this host: the *workspace* — workdir, state
dir, the last turn's session id — the generation the control plane fenced it
with, the residency's thread-attributed grant (design §14), and, once a live
turn has run, the *process* itself, taking follow-up turns on stdin. A thread
whose residency carries no grant still keeps the workspace: its turns restart
the CLI with ``--resume`` (warm_native), because the credential-rotation
experiments (tests/external) proved the pinned CLI cannot be re-keyed under a
live process — the grant is what makes a long-lived process billable at all.
The pool is the runner-side half of residency — the control plane's ledger is
authority, and this cache follows it: an establish that fails keeps nothing,
and a beat that comes back in ``residencies_lost`` retires the entry only if
the generation matches.

Threading: the claim loop mutates the pool; the host-heartbeat thread reads
``beats()`` and reports ``lost()``. One lock covers both — every operation is
quick (a dict and a process kill), and a pool that a heartbeat could observe
mid-mutation would report residencies it is about to drop.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_host.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Keep fewer resident workspaces than the disk has patience for. Each entry
#: pins a worktree and its session files; the budget is the eviction pressure
#: valve, LRU because the design says so (§7).
DEFAULT_RESIDENT_BUDGET = 8


@dataclass
class ResidentEntry:
    """One thread resident on this host."""

    thread_id: str
    generation: int
    #: The session the next turn resumes — updated after every settled turn,
    #: because ``--resume`` writes the continuation under a new id.
    session_id: str
    workdir: str
    state_dir: str
    runtime_id: str
    model: str
    #: The residency's thread-attributed inference grant (design §14), or
    #: ``None`` for a workspace-only residency whose turns ride warm_native.
    credential: str | None = None
    #: The model the credential is scoped to — a turn naming another model
    #: cannot ride the live process (D6: restart) but still reuses the
    #: workspace on its own per-attempt grant.
    grant_model: str | None = None
    #: The live process between turns (true HOT), attached after a settled
    #: turn when the residency has a credential. ``None`` = workspace only.
    live_session: Any | None = None
    last_turn_at: float = field(default_factory=time.monotonic)


class ResidentPool:
    """Hot sandboxes this runner is keeping alive between turns."""

    def __init__(
        self,
        *,
        establish: Callable[..., dict[str, Any] | None],
        release: Callable[[str, int], None],
        ttl_s: float,
        budget: int = DEFAULT_RESIDENT_BUDGET,
    ) -> None:
        #: ``establish(thread_id=..., runtime_id=..., runtime_version=...)``
        #: returns ``{generation, credential, model}`` — the fence and the
        #: thread-attributed grant — or ``None`` when residency was refused;
        #: refusal keeps nothing resident.
        self._establish = establish
        #: ``release(thread_id, generation)`` tells the control plane this
        #: host is done; best-effort, because the reaper is the backstop.
        self._release = release
        self._ttl_s = ttl_s
        self._budget = max(1, budget)
        self._entries: dict[str, ResidentEntry] = {}
        self._lock = threading.Lock()

    def lookup(self, thread_id: str | None) -> ResidentEntry | None:
        """The live entry for a thread, refreshed as most-recently-used."""
        if not thread_id:
            return None
        with self._lock:
            return self._entries.get(thread_id)

    def touch(self, thread_id: str) -> None:
        """Mark a turn served, resetting the entry's idle clock."""
        with self._lock:
            entry = self._entries.get(thread_id)
            if entry is not None:
                entry.last_turn_at = time.monotonic()

    def offer(
        self,
        *,
        thread_id: str,
        session_id: str,
        workdir: str,
        state_dir: str,
        runtime_id: str,
        runtime_version: str | None,
        model: str,
    ) -> int | None:
        """Keep a just-finished turn's workspace resident, if the CP agrees.

        Returns the generation now held for the thread — the caller's receipt,
        which run_once redeems at finish: a residency established (or
        refreshed) by an attempt that never confirms success is dropped, so a
        retry can never resume a session the failed attempt already advanced.
        An entry already resident is refreshed in place — new session id,
        fresh idle clock, no second establish. A new thread over budget
        evicts the least-recently-used entry first.
        """
        with self._lock:
            existing = self._entries.get(thread_id)
            if existing is not None:
                existing.session_id = session_id
                existing.last_turn_at = time.monotonic()
                return existing.generation
        granted = self._establish(
            thread_id=thread_id, runtime_id=runtime_id, runtime_version=runtime_version
        )
        if granted is None:
            return None
        generation = granted["generation"]
        evicted: list[ResidentEntry] = []
        with self._lock:
            stale = self._entries.pop(thread_id, None)
            if stale is not None:
                evicted.append(stale)
            while len(self._entries) >= self._budget:
                oldest = min(self._entries.values(), key=lambda entry: entry.last_turn_at)
                evicted.append(self._entries.pop(oldest.thread_id))
            self._entries[thread_id] = ResidentEntry(
                thread_id=thread_id,
                generation=generation,
                session_id=session_id,
                workdir=workdir,
                state_dir=state_dir,
                runtime_id=runtime_id,
                model=model,
                credential=granted.get("credential"),
                grant_model=granted.get("model"),
            )
        for entry in evicted:
            self._retire(entry, tell_control_plane=True)
        return generation

    def drop(self, thread_id: str, *, release: bool, generation: int | None = None) -> None:
        """Tear a residency down now (restart path, lost beat, mismatch).

        With ``generation``, the drop redeems a specific receipt: it removes
        the entry only while that generation still holds it, so a stale
        cleanup path can never end a successor another turn has since
        established (review: the receipt must be generation-scoped all the
        way down, not just returned).
        """
        with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None:
                return
            if generation is not None and entry.generation != generation:
                return
            del self._entries[thread_id]
        self._retire(entry, tell_control_plane=release)

    def beats(self) -> list[dict[str, int | str]]:
        """What the host heartbeat reports still alive here."""
        with self._lock:
            return [
                {"thread_id": entry.thread_id, "generation": entry.generation}
                for entry in self._entries.values()
            ]

    def handle_lost(self, lost: list[dict[str, Any]]) -> None:
        """The control plane said these *generations* no longer hold anything.

        Generation-matched on purpose: a heartbeat request carrying gen N can
        be answered after this host has already re-established gen N+1, and a
        thread-id-only drop would let that stale answer kill the successor.
        A loss that names no generation is ignored for the same reason — an
        answer that cannot say *which* residency it means cannot end one.
        """
        for item in lost:
            thread_id = str(item.get("thread_id") or "")
            generation = item.get("generation")
            if not thread_id or not isinstance(generation, int):
                continue
            with self._lock:
                entry = self._entries.get(thread_id)
                if entry is None or entry.generation != generation:
                    continue
                del self._entries[thread_id]
            logger.info(
                "agent_residency_lost",
                extra={
                    "event": "agent_residency_lost",
                    "thread_id": thread_id,
                    "generation": generation,
                },
            )
            # No release call: a lost generation has nothing left to release.
            self._retire(entry, tell_control_plane=False)

    def sweep(self) -> None:
        """Local TTL: retire entries idle past the lease the CP granted.

        The reaper would lapse them anyway; sweeping locally returns the
        memory without waiting for a beat to be refused.
        """
        cutoff = time.monotonic() - self._ttl_s
        with self._lock:
            expired = [
                thread_id
                for thread_id, entry in self._entries.items()
                if entry.last_turn_at <= cutoff
            ]
        for thread_id in expired:
            self.drop(thread_id, release=True)

    def close_all(self) -> None:
        """Drain: release and tear down everything (shutdown, SIGTERM)."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._retire(entry, tell_control_plane=True)

    def attach_session(self, thread_id: str, generation: int, session: Any) -> bool:
        """Hand the entry its live process — only its own generation.

        The previous live session, if any, is closed: two processes over one
        workspace would interleave writes into the same session tree.
        """
        with self._lock:
            entry = self._entries.get(thread_id)
            if entry is None or entry.generation != generation:
                stale = None
            else:
                stale = entry.live_session
                entry.live_session = session
        if entry is None or entry.generation != generation:
            _close_quietly(session)
            return False
        if stale is not None and stale is not session:
            _close_quietly(stale)
        return True

    def detach_session(self, thread_id: str) -> None:
        """Drop the live process but keep the workspace resident (warm)."""
        with self._lock:
            entry = self._entries.get(thread_id)
            stale = entry.live_session if entry is not None else None
            if entry is not None:
                entry.live_session = None
        if stale is not None:
            _close_quietly(stale)

    def _retire(self, entry: ResidentEntry, *, tell_control_plane: bool) -> None:
        if entry.live_session is not None:
            _close_quietly(entry.live_session)
        if tell_control_plane:
            try:
                self._release(entry.thread_id, entry.generation)
            except Exception:
                logger.warning(
                    "agent_residency_release_failed",
                    extra={
                        "event": "agent_residency_release_failed",
                        "thread_id": entry.thread_id,
                    },
                )


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:  # teardown must not take the runner down
        logger.warning(
            "agent_resident_session_close_failed",
            extra={"event": "agent_resident_session_close_failed"},
        )
