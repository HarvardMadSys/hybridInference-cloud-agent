"""Two credential surfaces never resolve to one configured secret.

The sandbox manager serves workspace files and terminals; enrollment issues the
per-host heartbeat/claim credential. They are separate permissions and the
host credential is not an operator-provided Compose substitution at all.

Nothing caught it because each variable was correct on its own: present,
required, no default. The defect only exists in the relationship between two
lines, which is not where anyone looks.

So this reads every compose file and refuses when two credential-shaped keys
substitute the same variable. Separation is checked structurally rather than
per-pair, because the next instance will be a pair nobody thought to name.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Keys whose value is a secret. Narrow on purpose: a URL or a hostname shared
#: between services is ordinary, and flagging those would make this noise.
_CREDENTIAL = re.compile(r"(TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY)$")

#: `${NAME}`, `${NAME:-default}`, `${NAME:?why}` — the variable, not the rest.
_SUBSTITUTION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

#: Pairs that share a variable on purpose, each with the reason. An entry here
#: is a claim that one secret really does authorize both things.
_ALLOWED_SHARING: dict[str, str] = {
    # The sandbox manager's token appears in two services of one stack — the sandbox manager that
    # verifies it and the runner that presents it. That is one credential used
    # at both ends of one hop, not two permissions collapsed into one.
}


def _compose_files() -> list[Path]:
    return sorted(p for p in REPO_ROOT.glob("deploy/**/*.yml") if "compose" in p.name)


def _credential_bindings(path: Path) -> dict[str, set[str]]:
    """Map each substituted variable to the credential keys that use it."""
    document = yaml.safe_load(path.read_text()) or {}
    by_variable: dict[str, set[str]] = defaultdict(set)
    for service in (document.get("services") or {}).values():
        environment = service.get("environment") or {}
        if not isinstance(environment, dict):
            continue
        for key, value in environment.items():
            if not _CREDENTIAL.search(key) or not isinstance(value, str):
                continue
            for variable in _SUBSTITUTION.findall(value):
                by_variable[variable].add(key)
    return by_variable


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_no_two_credentials_share_one_variable(path: Path) -> None:
    """A secret authorizes one thing, or the separation is only in the docs."""
    collisions = {
        variable: sorted(keys)
        for variable, keys in _credential_bindings(path).items()
        if len(keys) > 1 and variable not in _ALLOWED_SHARING
    }
    assert not collisions, (
        f"{path.relative_to(REPO_ROOT)} gives one secret to two permissions: "
        f"{collisions}. Split them, or add the variable to _ALLOWED_SHARING "
        f"with the reason one credential really does authorize both."
    )
