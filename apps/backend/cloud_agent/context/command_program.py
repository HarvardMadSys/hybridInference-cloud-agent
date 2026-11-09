"""Command program resolution: pure shell-text tokenization.

This module names the program a reader would call the subject of a
recorded command. Nothing here executes, expands or shells out.
Launchers, leading assignments and navigation are skipped, pipelines
contribute their first program, and an unresolvable construct is
grouped under ``shell`` rather than guessed. A program is never
invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Characters of a program name retained.
MAX_PROGRAM_CHARS = 120

ParseBasis = Literal["executable", "shell"]

#: Launchers in front of the real program, and the options that take a value.
_LAUNCHERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset(
        {
            "-u",
            "--user",
            "-g",
            "--group",
            "-p",
            "--prompt",
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-h",
            "--host",
            "-r",
            "--role",
            "-t",
            "--type",
            "-U",
            "--other-user",
            "-R",
            "--chroot",
        }
    ),
    "doas": frozenset({"-u", "-C"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "noglob": frozenset(),
    "nocorrect": frozenset(),
    "builtin": frozenset(),
}
#: Shell builtins that run no program of their own; a segment made of one is
#: navigation/setup, and the next segment is the command's subject.
_SETUP_BUILTINS = frozenset(
    {
        "cd",
        "pushd",
        "popd",
        "export",
        "local",
        "declare",
        "readonly",
        "typeset",
        "unset",
        "set",
        "shopt",
        "umask",
        "alias",
        "unalias",
        "trap",
        "source",
        ".",
    }
)
#: Shell control keywords: a construct rather than one program.
_CONTROL = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
        "fi",
    }
)
#: Tokens that end a command segment.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&", "\n"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*$")
#: A ``timeout`` duration: bare seconds or a GNU time spec.
_DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")
#: A program name made of an expansion cannot be named without running it.
_EXPANSION = re.compile(r"[$`]")


@dataclass(frozen=True)
class _Token:
    text: str
    #: True for a separator token.
    separator: bool = False


def _quoted(command: str, index: int, quote: str, word: list[str]) -> int:
    """Consume a quoted stretch into ``word``; return the index after it."""
    length = len(command)
    index += 1
    while index < length:
        char = command[index]
        if char == quote:
            return index + 1
        if char == "\\" and quote == '"' and index + 1 < length:
            index += 1
            word.append(command[index])
        else:
            word.append(char)
        index += 1
    return index


def _separator(command: str, index: int) -> tuple[str, int] | None:
    """A separator token at ``index`` and the index after it, if there is one."""
    char = command[index]
    if char == "\n":
        return "\n", index + 1
    if char not in "&|;":
        return None
    if command.startswith("&&", index) or command.startswith("||", index):
        return command[index : index + 2], index + 2
    if command.startswith("|&", index):
        return "|&", index + 2
    return char, index + 1


def _tokenize(command: str) -> tuple[list[_Token], bool]:
    """Split a shell command into words and separators, respecting quotes.

    Returns the tokens and whether the command contains a heredoc redirection.
    No substitution is performed; a quoted word keeps its literal text.
    """
    tokens: list[_Token] = []
    word: list[str] = []
    heredoc = False
    index = 0
    length = len(command)

    def flush() -> None:
        if word:
            tokens.append(_Token("".join(word)))
            word.clear()

    while index < length:
        char = command[index]
        if char in {"'", '"'}:
            index = _quoted(command, index, char, word)
            continue
        if char == "\\" and index + 1 < length:
            if command[index + 1] == "\n":
                # A line continuation joins the words either side of it.
                index += 2
                continue
            word.append(command[index + 1])
            index += 2
            continue
        if char in {" ", "\t", "\r"}:
            flush()
            index += 1
            continue
        separator = _separator(command, index)
        if separator is not None:
            flush()
            text, index = separator
            tokens.append(_Token(text, separator=True))
            continue
        if char == "<" and command.startswith("<<", index):
            heredoc = True
        if char in {"(", ")", "{", "}"}:
            # Grouping punctuation belongs to the construct around the program.
            flush()
            tokens.append(_Token(char))
            index += 1
            continue
        word.append(char)
        index += 1
    flush()
    return tokens, heredoc


def _skip_launcher(tokens: Sequence[_Token], start: int) -> int | None:
    """Where the program after a launcher prefix starts, or None if malformed."""
    index = start + 1
    options = _LAUNCHERS[tokens[start].text.lower()]
    while index < len(tokens):
        token = tokens[index]
        if token.separator:
            return None
        text = token.text
        if text == "--":
            return index + 1
        if text.startswith("-") and text != "-":
            if text in options:
                index += 2
            else:
                index += 1
            continue
        if tokens[start].text.lower() == "env" and _ASSIGNMENT.match(text):
            index += 1
            continue
        if tokens[start].text.lower() == "timeout" and _DURATION.match(text):
            # ``timeout 30 npm``: the duration is the launcher's, not a program.
            index += 1
            continue
        return index
    return None


def _resolve_program(tokens: Sequence[_Token], index: int) -> tuple[str, ParseBasis]:
    """Walk from ``index`` to the program a reader would name, or the shell group."""
    while index < len(tokens):
        token = tokens[index]
        text = token.text
        if token.separator or text in {")", "}"}:
            index += 1
            continue
        if text in _CONTROL:
            return "shell", "shell"
        lowered = text.lower()
        # Navigation and assignment are setup, never the program.
        if _ASSIGNMENT.match(text):
            index += 1
            continue
        if lowered in _SETUP_BUILTINS:
            while index < len(tokens) and not tokens[index].separator:
                index += 1
            continue
        if lowered in _LAUNCHERS:
            next_index = _skip_launcher(tokens, index)
            if next_index is None:
                return "shell", "shell"
            # What follows a launcher can itself be another launcher.
            index = next_index
            continue
        if text in {"[", "[[", "test", "eval"}:
            return "shell", "shell"
        if _EXPANSION.search(text) or text.startswith("-") or not text:
            return "shell", "shell"
        # A path names its executable by its last component.
        name = text.rstrip("/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > MAX_PROGRAM_CHARS or not _PROGRAM.match(name):
            return "shell", "shell"
        return name, "executable"
    return "shell", "shell"


def parse_program(command: str) -> tuple[str, ParseBasis]:
    """The program a reader would call the subject of ``command``.

    Returns the program name and whether it is an executable or the ``shell``
    group an unresolvable construct falls under. Navigation, assignments and
    launchers are skipped only when the parser has evidence of a following
    command; a command whose head is only ``cd`` reports ``shell``, not ``cd``.
    """
    text = command.strip()
    if not text:
        return "shell", "shell"
    tokens, _heredoc = _tokenize(text)
    index = 0
    # Optional leading grouping or negation before the subject.
    while (
        index < len(tokens)
        and not tokens[index].separator
        and tokens[index].text in {"!", "(", "{"}
    ):
        index += 1
    return _resolve_program(tokens, index)
