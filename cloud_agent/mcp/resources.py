"""Connection-scoped resource addresses and capability resolution."""

from __future__ import annotations

from typing import Any

from cloud_agent.mcp.models import (
    Capability,
    CapabilityKind,
    CapabilitySnapshot,
    ThreadBinding,
    ToolPolicy,
)
from cloud_agent.mcp.resource_template_values import TemplateMatchLimit
from cloud_agent.mcp.resource_templates import MAX_RESOURCE_URI_LENGTH, matches_resource_template

_PREFIX = "mcp-resource://"


class ResourceResolutionError(ValueError):
    """A resource address cannot be resolved without guessing its authority."""


def resource_uri(connection_id: str | int, uri: str) -> str:
    """Wrap a server URI without decoding or changing any template expressions."""
    return f"{_PREFIX}{connection_id}/{uri}"


def split_resource_uri(uri: str) -> tuple[str, str]:
    """Read only addresses issued by this merged resource surface."""
    if not uri.startswith(_PREFIX) or len(uri) > MAX_RESOURCE_URI_LENGTH + 64:
        raise ResourceResolutionError(
            "Use a resource URI from resources/list or resources/templates/list."
        )
    connection_id, separator, original = uri[len(_PREFIX) :].partition("/")
    if (
        not separator
        or not connection_id.isascii()
        or not connection_id.isdecimal()
        or not original
    ):
        raise ResourceResolutionError("Invalid connection-scoped resource URI.")
    return connection_id, original


def resource_capability(
    snapshot: CapabilitySnapshot | None, binding: ThreadBinding, uri: str
) -> Capability | None:
    """Resolve a concrete resource before templates, preserving explicit denials."""
    if snapshot is None or not uri or len(uri) > MAX_RESOURCE_URI_LENGTH:
        return None
    exact = next((item for item in snapshot.resources if item.name == uri), None)
    if exact is not None:
        return exact
    try:
        matching = [
            item
            for item in snapshot.resource_templates
            if (
                binding.rule_for(CapabilityKind.RESOURCE_TEMPLATE, item.name) is not None
                or binding.selection_pending
            )
            and matches_resource_template(item.name, uri)
        ]
    except TemplateMatchLimit as exc:
        raise ResourceResolutionError(str(exc)) from exc
    denied = [item for item in matching if binding.resource_rules.get(item.name) is ToolPolicy.DENY]
    if denied:
        return denied[0]
    selected = [
        item
        for item in matching
        if binding.rule_for(CapabilityKind.RESOURCE_TEMPLATE, item.name) is not None
        or binding.selection_pending
    ]
    if len(selected) > 1:
        raise ResourceResolutionError(
            "This URI matches multiple enabled resource templates; choose one template for this session."
        )
    return selected[0] if selected else None


def scope_resource_result(result: dict[str, Any], connection_id: str | int) -> dict[str, Any]:
    """Keep resource links and embedded/read content on the same connection."""

    def resource(item: dict[str, Any]) -> dict[str, Any]:
        if isinstance(item.get("uri"), str):
            return {**item, "uri": resource_uri(connection_id, item["uri"])}
        return dict(item)

    def content(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        if item.get("type") == "resource_link":
            return resource(item)
        if item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            return {**item, "resource": resource(item["resource"])}
        return dict(item)

    scoped = dict(result)
    if isinstance(result.get("content"), list):
        scoped["content"] = [content(item) for item in result["content"]]
    if isinstance(result.get("contents"), list):
        scoped["contents"] = [
            resource(item) if isinstance(item, dict) else item for item in result["contents"]
        ]
    if isinstance(result.get("messages"), list):
        scoped["messages"] = [
            {**item, "content": content(item.get("content"))} if isinstance(item, dict) else item
            for item in result["messages"]
        ]
    return scoped
