"""Stateful exact-value redaction before output is truncated or persisted."""

from __future__ import annotations

import re
from bisect import bisect_left
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


MIN_SECRET_CHARS = 8

# A stream's tail that may still grow into a marker or a pattern is found by
# this many leading characters; a shorter tail is looked up whole.
_PROBE_CHARS = 8
# What such a tail may still grow into.
_MARKER = 1
_PATTERN = 2


def secret_patterns(value: str) -> tuple[str, ...]:
    """Match complete values and substantive multiline fragments, never PEM delimiters."""
    if not value:
        return ()
    lines = (
        line
        for line in value.splitlines()
        if len(line.strip()) >= MIN_SECRET_CHARS
        and not re.fullmatch(r"-----[A-Z0-9 -]+-----", line.strip())
    )
    return tuple(dict.fromkeys((value, *lines)))


def _starts_longer(items: tuple[str, ...], rest: str) -> bool:
    """Whether one of the sorted ``items`` is longer than ``rest`` and starts with it."""
    # The items starting with ``rest`` sort together from where ``rest`` would.
    index = bisect_left(items, rest)
    if index < len(items) and items[index] == rest:
        index += 1
    return index < len(items) and items[index].startswith(rest)


class _Matcher:
    """What one secret set compiles to: built once, then shared read-only.

    Masking keeps the result of the original scan, which walked the text one
    position at a time: the longest marker starting there was passed through
    unchanged, else the longest pattern was replaced by its marker, else one
    character was copied. One alternation of the markers, longest first, then
    the patterns, longest first, finds exactly those spans: a regular
    expression matches at the leftmost position where any alternative does,
    and takes the first alternative that matches there. Every alternative is a
    nonempty literal, so no match is empty.
    """

    def __init__(self, mapping: frozenset[tuple[str, str]]) -> None:
        # Ties in length are ordered only so a set always builds the same way:
        # two different strings of one length never start at one position.
        patterns = sorted(mapping, key=lambda item: (-len(item[0]), item[0]))
        markers = sorted({marker for _, marker in mapping}, key=lambda m: (-len(m), m))
        values = [pattern for pattern, _ in patterns]
        alternatives = [*markers, *values]
        self._regex = re.compile("|".join(map(re.escape, alternatives)))
        # A value that is also a marker matches as the marker, which stays.
        self._replacements = dict(patterns) | {marker: marker for marker in markers}
        self._markers = frozenset(markers)
        self._values = tuple(values)
        self._longest = max(map(len, alternatives))
        self._sorted_markers = tuple(sorted(markers))
        self._sorted_values = tuple(sorted(values))
        short: dict[str, int] = {}
        probes: set[str] = set()
        for kind, items in ((_MARKER, markers), (_PATTERN, values)):
            for item in items:
                for size in range(1, min(_PROBE_CHARS, len(item))):
                    short[item[:size]] = short.get(item[:size], 0) | kind
                if len(item) > _PROBE_CHARS:
                    probes.add(item[:_PROBE_CHARS])
        self._short = short
        self._probes = tuple(sorted(probes))

    def mask(self, text: str) -> str:
        """Mask complete text."""
        # With no pattern present the scan copied every character and marker,
        # so such text comes back unchanged.
        if not any(value in text for value in self._values):
            return text
        return self._regex.sub(self._replacement, text)

    def _replacement(self, match: re.Match[str]) -> str:
        return self._replacements[match.group()]

    def mask_complete(self, pending: str) -> tuple[str, int]:
        """Mask what may leave a stream now; return it and where the held tail starts.

        The scan of a stream stops at the first position it reaches whose rest
        could still grow into a longer marker, or into a longer pattern when no
        marker starts there, so a value is masked only once all of it is known.
        Before that position, it masks exactly as for complete text.
        """
        held = self._held(pending)
        if not held:
            return self.mask(pending), len(pending)
        pieces: list[str] = []
        pos = index = 0
        while True:
            match = self._regex.search(pending, pos)
            start = len(pending) if match is None else match.start()
            # A held position inside a span already replaced or passed through
            # is never reached.
            while index < len(held) and held[index][0] < pos:
                index += 1
            if index < len(held):
                at, kind = held[index]
                # Before ``start`` nothing matches, so each position is reached.
                # At ``start`` a longer marker is waited for first; a longer
                # pattern only when the match there is not a marker.
                if at < start or (
                    match is not None
                    and at == start
                    and (kind & _MARKER or match.group() not in self._markers)
                ):
                    pieces.append(pending[pos:at])
                    return "".join(pieces), at
            if match is None:
                pieces.append(pending[pos:])
                return "".join(pieces), len(pending)
            pieces.append(pending[pos:start])
            pieces.append(self._replacements[match.group()])
            pos = match.end()

    def _held(self, pending: str) -> list[tuple[int, int]]:
        """Each position whose rest of ``pending`` begins a longer marker or pattern, in order."""
        end = len(pending)
        # A rest as long as the longest marker or pattern starts nothing longer.
        low = max(0, end - self._longest + 1)
        held: list[tuple[int, int]] = []
        for probe in self._probes:
            at = pending.find(probe, low)
            while at >= 0:
                kind = self._kind(pending[at:])
                if kind:
                    held.append((at, kind))
                at = pending.find(probe, at + 1)
        for at in range(max(low, end - _PROBE_CHARS + 1), end):
            kind = self._short.get(pending[at:], 0)
            if kind:
                held.append((at, kind))
        held.sort()
        return held

    def _kind(self, rest: str) -> int:
        return (_MARKER if _starts_longer(self._sorted_markers, rest) else 0) | (
            _PATTERN if _starts_longer(self._sorted_values, rest) else 0
        )


@lru_cache(maxsize=32)
def _matcher(mapping: frozenset[tuple[str, str]]) -> _Matcher:
    """Compile a secret set once: a runner builds a redactor per preview stream."""
    return _Matcher(mapping)


class SecretRedactor:
    """Mask known values, including values crossing output chunk boundaries."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        patterns: dict[str, str] = {}
        for name, value in secrets.items():
            for pattern in secret_patterns(value):
                if pattern:
                    patterns.setdefault(pattern, f"[secret:{name}]")
        self._patterns = sorted(patterns.items(), key=lambda item: -len(item[0]))
        self._pending = ""
        self._matcher = _matcher(frozenset(patterns.items())) if patterns else None

    def text(self, text: str) -> str:
        """Mask complete text without rescanning replacement markers."""
        if self._matcher is None:
            return text
        return self._matcher.mask(text)

    def feed(self, chunk: str, *, final: bool = False) -> str:
        """Retain any possible trailing secret prefix until more output arrives."""
        if self._matcher is None:
            return chunk
        pending = self._pending + chunk
        if final:
            # Nothing more arrives, so nothing is held back.
            self._pending = ""
            return self._matcher.mask(pending)
        safe, held = self._matcher.mask_complete(pending)
        self._pending = pending[held:]
        return safe

    def flush(self) -> str:
        """Finish a logical stream without uploading unchecked pending text."""
        return self.feed("", final=True)

    def payload(self, value: Any, *, text_data: bool = False) -> Any:
        """Mask text content while preserving protocol control fields."""
        if not self._patterns:
            # Nothing can be masked, so the value is returned as it is.
            return value
        if isinstance(value, list):
            return [
                self.text(item)
                if isinstance(item, str)
                else self.payload(item, text_data=text_data)
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        protected = {
            "id",
            "job_id",
            "thread_id",
            "binding_id",
            "sandbox_id",
            "host_id",
            "measurement_id",
            "event_type",
            "state",
            "phase",
            "status",
            "sha256",
            "digest",
            "token",
            "owner",
            "credential",
            "runtime",
            "runtime_id",
            "runtime_version",
            "kind",
            "type",
            "schema_version",
            "version",
            "preview_id",
            "native_session_id",
            "model",
            "models",
            "model_alias",
            "model_name",
            "model_allowlist",
            "stage",
            "container_id",
            "base_sha",
        }
        return {
            key: (
                self.text(item)
                if isinstance(item, str)
                and (
                    text_data
                    or (
                        key not in protected
                        and not key.endswith(
                            ("_id", "_sha", "_sha256", "_digest", "_token", "_credential")
                        )
                    )
                )
                else item
                if key in protected and not text_data
                else self.payload(
                    item,
                    text_data=text_data
                    or key
                    in {
                        "args",
                        "arguments",
                        "parameters",
                        "result",
                        "output",
                        "input",
                        "text",
                        "content",
                        "data",
                        "raw",
                        "stdout",
                        "stderr",
                    },
                )
            )
            for key, item in value.items()
        }

    def contains(self, value: bytes) -> bool:
        """Inspect staged file bytes, including non-text files."""
        return any(pattern.encode("utf-8") in value for pattern, _ in self._patterns)
