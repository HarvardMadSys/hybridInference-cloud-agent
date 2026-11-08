"""Expect a flow's refusal as the API would answer it.

Application flows raise ``Refused`` and ``Unavailable``; the API maps them onto
an ``HTTPException`` in one place. A test that calls a flow directly and pins
the status code and body the owner sees reads it through the same mapping.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from cloud_agent.api.errors import as_http_exception
from cloud_agent.domain.errors import Refused, Unavailable

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def raises_http() -> Iterator[pytest.ExceptionInfo[HTTPException]]:
    """``pytest.raises(HTTPException)``, with a domain error read as its HTTP answer."""
    with pytest.raises(HTTPException) as info:
        try:
            yield info
        except (Refused, Unavailable) as exc:
            raise as_http_exception(exc) from exc
