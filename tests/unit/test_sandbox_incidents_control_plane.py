"""Sandbox incidents on the control plane: stored from beats, shown, and told.

- **A beat is never refused for its incidents**, and it confirms them only
  when every row was stored, since the host forgets rows on that word alone.
- **The user sees a line only for what broke a turn, in the chat it concerns.**
- **The agent hears what it was not told**, through a cursor the claim only
  marks: the first prompt also says the sandbox has a browser.
"""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from typing import Any

from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    _enroll,
    _live_group,
    client,
)

from cloud_agent.application.sandbox_incidents import (
    BROWSER_NOTE,
    SandboxNotes,
    incident_line,
    record_heartbeat_incidents,
    sandbox_notes,
)
from cloud_agent.prompt_injections import claim_prompt
from cloud_agent.schemas.hosts import RetainedSandboxBeat
from cloud_agent_shared.sandbox_incidents import parse_incidents
from cloud_agent_shared.shared_protocol import SANDBOX_BROWSER_CAPABILITY


def _incident(kind: str, n: int = 1, **fields: Any) -> dict[str, Any]:
    return {
        "incident_id": f"id-{kind}-{n}",
        "kind": kind,
        "scope": fields.pop("scope", "sandbox"),
        "thread_id": fields.pop("thread_id", None),
        "observed_at": 1_800_000_000.0 + n,
        "count": fields.pop("count", 1),
        "details": fields,
    }


class Store:
    def __init__(self, *, fail: bool = False, untold: dict | None = None) -> None:
        self.fail = fail
        self.untold = untold
        self.recorded: list[dict] = []
        self.marked: list[dict] = []

    async def record_sandbox_incidents(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("database away")
        self.recorded.append(kwargs)
        return [{"id": n + 1, **row} for n, row in enumerate(kwargs["incidents"])]

    async def untold_incidents(self, **kwargs: Any) -> dict | None:
        return self.untold

    async def mark_attempt_incidents(self, **kwargs: Any) -> None:
        self.marked.append(kwargs)


def _group(*incidents: dict[str, Any]) -> RetainedSandboxBeat:
    return RetainedSandboxBeat.model_validate(
        {**_live_group(("th-1", 1)), "incidents": list(incidents)}
    )


# -- the beat ------------------------------------------------------------------------


def test_a_malformed_incident_is_dropped_and_the_beat_is_read() -> None:
    group = _group(
        _incident("out_of_memory"),
        {"incident_id": "x", "kind": "out_of_memory"},  # no scope
        _incident("no_such_kind"),
        _incident("mcp_server_failed", scope="member"),  # a member row with no member
        _incident("mcp_server_failed", 2, scope="member", thread_id="th-1", phase="installed"),
        _incident("browser_crashed", stderr="secret"),  # not a field it may carry
        "junk",
    )
    assert [incident.kind for incident in group.incidents] == ["out_of_memory"]
    assert parse_incidents("not a list") == []


async def test_new_rows_that_broke_a_turn_are_shown_in_the_turns_they_concern() -> None:
    store = Store()
    group = _group(
        _incident("out_of_memory"),
        _incident("out_of_memory.repeated", count=4),
        _incident("memory_high", peak=9, limit=10),
        _incident(
            "mcp_server_failed", scope="member", thread_id="th-1", alias="docs", phase="start"
        ),
    )

    assert await record_heartbeat_incidents(store, "runner-a", [group]) is True

    [recorded] = store.recorded
    assert recorded["host"] == "runner-a" and recorded["sandbox_id"] == group.sandbox_id
    events = [row["event_type"] for row in recorded["incidents"]]
    assert events == [
        "incident.out_of_memory",
        "incident.out_of_memory.repeated",
        "incident.memory_high",
        "incident.mcp_server_failed",
    ]
    details = recorded["incidents"][3]["details"]
    assert details["thread_id"] == "th-1" and details["incident_id"].startswith("id-")
    assert details["observed_at"].startswith("2027-")
    # Each row carries its notice to the store, which writes both in one
    # transaction. A repeat summary and a peak are not shown; a member's
    # failure goes to that member.
    notices = [row.get("notice") for row in recorded["incidents"]]
    assert notices[1:3] == [None, None]
    assert [(note["thread_id"], note["payload"]) for note in (notices[0], notices[3])] == [
        (None, {"phase": "sandbox_incident", "incident": "out_of_memory"}),
        (
            "th-1",
            {
                "phase": "sandbox_incident",
                "incident": "mcp_server_failed",
                "alias": "docs",
                "server_phase": "start",
            },
        ),
    ]


async def test_a_store_failure_withholds_the_confirmation_and_never_raises() -> None:
    assert (
        await record_heartbeat_incidents(
            Store(fail=True), "h", [_group(_incident("out_of_memory"))]
        )
        is False
    )


async def test_the_heartbeat_confirms_what_it_stored(client) -> None:
    async_client, store = client
    credential = await _enroll(async_client, store)
    stored: list[dict] = []

    async def record(**kwargs: Any) -> list[dict]:
        stored.append(kwargs)
        return []

    store.record_sandbox_incidents = record
    group = {**_live_group(("th-1", 1)), "incidents": [_incident("browser_crashed")]}
    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [group]},
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["incidents"] == "accepted"
    assert [row["event_type"] for row in stored[0]["incidents"]] == ["incident.browser_crashed"]


# -- what the agent is told ------------------------------------------------------------


def _claim(turn_no: int = 1) -> dict[str, Any]:
    return {"thread_id": "th-1", "attempt_id": 7, "turn_no": turn_no}


async def test_the_first_turn_on_a_browser_host_is_told_about_the_browser() -> None:
    assert (await sandbox_notes(Store(), _claim(1), [SANDBOX_BROWSER_CAPABILITY])).browser
    assert not (await sandbox_notes(Store(), _claim(2), [SANDBOX_BROWSER_CAPABILITY])).browser
    assert not (await sandbox_notes(Store(), _claim(1), [])).browser


async def test_untold_incidents_are_told_newest_first_and_marked_on_the_attempt() -> None:
    rows = [
        {
            "id": n,
            "event_type": "incident.browser_crashed",
            "details": {"observed_at": f"2026-11-05T10:0{n}:00+00:00"},
        }
        for n in range(1, 8)
    ]
    store = Store(untold={"through_id": 42, "rows": rows, "total": 9})

    notes = await sandbox_notes(store, _claim(3), [])

    assert store.marked == [{"attempt_id": 7, "thread_id": "th-1", "through_id": 42}]
    assert len(notes.incident_lines) == 5
    assert notes.incident_lines[0].startswith("The browser stopped at 10:07 UTC")
    text = notes.incidents_text()
    assert text.startswith("[Platform note] Since you were last told")
    assert text.rstrip().endswith("- And 4 more.")


async def test_a_sandbox_with_no_incident_marks_nothing() -> None:
    store = Store(untold=None)
    notes = await sandbox_notes(store, _claim(2), [])
    assert store.marked == [] and notes.incidents_text() == ""


def test_each_incident_reads_plainly() -> None:
    at = {"observed_at": "2026-11-05T15:02:00+00:00"}
    assert "ran out of memory at 15:02 UTC" in incident_line("incident.out_of_memory", at)
    assert "started again" in incident_line("incident.container_exited", at)
    assert "pages that were open are gone" in incident_line("incident.browser_crashed", at)
    start = incident_line("incident.mcp_server_failed", {**at, "alias": "docs", "phase": "start"})
    crash = incident_line("incident.mcp_server_failed", {**at, "alias": "docs", "phase": "crashed"})
    assert "`docs` could not start" in start and "`docs` stopped unexpectedly" in crash
    repeated = incident_line("incident.out_of_memory.repeated", {**at, "count": 3})
    assert repeated.endswith("It happened 3 more time(s).")
    assert "unknown time" in incident_line("incident.out_of_memory", {})


def test_the_prompt_records_both_notes_by_size() -> None:
    notes = SandboxNotes(
        browser=True, incident_lines=("The browser stopped.",), untold=1, through_id=3
    )
    prompt, injections = claim_prompt(
        "task",
        attachments=None,
        memories=None,
        skills=None,
        ssh_servers=[],
        goal=None,
        sandbox=notes,
    )
    assert prompt.startswith("[Platform note] Since you were last told")
    assert BROWSER_NOTE in prompt and prompt.endswith("task")
    assert [(item["producer"], item["items"]) for item in injections] == [
        ("sandbox_browser", 1),
        ("sandbox_incidents", 1),
    ]
    _, without = claim_prompt(
        "task", attachments=None, memories=None, skills=None, ssh_servers=[], goal=None
    )
    assert without == []
