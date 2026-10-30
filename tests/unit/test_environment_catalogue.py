"""Catalogue round trips stay bounded with cold, warm and overflowing caches."""

from contextlib import asynccontextmanager

import pytest

from cloud_agent import settings
from cloud_agent.environment_fingerprints import setup_fingerprint
from cloud_agent.storage.repositories import environments


class Connection:
    def __init__(self, count):
        self.calls = []
        self.rows = [
            {
                "id": f"e{i}",
                "revision": 1,
                "repo": "o/r",
                "name": f"env{i}",
                "team_id": None,
                "archived_at": None,
                "updated_at": "now",
                "is_default": False,
                "default_revision": 0,
            }
            for i in range(count)
        ]

    async def fetch(self, query, *args):
        self.calls.append(query)
        if "FROM accessible e LEFT JOIN" in query:
            return self.rows
        if "WHERE (environment_id, revision) IN" in query:
            return [
                {
                    "id": "v" + row["id"],
                    "environment_id": row["id"],
                    "revision": 1,
                    "document": {"setup_script": "install"},
                }
                for row in self.rows
            ]
        if "DISTINCT ON (v.environment_id)" in query:
            return []
        if "AS version_id" in query:
            return [
                {
                    "id": row["id"],
                    "team_id": row["team_id"],
                    "version_id": "v" + row["id"],
                    "document": {"setup_script": "install"},
                }
                for row in self.rows
            ]
        if "FROM agent_environment_version_variables" in query:
            return [
                {
                    "version_id": version,
                    "name": version,
                    "value_ciphertext": version,
                    "secret": False,
                    "phase": "both",
                    "updated_at": "now",
                }
                for version in args[0]
            ]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.calls.append(query)
        return "global"

    @asynccontextmanager
    async def acquire(self):
        yield self


@pytest.mark.parametrize("count", [1, 100, 513])
async def test_catalogue_and_fingerprints_have_constant_query_counts(monkeypatch, count):
    monkeypatch.setattr(
        environments,
        "_cipher",
        lambda: type("Cipher", (), {"decrypt": staticmethod(lambda value: value)})(),
    )
    monkeypatch.setattr(
        environments,
        "optional_setup_fingerprint",
        lambda script, values: ",".join(v.value for v in values),
    )
    connection = Connection(count)
    repository = environments.PostgresEnvironmentRepository(connection)
    catalogue = await repository.list_environments("u")
    assert len(catalogue) == count
    assert len(connection.calls) == 4
    connection.calls.clear()
    ids = [row["id"] for row in catalogue]
    cold = await repository.current_setup_fingerprints("u", ids)
    assert len(connection.calls) == 3
    assert all(value == f"global,v{key}" for key, value in cold.items())
    connection.calls.clear()
    assert await repository.current_setup_fingerprints("u", ids) == cold
    assert len(connection.calls) == (3 if count > 512 else 2)


async def test_globals_are_decoded_once_and_never_join_team_setup(monkeypatch):
    connection = Connection(3)
    connection.rows[-1]["team_id"] = "team"
    repository = environments.PostgresEnvironmentRepository(connection)
    decoded = []
    original_decode = repository._environment_decode

    def decode(variables):
        decoded.extend(v["version_id"] for v in variables)
        return original_decode(variables)

    monkeypatch.setattr(repository, "_environment_decode", decode)
    monkeypatch.setattr(
        environments,
        "_cipher",
        lambda: type("Cipher", (), {"decrypt": staticmethod(lambda value: value)})(),
    )
    monkeypatch.setattr(
        environments,
        "optional_setup_fingerprint",
        lambda script, values: ",".join(v.value for v in values),
    )
    result = await repository.current_setup_fingerprints(
        "u", [row["id"] for row in connection.rows]
    )
    assert decoded.count("global") == 1
    assert result == {"e0": "global,ve0", "e1": "global,ve1", "e2": "ve2"}


async def test_unreadable_globals_fail_closed_without_poisoning_team_fingerprints(monkeypatch):
    connection = Connection(2)
    connection.rows[-1]["team_id"] = "team"
    repository = environments.PostgresEnvironmentRepository(connection)

    def decrypt(value):
        if value == "global":
            raise environments.EnvironmentEncryptionError("unreadable global ciphertext")
        return value

    monkeypatch.setattr(
        environments,
        "_cipher",
        lambda: type("Cipher", (), {"decrypt": staticmethod(decrypt)})(),
    )
    monkeypatch.setattr(
        environments,
        "optional_setup_fingerprint",
        lambda script, values: ",".join(v.value for v in values),
    )
    expected = {"e0": None, "e1": "ve1"}
    assert await repository.current_setup_fingerprints("u", ["e0", "e1"]) == expected
    assert await repository.current_setup_fingerprints("u", ["e0", "e1"]) == expected


async def test_missing_key_fingerprint_recovers_after_live_settings_setup(monkeypatch):
    settings.reset_settings()
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY", "test-master-key-longer-than-thirty-two-bytes"
    )
    repository = environments.PostgresEnvironmentRepository(Connection(1))
    monkeypatch.setattr(repository, "_environment_decode", lambda values: [])
    try:
        assert await repository.current_setup_fingerprints("u", ["e0"]) == {"e0": None}
        settings.apply_settings(
            1,
            settings.encode_changes(
                {
                    "source_control_encryption_key": (
                        "test-user-credential-key-longer-than-thirty-two-bytes"
                    )
                }
            ),
        )
        recovered = await repository.current_setup_fingerprints("u", ["e0"])
        assert recovered["e0"] == setup_fingerprint("install", [])
    finally:
        settings.reset_settings()
