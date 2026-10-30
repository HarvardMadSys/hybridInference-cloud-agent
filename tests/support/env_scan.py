"""Find the environment variables Python source reads, and how it reads them.

Used by ``tests/unit/test_env_classification.py``. The scan is a syntactic
approximation, not program analysis: it follows the spellings this repository
uses and reports, rather than guesses, a read whose variable it cannot name.

A read is recorded with ``via``:

* ``raw`` — straight from the process environment: ``os.getenv``,
  ``os.environ[...]``, ``.get``/``.pop``/``.setdefault``, ``in os.environ``,
  ``map(os.environ.get, NAMES)``, or the same through a mapping bound to
  ``os.environ`` (``source = env if env is not None else dict(os.environ)``).
* ``compat`` — ``manager_env(...)`` or a mapping from
  ``manager_environment(os.environ)``: raw, but a former name is accepted too.
* ``resolver`` — ``setting_env(...)`` or a mapping from
  ``settings_environment()``: the adopted settings snapshot for a registered
  name, the environment otherwise.
* ``copy`` — the whole environment handed on (``dict(os.environ)`` given to a
  child process, ``os.environ.items()``, ``os.environ`` passed as an argument).
  No name is read here; the child decides.

``names`` is ``None`` when the source does not say which variable is read.
``argument`` then holds the expression that chose it, such as
``setting.env``, so a caller can tell a registry loop from anything else.

A mapping handed to a function the scan can name (``module:Class.function``,
through an import, ``self.`` or a class) makes that parameter a mapping too,
so ``network_from_env(source)`` reading ``source.get("X")`` is a read of
``X``; the hand-off itself is then not a copy. A function whose parameter is
handed straight to one of these reads is a helper: within its module,
``helper("NAME")`` is a read of ``NAME`` at the call, and the helper's own read
is recorded as ``helper`` rather than as an unnamed read. ``env.pop("X", None)``
as a statement removes a variable from a child's environment and is not a
read. Python source embedded in a string constant (a script the code runs in a
child) is scanned too, with ``embedded`` set.

Not seen: names built at runtime (``"AGENT_" + suffix``), mappings a caller
outside the scan supplies, overridden or dynamically dispatched callees,
``getattr(os, "environ")``, ``os.environb``, and what third-party libraries
read for themselves. :func:`template_names` covers Compose, example env files,
Dockerfile ``ARG`` lines and the web app; shell scripts are not read.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path

Via = Literal["raw", "compat", "resolver", "copy", "helper"]

#: Functions that read one variable by name, and what they read through.
_NAMED_READERS: dict[str, Via] = {"manager_env": "compat", "setting_env": "resolver"}
#: Functions that return an environment mapping when given ``os.environ``
#: (``manager_environment``) or by themselves (``settings_environment``).
_MAPPING_SOURCES: dict[str, Via] = {"manager_environment": "compat"}
_MAPPING_FACTORIES: dict[str, Via] = {"settings_environment": "resolver"}
_MAPPING_READS = frozenset({"get", "pop", "setdefault"})


@dataclass(frozen=True)
class EnvRead:
    """One environment read found in the source."""

    module: str
    line: int
    #: Dotted name of the enclosing function or class, or ``<module>``.
    function: str
    via: Via
    #: The variables read; ``None`` when the source does not tell which.
    names: tuple[str, ...] | None
    #: The source of the expression that chose an unnamed variable.
    argument: str = ""
    #: The read is in Python source held in a string constant.
    embedded: bool = False

    def __str__(self) -> str:
        what = ", ".join(self.names) if self.names is not None else f"<{self.argument or '?'}>"
        where = " (embedded script)" if self.embedded else ""
        return f"{self.module}:{self.line} {self.function} [{self.via}] {what}{where}"


ConstantTable = dict[str, dict[str, tuple[str, ...]]]
#: ``module:Qualified.name`` of a function -> argument position or keyword ->
#: what a caller hands it there.
Handoffs = dict[str, dict[int | str, Via]]


def string_constants(
    tree: ast.Module, known: dict[str, tuple[str, ...]] | None = None
) -> dict[str, tuple[str, ...]]:
    """Module-level names bound to a string, or to a tuple/list/set of strings.

    A name bound to another such name (``ENV_KEY = USER_KEY``), or a tuple of
    them, takes its strings; ``known`` supplies names bound elsewhere, such as
    imported constants.
    """
    constants: dict[str, tuple[str, ...]] = dict(known or {})
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        found = _literal_strings(value, constants)
        for target in targets:
            if isinstance(target, ast.Name) and found is not None:
                constants[target.id] = found
    return constants


def _literal_strings(
    node: ast.expr, constants: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("frozenset", "set", "tuple")
        and len(node.args) == 1
        and not node.keywords
    ):
        return _literal_strings(node.args[0], constants)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        found = [_literal_strings(item, constants) for item in node.elts]
        if all(item is not None for item in found):
            return tuple(name for item in found for name in item)  # type: ignore[union-attr]
    return None


class _Index:
    """A parsed module's nodes, walked once, and each node's parent."""

    def __init__(self, tree: ast.Module) -> None:
        self.nodes = list(ast.walk(tree))
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in self.nodes:
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node


def _qualified(node: ast.AST, parents: dict[ast.AST, ast.AST], dotted: str) -> str:
    """``module:Class.function`` for a definition."""
    parts: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(current.name)
        current = parents.get(current)
    return f"{dotted}:{'.'.join(reversed(parts))}"


class _Module:
    """The scan of one parsed module."""

    def __init__(
        self,
        tree: ast.Module,
        module: str,
        imported: ConstantTable,
        *,
        own: dict[str, tuple[str, ...]],
        embedded: bool,
        handoffs: Handoffs | None = None,
        defined: frozenset[str] = frozenset(),
        dotted: str = "",
        index: _Index | None = None,
    ) -> None:
        self.tree = tree
        self.module = module
        self.dotted = dotted
        self.embedded = embedded
        index = index or _Index(tree)
        self.nodes = index.nodes
        self.parents = index.parents
        self.constants = string_constants(tree, own)
        self.module_constants: dict[str, dict[str, tuple[str, ...]]] = {}
        self.os_names = {"os"}
        self.getenv_names: set[str] = set()
        self.environ_names: set[str] = set()
        self.function_names: dict[str, str] = {}
        #: Local name -> ``module`` for a module, ``module:Name`` for an object.
        self.import_targets: dict[str, str] = {}
        self._bind_imports(imported)
        self._follow_aliases()
        self.mappings: dict[tuple[ast.AST, str], Via] = {}
        self.defined = defined
        self._bind_parameters(handoffs or {})
        self._bind_mappings()
        self.helpers: dict[str, tuple[int, Via]] = {}
        self.reads: list[tuple[int, int, EnvRead]] = []

    # -- names ------------------------------------------------------------

    def _bind_imports(self, imported: ConstantTable) -> None:
        for node in self.nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.import_targets[alias.asname] = alias.name
                    if alias.name == "os":
                        self.os_names.add(alias.asname or "os")
                    elif alias.name in imported:
                        local = alias.asname or alias.name
                        self.module_constants[local] = imported[alias.name]
            elif isinstance(node, ast.ImportFrom) and node.level and self.dotted:
                base = self.dotted.split(".")[: -node.level]
                source = ".".join([*base, node.module] if node.module else base)
                for alias in node.names:
                    self.import_targets[alias.asname or alias.name] = f"{source}:{alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.import_targets[local] = f"{node.module}:{alias.name}"
                    if node.module == "os" and alias.name == "getenv":
                        self.getenv_names.add(local)
                    elif node.module == "os" and alias.name == "environ":
                        self.environ_names.add(local)
                    elif alias.name in imported.get(node.module, {}):
                        self.constants.setdefault(local, imported[node.module][alias.name])
                    elif f"{node.module}.{alias.name}" in imported:
                        self.module_constants[local] = imported[f"{node.module}.{alias.name}"]
                    if alias.name in (*_NAMED_READERS, *_MAPPING_SOURCES, *_MAPPING_FACTORIES):
                        self.function_names[local] = alias.name
        for name in (*_NAMED_READERS, *_MAPPING_SOURCES, *_MAPPING_FACTORIES):
            self.function_names.setdefault(name, name)

    def kind(self, node: ast.AST) -> Literal["getenv", "environ"] | None:
        """``getenv`` for ``os.getenv``/``os.environ.get``; ``environ`` for ``os.environ``."""
        if isinstance(node, ast.Name):
            if node.id in self.getenv_names:
                return "getenv"
            if node.id in self.environ_names:
                return "environ"
            return None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in self.os_names:
                if node.attr == "getenv":
                    return "getenv"
                if node.attr == "environ":
                    return "environ"
                return None
            if node.attr == "get" and self.kind(node.value) == "environ":
                return "getenv"
        return None

    def _follow_aliases(self) -> None:
        """Names bound to ``os.getenv`` or ``os.environ`` (``_get = os.getenv``) read too."""
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                found = self.kind(value)
                if found is None:
                    continue
                bucket = self.getenv_names if found == "getenv" else self.environ_names
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in bucket:
                        bucket.add(target.id)
                        changed = True

    def called(self, call: ast.AST) -> str | None:
        """The known environment function ``call`` calls, by its canonical name."""
        if not isinstance(call, ast.Call):
            return None
        func = call.func
        if isinstance(func, ast.Name):
            return self.function_names.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in self.function_names.values():
            return func.attr
        return None

    # -- mappings ---------------------------------------------------------

    def scope(self, node: ast.AST) -> ast.AST:
        current = self.parents.get(node)
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            current = self.parents.get(current)
        return current if current is not None else self.tree

    def class_scope(self, node: ast.AST) -> ast.AST:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, ast.ClassDef):
            current = self.parents.get(current)
        return current if current is not None else self.tree

    def source_of(self, expr: ast.AST | None) -> Via | None:
        """What an expression is when it is an environment mapping, else ``None``."""
        if expr is None:
            return None
        if self.kind(expr) == "environ":
            return "raw"
        if isinstance(expr, ast.IfExp):
            return self.source_of(expr.body) or self.source_of(expr.orelse)
        if isinstance(expr, ast.BoolOp):
            return next((v for v in map(self.source_of, expr.values) if v), None)
        if isinstance(expr, ast.Dict):
            # `{**os.environ, "NAME": value}`: the environment, with additions.
            spread = [
                value for key, value in zip(expr.keys, expr.values, strict=True) if key is None
            ]
            return next((v for v in map(self.source_of, spread) if v), None)
        if isinstance(expr, ast.Call):
            called = self.called(expr)
            if called in _MAPPING_FACTORIES:
                return _MAPPING_FACTORIES[called]
            if called in _MAPPING_SOURCES and expr.args and self.source_of(expr.args[0]):
                return _MAPPING_SOURCES[called]
            if isinstance(expr.func, ast.Name) and expr.func.id == "dict" and expr.args:
                return self.source_of(expr.args[0])
            if isinstance(expr.func, ast.Attribute) and expr.func.attr == "copy":
                return self.source_of(expr.func.value)
            return None
        if isinstance(expr, ast.Name):
            return self.mappings.get((self.scope(expr), expr.id))
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "self"
        ):
            return self.mappings.get((self.class_scope(expr), f"self.{expr.attr}"))
        return None

    def _bind_mappings(self) -> None:
        """Local names (and ``self`` attributes) holding an environment mapping."""
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                via = self.source_of(value)
                if via is None:
                    continue
                for target in targets:
                    key: tuple[ast.AST, str] | None = None
                    if isinstance(target, ast.Name):
                        key = (self.scope(target), target.id)
                    elif (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        key = (self.class_scope(target), f"self.{target.attr}")
                    if key is not None and key not in self.mappings:
                        self.mappings[key] = via
                        changed = True

    def qualified(self, node: ast.AST) -> str:
        """``module:Class.function`` for a definition in this module."""
        return _qualified(node, self.parents, self.dotted)

    def callee(self, call: ast.Call) -> str | None:
        """The scanned function ``call`` calls, when the source says which."""
        candidates: list[str] = []
        func = call.func
        if isinstance(func, ast.Name):
            target = self.import_targets.get(func.id, f"{self.dotted}:{func.id}")
            candidates = [target, f"{target}.__init__"]
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
            if owner in ("self", "cls"):
                enclosing = self.class_scope(call)
                if isinstance(enclosing, ast.ClassDef):
                    candidates = [f"{self.qualified(enclosing)}.{func.attr}"]
            else:
                target = self.import_targets.get(owner, f"{self.dotted}:{owner}")
                candidates = [f"{target}.{func.attr}"]
                if ":" in target:
                    module, name = target.split(":", 1)
                    candidates.append(f"{module}.{name}:{func.attr}")
                else:
                    candidates.append(f"{target}:{func.attr}")
                candidates += [f"{candidate}.__init__" for candidate in candidates]
        return next((candidate for candidate in candidates if candidate in self.defined), None)

    def _bind_parameters(self, handoffs: Handoffs) -> None:
        """Parameters that a caller somewhere hands an environment mapping."""
        for node in self.nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            received = handoffs.get(self.qualified(node))
            if not received:
                continue
            positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
            if positional[:1] in (["self"], ["cls"]):
                positional = positional[1:]
            keywords = {*positional, *(a.arg for a in node.args.kwonlyargs)}
            for slot, via in received.items():
                name = (
                    positional[slot]
                    if isinstance(slot, int) and slot < len(positional)
                    else slot
                    if isinstance(slot, str) and slot in keywords
                    else None
                )
                if name is not None:
                    self.mappings.setdefault((node, name), via)

    def handoffs(self) -> Handoffs:
        """Calls here that hand an environment mapping to a named function."""
        found: Handoffs = {}
        for node in self.nodes:
            if not isinstance(node, ast.Call):
                continue
            callee = self.callee(node)
            if callee is None or self.called(node):
                continue
            slots: list[tuple[int | str, ast.expr]] = [*enumerate(node.args)]
            slots += [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
            for slot, argument in slots:
                via = self.source_of(argument)
                if via is not None:
                    found.setdefault(callee, {})[slot] = via
        return found

    # -- names a read takes -----------------------------------------------

    def loop_values(self, name: str, node: ast.AST) -> tuple[str, ...] | None:
        """The strings ``name`` takes, when it is the variable of a loop over constants."""
        current = self.parents.get(node)
        while current is not None:
            loops: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(current, ast.For):
                loops.append((current.target, current.iter))
            elif isinstance(current, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                loops.extend((loop.target, loop.iter) for loop in current.generators)
            for target, iterable in loops:
                if isinstance(target, ast.Name) and target.id == name:
                    return self.resolve(iterable, current)
            current = self.parents.get(current)
        return None

    def resolve(self, expr: ast.AST | None, at: ast.AST) -> tuple[str, ...] | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return (expr.value,)
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            found = [self.resolve(item, at) for item in expr.elts]
            if all(item is not None for item in found):
                return tuple(name for item in found for name in item)  # type: ignore[union-attr]
            return None
        if isinstance(expr, ast.Starred):
            return self.resolve(expr.value, at)
        if isinstance(expr, ast.Name):
            looped = self.loop_values(expr.id, at)
            if looped is not None:
                return looped
            return self.constants.get(expr.id)
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id in self.module_constants
        ):
            return self.module_constants[expr.value.id].get(expr.attr)
        return None

    # -- recording --------------------------------------------------------

    def function_of(self, node: ast.AST) -> str:
        parts: list[str] = []
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(current.name)
            current = self.parents.get(current)
        return ".".join(reversed(parts)) or "<module>"

    def parameter_index(self, expr: ast.AST | None, at: ast.AST) -> int | None:
        """The position of the enclosing function's parameter ``expr`` names, if any."""
        if not isinstance(expr, ast.Name) or self.loop_values(expr.id, at) is not None:
            return None
        function = self.scope(at)
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        names = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
        if expr.id in names and not any(
            isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr))
            and any(
                isinstance(target, ast.Name) and target.id == expr.id
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]  # type: ignore[union-attr]
                )
            )
            for node in ast.walk(function)
        ):
            return names.index(expr.id)
        return None

    def record(
        self,
        node: ast.AST,
        via: Via,
        chooser: ast.AST | None,
        at: ast.AST,
        *,
        names: tuple[str, ...] | None = None,
    ) -> None:
        if names is None and chooser is not None:
            names = self.resolve(chooser, at)
        argument = ast.unparse(chooser) if chooser is not None and names is None else ""
        if names is None and via != "copy":
            index = self.parameter_index(chooser, at)
            function = self.scope(at)
            if index is not None and isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.helpers.setdefault(function.name, (index, via))
                via = "helper"
        line, column = getattr(node, "lineno", 0), getattr(node, "col_offset", 0)
        read = EnvRead(
            self.module, line, self.function_of(node), via, names, argument, self.embedded
        )
        self.reads.append((line, column, read))

    def scan(self) -> None:
        for node in self.nodes:
            self._scan_known_call(node)
            self._scan_mapping_use(node)
            self._scan_os(node)
        self._scan_helper_calls()

    def discarded_pop(self, call: ast.Call) -> bool:
        """``env.pop("X", None)`` as a statement: a removal, not a read."""
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "pop"
            and isinstance(self.parents.get(call), ast.Expr)
        )

    def _scan_known_call(self, node: ast.AST) -> None:
        called = self.called(node)
        if called in _NAMED_READERS and isinstance(node, ast.Call):
            chooser = node.args[0] if node.args else None
            self.record(node, _NAMED_READERS[called], chooser, node)

    def is_alias(self, node: ast.AST) -> bool:
        """Whether ``node`` loads a local name or ``self`` attribute bound to an env mapping."""
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            return (self.scope(node), node.id) in self.mappings
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and (self.class_scope(node), f"self.{node.attr}") in self.mappings
        )

    def _scan_mapping_use(self, node: ast.AST) -> None:
        """``mapping.get("X")``, ``mapping["X"]`` and ``"X" in mapping`` on an env mapping."""
        if self.kind(node) is not None:
            return  # os.environ itself is handled by _scan_os
        if self.is_alias(node):
            self._whole_use(node, self.source_of(node) or "raw")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _MAPPING_READS and self.kind(node.func.value) is None:
                via = self.source_of(node.func.value)
                if via is not None and not self.discarded_pop(node):
                    self.record(node, via, node.args[0] if node.args else None, node)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if self.kind(node.value) is None:
                via = self.source_of(node.value)
                if via is not None:
                    self.record(node, via, node.slice, node)
        elif isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            for comparator in node.comparators:
                if self.kind(comparator) is None:
                    via = self.source_of(comparator)
                    if via is not None:
                        self.record(node, via, node.left, node)

    def _scan_os(self, node: ast.AST) -> None:
        found = self.kind(node)
        if found is None:
            return
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            return  # an alias being bound
        parent = self.parents.get(node)
        if isinstance(parent, ast.Attribute) and self.kind(parent) is not None:
            return  # `os.environ` inside `os.environ.get`: the outer node is the read
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and parent.value is node:
            return  # an alias, followed by _follow_aliases
        if found == "getenv":
            if isinstance(parent, ast.Call) and parent.func is node:
                chooser = parent.args[0] if parent.args else None
                self.record(node, "raw", chooser, parent)
            elif (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "map"
                and len(parent.args) == 2
                and parent.args[0] is node
            ):
                self.record(node, "raw", parent.args[1], parent)
            else:
                self.record(node, "raw", None, node)  # handed on, called elsewhere
            return
        if isinstance(parent, ast.Subscript) and parent.value is node:
            if isinstance(parent.ctx, ast.Load):
                self.record(node, "raw", parent.slice, parent)
            return  # a write or a delete
        if (
            isinstance(parent, ast.Compare)
            and node in parent.comparators
            and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
        ):
            self.record(node, "raw", parent.left, parent)
            return
        if isinstance(parent, ast.Attribute) and parent.value is node:
            call = self.parents.get(parent)
            if parent.attr in ("setdefault", "pop") and isinstance(call, ast.Call):
                if not self.discarded_pop(call):
                    self.record(node, "raw", call.args[0] if call.args else None, call)
                return
            if parent.attr in ("update", "clear"):
                return  # writes
        self._whole_use(node, "raw")

    def _whole_use(self, node: ast.AST, via: Via) -> None:
        """An environment mapping used other than by one name: a prefix scan, or a copy."""
        if self._feeds_mapping(node):
            return  # a read by name, or bound to a mapping whose reads are recorded
        prefix = self._prefix_of(node)
        if prefix is not None:
            self.record(node, via, None, node, names=(f"{prefix}*",))
            return
        line, column = getattr(node, "lineno", 0), getattr(node, "col_offset", 0)
        read = EnvRead(
            self.module,
            line,
            self.function_of(node),
            "copy",
            None,
            ast.unparse(self.parents.get(node, node)),
            self.embedded,
        )
        self.reads.append((line, column, read))

    def _prefix_of(self, node: ast.AST) -> str | None:
        """``P`` in ``[... for k, v in os.environ.items() if k.startswith("P")]``."""
        iterable: ast.AST = node
        parent = self.parents.get(node)
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr in ("items", "keys")
            and isinstance(self.parents.get(parent), ast.Call)
        ):
            iterable = self.parents[parent]
        loop = self.parents.get(iterable)
        if not (isinstance(loop, ast.comprehension) and loop.iter is iterable):
            return None
        target = loop.target
        if isinstance(target, ast.Tuple) and target.elts:
            target = target.elts[0]
        if not isinstance(target, ast.Name):
            return None
        for condition in loop.ifs:
            for call in ast.walk(condition):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "startswith"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == target.id
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    return call.args[0].value
        return None

    def _feeds_mapping(self, node: ast.AST) -> bool:
        """Whether ``node`` is part of an expression bound to an environment mapping."""
        current: ast.AST = node
        parent = self.parents.get(current)
        while parent is not None and not isinstance(parent, ast.stmt):
            if (
                isinstance(parent, ast.Call)
                and current is not parent.func
                and self.source_of(current) is not None
                and self.callee(parent) is not None
            ):
                return True  # handed to a scanned function, whose reads are recorded
            if isinstance(parent, ast.Compare) and all(
                isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops
            ):
                return True  # `env is not None` reads nothing
            if isinstance(parent, (ast.If, ast.While, ast.UnaryOp)) or (
                isinstance(parent, ast.IfExp) and parent.test is current
            ):
                return True  # a truth test reads nothing
            if (
                isinstance(parent, ast.Attribute)
                and parent.attr in ("update", "clear")
                and parent.value is current
            ):
                return True  # a write
            if (
                isinstance(parent, ast.Attribute)
                and parent.attr in _MAPPING_READS
                and isinstance(self.parents.get(parent), ast.Call)
            ):
                # `(env or os.environ).get("X")`: recorded as a mapping read.
                return self.source_of(current) is not None
            if isinstance(parent, ast.Subscript) and parent.value is current:
                return self.source_of(current) is not None
            if isinstance(parent, ast.Compare) and current in parent.comparators:
                return self.source_of(current) is not None
            if not isinstance(parent, (ast.IfExp, ast.BoolOp, ast.Call, ast.Dict)) or (
                isinstance(parent, ast.Call)
                and not (
                    (isinstance(parent.func, ast.Name) and parent.func.id == "dict")
                    or (isinstance(parent.func, ast.Attribute) and parent.func.attr == "copy")
                    or self.called(parent) in _MAPPING_SOURCES
                )
            ):
                return False  # used some other way: handed on or copied
            current, parent = parent, self.parents.get(parent)
        if isinstance(parent, (ast.If, ast.While)) and parent.test is current:
            return True  # a truth test reads nothing
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            return any(
                (isinstance(t, ast.Name) and (self.scope(t), t.id) in self.mappings)
                or (
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                    and (self.class_scope(t), f"self.{t.attr}") in self.mappings
                )
                for t in targets
            )
        return False

    def _scan_helper_calls(self) -> None:
        """``helper("NAME")`` for a function of this module that reads its parameter."""
        if not self.helpers:
            return
        for node in self.nodes:
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            helper = self.helpers.get(node.func.id)
            if helper is None or node.func.id in self.function_names:
                continue
            index, via = helper
            chooser = node.args[index] if len(node.args) > index else None
            self.record(node, via, chooser, node)

    def embedded_sources(self) -> list[tuple[str, str]]:
        """Python programs held in string constants, with the function holding each."""
        found = []
        for node in self.nodes:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "environ" in node.value
                and "\n" in node.value
            ):
                try:
                    ast.parse(textwrap.dedent(node.value))
                except SyntaxError:
                    continue
                found.append((textwrap.dedent(node.value), self.function_of(node)))
        return found


def find_env_reads(
    source: str,
    module: str,
    imported_constants: ConstantTable | None = None,
    *,
    dotted: str = "",
    embedded: bool = False,
    handoffs: Handoffs | None = None,
    defined: frozenset[str] = frozenset(),
    tree: ast.Module | None = None,
    index: _Index | None = None,
) -> list[EnvRead]:
    """Return every environment read in ``source``, in source order.

    Args:
        source: A Python module's text.
        module: Its path, for the report.
        imported_constants: Module-level string constants of the other scanned
            modules by dotted module name, so ``os.getenv(NAME)`` resolves a
            ``NAME`` imported from one of them.
        dotted: Its dotted module name, whose entry in ``imported_constants``
            holds what it imported relatively or re-exports.
        embedded: ``source`` is a script held in another module's string.
        handoffs: Which functions callers hand an environment mapping, and
            where; found within ``source`` alone when not given.
        defined: The functions the scan defines (``module:Qualified.name``),
            whose reads of a mapping handed to them are therefore seen.
        tree: ``source`` already parsed.
        index: ``tree`` already walked.
    """
    imported = imported_constants or {}
    tree = tree or ast.parse(source)
    index = index or _Index(tree)
    if handoffs is None:
        handoffs, defined = _local_handoffs(tree, dotted, index)
    scan = _Module(
        tree,
        module,
        imported,
        own=imported.get(dotted, {}),
        embedded=embedded,
        handoffs=handoffs,
        defined=defined,
        dotted=dotted,
        index=index,
    )
    scan.scan()
    reads = [read for _line, _column, read in sorted(scan.reads, key=lambda item: item[:2])]
    if not embedded:
        for script, function in scan.embedded_sources():
            for read in find_env_reads(script, module, imported_constants, embedded=True):
                reads.append(replace(read, line=0, function=function))
    return reads


def _defined_functions(index: _Index, dotted: str) -> set[str]:
    """``module:Class.function`` of every function a module defines."""
    return {
        _qualified(node, index.parents, dotted)
        for node in index.nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _local_handoffs(
    tree: ast.Module, dotted: str, index: _Index
) -> tuple[Handoffs, frozenset[str]]:
    """Handoffs within one module, followed until nothing new is handed on."""
    defined = frozenset(_defined_functions(index, dotted))
    handoffs: Handoffs = {}
    while True:
        scan = _Module(
            tree,
            "",
            {},
            own={},
            embedded=False,
            handoffs=handoffs,
            defined=defined,
            dotted=dotted,
            index=index,
        )
        found = scan.handoffs()
        if found == handoffs:
            return handoffs, defined
        handoffs = found


def _dotted(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def scan_paths(paths: list[Path], *, root: Path, package_root: Path) -> list[EnvRead]:
    """Scan ``paths``, resolving constants imported between them.

    ``package_root`` is where dotted module names start (``apps/backend``), so
    ``from cloud_agent.gateway import ENV_BASE_URL`` resolves; a re-export
    (``from .policy import ENV_X`` in a package ``__init__``) carries the
    constant along. Each read's module is its path relative to ``root``.
    """
    sources = {path: path.read_text(encoding="utf-8") for path in paths}
    trees = {path: ast.parse(source) for path, source in sources.items()}
    indexes = {path: _Index(tree) for path, tree in trees.items()}
    names = {
        path: _dotted(path, package_root) if path.is_relative_to(package_root) else ""
        for path in paths
    }
    constants: ConstantTable = {
        names[path]: string_constants(tree) for path, tree in trees.items() if names[path]
    }
    changed = True
    while changed:
        changed = False
        for path, tree in trees.items():
            own = constants.get(names[path])
            if own is None:
                continue
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                source = node.module or ""
                if node.level:
                    base = names[path].split(".")
                    base = base[: len(base) - node.level + (path.name == "__init__.py")]
                    source = ".".join([*base, source] if source else base)
                for alias in node.names:
                    value = constants.get(source, {}).get(alias.name)
                    local = alias.asname or alias.name
                    if value is not None and local not in own:
                        own[local] = value
                        changed = True
            # Names bound to an imported constant, now that it is known.
            for local, value in string_constants(tree, own).items():
                if own.get(local) != value:
                    own[local] = value
                    changed = True
    defined = frozenset(
        name for path in paths for name in _defined_functions(indexes[path], names[path])
    )
    # Only a module that touches the environment, or receives it, can hand it on.
    relevant = {path: trees[path] for path in paths if "environ" in sources[path]}
    handoffs: Handoffs = {}
    while True:
        found: Handoffs = {}
        for path, tree in relevant.items():
            scan = _Module(
                tree,
                "",
                constants,
                own=constants.get(names[path], {}),
                embedded=False,
                handoffs=handoffs,
                defined=defined,
                dotted=names[path],
                index=indexes[path],
            )
            for callee, slots in scan.handoffs().items():
                found.setdefault(callee, {}).update(slots)
        if found == handoffs:
            break
        handoffs = found
    words = ("environ", *_NAMED_READERS, *_MAPPING_SOURCES, *_MAPPING_FACTORIES)
    received = {callee.split(":", 1)[0] for callee in handoffs}
    reads: list[EnvRead] = []
    for path in paths:
        if names[path] not in received and not any(word in sources[path] for word in words):
            continue  # nothing here can read the environment
        reads.extend(
            find_env_reads(
                sources[path],
                path.relative_to(root).as_posix(),
                constants,
                dotted=names[path],
                handoffs=handoffs,
                defined=defined,
                tree=trees[path],
                index=indexes[path],
            )
        )
    return reads


#: ``${NAME}``, ``${NAME:-default}``, ``${NAME-default}``: what Compose
#: substitutes from the deployment's environment or ``.env``.
_COMPOSE_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
#: ``NAME=`` or ``# NAME=`` at the start of a line of an example env file: a
#: commented line is still a name the template tells an operator about.
_EXAMPLE_KEY = re.compile(r"^#?[ \t]*([A-Z][A-Z0-9_]*)=", re.MULTILINE)
_DOCKERFILE_ARG = re.compile(r"^ARG[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
#: ``process.env.NAME`` and ``process.env['NAME']``.
_PROCESS_ENV = re.compile(
    r"process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\])"
)
#: The quoted names the web server's site configuration reads
#: (``SITE_CONFIG_ENV`` in ``apps/frontend/src/config/site.ts``).
_QUOTED_AGENT_NAME = re.compile(r"['\"](AGENT_[A-Z0-9_]+)['\"]")


@dataclass(frozen=True)
class TemplateName:
    """A variable named by a deployment template, a build file or the web app."""

    path: str
    #: ``compose`` (substituted), ``container`` (a key under ``environment:``),
    #: ``build_arg``, ``example`` (an ``.env.example`` key), ``dockerfile_arg``,
    #: or ``web`` (read by the web app).
    kind: str
    name: str


def template_names(root: Path, files: list[str]) -> list[TemplateName]:
    """Names in Compose files, example env files, Dockerfiles and web sources.

    ``files`` are paths relative to ``root`` (``git ls-files``). Shell
    scripts are not read: a ``$NAME`` there may as well be a local variable.
    A Dockerfile's ``${...}`` is likewise shell; only its ``ARG`` lines count.
    """
    found: list[TemplateName] = []
    for file in files:
        path = root / file
        name = path.name
        if re.search(r"(^|/)docker-compose[^/]*\.ya?ml$", file):
            text = path.read_text(encoding="utf-8")
            found += [
                TemplateName(file, "compose", n) for n in _COMPOSE_INTERPOLATION.findall(text)
            ]
            document = yaml.safe_load(text) or {}
            for service in (document.get("services") or {}).values():
                environment = service.get("environment") or {}
                keys = (
                    list(environment)
                    if isinstance(environment, dict)
                    else [item.split("=", 1)[0] for item in environment]
                )
                found += [TemplateName(file, "container", key) for key in keys]
                build = service.get("build")
                args = (build.get("args") or {}) if isinstance(build, dict) else {}
                arg_names = (
                    list(args) if isinstance(args, dict) else [a.split("=", 1)[0] for a in args]
                )
                found += [TemplateName(file, "build_arg", arg) for arg in arg_names]
        elif name.endswith(".env.example") or name == ".env.example":
            text = path.read_text(encoding="utf-8")
            found += [TemplateName(file, "example", n) for n in _EXAMPLE_KEY.findall(text)]
        elif name.startswith("Dockerfile"):
            text = path.read_text(encoding="utf-8")
            found += [
                TemplateName(file, "dockerfile_arg", n) for n in _DOCKERFILE_ARG.findall(text)
            ]
        elif (
            file.startswith("apps/frontend/")
            and re.search(r"\.(ts|tsx|js|mjs|cjs)$", name)
            and not re.search(r"\.test\.|(^|/)__tests__/|\.d\.ts$", file)
        ):
            text = path.read_text(encoding="utf-8")
            for dotted, quoted in _PROCESS_ENV.findall(text):
                found.append(TemplateName(file, "web", dotted or quoted))
            if file == "apps/frontend/src/config/site.ts":
                found += [TemplateName(file, "web", n) for n in _QUOTED_AGENT_NAME.findall(text)]
    return found


def python_sources(root: Path, files: list[str]) -> list[Path]:
    """The Python the scan reads: the three packages, deploy and repository scripts.

    Includes the sandbox image's extensionless Python helpers
    (``deploy/sandbox/http-agent-live-bridge``), recognised by their ``#!``
    line. Tests are not scanned.
    """
    paths = []
    for file in files:
        if not (
            file.startswith(("apps/backend/", "deploy/", "scripts/")) or file == "migrations/env.py"
        ):
            continue
        path = root / file
        if file.endswith(".py"):
            paths.append(path)
        elif file.startswith("deploy/sandbox/") and "." not in path.name:
            with path.open("rb") as handle:
                first = handle.readline()
            if first.startswith(b"#!") and b"python" in first:
                paths.append(path)
    return sorted(paths)
