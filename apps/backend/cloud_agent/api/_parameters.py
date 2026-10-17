r"""Path and query parameters Postgres cannot take, refused before any route runs.

A parameter reaches a query as it was sent. Postgres refuses NUL in ``text``,
and asyncpg refuses an integer its column cannot hold, so either answered as
an unhandled 500 from whichever query met it first. This dependency runs ahead
of every route's own and answers both with a 422, in the shape FastAPI gives
any other invalid parameter: the one the ``^[^\x00]*$`` patterns a few
parameters carried already gave.

An integer is held to ``bigint``, the widest integer column there is. Which
parameters are integers is the route's own declaration: a search for twenty
digits is not one.
"""

from __future__ import annotations

from typing import Any, get_args

from fastapi import Request  # noqa: TC002 — FastAPI resolves annotations at runtime.
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1
#: The pattern a parameter refusing NUL declared, which a refusal here names.
NO_NUL_PATTERN = "^[^\\x00]*$"
#: Any decimal this long or shorter fits a ``bigint``, sign included.
_SHORT_DIGITS = 18
_INTEGER: TypeAdapter[int] = TypeAdapter(int)


def _reads_integer(annotation: Any) -> bool:
    """Whether FastAPI reads a parameter with this annotation as an integer."""
    if isinstance(annotation, type):
        return issubclass(annotation, int) and annotation is not bool
    return any(_reads_integer(argument) for argument in get_args(annotation))


def _integer_parameters(route: object) -> set[tuple[str, str]]:
    """The ``(location, name)`` of each path and query integer ``route`` reads."""
    found: set[tuple[str, str]] = set()
    pending = [route.dependant] if isinstance(route, APIRoute) else []
    while pending:
        dependant = pending.pop()
        for location, fields in (
            ("path", dependant.path_params),
            ("query", dependant.query_params),
        ):
            found.update(
                (location, field.alias)
                for field in fields
                if _reads_integer(field.field_info.annotation)
            )
        pending.extend(dependant.dependencies)
    return found


def _too_wide(value: str) -> int | None:
    """The integer ``value`` reads as, when a ``bigint`` cannot hold it."""
    if len(value) <= _SHORT_DIGITS:
        return None
    try:
        number = _INTEGER.validate_python(value)
    except ValidationError:
        return None
    return None if BIGINT_MIN <= number <= BIGINT_MAX else number


def _holds_nul(location: str, name: str, value: str) -> dict[str, Any]:
    return {
        "type": "string_pattern_mismatch",
        "loc": (location, name),
        "msg": f"String should match pattern '{NO_NUL_PATTERN}'",
        "input": value,
        "ctx": {"pattern": NO_NUL_PATTERN},
    }


def _out_of_range(location: str, name: str, value: str, number: int) -> dict[str, Any]:
    bound, word, key = (
        (BIGINT_MAX, "less", "le") if number > BIGINT_MAX else (BIGINT_MIN, "greater", "ge")
    )
    return {
        "type": f"{word}_than_equal",
        "loc": (location, name),
        "msg": f"Input should be {word} than or equal to {bound}",
        "input": value,
        "ctx": {key: bound},
    }


async def refuse_unstorable_parameters(request: Request) -> None:
    """Answer 422 for a parameter holding NUL, or an integer too wide for a ``bigint``."""
    errors: list[dict[str, Any]] = []
    integers: set[tuple[str, str]] | None = None
    parameters = [
        *(("path", name, value) for name, value in request.path_params.items()),
        *(("query", name, value) for name, value in request.query_params.multi_items()),
    ]
    for location, name, value in parameters:
        if not isinstance(value, str):
            continue
        if "\x00" in value:
            errors.append(_holds_nul(location, name, value))
        elif (number := _too_wide(value)) is not None:
            if integers is None:
                integers = _integer_parameters(request.scope.get("route"))
            if (location, name) in integers:
                errors.append(_out_of_range(location, name, value, number))
    if errors:
        raise RequestValidationError(errors)
