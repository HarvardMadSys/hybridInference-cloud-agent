"""Bounded matching of RFC 6570 resource-template URI expansions.

Templates are supplied by upstream servers, so matching uses RE2 rather than a
backtracking engine. Only URI text is matched; it is never treated as a network
target. Requests still go to the owner's configured MCP server.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any
from urllib.parse import quote, unquote

import re2

MAX_TEMPLATE_LENGTH = 2048
MAX_RESOURCE_URI_LENGTH = 8192
_MAX_PATTERN_LENGTH = 32768
_VARIABLE = re.compile(
    r"((?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+(?:\.(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+)*)(\*|:[1-9][0-9]{0,3})?"
)
_UNRESERVED = r"[A-Za-z0-9_~.\-]"
_RESERVED = r"[:/?#\[\]@!$&'()*+,;=]"
_PERCENT = r"%[0-9A-Fa-f]{2}"


def _variable_pattern(
    specification: str, operator: str, captures: list[tuple[str, int | None]]
) -> str:
    """Describe the scalar or composite value of one expression variable."""
    parsed = _VARIABLE.fullmatch(specification)
    if parsed is None:
        raise ValueError("invalid URI-template variable")
    name, modifier = parsed.groups()
    atom = f"(?:{_UNRESERVED}|{_PERCENT}"
    atom += f"|{_RESERVED})" if operator in "+#" and operator else ")"
    scalar = f"{atom}*"
    separator = (
        operator
        if operator in "./;" and operator
        else "&"
        if operator in "?&" and operator
        else ","
    )
    named = operator in {"?", "&", ";"}
    prefix = int(modifier[1:]) if modifier and modifier.startswith(":") else None
    if modifier == "*":
        # Lists repeat the variable name for named operators; exploded maps
        # use their own keys. Delimiters are protocol syntax, never wildcards.
        item = f"{re.escape(name)}={scalar}" if named else scalar
        if operator == ";":
            item = f"{re.escape(name)}(?:={scalar})?"
        pair = f"{scalar}={scalar}"
        if operator == ";":
            pair = f"{scalar}(?:={scalar})?"
        sep = re.escape(separator)
        return f"(?:{item}(?:{sep}{item})*|{pair}(?:{sep}{pair})*)"
    value = scalar if prefix is not None else f"{scalar}(?:,{scalar})*"
    # Capture non-exploded values to enforce Unicode prefix lengths and equal
    # repeated scalar variables after percent decoding.
    captures.append((name, prefix))
    value = f"({value})"
    if not named:
        return value
    if operator == ";":
        return f"{re.escape(name)}(?:={value})?"
    return f"{re.escape(name)}={value}"


def _expression_pattern(expression: str, captures: list[tuple[str, int | None]]) -> str:
    """Allow omitted variables while retaining the expression's first delimiter."""
    operator = expression[0] if expression and expression[0] in "+#./;?&" else ""
    variables = expression[len(operator) :].split(",")
    if not variables or len(variables) > 8:
        raise ValueError("too many URI-template variables")
    first = "" if operator in {"", "+"} else re.escape(operator)
    separator = operator if operator in {".", "/", ";"} else "&" if operator in {"?", "&"} else ","
    choices = []
    for index, variable in enumerate(variables):
        tail = _variable_pattern(variable, operator, captures)
        for later in variables[index + 1 :]:
            tail += f"(?:{re.escape(separator)}{_variable_pattern(later, operator, captures)})?"
        choices.append(tail)
    return f"(?:{first}(?:{'|'.join(choices)}))?"


@lru_cache(maxsize=64)
def _compile(template: str) -> tuple[Any, tuple[tuple[str, int | None], ...]] | None:
    """Compile a bounded matcher, caching only the bounded template text."""
    if not template or len(template) > MAX_TEMPLATE_LENGTH:
        return None
    pattern = ""
    captures: list[tuple[str, int | None]] = []
    offset = 0
    expressions = 0
    try:
        while offset < len(template):
            start = template.find("{", offset)
            literal = template[offset:] if start < 0 else template[offset:start]
            if "}" in literal:
                return None
            pattern += re.escape(quote(literal, safe=":/?#[]@!$&'()*+,;=-._~%"))
            if start < 0:
                break
            end = template.find("}", start + 1)
            if end < 0 or expressions >= 32:
                return None
            pattern += _expression_pattern(template[start + 1 : end], captures)
            if len(pattern) > _MAX_PATTERN_LENGTH:
                return None
            offset = end + 1
            expressions += 1
        if len(pattern) > _MAX_PATTERN_LENGTH:
            return None
        options = re2.Options()
        options.max_mem = 1024 * 1024
        options.log_errors = False
        return re2.compile(pattern, options=options), tuple(captures)
    except (ValueError, re2.error):
        return None


def matches_resource_template(template: str, uri: str) -> bool:
    """Whether a concrete URI matches the template within the relay's bounds."""
    if len(template) > MAX_TEMPLATE_LENGTH or not uri or len(uri) > MAX_RESOURCE_URI_LENGTH:
        return False
    compiled = _compile(template)
    if compiled is None:
        return False
    pattern, captures = compiled
    try:
        matched = pattern.fullmatch(uri)
    except UnicodeEncodeError:
        return False
    if matched is None:
        return False
    values: dict[str, tuple[str, int | None]] = {}
    for raw, (name, prefix) in zip(matched.groups(), captures, strict=True):
        if raw is None:
            continue
        try:
            value = unquote(raw, errors="strict")
        except UnicodeDecodeError:
            return False
        if prefix is not None and len(value) > prefix:
            return False
        previous = values.get(name)
        if previous is not None:
            old, old_prefix = previous
            if prefix is None and old_prefix is None and old != value:
                return False
            if prefix is not None and old_prefix is None and value != old[:prefix]:
                return False
            if old_prefix is not None and prefix is None and old != value[:old_prefix]:
                return False
            if (
                old_prefix is not None
                and prefix is not None
                and old[: min(prefix, old_prefix)] != value[: min(prefix, old_prefix)]
            ):
                return False
        if previous is None or prefix is None or (previous[1] is not None and prefix > previous[1]):
            values[name] = value, prefix
    return True
