"""MCP selection, binding, and claim behavior for the job router.

The fake store and HTTP fixtures remain owned by the core route suite; this
module contains only the intact MCP-facing behavior cases.
"""

from __future__ import annotations

from datetime import timedelta

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    _OTHER,
    _OWNER,
    _TEST_MODELS,
    _UPSTREAM_TOKEN,
    UTC,
    Any,
    ASGITransport,
    AsyncClient,
    FakeStorage,
    _api_key_secret,
    _build_app,
    _create_job,
    _FakeOwners,
    _next_turn,
    _send,
    client,
    datetime,
    json,
    pytest,
    store,
    worker,
)
from tests.support.owners import connection_claim, on_connection

pytestmark = pytest.mark.asyncio


# ── MCP connections ───────────────────────────────────────────────────


def _binding(
    connection_id: int,
    *,
    alias: str = "github",
    tools: dict[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One ``McpBindingInput`` body, as the composer sends it.

    ``tools`` is the selection itself, name to deny/confirm/allow. A name that
    is absent is denied, which is why every case here names the tools it means.
    """
    binding: dict[str, Any] = {
        "connection_id": connection_id,
        "alias": alias,
        "tools": {"get_issue": "allow"} if tools is None else tools,
        "resources": {},
        "prompts": {},
        "enabled": True,
    }
    binding.update(overrides)
    return binding


def _job_body(**overrides: Any) -> dict[str, Any]:
    """A valid submission, with the fields under test overridable."""
    body: dict[str, Any] = {"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"}
    body.update(overrides)
    return on_connection(body)


@pytest.mark.parametrize(
    ("runtime", "plugins"),
    [
        ("claude-code", [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]),
        ("codex", [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]),
        ("pi", [{"plugin": "@owner/agent-extension@1.2.3"}]),
        ("opencode", [{"plugin": "@owner/agent-extension@1.2.3"}]),
        ("kilo", [{"plugin": "@owner/agent-extension@1.2.3"}]),
    ],
)
async def test_plugins_are_retained_and_visible_on_job_reads(
    client: AsyncClient, store: FakeStorage, runtime: str, plugins: list[dict[str, str]]
):
    created = await client.post("/v1/agent/jobs", json=_job_body(runtime=runtime, plugins=plugins))

    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    assert store.jobs[job_id]["plugins"] == plugins
    assert [
        {key: value for key, value in entry.items() if value is not None}
        for entry in created.json()["plugins"]
    ] == plugins
    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["plugins"] == created.json()["plugins"]


async def test_plugins_cannot_be_changed_in_a_follow_up(client: AsyncClient, store: FakeStorage):
    parent_id = await _create_job(client)
    response = await _send(client, store, parent_id, {"prompt": "continue", "plugins": []})

    assert response.status_code == 422, response.text
    assert store.pending == {}


async def test_duplicate_plugins_are_refused_before_model_lookup(
    client: AsyncClient, store: FakeStorage
):
    plugin = {"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}
    response = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=[plugin, plugin], model="no-such-model")
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["type"] == "invalid_plugin_selection"
    assert store.jobs == {}


async def test_plugin_shape_is_validated_for_the_requested_runtime(
    client: AsyncClient, store: FakeStorage
):
    response = await client.post(
        "/v1/agent/jobs",
        json=_job_body(plugins=[{"plugin": "reviewer", "marketplace": "owner/plugins"}]),
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["type"] == "invalid_plugin_selection"
    assert store.jobs == {}


async def test_plugin_changes_cannot_reuse_a_submission_key(
    client: AsyncClient, store: FakeStorage
):
    first_plugins = [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}]
    headers = {"Idempotency-Key": "plugin-submit"}
    first = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=first_plugins), headers=headers
    )
    assert first.status_code == 201, first.text
    repeated = await client.post(
        "/v1/agent/jobs", json=_job_body(plugins=first_plugins), headers=headers
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == first.json()["id"]
    changed = await client.post(
        "/v1/agent/jobs",
        json=_job_body(plugins=[{**first_plugins[0], "marketplace": "owner/another-marketplace"}]),
        headers=headers,
    )
    assert changed.status_code == 409, changed.text
    assert len(store.jobs) == 1


async def test_the_composer_is_told_which_connections_and_which_runtimes(
    client: AsyncClient, store: FakeStorage
):
    """A picker built from anything but the create endpoint's rules is decoration.

    The composer learns which connections exist, what they offer and whether
    they currently work. It never learns the address or the credential:
    ``McpConnectionSummary`` has no field either could land in.
    """
    store.connect_mcp(
        user_id=_OWNER,
        label="DeepWiki",
        template="deepwiki",
        url="https://wiki.example.com/mcp",
        tools=("read_wiki",),
    )
    store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    config = (await client.get("/v1/agent/config")).json()

    assert [entry["label"] for entry in config["mcp_connections"]] == ["DeepWiki", "GitHub"]
    # The composer is told the same list the create path enforces, so a picker
    # cannot offer a runtime that would then be refused.
    assert config["mcp_runtimes"] == ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert sorted(config["plugin_runtimes"]) == ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert [entry["tool_count"] for entry in config["mcp_connections"]] == [1, 1]
    assert all(entry["available"] for entry in config["mcp_connections"])
    assert _UPSTREAM_TOKEN not in json.dumps(config)
    assert "mcp.example.com" not in json.dumps(config)


async def test_the_composer_offers_only_what_this_user_connected(
    client: AsyncClient, store: FakeStorage
):
    """The property the deployment catalogue could not have.

    Both users exist and both could connect the same service; only the
    connection differs, and it is the only thing that may decide what the owner
    sees.
    """
    store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")

    config = (await client.get("/v1/agent/config")).json()
    assert config["mcp_connections"] == []
    assert "ghu_someone_elses_secret" not in json.dumps(config)


async def test_omitting_bindings_means_no_connections(client: AsyncClient, store: FakeStorage):
    """Connecting is availability, not consent.

    A task that says nothing about tools gets none: a job that silently received
    every connection its owner has would be spending credentials the prompt was
    never written around.
    """
    store.connect_mcp(user_id=_OWNER)
    created = await client.post("/v1/agent/jobs", json=_job_body())

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == []


async def test_an_explicit_empty_list_means_no_connections(client: AsyncClient, store: FakeStorage):
    """Choosing none is a real choice and must not be read as "unset"."""
    store.connect_mcp(user_id=_OWNER)
    created = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=[]))

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == []


async def test_a_job_records_the_bindings_it_was_created_with(
    client: AsyncClient, store: FakeStorage
):
    """The response names the connection by alias and label, never by address."""
    row = store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )

    assert created.status_code == 201, created.text
    assert created.json()["mcp_bindings"] == [
        {"connection_id": row["id"], "alias": "github", "label": "GitHub"}
    ]
    assert "mcp.example.com" not in created.text


async def test_a_selection_is_stamped_against_the_snapshot_it_was_chosen_from(
    client: AsyncClient, store: FakeStorage
):
    """An approval is an approval *of something*.

    The fingerprint records the tool's description and schema at the moment the
    owner allowed it. Without it, a server that later redefines the tool would
    inherit the old approval for a different thing wearing the same name.
    """
    row = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 201, created.text

    (binding,) = store.mcp_bindings[created.json()["thread_id"]]
    rule = binding["rules"]["get_issue"]
    assert rule["policy"] == "allow"
    assert len(rule["fingerprint"]) == 64


@pytest.mark.parametrize("explicit", [False, True])
async def test_new_jobs_cannot_store_ambiguous_mcp_aliases(client, store, explicit):
    first = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    second = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    selections = [_binding(first["id"]), _binding(second["id"])]
    for selection in selections:
        selection["alias"] = "github" if explicit else None
    created = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=selections))
    if explicit:
        assert created.status_code == 400
        assert "more than one connection" in created.text
    else:
        assert created.status_code == 201, created.text
        aliases = [binding["alias"] for binding in created.json()["mcp_bindings"]]
        assert len(aliases) == len(set(aliases)) == 2


@pytest.mark.parametrize("surface", ["resources", "prompts"])
@pytest.mark.parametrize("policy", ["allow", "confirm", "deny", "invalid"])
async def test_new_jobs_honor_mcp_resource_and_prompt_only_selections(
    client, store, surface, policy
):
    connection = store.connect_mcp(user_id=_OWNER)
    store.mcp_capabilities[connection["id"]][surface] = [{"name": "docs"}]
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(
            mcp_bindings=[
                {
                    "connection_id": connection["id"],
                    "tools": {},
                    surface: {"docs": policy},
                }
            ]
        ),
    )
    if policy == "invalid":
        assert created.status_code == 400
        assert store.mcp_bindings == {}
    else:
        assert created.status_code == 201, created.text
        binding = store.mcp_bindings[created.json()["thread_id"]][0]
        assert binding["enabled"] is (policy != "deny")
        assert binding["resource_rules" if surface == "resources" else "prompt_rules"] == {
            "docs": policy
        }


async def test_naming_a_connection_the_caller_does_not_own_is_refused_at_creation(
    client: AsyncClient, store: FakeStorage
):
    """Refused in the same words as one that does not exist.

    A caller must not be able to tell "nobody has this" from "somebody else has
    this" -- the difference is another user's account state, and the reply to a
    request that may not have it is the same either way.
    """
    theirs = store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")

    owned = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(theirs["id"])])
    )
    missing = await client.post("/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(9999)]))

    assert owned.status_code == 400
    assert (
        owned.json()["detail"]["error"]["message"].replace(str(theirs["id"]), "9999")
        == missing.json()["detail"]["error"]["message"]
    )
    assert "ghu_someone_elses_secret" not in owned.text


@pytest.mark.parametrize("runtime", ["codex", "kilo", "claude-code", "opencode", "pi"])
async def test_every_supported_runtime_accepts_mcp(
    client: AsyncClient, store: FakeStorage, runtime
):
    """The composer and submission API promise the same MCP experience."""
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(runtime=runtime, mcp_bindings=[_binding(row["id"])]),
    )
    assert created.status_code == 201
    assert created.json()["mcp_bindings"][0]["connection_id"] == row["id"]


async def test_an_unknown_tool_selection_is_refused_at_creation(
    client: AsyncClient, store: FakeStorage
):
    """A selection naming a tool the server does not offer is a refusal.

    Accepting it would create a job around a tool the agent turns out not to
    have, which does not fail -- it quietly produces the wrong work.
    """
    row = store.connect_mcp(user_id=_OWNER, tools=("get_issue",))
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(mcp_bindings=[_binding(row["id"], tools={"delete_repo": "allow"})]),
    )

    assert created.status_code == 400
    assert "delete_repo" in created.text
    assert "does not offer" in created.text


async def test_a_connection_that_cannot_be_used_says_why_instead_of_vanishing(
    client: AsyncClient, store: FakeStorage
):
    """Connected and unusable is a real state, and both surfaces must name it.

    Omitting it from the composer leaves its owner with a picker that silently
    drops the server they are looking for; letting a job name it puts a dead
    credential behind tools the prompt was written around.
    """
    row = store.connect_mcp(
        user_id=_OWNER,
        label="GitHub",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    config = (await client.get("/v1/agent/config")).json()
    (entry,) = config["mcp_connections"]
    assert entry["connection_id"] == row["id"]
    assert entry["available"] is False
    assert "expired" in entry["error"]

    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 400
    assert "expired" in created.text


async def test_disconnecting_clears_the_secret_and_bumps_the_epoch(
    client: AsyncClient, store: FakeStorage
):
    """A tombstone that still holds a token is not a disconnect.

    The row is kept so a binding can say *this was disconnected* rather than
    *this never existed*, and its versions move so any approval made against the
    old credential stops resolving instead of quietly continuing.
    """
    row = store.connect_mcp(user_id=_OWNER)
    other = store.connect_mcp(user_id=_OTHER, token="ghu_someone_elses_secret")
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    assert created.status_code == 201, created.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    assert row["access_token_ciphertext"] is None
    assert row["refresh_token_ciphertext"] is None
    assert row["deleted_at"] is not None
    assert row["status"] == "disabled"
    assert row["authorization_epoch"] == 2
    # Only the caller's row: a disconnect without its owner scope would take
    # everyone else's connection with it.
    assert other["access_token_ciphertext"] is not None
    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=other["id"], user_id=_OWNER)
        is False
    )
    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    assert len(await store.mcp.list_mcp_connections(user_id=_OWNER, include_deleted=True)) == 1


async def test_a_follow_up_runs_when_an_account_connection_was_disconnected(
    client: AsyncClient, store: FakeStorage
):
    """One disconnected integration must not prevent unrelated conversation work."""
    row = store.connect_mcp(user_id=_OWNER, label="GitHub")
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    job_id = created.json()["id"]
    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    follow_up = await _next_turn(client, store, job_id, {"prompt": "again"})

    assert follow_up["state"] == "queued"
    from cloud_agent.mcp.registry import servers_for_thread

    assert await servers_for_thread(store.mcp, thread_id=created.json()["thread_id"]) == {}


async def test_a_follow_up_still_runs_while_its_thread_s_connection_is_connected(
    client: AsyncClient, store: FakeStorage
):
    """The check must not refuse the ordinary case it runs on."""
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )

    follow_up = await _next_turn(client, store, created.json()["id"], {"prompt": "again"})

    assert follow_up["state"] == "queued"


async def test_disconnecting_leaves_a_running_jobs_binding_list_alone(
    client: AsyncClient, store: FakeStorage
):
    """A job keeps the connection it was created with and loses the credential.

    Nothing hunts down live jobs on disconnect: bindings are read per call, so
    the job's own list stays as it was and each call fails the usability check
    -- which is what the follow-up case above pins at the HTTP surface.
    """
    row = store.connect_mcp(user_id=_OWNER)
    created = await client.post(
        "/v1/agent/jobs", json=_job_body(mcp_bindings=[_binding(row["id"])])
    )
    job_id = created.json()["id"]
    assert created.json()["mcp_bindings"][0]["connection_id"] == row["id"]

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["mcp_bindings"] == [
        {"connection_id": row["id"], "alias": "github", "label": "GitHub"}
    ]
    assert row["access_token_ciphertext"] is None


async def test_the_integrations_page_sees_every_connection_and_its_reason(
    client: AsyncClient, store: FakeStorage
):
    """One request for one page, and an unusable connection is reported not hidden.

    The aggregate endpoint is where the settings screen learns both halves at
    once. A connection that has stopped working must appear with its reason:
    a page that silently omits it leaves its owner looking for a server that is
    right there, and the separate per-connection API is where they act on it.
    """
    working = store.connect_mcp(user_id=_OWNER, label="GitHub")
    store.connect_mcp(user_id=_OTHER, label="Someone else's", template="sentry")
    broken = store.connect_mcp(user_id=_OWNER, label="Broken", template="wiki")

    response = await client.get("/v1/agent/integrations")

    assert response.status_code == 200
    rows = {entry["name"]: entry for entry in response.json()["mcp"]}
    assert set(rows) == {"GitHub", "Broken"}
    assert rows["GitHub"]["connected"] is True
    assert rows["GitHub"]["configured"] is True
    assert rows["GitHub"]["error"] is None
    assert rows["Broken"]["connected"] is True
    del working, broken


async def test_the_claim_hands_the_runner_the_job_s_bindings(
    client: AsyncClient, store: FakeStorage
):
    """The runner passes aliases to the adapter; it never learns the addresses."""
    github = store.connect_mcp(user_id=_OWNER, label="GitHub", tools=("get_issue",))
    wiki = store.connect_mcp(
        user_id=_OWNER,
        label="DeepWiki",
        template="deepwiki",
        url="https://wiki.example.com/mcp",
        tools=("read_wiki",),
    )
    created = await client.post(
        "/v1/agent/jobs",
        json=_job_body(
            mcp_bindings=[
                _binding(github["id"]),
                _binding(wiki["id"], alias="deepwiki", tools={"read_wiki": "allow"}),
            ]
        ),
    )
    assert created.status_code == 201, created.text

    app = _build_app(store, claim_auth=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as runner:
        claimed = await runner.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "w-1", "lease_ttl_seconds": 60}),
        )
    assert claimed.status_code == 200
    body = claimed.json()
    # Aliases, not summaries: the runtime builds the relay URL's path segment
    # from each name, so a dict here would either fail the schema or address a
    # server that does not exist.
    # The platform's memory tools ride the same endpoint, after the connections.
    assert body["mcp_bindings"] == ["deepwiki", "github", worker.memory_tool.RUNTIME_SERVER_ID]
    assert "mcp.example.com" not in json.dumps(body)
    assert "wiki.example.com" not in json.dumps(body)


async def test_the_claim_hands_over_the_mcp_credential_too(client: AsyncClient):
    """**The whole MCP chain, measured across the claim boundary.**

    This is where it broke and nothing noticed. `worker_claim` minted an
    `mcp`-scoped token and computed the relay address, and
    `WorkerClaimResponse` declared neither — so pydantic dropped both on the
    way out and every sandbox got an empty MCP credential.

    The relay's own tests all passed, because they construct a token and call
    the endpoint directly. They never cross this boundary, which is exactly
    where the two halves stopped agreeing: a field with a writer, a reader, and
    a schema in between that knew about neither.
    """
    await _create_job(client)

    body = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()

    assert body["mcp_token"], "the sandbox was handed no MCP credential"

    from cloud_agent.tokens import SCOPE_MCP, parse_worker_token

    # Scoped, and not the same credential as the runner's.
    assert parse_worker_token(body["mcp_token"])["scope"] == SCOPE_MCP
    assert body["mcp_token"] != body["worker_token"]


@pytest.mark.parametrize("runtime", ["codex", "kilo", "claude-code", "opencode", "pi"])
@pytest.mark.parametrize(
    ("selection", "serper_key", "brave_key", "parallel_key"),
    [
        ("serper", "serper-claim-secret", "", ""),
        ("brave", "", "brave-claim-secret", ""),
        ("parallel", "", "", "parallel-claim-secret"),
        ("auto", "serper-all-secret", "brave-all-secret", "parallel-all-secret"),
    ],
)
async def test_web_search_provider_enables_mcp_without_exposing_keys(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    selection: str,
    serper_key: str,
    brave_key: str,
    parallel_key: str,
) -> None:
    values = {
        worker.web_search.PROVIDER_SETTING: selection,
        worker.web_search.SERPER_API_KEY_SETTING: serper_key,
        worker.web_search.BRAVE_API_KEY_SETTING: brave_key,
        worker.web_search.PARALLEL_API_KEY_SETTING: parallel_key,
    }
    monkeypatch.setattr(
        worker.web_search,
        "text_setting",
        lambda key: values[key],
    )
    created = await client.post("/v1/agent/jobs", json=_job_body(runtime=runtime))
    assert created.status_code == 201, created.text

    body = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()

    assert body["mcp_bindings"] == [
        worker.web_search.RUNTIME_SERVER_ID,
        worker.memory_tool.RUNTIME_SERVER_ID,
    ]
    assert body["mcp_token"]
    assert all(
        secret not in json.dumps(body) for secret in (serper_key, brave_key, parallel_key) if secret
    )


async def test_the_claim_names_no_mcp_relay_address(client: AsyncClient, monkeypatch):
    """The relay is the claiming host's own, so the control plane names none.

    It used to hand one over from `AGENT_SANDBOX_MCP_URL`, a copy of every
    host's tunnel alias that nothing kept in step with the hosts. Left unset,
    every turn whose owner had connected a tool failed at its resident launch.
    A control plane whose environment still carries the variable must not
    bring the copy back.
    """
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")
    await _create_job(client)

    body = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()

    assert "mcp_base_url" not in body
    assert "agent-mcp" not in json.dumps(body)
