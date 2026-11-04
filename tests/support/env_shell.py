"""The environment inputs a deployment or tooling shell script consumes.

Used by ``tests/unit/test_env_classification.py``. This is a lexer and a few
patterns, not a shell interpreter. It answers one question per script: which
variables does the script read that it did not set itself, so that their
value must come from whoever runs it (the operator's environment, a ``.env``
the script loads, CI, or a parent script)?

The text is first reduced to what the shell expands: comments, single-quoted
strings (``'...'`` and ``$'...'``) and the bodies of quoted here-documents
(``<<'EOF'``) are blanked, line numbers kept. Then:

* **References** are ``$NAME``, ``${NAME...}`` and bare names inside
  ``$(( ))``/``(( ))``. Positional and special parameters (``$1``, ``$@``,
  ``$#``, ``$?``...) and the shell's own variables (``BASH_SOURCE``, ``IFS``,
  ``RANDOM``...) are never inputs.
* **Assignments** are ``NAME=`` at the start of a command (also after ``;``,
  ``&&``, ``then``, a ``case`` arm...), ``local``/``declare``/``typeset`` names,
  ``export``/``readonly`` with a value, ``for NAME in``, the names ``read``,
  ``mapfile``/``readarray``, ``getopts`` and ``printf -v`` fill. A bare
  ``export NAME`` passes an inherited value on and is not an assignment; a
  ``NAME=value command`` prefix is (it sets the child's value, it reads none).
* A name is an **input** when it is referenced and either never assigned, or
  defaults itself (``NAME="${NAME:-x}"``, ``${NAME:=x}``), or is read guarded
  (``${NAME:-}``, ``${NAME-}``, ``${NAME:+}``, ``${NAME+}``) on a line before
  its first assignment, the ``set -u`` idiom for "may come from outside".
* A **helper** is a function that reads the variable its first argument names:
  ``${!1}``, ``local name="$1"`` then ``${!name}``, or ``"$1"``/``"$name"``
  handed to another helper; ``for name in "$@"`` makes every argument count
  (``require_env A B``). ``helper LITERAL`` is then a read of ``LITERAL``
  (``env_or_dotenv AGENT_HOST_SLOTS``). A function that looks a name up some
  other way (in a rendered Compose environment) is declared by the caller.
* ``${!name}`` and ``helper "$name"`` inside ``for name in A B C`` are reads
  of ``A``, ``B`` and ``C``.

Reported as **dynamic** rather than guessed: any other ``${!name}``, a helper
called with a computed argument (``"${prefix}_SSH_KEY"``), and ``eval``.
``${!array[@]}`` lists an array's keys and is not a read.

Not seen: a variable a function reads that only its caller assigns, values a
sourced file sets, reads inside strings handed to another shell (``bash -c``,
``ssh``), and ordering inside functions (a guarded read in a function defined
above a later global assignment counts as before it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

How = Literal["unassigned", "defaulted", "guarded", "helper", "loop"]

#: The shell's own variables: set by bash, never an input.
SHELL_VARIABLES = frozenset(
    {
        "BASH",
        "BASHPID",
        "BASH_LINENO",
        "BASH_REMATCH",
        "BASH_SOURCE",
        "BASH_VERSION",
        "COLUMNS",
        "EPOCHREALTIME",
        "EPOCHSECONDS",
        "EUID",
        "FUNCNAME",
        "GROUPS",
        "HOSTTYPE",
        "IFS",
        "LINENO",
        "LINES",
        "OLDPWD",
        "OPTARG",
        "OPTIND",
        "OSTYPE",
        "PIPESTATUS",
        "PPID",
        "PWD",
        "RANDOM",
        "REPLY",
        "SECONDS",
        "SHLVL",
        "UID",
    }
)

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_REFERENCE = re.compile(rf"\$\{{([!#]?)({_NAME})(\[[^]]*\])?([^}}]*)\}}|\$({_NAME})")
_ARITHMETIC = re.compile(r"\(\((.*?)\)\)", re.DOTALL)
_COMMAND_START = r"(?:^|[;&|({)]|\bthen\b|\bdo\b|\belse\b|\bif\b|\bwhile\b|\buntil\b|!)"
_ASSIGNMENT = re.compile(rf"{_COMMAND_START}[ \t]*({_NAME})(\[[^]]*\])?\+?=", re.MULTILINE)
_DECLARATION = re.compile(r"\b(local|declare|typeset|export|readonly)\b([^\n;&|]*)")
_FOR = re.compile(rf"\bfor[ \t]+({_NAME})[ \t]+in\b((?:\\\n|[^;\n])*)")
_READ = re.compile(r"\bread\b([^\n;&|<>]*)")
_FILLS = re.compile(
    rf"\b(?:getopts[ \t]+\S+[ \t]+|(?:mapfile|readarray)(?:[ \t]+-[A-Za-z]+(?:[ \t]+\S+)?)*[ \t]+"
    rf"|printf[ \t]+-v[ \t]+)({_NAME})"
)
_FUNCTION = re.compile(rf"^[ \t]*(?:function[ \t]+)?({_NAME})[ \t]*\(\)[ \t]*\{{", re.MULTILINE)
_SELF_DEFAULT = re.compile(
    rf"^[ \t]*(?:export[ \t]+|local[ \t]+|readonly[ \t]+)?({_NAME})=\"?\$\{{(\1):?[-=]",
    re.MULTILINE,
)
#: Flags of ``read`` that take a value; ``-a`` takes the array it fills.
_READ_VALUE_FLAGS = frozenset("dinNptu")


@dataclass(frozen=True)
class ShellInput:
    """A variable a script reads that its runner must supply."""

    path: str
    line: int
    name: str
    how: How


@dataclass(frozen=True)
class ShellDynamic:
    """A read whose variable the script computes."""

    path: str
    line: int
    #: Enclosing function, or ``<script>``.
    function: str
    construct: str


def expanded_text(text: str) -> str:
    """``text`` with what the shell does not expand blanked, newlines kept."""
    out: list[str] = []
    i, n = 0, len(text)
    state = "plain"
    heredocs: list[tuple[str, bool, bool]] = []
    while i < n:
        char = text[i]
        if state == "single":
            out.append("\n" if char == "\n" else " ")
            if char == "'":
                state = "plain"
            i += 1
            continue
        if char == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if state == "double":
            if char == '"':
                state = "plain"
            out.append(char)
            i += 1
            continue
        if char == "'" or (char == "$" and text.startswith("$'", i)):
            state = "single"
            width = 1 if char == "'" else 2
            out.append(" " * width)
            i += width
            continue
        if char == '"':
            state = "double"
            out.append(char)
            i += 1
            continue
        if char == "#" and (i == 0 or text[i - 1] in " \t\n;|&("):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        heredoc = re.match(rf"<<(-?)[ \t]*(['\"]?)({_NAME})\2", text[i:])
        if heredoc and not text.startswith("<<<", i):
            heredocs.append((heredoc[3], bool(heredoc[2]), heredoc[1] == "-"))
            out.append(heredoc[0])
            i += len(heredoc[0])
            continue
        out.append(char)
        i += 1
        if char == "\n" and heredocs:
            for word, quoted, tabs in heredocs:
                while i < n:
                    end = text.find("\n", i)
                    end = n if end < 0 else end
                    line = text[i:end]
                    done = (line.lstrip("\t") if tabs else line) == word
                    out.append(" " * len(line) if quoted or done else line)
                    i = end
                    if i < n:
                        out.append("\n")
                        i += 1
                    if done:
                        break
            heredocs = []
    return "".join(out)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _functions(text: str) -> list[tuple[str, int, int]]:
    """``(name, start, end)`` offsets of each function body, by brace matching."""
    found = []
    for match in _FUNCTION.finditer(text):
        depth, i = 1, match.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        found.append((match[1], match.start(), i))
    return found


def _assignments(text: str) -> dict[str, int]:
    """Each assigned name and the line of its first assignment."""
    first: dict[str, int] = {}

    def add(name: str, offset: int) -> None:
        first.setdefault(name, _line(text, offset))

    for match in _ASSIGNMENT.finditer(text):
        add(match[1], match.start(1))
    for match in _DECLARATION.finditer(text):
        keyword, rest = match[1], re.sub(r'"[^"]*"|\$\([^)]*\)', "''", match[2])
        for word in rest.split():
            named = re.fullmatch(rf"({_NAME})(=.*)?", word)
            if named and (named[2] or keyword not in ("export", "readonly")):
                add(named[1], match.start())
    for match in _FOR.finditer(text):
        add(match[1], match.start(1))
    for match in _FILLS.finditer(text):
        add(match[1], match.start(1))
    for match in _READ.finditer(text):
        words, skip = match[1].split(), False
        for index, word in enumerate(words):
            if skip:
                skip = False
            elif word.startswith("-"):
                flags = word[1:]
                if flags[-1:] == "a" and index + 1 < len(words):
                    add(words[index + 1], match.start())
                skip = bool(flags) and flags[-1] in _READ_VALUE_FLAGS | {"a"}
            elif re.fullmatch(_NAME, word):
                add(word, match.start())
    return first


def _loops(text: str) -> list[tuple[int, str, tuple[str, ...] | None]]:
    """``(offset, variable, words)`` per ``for`` loop; ``words`` is ``None`` unless literal."""
    found = []
    for match in _FOR.finditer(text):
        items = match[2].replace("\\\n", " ").split()
        literal = bool(items) and all(re.fullmatch(_NAME, item) for item in items)
        found.append((match.start(), match[1], tuple(items) if literal else None))
    return found


def _helpers(text: str, functions: list[tuple[str, int, int]]) -> dict[str, bool]:
    """Functions that read the variables their arguments name -> whether every argument.

    ``local name="$1"`` makes a helper of its first argument; ``for name in "$@"``
    of all of them (``require_env A B``).
    """
    helpers: dict[str, bool] = {}
    changed = True
    while changed:
        changed = False
        for name, start, end in functions:
            if name in helpers:
                continue
            body = text[start:end]
            # `${!1}` itself, or "$1" handed to a helper (`from_env`).
            if re.search(r"\$\{!1[}+:-]", body) or any(
                re.search(rf"\b{re.escape(helper)}[ \t]+\"?\$\{{?1\}}?\"?", body)
                for helper in helpers
            ):
                helpers[name] = False
                changed = True
                continue
            for variadic, pattern in (
                (False, rf"\b(?:local[ \t]+)?({_NAME})=\"?\$\{{?1\}}?\"?"),
                (True, rf"\bfor[ \t]+({_NAME})[ \t]+in[ \t]+\"\$@\""),
            ):
                parameter = re.search(pattern, body)
                if parameter is None:
                    continue
                p = re.escape(parameter[1])
                forwards = any(
                    re.search(rf"\b{re.escape(helper)}[ \t]+\"?\$\{{?{p}\}}?\"?", body)
                    for helper in helpers
                )
                if re.search(rf"\$\{{!{p}[}}+:-]", body) or forwards:
                    helpers[name] = variadic
                    changed = True
                    break
    return helpers


def scan_shell(
    text: str, path: str, declared: Iterable[str] = ()
) -> tuple[list[ShellInput], list[ShellDynamic]]:
    """The inputs ``text`` consumes and the reads it computes.

    ``declared`` names functions of this script that read the configuration
    value their first argument names some other way than ``${!name}`` (from a
    rendered Compose environment, say), which the patterns cannot recognise.
    """
    source = expanded_text(text)
    functions = _functions(source)
    helpers = dict.fromkeys(declared, False)
    helpers.update(_helpers(source, functions))
    assigned = _assignments(source)
    loops = _loops(source)
    # A first assignment that defaults the name to itself: `NAME="${NAME:-x}"`.
    defaulted = {
        match[1]
        for match in _SELF_DEFAULT.finditer(source)
        if assigned.get(match[1]) == _line(source, match.start(1))
    }

    def loop_words(variable: str, offset: int) -> tuple[str, ...] | None:
        """The literal words of the nearest ``for variable in`` before ``offset``."""
        before = [words for start, name, words in loops if name == variable and start < offset]
        return before[-1] if before else None

    def function_at(offset: int) -> tuple[str, int, int] | None:
        inside = [f for f in functions if f[1] <= offset < f[2]]
        return min(inside, key=lambda f: f[2] - f[1]) if inside else None

    def is_helper_body(offset: int) -> bool:
        enclosing = function_at(offset)
        return enclosing is not None and enclosing[0] in helpers

    inputs: list[ShellInput] = []
    dynamics: list[ShellDynamic] = []

    def dynamic(offset: int, construct: str) -> None:
        enclosing = function_at(offset)
        name = enclosing[0] if enclosing else "<script>"
        dynamics.append(ShellDynamic(path, _line(source, offset), name, construct))

    references: list[tuple[str, int, str]] = []
    for match in _REFERENCE.finditer(source):
        if match[1] == "!":
            if match[3]:
                continue  # ${!array[@]}: the array's keys
            words = loop_words(match[2], match.start())
            if words:
                # Reading back what the script itself set is not an input.
                line = _line(source, match.start())
                inputs += [ShellInput(path, line, w, "loop") for w in words if w not in assigned]
            elif not is_helper_body(match.start()):
                dynamic(match.start(), match[0])
            continue
        name = match[2] or match[5]
        references.append((name, match.start(), match[4] or ""))
    for match in _ARITHMETIC.finditer(source):
        for word in re.finditer(_NAME, match[1]):
            if source[match.start(1) + word.start() - 1 : match.start(1) + word.start()] != "$":
                references.append((word[0], match.start(1) + word.start(), ""))

    first_seen: dict[str, int] = {}
    for name, offset, operator in references:
        if name in SHELL_VARIABLES:
            continue
        line = _line(source, offset)
        how: How | None = None
        if name not in assigned:
            how = "unassigned"
        elif name in defaulted or (operator.startswith((":=", "=")) and line <= assigned[name]):
            how = "defaulted"
        elif operator.startswith((":-", "-", ":+", "+")) and line < assigned[name]:
            how = "guarded"
        if how is not None and name not in first_seen:
            first_seen[name] = line
            inputs.append(ShellInput(path, line, name, how))

    for helper, variadic in sorted(helpers.items()):
        call = re.compile(rf"(?<![\w-]){re.escape(helper)}((?:[ \t]+(?:\\\n[ \t]*)?[^\s;&|)]+)+)")
        for match in call.finditer(source):
            offset, line = match.start(), _line(source, match.start())
            arguments = match[1].replace("\\\n", " ").split()
            for argument in arguments if variadic else arguments[:1]:
                argument = argument.strip('"')
                if re.fullmatch(_NAME, argument):
                    inputs.append(ShellInput(path, line, argument, "helper"))
                    continue
                # "$name" or "${prefix}_SUFFIX" from a loop over literal words.
                loop = re.fullmatch(rf"\$\{{?({_NAME})\}}?([A-Za-z0-9_]*)", argument)
                words = loop_words(loop[1], offset) if loop else None
                if loop and words:
                    inputs += [ShellInput(path, line, w + loop[2], "loop") for w in words]
                elif not (
                    is_helper_body(offset)
                    and ((loop and not loop[2]) or re.fullmatch(r"\$\{?1\}?", argument))
                ):
                    dynamic(offset, f"{helper} {argument}")
    for match in re.finditer(r"\beval\b", source):
        dynamic(match.start(), "eval")
    return inputs, dynamics


#: The shell the gate reads: deployment entry points, host setup, the image
#: build helpers, and the repository's own tooling scripts.
SHELL_ROOTS = ("deploy/", "scripts/")


def shell_sources(files: list[str]) -> list[str]:
    """Shell scripts under :data:`SHELL_ROOTS`, by ``.sh`` suffix."""
    return sorted(file for file in files if file.startswith(SHELL_ROOTS) and file.endswith(".sh"))
