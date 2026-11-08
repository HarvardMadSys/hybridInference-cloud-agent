"""The runner's masking set is derived from the claim, and covers every marked credential.

A credential a claim puts into the sandbox is marked
:class:`~cloud_agent_shared.secret_redaction.Masked` on the model that carries
it. These tests walk every model ``ClaimedJob`` can hold: each marked field
must stay out of ``repr``, be filled by the representative claim below, and
have every value it holds in :func:`claim_secret_values`. A new credential
that is marked but not named by its model's ``secrets()`` fails here instead
of reaching durable output.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any, get_args, get_type_hints

from pydantic import BaseModel
from tests.support.ssh_keys import host_key, private_key

from cloud_agent_host.claimed_job import ClaimedJob, claim_secret_values
from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable
from cloud_agent_shared.model_connections import ModelConnectionAccess
from cloud_agent_shared.sandbox_mcp import SandboxMcpServer
from cloud_agent_shared.secret_redaction import Masked, SecretRedactor
from cloud_agent_shared.ssh_access import SshAccess, SshMemberAccess, SshServerAccess

if TYPE_CHECKING:
    from collections.abc import Iterator

_CONNECTION = "mconn_" + "e" * 32


def _server(name: str) -> SshServerAccess:
    return SshServerAccess(
        server_id="3f9c2a1e-0000-4000-8000-00000000000" + str(len(name)),
        name=name,
        address="93.184.216.34",
        port=22,
        username="ubuntu",
        host_key_type="ssh-ed25519",
        host_key=host_key(),
        private_key=private_key(),
    )


_ENVIRONMENT = BoundEnvironment(
    binding_id="bind_1",
    variables=[
        EnvironmentVariable(name="TOKEN", value="setup-token-value"),
        EnvironmentVariable(name="PLAIN", value="not-a-secret-value", secret=False),
    ],
    runtime_variables=[
        EnvironmentVariable(name="TOKEN", value="runtime-token-value"),
        EnvironmentVariable(name="REGION", value="eu-west-1-plain", secret=False),
    ],
    previous_secrets={"API_KEY@history0": "replaced-runtime-secret"},
)
_SSH = SshAccess(
    revision=3,
    members=[
        SshMemberAccess(thread_id="athr_1", slot_id=1, servers=[_server("gpu")]),
        SshMemberAccess(thread_id="athr_2", servers=[_server("db"), _server("lab")]),
    ],
)
_DIRECT = ModelConnectionAccess(
    connection_id=_CONNECTION,
    revision=4,
    base_url="https://chat.example/v1",
    protocol="chat_completions",
    api_key="fixture-chat-upstream-key-0123456789",
)
_TRANSLATED = ModelConnectionAccess(
    **_DIRECT.model_dump(), client_protocol="responses", local_token="c" * 64
)
_BARE = ClaimedJob(
    job_id="ajob_1",
    thread_id="athr_1",
    attempt_id=7,
    attempt_no=1,
    repo="o/n",
    base_sha="a" * 40,
    task_prompt="do it",
    runtime="codex",
    model="model",
    worker_token="ajt.a.b",
    clone_token="ghs_clone-token-runner-only",
)
#: Every credential-bearing field filled, the translated plan included.
_FULL = replace(
    _BARE,
    environment=_ENVIRONMENT,
    ssh_access=_SSH,
    model_connection=_TRANSLATED,
    sandbox_mcp_servers=(SandboxMcpServer(alias="docs", command="npx", args=("pkg",)),),
)
#: :data:`_FULL` with a program's own key as well, which the old hand list missed.
_PROGRAM = SandboxMcpServer(
    alias="search",
    command="npx",
    args=("pkg@1.2.3",),
    env={"API_KEY": "secret://API_KEY", "REGION": "eu"},
    secret_env={"API_KEY": "program-sealed-api-key-value"},
)
_WITH_PROGRAM_KEY = replace(_FULL, sandbox_mcp_servers=(*_FULL.sandbox_mcp_servers, _PROGRAM))


def _hand_assembled(job: ClaimedJob) -> dict[str, str]:
    """The list ``claim_secret_values`` replaced, kept as the reference it must equal."""
    values = job.environment.secrets() if job.environment else {}
    if job.ssh_access is not None:
        values = {**values, **job.ssh_access.secrets()}
    if job.model_connection is not None:
        values = {**values, **job.model_connection.secrets()}
    return values


def _models(annotation: Any) -> Iterator[type[BaseModel]]:
    """The pydantic models an annotation names, at any depth of its arguments."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for argument in get_args(annotation):
        yield from _models(argument)


def _marker(model: type[BaseModel], name: str) -> Masked | None:
    return next(
        (item for item in model.model_fields[name].metadata if isinstance(item, Masked)), None
    )


def _marked_fields() -> set[tuple[type[BaseModel], str]]:
    """Every marked field of every model a ``ClaimedJob`` can carry."""
    pending = [model for hint in get_type_hints(ClaimedJob).values() for model in _models(hint)]
    seen: set[type[BaseModel]] = set()
    marked: set[tuple[type[BaseModel], str]] = set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            if _marker(model, name) is not None:
                marked.add((model, name))
            pending.extend(_models(info.annotation))
    return marked


def _marked_values(value: Any) -> Iterator[tuple[type[BaseModel], str, str]]:
    """Each secret a marked field of ``value`` holds, with the field it is in."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            item = getattr(value, name)
            marker = _marker(type(value), name)
            if marker is not None and (marker.when is None or getattr(value, marker.when)):
                for secret in item.values() if isinstance(item, dict) else (item,):
                    if secret:
                        yield type(value), name, secret
            yield from _marked_values(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _marked_values(item)


def _claim_values(job: ClaimedJob) -> Iterator[tuple[type[BaseModel], str, str]]:
    for item in fields(job):
        yield from _marked_values(getattr(job, item.name))


def test_the_marked_fields_are_the_claims_sandbox_credentials():
    # Adding a credential to a claim model adds it here, where review sees it.
    assert _marked_fields() == {
        (EnvironmentVariable, "value"),
        (BoundEnvironment, "previous_secrets"),
        (SshServerAccess, "private_key"),
        (ModelConnectionAccess, "api_key"),
        (ModelConnectionAccess, "local_token"),
        (SandboxMcpServer, "secret_env"),
    }


def test_a_marked_field_never_shows_in_a_repr():
    for model, name in _marked_fields():
        assert model.model_fields[name].repr is False, (model.__name__, name)
    shown = repr(_WITH_PROGRAM_KEY) + repr(_ENVIRONMENT) + repr(_SSH) + repr(_TRANSLATED)
    shown += repr(_PROGRAM)
    for _, _, secret in _claim_values(_WITH_PROGRAM_KEY):
        assert secret not in shown


def test_every_marked_value_in_a_claim_is_masked():
    found = list(_claim_values(_WITH_PROGRAM_KEY))
    # The representative claim fills every marked field, so none passes unseen.
    assert {(model, name) for model, name, _ in found} == _marked_fields()
    masked = set(claim_secret_values(_WITH_PROGRAM_KEY).values())
    missing = [(model.__name__, name) for model, name, secret in found if secret not in masked]
    assert missing == []


def test_the_derived_set_is_the_hand_assembled_one():
    claims = [
        _BARE,
        replace(_BARE, environment=_ENVIRONMENT),
        replace(_BARE, ssh_access=_SSH),
        replace(_BARE, model_connection=_DIRECT),
        replace(_BARE, model_connection=_TRANSLATED),
        _FULL,
    ]
    for job in claims:
        # In order too: the redactor shows the first name a value has.
        assert list(claim_secret_values(job).items()) == list(_hand_assembled(job).items())
        assert SecretRedactor(claim_secret_values(job)).text(repr(_hand_assembled(job))) == (
            SecretRedactor(_hand_assembled(job)).text(repr(_hand_assembled(job)))
        )
    assert list(claim_secret_values(_FULL)) == [
        "TOKEN",
        "API_KEY@history0",
        "TOKEN@runtime",
        "ssh-key:gpu",
        "ssh-key:db",
        "ssh-key:lab",
        f"model-connection:{_CONNECTION}",
        f"model-connection-translator:{_CONNECTION}",
    ]
    assert claim_secret_values(_BARE) == {}


def test_runner_only_tokens_and_plain_variables_stay_unmasked():
    masked = set(claim_secret_values(_FULL).values())
    for value in (
        "ajt.a.b",
        "ghs_clone-token-runner-only",
        "not-a-secret-value",
        "eu-west-1-plain",
    ):
        assert value not in masked


def test_a_programs_own_key_is_masked_under_its_server():
    """#1104 decrypts a program's sealed keys into the claim; output must not show them."""
    values = claim_secret_values(_WITH_PROGRAM_KEY)
    assert values["sandbox-mcp:search:API_KEY"] == "program-sealed-api-key-value"
    assert list(values)[: len(claim_secret_values(_FULL))] == list(claim_secret_values(_FULL))
    redacted = SecretRedactor(values).text("key=program-sealed-api-key-value")
    assert redacted == "key=[secret:sandbox-mcp:search:API_KEY]"
    # A plain env value and a secret:// reference are configuration, not secrets.
    assert "eu" not in values.values() and "secret://API_KEY" not in values.values()


def test_a_programs_short_key_does_not_blank_the_output():
    """A one-character key is not masked: it would replace every such character."""
    short = SandboxMcpServer(
        alias="pin",
        command="npx",
        args=("pkg@1.2.3",),
        env={"API_PIN": "secret://API_PIN"},
        secret_env={"API_PIN": "1"},
    )
    values = claim_secret_values(replace(_BARE, sandbox_mcp_servers=(short,)))
    assert values == {}
    output = "Ran 12 tests in 1.0s"
    assert SecretRedactor(values).text(output) == output
