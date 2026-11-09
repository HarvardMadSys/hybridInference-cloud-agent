"""Runtime identifiers name the project, not a deployment of it.

The first step of docs/agents/specs/2026-11-09-neutral-runtime-identifiers.md:
a temporary file or directory lives only in the process that creates it, so its
name can leave the FreeInference namespace with no compatibility step. Names
that outlive a release (Docker labels, sandbox paths, environment variables)
move later, by the dual-read sequence that spec lays out, and are not checked
here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("apps/backend", "deploy", "scripts")
TEMPFILE_FACTORIES = frozenset(
    {
        "NamedTemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
        "TemporaryFile",
        "mkdtemp",
        "mkstemp",
    }
)
NAME_KEYWORDS = frozenset({"prefix", "suffix"})


def _literal_parts(node: ast.expr) -> list[str]:
    """The constant text of a string or f-string argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def _deployment_named_temporaries(source: str) -> list[int]:
    """Lines where a tempfile factory is given a FreeInference-named prefix or suffix."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name not in TEMPFILE_FACTORIES:
            continue
        for keyword in node.keywords:
            if keyword.arg in NAME_KEYWORDS and any(
                "freeinference" in text.lower() for text in _literal_parts(keyword.value)
            ):
                found.append(node.lineno)
    return found


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ('tempfile.TemporaryFile(prefix="freeinference-x-")', [1]),
        ('TemporaryDirectory(prefix=f"FreeInference-{n}-")', [1]),
        ('tempfile.mkstemp(suffix=".freeinference")', [1]),
        ('tempfile.TemporaryFile(prefix="cloud-agent-x-")', []),
        ('open(prefix="freeinference-x-")', []),
    ],
)
def test_detector_reads_prefixes_and_suffixes(source: str, lines: list[int]) -> None:
    assert _deployment_named_temporaries(source) == lines


def test_temporary_names_are_deployment_neutral() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for root in SCANNED
        for path in sorted((REPO / root).rglob("*.py"))
        if "node_modules" not in path.parts
        for line in _deployment_named_temporaries(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "temporary names use the cloud-agent- prefix, not a deployment's name: "
        + ", ".join(offenders)
    )
