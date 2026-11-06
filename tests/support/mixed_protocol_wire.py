"""What a receiver's own models make of a message from another release.

Shared by the two sides of ``scripts/check_mixed_protocol.py``: the control
plane reads every host request through it, and the host every answer. It runs
in whichever release's virtual environment imported it, against that release's
models, so it relies on nothing but Pydantic 2 and FastAPI's route objects.

A model that ignores unknown keys accepts a message whose field it does not
know without a word: the value is simply gone. That is the failure this module
exists to make visible. :func:`unknown_fields` names every key the receiving
model would drop, however deep, and :func:`contract_errors` says whether the
message satisfies the model at all.
"""

from __future__ import annotations

import importlib
import pkgutil
import types
import typing
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.routing import Match


def _strip(annotation: Any) -> Any:
    """``Annotated[X, ...]`` is ``X`` for the shape of a message."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _fits(annotation: Any, value: Any) -> bool:
    try:
        TypeAdapter(annotation).validate_python(value)
    except (ValidationError, TypeError, ValueError):
        return False
    return True


def _model_unknown(model: type[BaseModel], value: dict[str, Any], path: str) -> list[str]:
    known: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        known[name] = field.annotation
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                known[alias] = field.annotation
    found: list[str] = []
    for key, item in value.items():
        if key not in known:
            # ``extra="forbid"`` answers with a 422 of its own, and "allow"
            # keeps the value: only "ignore", the default, loses it quietly.
            # A null carries nothing to lose.
            if item is not None and model.model_config.get("extra", "ignore") == "ignore":
                found.append(f"{path}.{key}" if path else key)
            continue
        found.extend(unknown_fields(known[key], item, f"{path}.{key}" if path else key))
    return found


def unknown_fields(annotation: Any, value: Any, path: str = "") -> list[str]:
    """Every key in ``value`` that a receiver typed ``annotation`` would silently drop."""
    annotation = _strip(annotation)
    if value is None:
        return []
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        # The member the receiver would pick; failing that, the first model.
        chosen = next((arg for arg in options if _fits(arg, value)), None)
        if chosen is None:
            chosen = next(
                (arg for arg in options if isinstance(_strip(arg), type)),
                options[0] if options else Any,
            )
        return unknown_fields(chosen, value, path)
    if origin in (list, tuple, set, frozenset) and isinstance(value, list):
        args = [arg for arg in typing.get_args(annotation) if arg is not Ellipsis]
        item_type = args[0] if args else Any
        return [
            key
            for index, item in enumerate(value)
            for key in unknown_fields(item_type, item, f"{path}[{index}]")
        ]
    if origin is dict and isinstance(value, dict):
        pair = typing.get_args(annotation)
        item_type = pair[1] if len(pair) == 2 else Any
        return [
            key
            for name, item in value.items()
            for key in unknown_fields(item_type, item, f"{path}[{name!r}]")
        ]
    if (
        isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
        and isinstance(value, dict)
    ):
        return _model_unknown(annotation, value, path)
    return []


def contract_errors(annotation: Any, value: Any) -> str | None:
    """Why ``value`` does not satisfy ``annotation``, or ``None`` when it does."""
    try:
        TypeAdapter(annotation).validate_python(value)
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
    return None


#: Prefixes of the routes a host calls. The owner and admin API is not part
#: of the host protocol.
HOST_PREFIXES = ("/v1/agent/host/", "/v1/agent/worker/")


def host_routes() -> list[Any]:
    """Every route of the host protocol in this release, from each API module's router.

    Read from the modules rather than from an assembled app, whose route list
    holds included routers rather than their routes in later FastAPI releases.
    """
    import cloud_agent.api

    routes: list[Any] = []
    for module in pkgutil.iter_modules(cloud_agent.api.__path__):
        router = getattr(importlib.import_module(f"cloud_agent.api.{module.name}"), "router", None)
        for route in getattr(router, "routes", ()):
            if getattr(route, "dependant", None) is not None and str(
                getattr(route, "path", "")
            ).startswith(HOST_PREFIXES):
                routes.append(route)
    return routes


def matching_route(routes: list[Any], scope: dict[str, Any]) -> Any | None:
    """The route among ``routes`` that would serve ``scope``, or ``None``."""
    for route in routes:
        if getattr(route, "dependant", None) is None:
            continue
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def body_annotation(route: Any) -> Any | None:
    """The model a route parses its whole JSON body with, or ``None`` when it has none."""
    params = list(route.dependant.body_params)
    if len(params) != 1:
        return None
    return params[0].field_info.annotation


__all__ = [
    "HOST_PREFIXES",
    "body_annotation",
    "contract_errors",
    "host_routes",
    "matching_route",
    "unknown_fields",
]
