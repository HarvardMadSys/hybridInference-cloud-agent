"""Batch partial answer text into bounded-frequency preview reports."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


#: it. They are a preview: each harness still reports the complete text as the
#: ``message`` it always did, with the same ``preview_id``, and that replaces it.
REPLY_PREVIEW = "reply-preview"


@dataclass
class _PreviewStream:
    shown: str = ""
    pending: str = ""
    reported_at: float | None = None


class ReplyPreview:
    """Batch an answer's text into preview pieces while the model writes it.

    The CLIs report text a few tokens at a time, and every piece reported is an
    HTTP write and a stored row. So pieces are gathered: the first piece of an
    answer goes out at once, the rest at most once per :attr:`INTERVAL_S`. A
    piece's payload is ``{protocol, preview_id, delta}``, plus ``reset`` when its
    delta replaces the text shown so far instead of extending it.
    """

    INTERVAL_S = 0.2

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        """``clock`` is for tests; it defaults to ``time.monotonic``."""
        self._clock = clock or time.monotonic
        self._streams: dict[str, _PreviewStream] = {}

    def add(self, preview_id: str, delta: str) -> dict[str, Any] | None:
        """Gather ``delta``; return a piece when one is due."""
        stream = self._streams.setdefault(preview_id, _PreviewStream())
        stream.pending += delta
        if not stream.pending:
            return None
        now = self._clock()
        if stream.reported_at is not None and now - stream.reported_at < self.INTERVAL_S:
            return None
        return self._report(preview_id, stream, now)

    def grow_to(self, preview_id: str, text: str) -> dict[str, Any] | None:
        """The same, for a harness that resends the whole text so far."""
        stream = self._streams.setdefault(preview_id, _PreviewStream())
        known = stream.shown + stream.pending
        if text.startswith(known):
            return self.add(preview_id, text[len(known) :])
        # Rewritten rather than extended: start the preview again from here.
        stream.shown, stream.pending, stream.reported_at = text, "", self._clock()
        return {"protocol": REPLY_PREVIEW, "preview_id": preview_id, "delta": text, "reset": True}

    def flush(self, preview_id: str) -> dict[str, Any] | None:
        """Report what is still gathered now, for text that is complete."""
        stream = self._streams.get(preview_id)
        if stream is None or not stream.pending:
            return None
        return self._report(preview_id, stream, self._clock())

    def finish(self, preview_id: str) -> None:
        """Forget a preview: its complete message has arrived."""
        self._streams.pop(preview_id, None)

    def clear(self) -> None:
        """Forget every preview, at a turn's end."""
        self._streams.clear()

    def _report(self, preview_id: str, stream: _PreviewStream, now: float) -> dict[str, Any]:
        delta, stream.pending = stream.pending, ""
        stream.shown += delta
        stream.reported_at = now
        return {"protocol": REPLY_PREVIEW, "preview_id": preview_id, "delta": delta}
