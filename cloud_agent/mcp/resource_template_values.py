"""Find bounded RFC 6570 variable assignments and verify their full expansion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations, product
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from uritemplate import URITemplate

_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
MAX_MATCH_STEPS = 16384


class TemplateMatchLimit(ValueError):
    """Matching is indeterminate; callers must not fall through to another rule."""


def canonical_percent(uri: str) -> str:
    """Ignore hex-letter case without decoding reserved URI delimiters."""
    return _PERCENT_ESCAPE.sub(lambda match: match[0].upper(), uri)


@dataclass(frozen=True)
class Variable:
    """One variable occurrence, including its expression and lexical bounds."""

    name: str
    operator: str
    modifier: str
    expression: int
    matcher: Any
    expander: URITemplate

    @property
    def prefix(self) -> int | None:
        """A scalar prefix restriction, if present."""
        return int(self.modifier[1:]) if self.modifier.startswith(":") else None

    @property
    def first(self) -> str:
        """The first delimiter of this expression."""
        return "" if self.operator in {"", "+"} else self.operator

    @property
    def separator(self) -> str:
        """The separator between defined variables or exploded values."""
        if self.operator in {".", "/", ";"}:
            return self.operator
        return "&" if self.operator in {"?", "&"} else ","

    def render(self, value: Any, emitted: bool) -> str:
        """Expand one variable with the correct first or subsequent delimiter."""
        expanded = self.expander.expand({self.name: value})
        body = expanded[len(self.first) :]
        return (self.separator if emitted else self.first) + body


@dataclass(frozen=True)
class _Binding:
    """An inferred value, or an incomplete scalar prefix awaiting a later use."""

    value: Any
    prefix: int | None = None


def _decoded(raw: str) -> Iterator[str]:
    """Decode text while also permitting reserved expansions of literal escapes."""
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return
    yield decoded
    if raw != decoded:
        yield raw


def _partitions(
    raw: str, separator: str, ambiguous: bool, spend: Callable[[int], None]
) -> Iterator[list[str]]:
    """Consider delimiters inside reserved values without unbounded branching."""
    positions = [index for index, char in enumerate(raw) if char == separator]
    sizes = range(len(positions), -1, -1) if ambiguous else (len(positions),)
    for size in sizes:
        for cuts in combinations(positions, size):
            spend(len(cuts) + 1 + len(raw) // 64)
            starts = (0, *(cut + 1 for cut in cuts))
            ends = (*cuts, len(raw))
            yield [raw[start:end] for start, end in zip(starts, ends, strict=True)]


def _maps(
    parts: list[str], operator: str, spend: Callable[[int], None]
) -> Iterator[list[tuple[str, str]]]:
    """Infer map pairs, including reserved equals signs inside keys or values."""
    candidates = []
    for part in parts:
        positions = [index for index, char in enumerate(part) if char == "="]
        # Charge before creating candidate key/value slices: a long run of
        # equals signs must not allocate a quadratic collection first.
        spend((len(positions) + 1) * (len(part) // 64 + 1))
        if not positions:
            if operator != ";":
                return
            candidates.append([(unquote(part, errors="strict"), "")])
        else:
            candidates.append(
                [
                    (
                        unquote(part[:index], errors="strict"),
                        unquote(part[index + 1 :], errors="strict"),
                    )
                    for index in (positions if operator in {"+", "#"} else positions[:1])
                ]
            )
    for pairs in product(*candidates):
        spend(len(pairs) + 1)
        if len({key for key, _ in pairs}) == len(pairs):
            yield list(pairs)


def _values(variable: Variable, raw: str, spend: Callable[[int], None]) -> Iterator[Any]:
    """Infer scalar, list and map candidates; expansion checks every candidate."""
    named = variable.operator in {";", "?", "&"}
    name = canonical_percent(variable.name)
    scalar: str | None = raw
    if named:
        if raw == name and variable.operator == ";":
            scalar = ""
        elif raw.startswith(name + "="):
            scalar = raw[len(name) + 1 :]
        else:
            scalar = None
    if scalar is not None:
        yield from _decoded(scalar)
    if variable.prefix is not None:
        return
    try:
        if variable.modifier != "*" and scalar is not None and "," in scalar:
            for partition in _partitions(scalar, ",", variable.operator in {"+", "#"}, spend):
                parts = [unquote(item, errors="strict") for item in partition]
                yield parts
                if len(parts) % 2 == 0 and len(set(parts[::2])) == len(parts) // 2:
                    yield list(zip(parts[::2], parts[1::2], strict=True))
        if variable.modifier == "*":
            for parts in _partitions(
                raw, variable.separator, variable.operator in {"+", "#", "."}, spend
            ):
                if named:
                    entries = [item.partition("=") for item in parts]
                    if all(key == name for key, _, _ in entries):
                        yield [unquote(value, errors="strict") for _, _, value in entries]
                else:
                    yield [unquote(item, errors="strict") for item in parts]
                yield from _maps(parts, variable.operator, spend)
    except UnicodeDecodeError:
        return


def _compatible(previous: _Binding | None, value: Any) -> bool:
    """A later full value must preserve every character of an earlier prefix."""
    if previous is None:
        return True
    return isinstance(value, str) and value.startswith(previous.value)


def matches_expansion(expander: URITemplate, tokens: tuple[str | Variable, ...], uri: str) -> bool:
    """Match only with a consistent assignment that recreates the complete URI.

    RE2 has already rejected lexical mismatches. The search below handles
    ambiguous boundaries and repeated variables, with a fixed work budget.
    Exhaustion is distinct from a mismatch so an explicit deny cannot be
    bypassed by a competing, less restrictive template.
    """
    steps = 0
    target = canonical_percent(uri)

    def spend(amount: int = 1) -> None:
        nonlocal steps
        steps += amount
        if steps > MAX_MATCH_STEPS:
            raise TemplateMatchLimit("The resource URI template is too ambiguous to authorize.")

    def search(
        index: int, offset: int, bindings: dict[str, _Binding], emitted: bool = False
    ) -> bool:
        spend()
        if index == len(tokens):
            return (
                offset == len(uri)
                and canonical_percent(
                    expander.expand({name: binding.value for name, binding in bindings.items()})
                )
                == target
            )
        token = tokens[index]
        if isinstance(token, str):
            return uri.startswith(token, offset) and search(
                index + 1, offset + len(token), bindings
            )
        previous_token = tokens[index - 1] if index else None
        if (
            not isinstance(previous_token, Variable)
            or previous_token.expression != token.expression
        ):
            emitted = False
        previous = bindings.get(token.name)
        if previous is not None and (
            previous.prefix is None
            or (token.prefix is not None and token.prefix <= previous.prefix)
        ):
            if previous.value is None:
                return search(index + 1, offset, bindings, emitted)
            if token.prefix is not None and not isinstance(previous.value, str):
                return False
            rendered = canonical_percent(token.render(previous.value, emitted))
            return target.startswith(rendered, offset) and search(
                index + 1, offset + len(rendered), bindings, True
            )
        # Undefined is a global assignment, not an independent omission at
        # every occurrence. An empty scalar remains defined and emits delimiters.
        if previous is None and search(
            index + 1, offset, {**bindings, token.name: _Binding(None)}, emitted
        ):
            return True
        delimiter = token.separator if emitted else token.first
        if not uri.startswith(delimiter, offset):
            return False
        start = offset + len(delimiter)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        # Literal anchors make long paths cheap; adjacent variables use the
        # same bounded search rather than trusting a greedy capture boundary.
        ends: Iterable[int]
        if isinstance(following, str) and following:
            ends = []
            end = uri.find(following, start)
            while end >= 0:
                spend()
                ends.append(end)
                end = uri.find(following, end + 1)
        elif following is None:
            ends = [len(uri)]
        else:
            ends = range(len(uri), start - 1, -1)
        for end in ends:
            spend()
            raw = uri[start:end]
            if token.matcher.fullmatch(raw) is None:
                continue
            for value in _values(token, raw, spend):
                spend()
                if not _compatible(previous, value):
                    continue
                prefix = token.prefix
                if prefix is not None and (not isinstance(value, str) or len(value) > prefix):
                    continue
                if canonical_percent(token.render(value, emitted)) != target[offset:end]:
                    continue
                # A value shorter than the prefix bound is already complete.
                partial = prefix if prefix is not None and len(value) == prefix else None
                if search(index + 1, end, {**bindings, token.name: _Binding(value, partial)}, True):
                    return True
        return False

    return search(0, 0, {})
