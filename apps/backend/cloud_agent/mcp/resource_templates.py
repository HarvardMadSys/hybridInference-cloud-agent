"""Bounded matching of RFC 6570 resource-template URI expansions.

RE2 rejects lexical mismatches before a bounded search verifies an actual
expansion with consistent variables. Only URI text is matched; it is never
treated as a network target. Requests go to the owner's configured MCP server.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import re2
from uritemplate import URITemplate

from cloud_agent.mcp.resource_template_values import Variable, canonical_percent, matches_expansion

MAX_TEMPLATE_LENGTH = 2048
MAX_RESOURCE_URI_LENGTH = 8192
_MAX_PATTERN_LENGTH = 32768
_VARIABLE = re.compile(
    r"((?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+(?:\.(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+)*)(\*|:[1-9][0-9]{0,3})?"
)
_UNRESERVED = r"[A-Za-z0-9_~.\-]"
_RESERVED = r"[:/?#\[\]@!$&'()*+,;=]"
_PERCENT = r"%[0-9A-Fa-f]{2}"


def _variable_pattern(specification: str, operator: str) -> str:
    """Describe the scalar or composite value of one expression variable."""
    parsed = _VARIABLE.fullmatch(specification)
    if parsed is None:
        raise ValueError("invalid URI-template variable")
    name, modifier = parsed.groups()
    name = canonical_percent(name)
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
    if not named:
        return value
    if operator == ";":
        return f"{re.escape(name)}(?:={value})?"
    return f"{re.escape(name)}={value}"


def _expression_pattern(expression: str) -> str:
    """Allow omitted variables while retaining the expression's first delimiter."""
    operator = expression[0] if expression and expression[0] in "+#./;?&" else ""
    variables = expression[len(operator) :].split(",")
    if not variables or len(variables) > 8:
        raise ValueError("too many URI-template variables")
    first = "" if operator in {"", "+"} else re.escape(operator)
    separator = operator if operator in {".", "/", ";"} else "&" if operator in {"?", "&"} else ","
    choices = []
    for index, variable in enumerate(variables):
        tail = _variable_pattern(variable, operator)
        for later in variables[index + 1 :]:
            tail += f"(?:{re.escape(separator)}{_variable_pattern(later, operator)})?"
        choices.append(tail)
    return f"(?:{first}(?:{'|'.join(choices)}))?"


@lru_cache(maxsize=64)
def _compile(template: str) -> tuple[Any, URITemplate, tuple[str | Variable, ...]] | None:
    """Compile a bounded matcher, caching only the bounded template text."""
    if not template or len(template) > MAX_TEMPLATE_LENGTH:
        return None
    pattern = ""
    tokens: list[str | Variable] = []
    normalized = ""
    offset = 0
    expressions = 0
    options = re2.Options()
    options.max_mem = 1024 * 1024
    options.log_errors = False
    try:
        while offset < len(template):
            start = template.find("{", offset)
            literal = template[offset:] if start < 0 else template[offset:start]
            if "}" in literal:
                return None
            literal = canonical_percent(quote(literal, safe=":/?#[]@!$&'()*+,;=-._~%"))
            pattern += re.escape(literal)
            normalized += literal
            if literal:
                tokens.append(literal)
            if start < 0:
                break
            end = template.find("}", start + 1)
            if end < 0 or expressions >= 32:
                return None
            expression = template[start + 1 : end]
            pattern += _expression_pattern(expression)
            normalized += template[start : end + 1]
            operator = expression[0] if expression[0] in "+#./;?&" else ""
            for specification in expression[len(operator) :].split(","):
                parsed = _VARIABLE.fullmatch(specification)
                assert parsed is not None  # validated by _expression_pattern
                name, modifier = parsed.groups()
                tokens.append(
                    Variable(
                        name,
                        operator,
                        modifier or "",
                        expressions,
                        re2.compile(_variable_pattern(specification, operator), options=options),
                        URITemplate("{" + operator + specification + "}"),
                    )
                )
            if len(pattern) > _MAX_PATTERN_LENGTH:
                return None
            offset = end + 1
            expressions += 1
        if len(pattern) > _MAX_PATTERN_LENGTH:
            return None
        return re2.compile(pattern, options=options), URITemplate(normalized), tuple(tokens)
    except (ValueError, re2.error):
        return None


def matches_resource_template(template: str, uri: str) -> bool:
    """Whether a concrete URI matches the template within the relay's bounds."""
    if len(template) > MAX_TEMPLATE_LENGTH or not uri or len(uri) > MAX_RESOURCE_URI_LENGTH:
        return False
    compiled = _compile(template)
    if compiled is None:
        return False
    pattern, expander, tokens = compiled
    try:
        uri = canonical_percent(uri)
        if pattern.fullmatch(uri) is None:
            return False
        return matches_expansion(expander, tokens, uri)
    except UnicodeEncodeError:
        return False
