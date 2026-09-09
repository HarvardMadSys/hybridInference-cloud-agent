"""Scaffold smoke tests.

These assert the two packages are importable and independently versioned, and —
more usefully — that the control plane and host packages never import gateway
code. Rule 5 in AGENTS.md is easy to violate by pasting a moved file that still
references ``serving.*``; this catches it in CI rather than at deploy time.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import cloud_agent
import cloud_agent_host

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOTS = (_REPO_ROOT / "cloud_agent", _REPO_ROOT / "cloud_agent_host")

# Matches `import serving...` and `from serving... import ...`, which would mean
# a moved file still reaches into the HybridInference gateway package.
_GATEWAY_IMPORT = re.compile(r"^\s*(?:from|import)\s+serving\b", re.MULTILINE)


def test_packages_are_importable() -> None:
    assert cloud_agent.__version__
    assert cloud_agent_host.__version__


def test_no_gateway_imports() -> None:
    sources = [path for root in _PACKAGE_ROOTS for path in root.rglob("*.py")]
    # The roots have moved once already (the src-layout flatten). Empty means
    # they are wrong again — and a guard that scans nothing passes forever.
    assert sources, f"no sources under {_PACKAGE_ROOTS}; update _PACKAGE_ROOTS"
    offenders = [
        path.relative_to(_REPO_ROOT)
        for path in sources
        if _GATEWAY_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"gateway imports found in {offenders}; the gateway is reachable over HTTP only "
        "(AGENTS.md rule 5)"
    )


# The gateway's signing roots, by name. This service must never hold either:
# control tokens re-keyed to AGENT_CONTROL_TOKEN_SECRET in E3, and inference is
# authorized by grants the gateway mints and verifies.
_GATEWAY_SECRETS = ("API_KEY_SECRET", "JWT_SECRET_KEY")


def _executable_strings(tree: ast.AST) -> list[str]:
    """Every string literal that is not a docstring.

    Docstrings are excluded because they legitimately *explain* what this
    service no longer uses, and losing that explanation would cost more than
    the guard is worth. Comments never appear in the AST at all, which is the
    right outcome for the same reason: neither can be a use.
    """
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def test_no_source_file_reads_a_gateway_signing_secret() -> None:
    """AGENTS.md rule 4, enforced rather than asserted in prose.

    A reference creeping back in — via a later move, a copied fixture — is
    exactly the coupling the split exists to remove.
    """
    offenders: list[str] = []
    for root in (*_PACKAGE_ROOTS, _REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            literals = _executable_strings(tree)
            for secret in _GATEWAY_SECRETS:
                if secret in names or any(secret in text for text in literals):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)} -> {secret}")
    assert not offenders, f"gateway signing secret used in {offenders}"


def test_no_deployment_file_sets_a_gateway_signing_secret() -> None:
    """The same rule, on the files that actually configure a machine.

    Python was only ever half of it. A compose file, a Dockerfile or an env
    example is where this rule is most easily broken — by someone debugging at
    2am who has both repositories open — and a gateway signing secret present
    on this side would let the control plane mint gateway credentials for
    itself, which is precisely the coupling the split removes.

    Assignments only. These files are also where the rule gets *explained*, and
    a prohibition that cannot name what it prohibits is not much of one.

    Lives here, beside the Python check, so ``_GATEWAY_SECRETS`` has exactly
    one definition in the repository. A second copy in a deploy-specific test
    would be a list that can drift from the one the guard enforces — and would
    itself trip the check above.
    """
    assignment = re.compile(
        rf"^[^#\n]*\b(?:{'|'.join(_GATEWAY_SECRETS)})\s*[:=]",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for path in (_REPO_ROOT / "deploy").rglob("*"):
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".sh", ".env", ""}:
            continue
        if path.name.startswith("Dockerfile") or path.suffix in {".yml", ".yaml", ".sh", ".env"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if assignment.search(text):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"a deployment file sets a gateway signing secret: {offenders}. "
        "The gateway's keys stay with the gateway (AGENTS.md rule 4)."
    )
