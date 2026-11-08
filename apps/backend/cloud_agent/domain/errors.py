"""The two ways a flow declines: a refusal its caller can act on, or an outage.

Application flows run with and without a request behind them: a submission
and a waiting message pass the same admission checks, and only the first has
an HTTP answer to give. So the checks raise these, and the API maps them onto
status codes and the ``{"error": {"type", "message"}}`` envelope in one place
(``cloud_agent.api.errors``); a background caller reads the same exception by
its class rather than by a status code.

``type`` is the wire's ``error.type`` and ``message`` its sentence, both
already safe to show the owner. ``details`` are further owner-facing fields
the envelope carries between the two (a model connection's ``reason``, or
the ``offered_efforts`` a refused level could be replaced with).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

RefusalKind = Literal["invalid", "forbidden", "not_found", "conflict"]


class Refused(Exception):
    """A verdict the caller can understand: a bad argument, no permission, a conflict.

    Nothing about it improves by retrying the same request unchanged, except a
    ``conflict`` that names a ``retry_after_seconds``: another writer holds the
    thing for a moment.
    """

    def __init__(
        self,
        kind: RefusalKind,
        type: str,
        message: str,
        *,
        details: Mapping[str, str | list[str]] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind: RefusalKind = kind
        self.type = type
        self.message = message
        self.details: dict[str, str | list[str]] = dict(details or {})
        self.retry_after_seconds = retry_after_seconds


class Unavailable(Exception):
    """The flow cannot be done right now; the same request may succeed later.

    A ``type`` of ``None`` is an outage with no stable error type: its message
    is the whole answer.
    """

    def __init__(
        self,
        type: str | None,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.retry_after_seconds = retry_after_seconds


__all__ = ["RefusalKind", "Refused", "Unavailable"]
