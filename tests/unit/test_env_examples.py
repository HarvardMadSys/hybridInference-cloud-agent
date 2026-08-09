"""Every variable a stack refuses to start without has a line in its example.

The gap this closes is not "a variable is undocumented". It is that
`deploy/control-plane/` had no `.env.example` at all, so the only way to learn
what the stack needs was to read the compose file's interpolations — and the
web build arguments, which are *not* required and default to empty, produce a
stack that starts happily and is wrong in ways nobody sees until a user tries
to sign in.

So: the compose file is the source of truth, and the example is checked
against it. A new required variable fails here rather than at an operator's
first `docker compose up`.

Written as a test and not a checklist deliberately. A checklist is a thing
somebody remembers to update.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: (compose file, its example) — one pair per machine in the deployment.
STACKS = [
    pytest.param(
        REPO / "deploy/control-plane/docker-compose.yml",
        REPO / "deploy/control-plane/.env.example",
        id="control-plane",
    ),
    pytest.param(
        REPO / "deploy/host/docker-compose.sandbox.yml",
        REPO / "deploy/host/.env.example",
        id="host",
    ),
]

#: `${VAR:?message}` — the form that refuses to render when unset.
_REQUIRED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")

#: `${VAR:-default}` — everything else the file reads.
_OPTIONAL = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):-")

#: The scripts that stand in front of each stack and read the same `.env`.
#:
#: Not everything an operator must set reaches compose as an interpolation.
#: `COMPOSE_PROJECT` is passed as `-p`, so a check that read only the compose
#: file would call the one variable this deploy refuses to run without an
#: invention.
FRONTENDS = {
    "deploy/control-plane/docker-compose.yml": ("deploy/control-plane/deploy.sh",),
    "deploy/host/docker-compose.sandbox.yml": (
        "deploy/host/deploy.sh",
        "deploy/host/sandbox_host.sh",
    ),
}

#: Only the explicit lookups — `env_or_dotenv NAME` and `from_env NAME`. Any
#: `${NAME}` would count every shell local as configuration and leave this test
#: asserting nothing.
_SCRIPT_READ = re.compile(r"(?:env_or_dotenv|from_env)\s+([A-Z_][A-Z0-9_]*)")


def _assigned(example: Path) -> set[str]:
    """Variable names the example file assigns, commented lines excluded."""
    return {
        match.group(1)
        for line in example.read_text().splitlines()
        if (match := re.match(r"\s*([A-Z_][A-Z0-9_]*)=", line))
    }


@pytest.mark.parametrize(("compose", "example"), STACKS)
def test_every_required_variable_is_in_the_example(compose: Path, example: Path) -> None:
    """An operator copying the example gets a file that names every blocker."""
    required = set(_REQUIRED.findall(compose.read_text()))
    assert required, f"no required variables found in {compose.name} — regex drift?"
    missing = required - _assigned(example)
    assert not missing, (
        f"{example.relative_to(REPO)} is missing {sorted(missing)}; an operator "
        "copying it hits the failure one variable at a time"
    )


@pytest.mark.parametrize(("compose", "example"), STACKS)
def test_the_example_invents_nothing(compose: Path, example: Path) -> None:
    """A variable the compose file does not read is one nobody will notice.

    Renaming a variable in the compose file and not here leaves a line that
    looks like configuration and is not — set it, restart, and nothing
    changes. That is worse than an absent line, which at least fails.
    """
    text = compose.read_text()
    known = set(_REQUIRED.findall(text)) | set(_OPTIONAL.findall(text))
    for script in FRONTENDS[str(compose.relative_to(REPO))]:
        known |= set(_SCRIPT_READ.findall((REPO / script).read_text()))
    unread = _assigned(example) - known
    assert not unread, (
        f"{example.relative_to(REPO)} assigns {sorted(unread)}, which {compose.name} never reads"
    )
