"""The lifecycle vocabulary in ``domain/`` is the one the schema enforces.

``domain/execution.py`` calls itself "the names a row can carry, in one place.
The store writes them, the API reads them, and neither owns them." That claim
is only worth something if it is checked, because the failure it prevents is
silent: adding a state constant with no migration produces code that raises a
``CHECK`` violation at 3am, and adding a state to the schema without a constant
produces rows no reader recognizes.

This file derives the schema's own answer by walking the migration chain rather
than by copying the constant: the jobs state ``CHECK`` is dropped and re-added
by ``0005_queued_follow_ups`` (it retired ``waiting``), so a test that read the
baseline would assert against a constraint that no longer exists. Reading the
chain is the difference between pinning today's schema and pinning the *process*
that produces it.

What is deliberately not asserted: that ``storage/repositories`` uses only
these constants. It does not -- job-state literals appear inline in SQL in
around three dozen places -- and a test that failed on that would be a
permanent red light rather than a gate. The constants are authoritative for the
*values*; the literals agree with them today, and
``test_the_inline_job_state_literals_match_the_constants`` keeps the agreement
visible without demanding a refactor nobody has scheduled.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from cloud_agent.domain import execution

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations/versions"
STORAGE = REPO / "apps" / "backend" / "cloud_agent/storage"

#: The states ``0005_queued_follow_ups`` removed, and why. Kept here so a
#: future reader who greps for ``waiting`` and finds it in the baseline knows
#: it is history rather than a state the code may use.
RETIRED_JOB_STATES = {
    "waiting": "retired by 0005_queued_follow_ups; the scheduler checks "
    "earlier unfinished turns instead of gating them",
}


def _migration_order() -> list[Path]:
    """Every revision file in the order the chain applies them."""
    pairs: dict[str, tuple[str, tuple[str, ...]]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                target = getattr(node.targets[0], "id", None)
                if target in ("revision", "down_revision"):
                    values[target] = ast.literal_eval(node.value)
        parents = values.get("down_revision")
        if parents is None:
            parent_tuple: tuple[str, ...] = ()
        elif isinstance(parents, str):
            parent_tuple = (parents,)
        else:
            parent_tuple = tuple(parents)  # type: ignore[arg-type]
        pairs[str(values["revision"])] = (path, parent_tuple)

    ordered: list[Path] = []
    done: set[str] = set()
    while len(ordered) < len(pairs):
        progressed = False
        for revision, (path, parent_tuple) in pairs.items():
            if revision in done:
                continue
            if all(parent in done for parent in parent_tuple):
                ordered.append(path)
                done.add(revision)
                progressed = True
        assert progressed, "the migration chain has a cycle or a missing parent"
    return ordered


def _upgrade_source(path: Path) -> str:
    """The literal text an ``upgrade()`` body finally executes, as one string.

    Only ``upgrade()``, never ``downgrade()``. This matters and it is not
    pedantry: ``0005_queued_follow_ups`` re-adds the state ``CHECK`` twice, once
    in ``upgrade()`` without ``waiting`` and once in ``downgrade()`` with it
    restored. Walking the whole file takes whichever comes last in the text and
    asserts against the *downgrade* -- the wrong answer, failing in the
    direction that looks like a product bug rather than a parser bug.

    The statements are also assembled from adjacent Python string literals, so
    the quotes at a line break are source syntax rather than SQL. Joining the
    literals first, and only then matching, is what makes the pattern readable;
    matching against raw source needs a character class tolerant of ``\"``.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "upgrade":
            continue
        return "\n".join(_string_constants(node))
    return ""


def _string_constants(node: ast.AST) -> list[str]:
    """Every string literal in ``node``, in source order, as separate lines."""
    found: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append((child.lineno, child.value))
    found.sort(key=lambda item: item[0])
    return [value for _, value in found]


def _jobs_state_check() -> tuple[str, ...]:
    """The ``agent_jobs`` state ``CHECK`` after the whole chain has applied."""
    states: tuple[str, ...] | None = None
    pattern = re.compile(
        r"ADD CONSTRAINT agent_jobs_state_check[\s\"]*"
        r"\"?\s*CHECK \(state IN \(([^)]*)\)\)",
        re.S,
    )
    baseline = re.compile(
        r"CREATE TABLE agent_jobs \(.*?state TEXT NOT NULL DEFAULT 'queued'\s*\n\s*"
        r"CHECK \(state IN \(([^)]*)\)\)",
        re.S,
    )
    for path in _migration_order():
        if path.name.startswith("0001_"):
            # The baseline keeps its DDL in a module-level ``_STATEMENTS``
            # tuple and its ``upgrade()`` only iterates it, so the whole file
            # is the right source here -- unlike every later revision, whose
            # ``downgrade()`` must be excluded.
            match = baseline.search(path.read_text(encoding="utf-8"))
            if match:
                states = _split(match.group(1))
            continue
        for match in pattern.finditer(_upgrade_source(path)):
            states = _split(match.group(1))
    assert states is not None, "no agent_jobs state CHECK found anywhere in the chain"
    return states


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().strip("'").strip('"') for part in raw.split(",") if part.strip())


def _attempt_status_check() -> tuple[str, ...]:
    """The ``agent_attempts`` status ``CHECK``, from the baseline."""
    source = (VERSIONS / "0001_schema_baseline.py").read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE agent_attempts \(.*?status TEXT NOT NULL DEFAULT 'running'\s*\n\s*"
        r"CHECK \(status IN \(([^)]*)\)\)",
        source,
        re.S,
    )
    assert match, "the attempt status CHECK moved; update this parser with it"
    return _split(match.group(1))


def _job_state_constants() -> dict[str, str]:
    """Every module-level job-state constant, discovered rather than enumerated.

    Enumerating the five names by hand would only test the module I already
    read. The failure this file exists to catch is somebody *adding* a state --
    ``PAUSED = "paused"`` with no migration -- so the set has to be derived
    from the module. A job state here is an uppercase string constant that is
    not an attempt status, an event name, or a tuple of other states.
    """
    tree = ast.parse((REPO / "apps/backend/cloud_agent/domain/execution.py").read_text())
    excluded = {"ATTEMPT_RUNNING", "ATTEMPT_SUPERSEDED", "ATTEMPT_FINISHED", "ATTEMPT_ABORTED"}
    states: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], "id", None)
        if not name or not name.isupper():
            continue
        if name in excluded or name.startswith("EVENT_") or name.endswith("_STATES"):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            states[name] = node.value.value
    return states


def _attempt_status_constants() -> dict[str, str]:
    """The attempt-status constants, discovered the same way."""
    tree = ast.parse((REPO / "apps/backend/cloud_agent/domain/execution.py").read_text())
    statuses: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if (
                name
                and name.startswith("ATTEMPT_")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                statuses[name] = node.value.value
    return statuses


# ── jobs ──────────────────────────────────────────────────────────────────


def test_every_job_state_the_code_names_is_a_state_the_schema_accepts() -> None:
    """A constant with no migration is a ``CHECK`` violation waiting for traffic."""
    schema_states = set(_jobs_state_check())
    named = _job_state_constants()
    assert named, "no job-state constants were discovered; the parser lost its target"
    missing = {value for value in named.values() if value not in schema_states}
    assert not missing, (
        f"domain/execution.py names job states the schema rejects: {sorted(missing)}; "
        "add a migration that widens agent_jobs_state_check"
    )


def test_every_job_state_the_schema_accepts_is_accounted_for() -> None:
    """The reverse direction: a schema state nobody names is a row nobody reads."""
    schema_states = set(_jobs_state_check())
    named = set(_job_state_constants().values())
    unaccounted = schema_states - named - set(RETIRED_JOB_STATES)
    assert not unaccounted, (
        f"the schema accepts job states nothing in domain/ or the retired register "
        f"explains: {sorted(unaccounted)}"
    )


def test_the_retired_register_is_still_retired() -> None:
    """``waiting`` was removed by 0005; nothing may quietly bring it back.

    If the schema re-accepts it, this fails and whoever did it says why. That
    is the point: the state was two scheduler generations ago, and a partial
    resurrection would put queued work in a state the claim query skips.
    """
    schema_states = set(_jobs_state_check())
    still_retired = set(RETIRED_JOB_STATES) - schema_states
    assert still_retired == set(RETIRED_JOB_STATES), (
        "a retired job state is accepted by the schema again: "
        f"{sorted(set(RETIRED_JOB_STATES) & schema_states)}"
    )


def test_terminal_states_are_a_subset_of_the_states_a_row_may_carry() -> None:
    assert set(execution.TERMINAL_STATES) <= set(_jobs_state_check())
    # And they must be exactly the three the schema's terminal-shaped indexes
    # and the fork-copy CHECK agree on.
    assert set(execution.TERMINAL_STATES) == {
        execution.SUCCEEDED,
        execution.FAILED,
        execution.CANCELLED,
    }


# ── attempts ──────────────────────────────────────────────────────────────


def test_every_attempt_status_the_code_names_is_one_the_schema_accepts() -> None:
    schema_statuses = set(_attempt_status_check())
    named = _attempt_status_constants()
    assert named, "no attempt-status constants were discovered"
    rejected = {value for value in named.values() if value not in schema_statuses}
    assert not rejected, f"attempt statuses the schema rejects: {sorted(rejected)}"


def test_attempt_statuses_are_distinct_values() -> None:
    """Two constants sharing a value would collapse two lifecycle outcomes.

    ``aborted`` and ``superseded`` in particular must not alias: the first does
    not spend a retry, the second does.
    """
    values = [
        execution.ATTEMPT_RUNNING,
        execution.ATTEMPT_SUPERSEDED,
        execution.ATTEMPT_FINISHED,
        execution.ATTEMPT_ABORTED,
    ]
    assert len(set(values)) == len(values), f"duplicate attempt status values: {values}"
    assert execution.ATTEMPT_ABORTED != execution.ATTEMPT_SUPERSEDED


def test_event_names_are_distinct_and_non_empty() -> None:
    events = {execution.EVENT_ATTEMPT_SUPERSEDED, execution.EVENT_ATTEMPT_ABORTED}
    assert len(events) == 2
    assert all(event and event == event.strip() for event in events)


# ── the inline literals agree, even though they are not refactored ────────


def test_the_inline_job_state_literals_match_the_constants() -> None:
    """Storage writes state names inline; this keeps them spelling the same words.

    This does **not** require the storage layer to import the constants -- it
    does not, and unifying that is a separate change. It requires only that the
    literals it does use are states the schema accepts, so a rename in
    ``domain/`` cannot leave a stale string writing an unaccepted value.
    """
    schema_states = set(_jobs_state_check())
    allowed = schema_states | set(RETIRED_JOB_STATES)
    offenders: list[str] = []
    literal = re.compile(r"'(queued|waiting|running|succeeded|failed|cancelled)'")
    for path in sorted(STORAGE.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        for match in literal.finditer(path.read_text(encoding="utf-8")):
            if match.group(1) not in allowed:
                offenders.append(f"{path.relative_to(REPO)}: {match.group(1)}")
    assert not offenders, f"job-state literals the schema does not accept: {offenders}"


def test_the_state_check_parser_found_a_real_constraint() -> None:
    """Guard the guard: a parser that silently returns nothing proves nothing."""
    states = _jobs_state_check()
    assert len(states) >= 5, states
    assert execution.SUCCEEDED in states
    assert execution.QUEUED in states
