r"""Parameters Postgres cannot take, refused before any route runs.

A parameter reaches a query as it was sent. Postgres refuses NUL in ``text``,
and asyncpg refuses an integer its column cannot hold, so either answered as
an unhandled 500 from whichever query met it first. This dependency runs ahead
of every route's own and answers both with a 422, in the shape FastAPI gives
any other invalid parameter: the one the ``^[^\x00]*$`` patterns a few
parameters carried already gave.

An integer is held to ``bigint``, the widest integer column there is. Which
parameters are integers is the route's own declaration: a search for twenty
digits is not one.

A JSON body is checked too when a person wrote it: on a route an owner, an
administrator or the on-call relay calls, any string in it, a key included,
holding NUL or half a surrogate pair is refused. Neither means anything in what
a person writes, and asyncpg cannot send a surrogate at all. What hosts and
sandboxes report is not refused here, since refusing a report loses it: their
models replace those characters (``cloud_agent_shared.storable_text``).
"""

from __future__ import annotations

import re
from functools import cache
from typing import TYPE_CHECKING, Any, get_args

from fastapi import Request  # noqa: TC002 — FastAPI resolves annotations at runtime.
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1
#: The pattern a parameter refusing NUL declared, which a refusal here names.
NO_NUL_PATTERN = "^[^\\x00]*$"
#: Any decimal this long or shorter fits a ``bigint``, sign included.
_SHORT_DIGITS = 18
_INTEGER: TypeAdapter[int] = TypeAdapter(int)
_SURROGATE = re.compile("[\ud800-\udfff]")


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


@cache
def _people() -> frozenset[Callable[..., Any]]:
    """The dependencies that authenticate whoever writes a body by hand."""
    from cloud_agent.api._owner_auth import authenticate_agent_owner
    from cloud_agent.api.deps import verify_admin_access
    from cloud_agent.api.service_jobs import verify_oncall_dispatch

    return frozenset({authenticate_agent_owner, verify_admin_access, verify_oncall_dispatch})


def _written_by_a_person(route: object) -> bool:
    """Whether ``route`` authenticates an owner, an administrator or the on-call relay."""
    pending = [route.dependant] if isinstance(route, APIRoute) else []
    while pending:
        dependant = pending.pop()
        if any(dependant.call is person for person in _people()):
            return True
        pending.extend(dependant.dependencies)
    return False


def _too_wide(value: str) -> int | None:
    """The integer ``value`` reads as, when a ``bigint`` cannot hold it."""
    if len(value) <= _SHORT_DIGITS:
        return None
    try:
        number = _INTEGER.validate_python(value)
    except ValidationError:
        return None
    return None if BIGINT_MIN <= number <= BIGINT_MAX else number


def _holds_nul(loc: tuple[str | int, ...], value: str) -> dict[str, Any]:
    return {
        "type": "string_pattern_mismatch",
        "loc": loc,
        "msg": f"String should match pattern '{NO_NUL_PATTERN}'",
        "input": value,
        "ctx": {"pattern": NO_NUL_PATTERN},
    }


def _holds_surrogate(loc: tuple[str | int, ...], value: str) -> dict[str, Any]:
    """Pydantic's own refusal of a string that is not Unicode text."""
    return {
        "type": "string_unicode",
        "loc": loc,
        "msg": "Input should be a valid string, unable to parse raw data as a unicode string",
        "input": value,
    }


def _out_of_range(loc: tuple[str | int, ...], value: str, number: int) -> dict[str, Any]:
    bound, word, key = (
        (BIGINT_MAX, "less", "le") if number > BIGINT_MAX else (BIGINT_MIN, "greater", "ge")
    )
    return {
        "type": f"{word}_than_equal",
        "loc": loc,
        "msg": f"Input should be {word} than or equal to {bound}",
        "input": value,
        "ctx": {key: bound},
    }


def _strings(value: Any) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Every string in a JSON value, object keys included, with where it is."""
    pending: list[tuple[tuple[str | int, ...], Any]] = [((), value)]
    while pending:
        loc, item = pending.pop()
        if isinstance(item, str):
            yield loc, item
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    yield (*loc, key), key
                pending.append(((*loc, key), child))
        elif isinstance(item, list):
            pending.extend(((*loc, index), child) for index, child in enumerate(item))


async def _body_errors(request: Request, route: object) -> list[dict[str, Any]]:
    """The refusals for a person's JSON body, which FastAPI parsed before this runs."""
    if not isinstance(route, APIRoute) or route.body_field is None:
        return []
    # The bodies FastAPI read as JSON: an absent type is not one of them.
    kind = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if not (kind.startswith("application/") and kind.endswith(("/json", "+json"))):
        return []
    if not await request.body():
        return []
    try:
        body = await request.json()
    except ValueError:
        return []
    errors: list[dict[str, Any]] = []
    for loc, text in _strings(body):
        if "\x00" in text:
            errors.append(_holds_nul(("body", *loc), text))
        elif _SURROGATE.search(text):
            errors.append(_holds_surrogate(("body", *loc), text))
    # Asked only once something would be refused: most bodies hold neither.
    return errors if errors and _written_by_a_person(route) else []


async def refuse_unstorable_parameters(request: Request) -> None:
    """Answer 422 for a parameter or a person's body that Postgres cannot take."""
    route = request.scope.get("route")
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
            errors.append(_holds_nul((location, name), value))
        elif (number := _too_wide(value)) is not None:
            if integers is None:
                integers = _integer_parameters(route)
            if (location, name) in integers:
                errors.append(_out_of_range((location, name), value, number))
    errors.extend(await _body_errors(request, route))
    if errors:
        raise RequestValidationError(errors)
