"""Compatibility fields shared by sandbox-manager host responses."""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class _SandboxManagerWireNames(BaseModel):
    """Expose current names while keeping older host clients interoperable."""

    @model_validator(mode="after")
    def _legacy_manager_fields(self) -> _SandboxManagerWireNames:
        for current, previous in (
            ("sandbox_manager_url", "broker_url"),
            ("sandbox_manager_error", "broker_error"),
        ):
            if current in type(self).model_fields:
                setattr(self, previous, getattr(self, current))
        return self
