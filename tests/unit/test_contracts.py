"""The gateway contracts under `contracts/` are real documents.

A cross-repo contract that nobody checks is a comment in a language nobody
reads. Three things are checked here, in increasing order of how easily they
rot:

1. **It parses and validates as OpenAPI.** The plan's stated acceptance.
2. **Every path a document names exists** — the gateway source it was written
   from, and the tests on both sides that pin it. Twice while writing these
   documents a plausible-looking test path was cited that did not exist, and
   both were caught by hand; a reference that cannot be followed is worse than
   no reference, because it reads as evidence the contract was verified.
3. **Every endpoint this service calls appears in some document.** The failure
   mode this catches is the quiet one: a new gateway call added to
   ``gateway.py`` or ``grants.py`` without a corresponding contract, so the
   dependency exists but nothing records that it was agreed.

Item 2 only checks the paths *inside this repository* and the fact that a
gateway branch is named. It cannot check the gateway files, which live in
another repository on unmerged branches; that verification is the "Cross-repo
readiness" register in MIGRATION.md, redone when those branches land.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "gateway_contracts"

DOCUMENTS = sorted(CONTRACTS.glob("*.yaml"))

#: The client modules whose calls must be described by some document.
CLIENT_SOURCES = (
    REPO_ROOT / "apps" / "backend" / "cloud_agent" / "gateway.py",
    REPO_ROOT / "apps" / "backend" / "cloud_agent" / "grants.py",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_there_is_at_least_one_contract() -> None:
    """A glob that matches nothing passes every parametrised test below."""
    assert DOCUMENTS, "contracts/ has no documents; every check here would vacuously pass"


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_a_contract_is_valid_openapi(path: Path) -> None:
    """The documents' own acceptance: these lint."""
    spec, base_uri = read_from_filename(str(path))
    validate(spec, base_uri=base_uri)


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_a_contract_names_where_it_came_from(path: Path) -> None:
    """Without a source, a contract is an assertion about a gateway nobody can
    check — and these were written against branches, which move."""
    source = _load(path)["info"]["x-source"]
    assert source["repo"]
    assert source["branch"], "a contract written from an unmerged branch must say which"
    assert source["files"], "name the gateway modules, so a reviewer can diff them"


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda p: p.name)
def test_every_pinning_test_a_contract_cites_exists(path: Path) -> None:
    """A citation that cannot be followed reads as verification that did not
    happen. Only this repository's side can be checked here; the gateway's
    lives in another repo, on branches, and is tracked in MIGRATION.md."""
    consumer = _load(path).get("x-pinned-by", {}).get("consumer", [])
    assert consumer, f"{path.name} claims no consumer test pins it"
    missing = [cited for cited in consumer if not (REPO_ROOT / cited).exists()]
    assert not missing, f"{path.name} cites tests that do not exist: {missing}"


def test_every_gateway_call_this_service_makes_is_described() -> None:
    """The quiet failure: a new call added to a client with no contract for it.

    The dependency then exists in code and nowhere in the record, which is
    exactly the state the split was meant to end. Paths are read out of the
    clients' own literals rather than a hand-kept list, so adding a call
    without adding a document fails here.
    """
    described: set[str] = set()
    for path in DOCUMENTS:
        described.update(_load(path).get("paths", {}))

    # Path literals as the clients write them: "/internal/..." or "/v1/...",
    # with f-string interpolations left in place and normalised below.
    literal = re.compile(r"""["']/(?:internal|v1)/[^"'\s]*["']""")
    called: set[str] = set()
    for source in CLIENT_SOURCES:
        for match in literal.findall(source.read_text()):
            called.add(match[1:-1])

    def normalise(raw: str) -> str:
        # "/internal/agent-grants/{grant_id}/renew" from an f-string reads
        # "/internal/agent-grants/{grant_id}/renew" already; a plain
        # interpolation of a differently-named variable is normalised to the
        # document's parameter name by position, not by spelling.
        return re.sub(r"\{[^}]*\}", "{}", raw)

    described_shapes = {normalise(p) for p in described}
    undescribed = sorted(p for p in called if normalise(p) not in described_shapes)
    assert not undescribed, (
        "these gateway calls are made but described by no contract: "
        f"{undescribed}. Add them to contracts/, or the dependency exists "
        "only in code."
    )
