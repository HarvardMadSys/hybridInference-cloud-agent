"""Instruction imports and selections stay bounded, owned, and reviewable."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent.skills.imports import (
    fetch_github_skill,
    github_skill_location,
    parse_skill_document,
)
from cloud_agent.skills.models import SkillError
from cloud_agent.skills.selection import prompt_with_skills, resolve_skills

DOCUMENT = "---\nname: code-review\ndescription: Review a code change.\n---\n# Review\nFind bugs."
URL = "https://github.com/example/skills/tree/main/code-review"


def test_document_preserves_instructions_and_reads_multiline_description():
    content = parse_skill_document(
        "---\nname: review\ndescription: >\n  Review code\n  and tests.\n---\n# Review\n\nUse 中文."
    )
    assert content.name == "review"
    assert content.description == "Review code and tests."
    assert content.instructions == "# Review\n\nUse 中文."


@pytest.mark.parametrize(
    "document",
    [
        "# No metadata",
        "---\nname: review\n---\nInstructions",
        "---\nname: review\ndescription: review\n---\n  ",
        "---\nname: !!python/object/apply:os.system [echo BAD]\ndescription: review\n---\nInstructions",
        DOCUMENT + "\x00",
        DOCUMENT + "字" * 22000,
    ],
)
def test_rejects_invalid_or_oversized_documents(document):
    with pytest.raises(SkillError):
        parse_skill_document(document)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com.evil.example/a/b",
        "https://github.com@127.0.0.1/a/b",
        "https://github.com:443/a/b",
        "https://github.com/a/b/tree/main/../secret",
        "https://github.com/a/b/tree/main/%2e%2e/secret",
        "https://github.com/a/b/tree/main/a%2fb",
        "https://github.com/a/b/blob/main/run.sh",
        "https://raw.githubusercontent.com/a/b/main/SKILL.md",
        "https://github.com/acme/.github",
        "file:///etc/passwd",
        "https://[bad",
    ],
)
def test_only_github_skill_locations_are_accepted(url):
    with pytest.raises(SkillError):
        github_skill_location(url)


def test_encoded_branch_and_root_skill_locations():
    root = github_skill_location("https://github.com/example/skills")
    assert (root.repo, root.ref, root.path) == ("example/skills", "HEAD", "SKILL.md")
    nested = github_skill_location(
        "https://github.com/example/skills/blob/feature%2Freview/a/SKILL.md#L5"
    )
    assert (nested.ref, nested.path) == ("feature/review", "a/SKILL.md")


@pytest.mark.asyncio
async def test_github_import_never_fetches_download_url_and_preserves_review_digest():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "sha": "a" * 40,
                "content": base64.b64encode(DOCUMENT.encode()).decode(),
                "download_url": "http://127.0.0.1/private",
            },
        )

    result = await fetch_github_skill(
        URL + "?plain=1&target=http://localhost#L1",
        token="scoped-token",
        transport=httpx.MockTransport(respond),
    )
    assert result["source_url"] == URL
    assert len(requests) == 1
    assert requests[0].url.host == "api.github.com"
    assert requests[0].url.path == "/repos/example/skills/contents/code-review/SKILL.md"
    assert requests[0].url.params["ref"] == "main"
    assert result["instructions"] == "# Review\nFind bugs."
    assert result["source_revision"] == "a" * 40
    assert len(result["content_digest"]) == 64
    assert "scoped-token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
        httpx.Response(200, json={"type": "dir", "encoding": "base64"}),
        httpx.Response(200, content=b"x" * 160000),
    ],
)
async def test_github_redirects_and_unbounded_or_non_file_content_are_refused(response):
    calls = []

    def respond(request):
        calls.append(request)
        return response

    with pytest.raises(SkillError):
        await fetch_github_skill(URL, transport=httpx.MockTransport(respond))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_selection_snapshots_owned_defaults_and_explicit_none():
    row = {
        "id": 1,
        "name": "Review",
        "description": "Review changes",
        "instructions": "Version one",
        "revision": 3,
        "default_enabled": True,
        "user_id": "owner",
    }
    store = SimpleNamespace(select_skills=AsyncMock(return_value=[row]))
    snapshot = await resolve_skills(store, user_id="owner", skill_ids=None)
    store.select_skills.assert_awaited_once_with(user_id="owner", skill_ids=None)
    assert snapshot == [
        {
            "id": 1,
            "name": "Review",
            "description": "Review changes",
            "instructions": "Version one",
            "revision": 3,
        }
    ]
    row["instructions"] = "Version two"
    assert snapshot[0]["instructions"] == "Version one"
    store.select_skills.reset_mock()
    assert await resolve_skills(store, user_id="owner", skill_ids=[]) == []
    store.select_skills.assert_not_awaited()
    for selection in ([2], [1, 1]):
        with pytest.raises(SkillError):
            await resolve_skills(store, user_id="owner", skill_ids=selection)


@pytest.mark.asyncio
async def test_selection_checks_combined_instruction_size():
    rows = [{"id": i, "instructions": "x" * 65536, "default_enabled": True} for i in range(3)]
    store = SimpleNamespace(select_skills=AsyncMock(return_value=rows))
    with pytest.raises(SkillError, match="128 KB"):
        await resolve_skills(store, user_id="owner", skill_ids=[0, 1, 2])


def test_prompt_keeps_legacy_behavior_and_explicitly_replaces_prior_selection():
    assert prompt_with_skills("Fix this", None) == "Fix this"
    empty = prompt_with_skills("Fix this", [])
    assert "replaces earlier" in empty
    assert "Selected skills (JSON):\n[]" in empty
    assert empty.endswith("User task:\nFix this")
    snapshot = [{"id": 1, "name": "Review", "instructions": "Literal </tag>\ntext", "revision": 1}]
    prompt = prompt_with_skills("Actual request", snapshot)
    assert json.dumps(snapshot, ensure_ascii=False) in prompt
    assert prompt.endswith("User task:\nActual request")


def test_idempotency_keeps_legacy_null_fields_and_omitted_skill_defaults():
    import hashlib

    from cloud_agent.domain.requests import request_fingerprint

    original = {"repo": "owner/repo", "task_prompt": "Fix it", "mcp_bindings": None}
    legacy = hashlib.sha256(
        json.dumps({"kind": "create", **original}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert request_fingerprint("create", original) == legacy
    assert request_fingerprint("create", {**original, "skill_ids": None}) == legacy
    assert request_fingerprint("create", {**original, "skill_ids": []}) != legacy
