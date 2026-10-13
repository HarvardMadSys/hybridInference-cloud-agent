"""The schema facade must expose ready-to-use models across domain boundaries."""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from pydantic import BaseModel

from cloud_agent import schemas


def _public_models() -> list[type[BaseModel]]:
    return [
        value
        for name in schemas.__all__
        if isinstance(value := getattr(schemas, name), type) and issubclass(value, BaseModel)
    ]


@pytest.mark.parametrize("model", _public_models(), ids=lambda model: model.__name__)
def test_public_models_resolve_runtime_annotations(model: type[BaseModel]) -> None:
    """Requests and OpenAPI generation need no caller-supplied model rebuild."""
    assert model.__pydantic_complete__
    assert model.model_json_schema(mode="validation")
    assert model.model_json_schema(mode="serialization")


def test_domain_models_are_available_through_the_public_facade() -> None:
    """A move into a domain module must keep the existing public import."""
    for info in pkgutil.iter_modules(schemas.__path__, prefix=f"{schemas.__name__}."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                assert name in schemas.__all__
                assert getattr(schemas, name) is value
