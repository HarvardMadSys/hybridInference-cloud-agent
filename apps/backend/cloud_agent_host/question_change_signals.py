"""Coalesced question-state hints shared by resident-turn frame handling."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypeVar, overload

if TYPE_CHECKING:
    from collections.abc import Iterator

_Key = TypeVar("_Key")
_Value = TypeVar("_Value")
_Default = TypeVar("_Default")
_MISSING = object()


class ChangeTrackedDict(dict[_Key, _Value]):
    """A dict whose meaningful mutations mark one coalesced change signal."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._changed = False

    def _mark(self) -> None:
        self._changed = True

    def __setitem__(self, key: _Key, value: _Value) -> None:
        super().__setitem__(key, value)
        self._mark()

    @overload
    def pop(self, key: _Key) -> _Value: ...

    @overload
    def pop(self, key: _Key, default: _Default) -> _Value | _Default: ...

    def pop(self, key: _Key, default: Any = _MISSING) -> Any:
        """Remove a key and signal only when it was present."""
        present = key in self
        value = super().pop(key) if default is _MISSING else super().pop(key, default)
        if present:
            self._mark()
        return value

    def clear(self) -> None:
        """Remove all entries and signal when the mapping was nonempty."""
        if self:
            self._mark()
        super().clear()

    def take_change(self) -> bool:
        """Return and clear the coalesced mutation signal."""
        changed = self._changed
        self._changed = False
        return changed


class QuestionChangeTrackable:
    """Expose the tracked question map's coalesced signal under its owner lock."""

    _questions_lock: Any
    _questions: Any

    def take_question_change(self) -> bool:
        """Consume the pending question-change signal under its owner lock."""
        with self._questions_lock:
            return self._questions.take_change()


def question_change_frames(turn: Any) -> Iterator[str]:
    """Yield one lightweight wire frame when a turn's question set changed."""
    take = getattr(turn, "take_question_change", None)
    if callable(take) and take():
        yield json.dumps({"kind": "question_changed"}, separators=(",", ":")) + "\n"


def question_change_frame_kind(frame: dict[str, Any], control: Any, job: Any) -> str | None:
    """Report a question wake-up and return ordinary frame kinds unchanged."""
    kind = frame.get("kind")
    if not isinstance(kind, str):
        return None
    if kind == "question_changed":
        job_id = getattr(job, "job_id", None)
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("question-change frame has no job identity")
        control.report_question_change(job_id)
        return None
    return kind
