"""A member's request-meter records, read from its CLI's stderr and reported per turn.

The member's loopback service writes one record per model request on the
CLI's stderr (:mod:`cloud_agent_shared.request_prefix`): a whole one the first
time it sees a prefix, a repeat naming its fingerprint after that. Here
:class:`PrefixRecords` takes those lines out of what the host keeps as the
CLI's diagnostic stderr -- so they never push a real error out of the tail --
and resolves each repeat to the whole record it repeats. :class:`TurnPrefixes`
then reports each prefix once per turn, as a ``request_prefix`` lifecycle
event, so every attempt says what its requests sent ahead of the
conversation however long ago the CLI first sent it.

Everything is bounded and best effort. A record that fails its checks, a
repeat whose record was never read, and a record past the queue's bound are
counted and dropped; a measurement never fails a turn.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any

from cloud_agent_host.harness_event import HarnessEvent
from cloud_agent_shared.request_prefix import MAX_RECORD_BYTES, RECORD_MARKER, read_record

#: ``cloud_agent_host.runtimes.events.LIFECYCLE``, named here rather than
#: imported: the sandbox process base imports this module, and the runtimes
#: import the sandbox.
LIFECYCLE = "lifecycle"

#: Records waiting for the turn to report them.
MAX_PENDING_RECORDS = 64
#: Whole records kept so a repeat can be resolved.
MAX_KNOWN_PREFIXES = 256
#: Distinct prefixes one turn reports; a turn that sends more is unusual.
MAX_TURN_PREFIXES = 20
#: The lifecycle phase a reported prefix is stored under.
PREFIX_PHASE = "request_prefix"


class PrefixRecords:
    """One member CLI's stderr, split into its meter's records and everything else.

    :meth:`feed` is called from the thread draining stderr; :meth:`take` from
    the turn's reader.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._known: OrderedDict[str, dict[str, Any]] = OrderedDict()
        #: An unterminated line that may be a record.
        self._partial = ""
        #: Inside a line that is not one: the rest of it is diagnostic.
        self._in_text = False
        self.dropped = 0

    def feed(self, chunk: str) -> str:
        """Take the records out of one stderr chunk; the diagnostic text that is left."""
        text: list[str] = []
        data, self._partial = self._partial + chunk, ""
        while data:
            if self._in_text:
                end = data.find("\n")
                if end < 0:
                    text.append(data)
                    break
                text.append(data[: end + 1])
                data, self._in_text = data[end + 1 :], False
                continue
            if not (data.startswith(RECORD_MARKER) or RECORD_MARKER.startswith(data)):
                self._in_text = True
                continue
            end = data.find("\n")
            if end < 0:
                if len(data.encode("utf-8", "replace")) > MAX_RECORD_BYTES:
                    # Too long for a record: whatever it is, it is the CLI's.
                    text.append(data)
                    self._in_text = True
                else:
                    self._partial = data
                break
            self._record(data[:end])
            data = data[end + 1 :]
        return "".join(text)

    def _record(self, line: str) -> None:
        record = read_record(line)
        with self._lock:
            if record is not None and record.get("repeat"):
                record = self._known.get(record["fp"])
            elif record is not None:
                self._known[record["fp"]] = record
                if len(self._known) > MAX_KNOWN_PREFIXES:
                    self._known.popitem(last=False)
            if record is None or len(self._pending) >= MAX_PENDING_RECORDS:
                self.dropped += 1
                return
            self._pending.append(record)

    def take(self) -> list[dict[str, Any]]:
        """Every record read since the last call, oldest first."""
        with self._lock:
            records = list(self._pending)
            self._pending.clear()
        return records


class TurnPrefixes:
    """Each prefix one turn's requests sent, reported the first time the turn sends it."""

    def __init__(self) -> None:
        self._reported: set[str] = set()

    def events(self, records: list[dict[str, Any]]) -> list[HarnessEvent]:
        """The lifecycle events for the prefixes among ``records`` this turn has not reported."""
        events = []
        for record in records:
            if record["fp"] in self._reported or len(self._reported) >= MAX_TURN_PREFIXES:
                continue
            self._reported.add(record["fp"])
            events.append(HarnessEvent(LIFECYCLE, {"phase": PREFIX_PHASE, "prefix": dict(record)}))
        return events


__all__ = ["PREFIX_PHASE", "PrefixRecords", "TurnPrefixes"]
