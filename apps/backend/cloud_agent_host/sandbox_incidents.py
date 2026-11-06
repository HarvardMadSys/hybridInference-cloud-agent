"""Observe what goes wrong in retained sandboxes, and hold it until it is stored.

Three sources, one outbox (``docs/agents/specs/2026-11-04-sandbox-incidents.md``):

- **Docker events.** One long-lived subscription to ``/events`` sees each
  container's ``oom`` (the cgroup's OOM killer killed some process in it) and
  ``die``. A ``die`` the platform caused (a stop, an idle stop, a deletion) is
  preceded by Docker's ``kill``, and is not an incident.
- **Resource readings.** The resource collector hands every reading over; one
  that crosses 90% of a limit is ``memory_high`` or ``pids_high``, re-armed
  only below 80%.
- **In-sandbox journals.** The browser service and each slot's MCP runner
  append unrequested failures to a bounded JSONL journal
  (``append_incident`` in their scripts). The host reads them, in one exec
  per sandbox, from the last ``(journal, seq)`` it holds: every minute while
  the container runs, and whenever Docker reports an incident for it.

Each observation becomes a final row (``cloud_agent_shared.sandbox_incidents``):
the first of a kind for a subject at once, repeats within the next minute as
one ``.repeated`` row when the window closes, and rows past a sandbox's hourly
cap as one ``dropped`` row. Nothing is counted by changing a sent row. An
observation is counted once, by the id of what it came from: Docker replays
its last events after a reconnect, and a replay is neither a new incident nor
a repeat. The outbox holds a sandbox's rows until a heartbeat answer confirms
them, so a failed beat sends them again and the control plane's unique index
makes the replay a no-op. A restarted host loses what it had not sent.

A member-scoped row (an MCP program, which runs per slot) carries the
``thread_id`` of the member it happened to. A slot is reused, so that is not
read off the slot when the journal is read: the runner journals the digest of
the credential of the member it served (its admission's ``token_sha256``), and
the host knows whose credential that was from admitting it
(:data:`MEMBER_CREDENTIALS`). A failure whose credential the host never
admitted, or has forgotten since it restarted, is attributed to nobody and not
recorded.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.observe.resources import daemon_client
from cloud_agent_shared.sandbox_incidents import (
    DROPPED_KIND,
    MCP_PHASES,
    MEMBER_KINDS,
    REPEATED_SUFFIX,
    SandboxIncident,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloud_agent_host.observe.sandbox_resources import DockerResourceReading

logger = get_logger(__name__)

#: Repeats of a kind for one subject within this long become one summary row.
WINDOW_S = 60.0
#: Rows one sandbox may send an hour, summaries included; the rest are counted.
HOURLY_CAP = 60
#: Rows one sandbox's outbox holds unconfirmed; the oldest go first, counted.
OUTBOX_MAX = 64
#: How often a running sandbox's journals are read.
DRAIN_INTERVAL_S = 60.0
#: A reading at this share of its limit is ``*_high``; it re-arms below the second.
HIGH_SHARE = 0.9
REARM_SHARE = 0.8
#: A ``die`` within this long of Docker's ``kill`` for the same container was asked for.
KILL_GRACE_S = 120.0
MAX_TARGETS = 4096
#: Source ids remembered so a replay is not counted again: far more than one
#: replay window can bring back.
MAX_SEEN = 8192
#: Member credentials remembered for attribution: many residencies per slot.
MAX_MEMBER_CREDENTIALS = 4096
#: The resident states in which a member holds its slot.
_HOLDS_SLOT = frozenset({"starting", "live", "restarting"})

BROWSER_JOURNAL = "/run/agent-browser/events.jsonl"
MCP_SLOTS = "/run/agent-mcp-slots"
#: The most one drain prints back.
MAX_DRAIN_BYTES = 512 * 1024

#: Run as root in the ``agent`` group inside the sandbox: the journals are
#: group-readable files in group-traversable directories. Reads the paths
#: given on stdin from their cursors and prints the newer lines, bounded.
DRAIN_SCRIPT = r"""
import json, sys
request = json.loads(sys.stdin.read() or "{}")
budget = int(request.get("budget", 524288))
out = {}
for path, cursor in request.get("journals", {}).items():
    journal, seq = (cursor or ["", 0])[:2]
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        continue
    fresh = []
    for line in lines:
        try:
            entry = json.loads(line)
            if entry.get("journal") == journal and int(entry.get("seq", 0)) <= int(seq):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        budget -= len(line) + 1
        if budget < 0:
            break
        fresh.append(line)
    out[path] = fresh
print(json.dumps(out))
"""


def slot_journal(slot: int) -> str:
    """The incident journal of one slot's MCP runner."""
    return f"{MCP_SLOTS}/{slot}/events.jsonl"


@dataclass
class _Target:
    sandbox_id: str
    container_id: str
    running: bool
    #: The member holding each resident slot now.
    slots: dict[int, str] = field(default_factory=dict)


@dataclass
class _Window:
    first_id: str
    kind: str
    scope: Literal["sandbox", "member"]
    thread_id: str | None
    started: float
    ended: float
    details: dict[str, Any]
    repeats: int = 0


class MemberCredentials:
    """Whose credential each admitted digest is, per sandbox; bounded. Thread-safe.

    The host admits a member's MCP servers with that member's credential
    (``cloud_agent_host.sandbox_mcp``), so it knows then, and only then, which
    chat a digest belongs to and which slot it was admitted to. A digest is one
    residency's: a member that comes back has a new one, so an old failure
    keeps its old owner however its slot was reused.
    """

    def __init__(self, limit: int = MAX_MEMBER_CREDENTIALS) -> None:
        """An empty registry holding at most ``limit`` digests."""
        self._limit = limit
        self._lock = threading.Lock()
        self._members: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()

    def admitted(self, sandbox_id: str, digest: str, *, thread_id: str, slot: int) -> None:
        """Remember that ``digest`` is ``thread_id``'s, admitted to ``slot``."""
        with self._lock:
            self._members[(sandbox_id, digest)] = (thread_id, slot)
            self._members.move_to_end((sandbox_id, digest))
            while len(self._members) > self._limit:
                self._members.popitem(last=False)

    def thread(self, sandbox_id: str, digest: str) -> str | None:
        """The chat whose credential ``digest`` is, in that sandbox; ``None`` if unknown."""
        with self._lock:
            found = self._members.get((sandbox_id, digest))
        return None if found is None else found[0]

    def slots(self, sandbox_id: str) -> set[int]:
        """Every slot a member of the sandbox was admitted to: its journal may hold failures."""
        with self._lock:
            return {
                slot for (sandbox, _), (_, slot) in self._members.items() if sandbox == sandbox_id
            }


#: The host's one registry: admissions and journal reads run in one process.
MEMBER_CREDENTIALS = MemberCredentials()


class IncidentOutbox:
    """Final rows per sandbox, from observations; held until confirmed. Thread-safe."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        """An empty outbox; ``clock`` is wall time, as rows carry it."""
        self._clock = clock
        self._lock = threading.Lock()
        #: The observations already counted, by their source's id; bounded.
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._rows: dict[str, deque[SandboxIncident]] = {}
        self._windows: dict[tuple[str, str, str, str], _Window] = {}
        #: Rows sent per (sandbox, hour), and rows dropped there.
        self._sent: dict[tuple[str, int], int] = {}
        self._dropped: dict[tuple[str, int], int] = {}

    def observe(
        self,
        sandbox_id: str,
        kind: str,
        *,
        incident_id: str,
        observed_at: float,
        thread_id: str | None = None,
        subject: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """One observation: a row now, or a repeat counted into its window."""
        scope: Literal["sandbox", "member"] = "member" if kind in MEMBER_KINDS else "sandbox"
        if scope == "member" and thread_id is None:
            return  # nobody to tell: the slot's member is gone
        key = (sandbox_id, kind, thread_id or "", subject)
        with self._lock:
            # A replayed source event (Docker's ``since`` after a reconnect, a
            # journal line read twice) is the same observation, counted once,
            # even after its row was confirmed and forgotten.
            if (sandbox_id, incident_id) in self._seen:
                return
            self._seen[(sandbox_id, incident_id)] = None
            while len(self._seen) > MAX_SEEN:
                self._seen.popitem(last=False)
            window = self._windows.get(key)
            if window is not None and observed_at - window.started < WINDOW_S:
                window.repeats += 1
                window.ended = max(window.ended, observed_at)
                return
            if window is not None:
                self._close_locked(sandbox_id, window)
            detail = dict(details or {})
            self._emit_locked(
                sandbox_id,
                SandboxIncident(
                    incident_id=incident_id,
                    kind=kind,
                    scope=scope,
                    thread_id=thread_id if scope == "member" else None,
                    observed_at=float(observed_at),
                    count=1,
                    details=detail,
                ),
            )
            self._windows[key] = _Window(
                incident_id, kind, scope, thread_id, observed_at, observed_at, detail
            )

    def tick(self) -> None:
        """Close windows past their minute, and hours past their end."""
        now = self._clock()
        with self._lock:
            for window_key, window in list(self._windows.items()):
                if now - window.started >= WINDOW_S:
                    del self._windows[window_key]
                    self._close_locked(window_key[0], window)
            hour = int(now // 3600)
            for sandbox_id, bucket in [key for key in self._dropped if key[1] < hour]:
                count = self._dropped.pop((sandbox_id, bucket))
                self._queue_locked(
                    sandbox_id,
                    SandboxIncident(
                        incident_id=f"dropped:{bucket}",
                        kind=DROPPED_KIND,
                        scope="sandbox",
                        thread_id=None,
                        observed_at=float(bucket * 3600),
                        count=count,
                    ),
                )
            for key in [key for key in self._sent if key[1] < hour - 1]:
                del self._sent[key]

    def pending(self, sandbox_id: str) -> list[dict[str, Any]]:
        """The rows a beat carries for one sandbox."""
        with self._lock:
            return [row.wire() for row in self._rows.get(sandbox_id, ())]

    def confirm(self, sandbox_id: str, incident_ids: Sequence[str]) -> None:
        """Forget rows the control plane stored."""
        stored = set(incident_ids)
        with self._lock:
            rows = self._rows.get(sandbox_id)
            if rows is None:
                return
            kept = deque(row for row in rows if row.incident_id not in stored)
            if kept:
                self._rows[sandbox_id] = kept
            else:
                del self._rows[sandbox_id]

    def forget(self, sandbox_ids: set[str]) -> None:
        """Drop everything of sandboxes no longer listed: no beat would carry it."""
        with self._lock:
            for sandbox_id in list(self._rows):
                if sandbox_id not in sandbox_ids:
                    del self._rows[sandbox_id]
            for key in [key for key in self._windows if key[0] not in sandbox_ids]:
                del self._windows[key]
            for seen in [seen for seen in self._seen if seen[0] not in sandbox_ids]:
                del self._seen[seen]

    def _close_locked(self, sandbox_id: str, window: _Window) -> None:
        if not window.repeats:
            return
        self._emit_locked(
            sandbox_id,
            SandboxIncident(
                incident_id=f"{window.first_id}:repeated",
                kind=window.kind + REPEATED_SUFFIX,
                scope=window.scope,
                thread_id=window.thread_id if window.scope == "member" else None,
                observed_at=float(window.started),
                count=window.repeats,
                details={**window.details, "window_end": float(window.ended)},
            ),
        )

    def _emit_locked(self, sandbox_id: str, row: SandboxIncident) -> None:
        """Send a row within the sandbox's hourly cap, or count it as dropped."""
        bucket = (sandbox_id, int(row.observed_at // 3600))
        if self._sent.get(bucket, 0) >= HOURLY_CAP:
            self._dropped[bucket] = self._dropped.get(bucket, 0) + 1
            return
        self._sent[bucket] = self._sent.get(bucket, 0) + 1
        self._queue_locked(sandbox_id, row)

    def _queue_locked(self, sandbox_id: str, row: SandboxIncident) -> None:
        rows = self._rows.setdefault(sandbox_id, deque())
        if any(item.incident_id == row.incident_id for item in rows):
            return
        if len(rows) >= OUTBOX_MAX:
            oldest = rows.popleft()
            bucket = (sandbox_id, int(self._clock() // 3600))
            self._dropped[bucket] = self._dropped.get(bucket, 0) + max(1, oldest.count)
        rows.append(row)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class SandboxIncidents:
    """The host's incident observer for one sandbox manager."""

    def __init__(
        self,
        *,
        read_journals: Callable[[str, bytes], bytes | None],
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], httpx.Client] | None = None,
        drain_interval_s: float = DRAIN_INTERVAL_S,
        members: MemberCredentials = MEMBER_CREDENTIALS,
    ) -> None:
        """``read_journals(sandbox_id, request)`` runs :data:`DRAIN_SCRIPT` in that sandbox."""
        self.outbox = IncidentOutbox(clock=clock)
        self._read_journals = read_journals
        self._members = members
        self._clock = clock
        self._client_factory = client_factory or (lambda: daemon_client(timeout_s=10.0))
        self._drain_interval_s = drain_interval_s
        self._lock = threading.Lock()
        self._targets: dict[str, _Target] = {}
        self._by_container: dict[str, str] = {}
        self._cursors: dict[tuple[str, str], tuple[str, int]] = {}
        self._killed: dict[str, float] = {}
        self._high: set[tuple[str, str]] = set()
        self._wanted: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the Docker subscription and the journal reader."""
        if self._threads:
            return
        self._stop.clear()
        for name, target in (("events", self._watch_events), ("journals", self._drain_loop)):
            thread = threading.Thread(target=target, daemon=True, name=f"sandbox-incidents-{name}")
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        """Stop both threads."""
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    # -- what the listing tells it ----------------------------------------

    def sync(
        self, rows: Sequence[Mapping[str, Any]], slots: Mapping[str, Mapping[int, str]]
    ) -> None:
        """The groups the sandbox manager holds now, and which member holds which slot."""
        targets: dict[str, _Target] = {}
        for row in rows[:MAX_TARGETS]:
            sandbox_id, container_id = row.get("sandbox_id"), row.get("container_id")
            if not isinstance(sandbox_id, str) or not isinstance(container_id, str):
                continue
            targets[sandbox_id] = _Target(
                sandbox_id,
                container_id,
                row.get("running") is True,
                dict(slots.get(sandbox_id, {})),
            )
        with self._lock:
            self._targets = targets
            self._by_container = {target.container_id: key for key, target in targets.items()}
            self._cursors = {
                key: value for key, value in self._cursors.items() if key[0] in targets
            }
        self.outbox.forget(set(targets))
        self.outbox.tick()

    def pending(self, sandbox_id: str) -> list[dict[str, Any]]:
        """The rows a beat carries for one sandbox."""
        return self.outbox.pending(sandbox_id)

    def annotate(
        self,
        rows: Sequence[dict[str, Any]],
        groups: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        complete: bool,
    ) -> None:
        """Give each listed group its pending rows; a complete listing also syncs.

        ``groups`` are the member descriptors by sandbox. A member holds its
        resident slot only while its process is starting, live or restarting.
        """
        if complete:
            slots = {
                sandbox_id: {
                    int(member["resident_slot_id"]): str(member["thread_id"])
                    for member in members
                    if member.get("resident_state") in _HOLDS_SLOT
                    and isinstance(member.get("resident_slot_id"), int)
                }
                for sandbox_id, members in groups.items()
            }
            self.sync(rows, slots)
        for row in rows:
            row["incidents"] = self.pending(str(row.get("sandbox_id")))

    def confirm(self, confirmed: Mapping[str, Sequence[str]]) -> None:
        """Forget rows the control plane stored, by sandbox."""
        for sandbox_id, incident_ids in confirmed.items():
            self.outbox.confirm(sandbox_id, incident_ids)

    # -- resource readings -------------------------------------------------

    def observe_reading(self, container_id: str, reading: DockerResourceReading) -> None:
        """A reading near a limit is an incident once, until it falls back."""
        with self._lock:
            sandbox_id = self._by_container.get(container_id)
        if sandbox_id is None:
            return
        now = self._clock()
        for kind, value, limit in (
            ("memory_high", reading.counters.mem_bytes, reading.memory_limit_bytes),
            ("pids_high", reading.pids, reading.pids_limit),
        ):
            if value is None or not limit:
                continue
            key = (container_id, kind)
            share = value / limit
            with self._lock:
                armed = key not in self._high
                if share >= HIGH_SHARE and armed:
                    self._high.add(key)
                elif share < REARM_SHARE:
                    self._high.discard(key)
                    continue
                else:
                    continue
            self.outbox.observe(
                sandbox_id,
                kind,
                incident_id=f"peak:{container_id[:12]}:{kind}:{int(now)}",
                observed_at=now,
                details={"peak": int(value), "limit": int(limit)},
            )

    # -- Docker events -----------------------------------------------------

    def _watch_events(self) -> None:
        since = self._clock()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._client_factory() as client:
                    filters = json.dumps({"type": ["container"], "event": ["oom", "die", "kill"]})
                    with client.stream(
                        "GET",
                        "/events",
                        params={"since": f"{since:.0f}", "filters": filters},
                        timeout=httpx.Timeout(10.0, read=None),
                    ) as response:
                        response.raise_for_status()
                        backoff = 1.0
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            event = _json(line)
                            if event is not None:
                                since = max(since, self.handle_event(event))
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "docker event stream lost; reconnecting",
                    extra={"event": "sandbox_incidents_events_lost", "error": type(exc).__name__},
                )
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle_event(self, event: Mapping[str, Any]) -> float:
        """Take one Docker event; the time it carried, for a reconnect's ``since``."""
        nanos = _int(event.get("timeNano"))
        at = nanos / 1e9 if nanos else float(_int(event.get("time")) or self._clock())
        raw_actor = event.get("Actor")
        actor: Mapping[str, Any] = raw_actor if isinstance(raw_actor, dict) else {}
        container_id = str(event.get("id") or actor.get("ID") or "")
        action = str(event.get("Action") or event.get("status") or "")
        with self._lock:
            sandbox_id = self._by_container.get(container_id)
            if action == "kill":
                self._killed[container_id] = at
                for key in [key for key, when in self._killed.items() if at - when > KILL_GRACE_S]:
                    self._killed.pop(key, None)
                return at
            asked = at - self._killed.get(container_id, float("-inf")) <= KILL_GRACE_S
        if sandbox_id is None:
            return at
        incident_id = f"docker:{container_id[:12]}:{nanos or int(at)}:{action}"
        if action == "oom":
            self.outbox.observe(
                sandbox_id, "out_of_memory", incident_id=incident_id, observed_at=at
            )
        elif action == "die" and not asked:
            raw_attributes = actor.get("Attributes")
            attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
            code = str(attributes.get("exitCode", ""))
            self.outbox.observe(
                sandbox_id,
                "container_exited",
                incident_id=incident_id,
                observed_at=at,
                details={"exit_code": int(code) if code.isdigit() else None},
            )
        else:
            return at
        self.drain_soon(sandbox_id)
        return at

    # -- journals ------------------------------------------------------------

    def drain_soon(self, sandbox_id: str) -> None:
        """Read one sandbox's journals at the reader's next turn."""
        with self._lock:
            self._wanted.add(sandbox_id)
        self._wake.set()

    def _drain_loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            self._wake.wait(timeout=max(1.0, self._drain_interval_s / 6))
            self._wake.clear()
            now = time.monotonic()
            with self._lock:
                due = now - last >= self._drain_interval_s
                wanted, self._wanted = self._wanted, set()
                running = [key for key, target in self._targets.items() if target.running]
            for sandbox_id in running if due else sorted(wanted):
                if self._stop.is_set():
                    return
                with contextlib.suppress(Exception):
                    self.drain(sandbox_id)
            if due:
                last = now
            self.outbox.tick()

    def drain(self, sandbox_id: str) -> int:
        """Read one sandbox's journals from their cursors; how many lines were new."""
        with self._lock:
            target = self._targets.get(sandbox_id)
            if target is None or not target.running:
                return 0
            # Every slot a member was ever admitted to, not only the slots held
            # now: a member that stopped may have left a failure behind.
            slots = set(target.slots) | self._members.slots(sandbox_id)
            paths = [BROWSER_JOURNAL, *(slot_journal(slot) for slot in sorted(slots))]
            cursors = {path: list(self._cursors.get((sandbox_id, path), ("", 0))) for path in paths}
        request = json.dumps({"journals": cursors, "budget": MAX_DRAIN_BYTES}).encode()
        raw = self._read_journals(sandbox_id, request)
        document = _json(raw) if raw else None
        if not isinstance(document, dict):
            return 0
        new = 0
        for path, lines in document.items():
            if path not in cursors or not isinstance(lines, list):
                continue
            for line in lines:
                entry = _json(line) if isinstance(line, str) else None
                if entry is None:
                    continue
                journal, seq = str(entry.get("journal") or ""), _int(entry.get("seq"))
                if not journal or seq is None:
                    continue
                with self._lock:
                    held = self._cursors.get((sandbox_id, path), ("", 0))
                    if held[0] == journal and seq <= held[1]:
                        continue
                    self._cursors[(sandbox_id, path)] = (journal, seq)
                self._journal_entry(target, path, journal, seq, entry)
                new += 1
        return new

    def _journal_entry(
        self, target: _Target, path: str, journal: str, seq: int, entry: Mapping[str, Any]
    ) -> None:
        kind = entry.get("kind")
        at = entry.get("ts")
        observed_at = float(at) if isinstance(at, (int, float)) and at > 0 else self._clock()
        incident_id = f"journal:{target.container_id[:12]}:{journal[:16]}:{seq}"
        signal = _int(entry.get("signal"))
        exit_code = _int(entry.get("exit_code"))
        if path == BROWSER_JOURNAL and kind == "browser_crashed":
            self.outbox.observe(
                target.sandbox_id,
                "browser_crashed",
                incident_id=incident_id,
                observed_at=observed_at,
                details={"exit_code": exit_code, "signal": signal},
            )
            return
        if kind != "mcp_server_failed" or path == BROWSER_JOURNAL:
            return
        alias = entry.get("alias")
        phase = entry.get("phase")
        member = entry.get("member")
        if _slot_of(path) is None or not isinstance(alias, str) or phase not in MCP_PHASES:
            return
        # Whose failure it was is fixed by the credential the runner served
        # then, never by who holds the slot now; an unknown one is nobody's.
        thread_id = self._members.thread(target.sandbox_id, member) if member else None
        self.outbox.observe(
            target.sandbox_id,
            "mcp_server_failed",
            incident_id=incident_id,
            observed_at=observed_at,
            thread_id=thread_id,
            subject=f"{alias[:40]}:{phase}",
            details={"alias": alias[:40], "phase": phase, "exit_code": exit_code, "signal": signal},
        )


def _slot_of(path: str) -> int | None:
    middle = path.removeprefix(f"{MCP_SLOTS}/").removesuffix("/events.jsonl")
    return int(middle) if middle.isdigit() else None


def _json(raw: str | bytes | None) -> Any:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "DRAIN_SCRIPT",
    "IncidentOutbox",
    "SandboxIncidents",
    "slot_journal",
]
