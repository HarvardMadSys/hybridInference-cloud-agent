"""pgAdmin remains opt-in, private, persistent, and part of release recipes."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from deploy.release.artifact_compose import is_pinned
from deploy.release.build_recipe import build

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy/control-plane/pgadmin-entrypoint.py"
SERVERS = ROOT / "deploy/control-plane/pgadmin-servers.json"


def _entrypoint() -> ModuleType:
    """The wrapper as a module; running it as a script is what execs pgAdmin."""
    spec = importlib.util.spec_from_file_location("pgadmin_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pgadmin_has_no_direct_public_port_or_database_password():
    compose = yaml.safe_load((ROOT / "deploy/control-plane/docker-compose.yml").read_text())
    pgadmin = compose["services"]["pgadmin"]
    assert pgadmin["profiles"] == ["pgadmin"]
    assert "ports" not in pgadmin
    assert pgadmin["networks"] == ["control-plane"]
    assert "agent-pgadmin:/var/lib/pgadmin" in pgadmin["volumes"]
    assert "PGADMIN_DEFAULT_PASSWORD" not in pgadmin["environment"]
    assert "PGADMIN_DEFAULT_EMAIL" not in pgadmin["environment"]
    assert "PGADMIN_DEFAULT_PASSWORD_FILE" not in pgadmin["environment"]
    assert "secrets" not in pgadmin
    assert "agent-pgadmin-bootstrap:/run/cloud-agent/pgadmin-bootstrap:ro" in pgadmin["volumes"]
    assert pgadmin["entrypoint"] == ["/venv/bin/python3", "/pgadmin-entrypoint.py"]
    assert pgadmin["group_add"] == ["10001"]
    assert pgadmin["depends_on"]["control-plane"]["condition"] == "service_started"
    assert (
        "agent-pgadmin-bootstrap:/var/lib/cloud-agent/pgadmin-bootstrap"
        in compose["services"]["control-plane"]["volumes"]
    )
    assert pgadmin["environment"]["PGADMIN_CONFIG_SERVER_MODE"] == "True"
    assert pgadmin["environment"]["PGADMIN_CONFIG_SESSION_COOKIE_SECURE"].endswith(":-True}")
    # Its version tag and its multi-architecture index digest, like every
    # third-party image the stack runs (test_artifact_compose.py holds them all).
    assert is_pinned(pgadmin["image"]), pgadmin["image"]


def test_release_recipe_preserves_pgadmin_and_its_server_definitions():
    with tarfile.open(fileobj=io.BytesIO(build()), mode="r:gz") as archive:
        definitions = archive.extractfile("recipe/deploy/control-plane/pgadmin-servers.json")
        assert definitions is not None
        server = json.load(definitions)["Servers"]["1"]
        assert server["Host"] == "postgres" and server["Port"] == 5432
        assert "Password" not in server
        compose = archive.extractfile("recipe/deploy/control-plane/docker-compose.yml")
        assert compose is not None
        service = yaml.safe_load(compose)["services"]["pgadmin"]
        assert service["profiles"] == ["pgadmin"]
        assert "build" not in service and "ports" not in service
        assert is_pinned(service["image"]), "a release must not deploy whatever a tag means today"
        entrypoint = archive.extractfile("recipe/deploy/control-plane/pgadmin-entrypoint.py")
        assert entrypoint is not None
        assert b"/entrypoint.sh" in entrypoint.read()


def test_the_bundled_server_names_the_database_the_postgres_service_names():
    """Not a literal: a deployment on another database would browse the one it left."""
    compose = yaml.safe_load((ROOT / "deploy/control-plane/docker-compose.yml").read_text())
    postgres = compose["services"]["postgres"]["environment"]
    pgadmin = compose["services"]["pgadmin"]["environment"]
    assert pgadmin["AGENT_POSTGRES_DB"] == postgres["POSTGRES_DB"]
    assert pgadmin["AGENT_POSTGRES_USER"] == postgres["POSTGRES_USER"]
    server = json.loads(SERVERS.read_text())["Servers"]["1"]
    assert server["MaintenanceDB"] == "${AGENT_POSTGRES_DB}"
    assert server["Username"] == "${AGENT_POSTGRES_USER}"


@pytest.mark.parametrize(
    ("environment", "database", "user"),
    [
        (
            {"AGENT_POSTGRES_DB": "cloudagent_accounts", "AGENT_POSTGRES_USER": "agents"},
            "cloudagent_accounts",
            "agents",
        ),
        # A new installation: Compose's defaults, or nothing at all.
        (
            {"AGENT_POSTGRES_DB": "cloudagent", "AGENT_POSTGRES_USER": "cloudagent"},
            "cloudagent",
            "cloudagent",
        ),
        ({}, "cloudagent", "cloudagent"),
    ],
)
def test_the_entrypoint_fills_in_the_deployment_database(
    environment: dict[str, str], database: str, user: str
):
    rendered = _entrypoint().render_servers(SERVERS.read_text(), environment)

    server = json.loads(rendered)["Servers"]["1"]
    assert (server["MaintenanceDB"], server["Username"]) == (database, user)
    assert server["Host"] == "postgres" and server["Port"] == 5432
    assert "Password" not in server


def test_a_definition_without_placeholders_is_imported_as_it_is():
    custom = json.dumps({"Servers": {"1": {"MaintenanceDB": "postgres", "Username": "admin"}}})

    assert _entrypoint().render_servers(custom, {"AGENT_POSTGRES_DB": "other"}) is None


@pytest.mark.parametrize("first_start", [True, False])
def test_pgadmin_imports_the_filled_definition_at_its_first_start_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_start: bool
):
    entrypoint = _entrypoint()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "email").write_text("admin@example.org")
    (bootstrap / "password").write_text("placeholder-password")
    pgadmin_database = tmp_path / "pgadmin4.db"
    if not first_start:
        pgadmin_database.write_bytes(b"")
    mounted = tmp_path / "servers.json"
    mounted.write_text(SERVERS.read_text())
    monkeypatch.setattr(entrypoint, "root", bootstrap)
    monkeypatch.setattr(entrypoint, "database", pgadmin_database)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("PGADMIN_DEFAULT_EMAIL", "PGADMIN_DEFAULT_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGADMIN_SERVER_JSON_FILE", str(mounted))
    monkeypatch.setenv("AGENT_POSTGRES_DB", "cloudagent_accounts")
    monkeypatch.setenv("AGENT_POSTGRES_USER", "cloudagent")
    started = []
    monkeypatch.setattr(os, "execv", lambda path, argv: started.append(path))

    entrypoint.main()

    assert started == ["/entrypoint.sh"]
    assert mounted.read_text() == SERVERS.read_text()
    imported = Path(os.environ["PGADMIN_SERVER_JSON_FILE"])
    if first_start:
        assert imported != mounted
        server = json.loads(imported.read_text())["Servers"]["1"]
        assert (server["MaintenanceDB"], server["Username"]) == (
            "cloudagent_accounts",
            "cloudagent",
        )
    else:
        # pgAdmin keeps the servers it already has; nothing is imported again.
        assert imported == mounted
        assert "PGADMIN_DEFAULT_PASSWORD_FILE" not in os.environ


@pytest.mark.parametrize(
    ("reference", "pinned"),
    [
        ("postgres:16.16-alpine@sha256:" + "a" * 64, True),
        ("nginx:1.27.5-alpine@sha256:" + "b" * 64, True),
        ("dpage/pgadmin4:9.18@sha256:" + "c" * 64, True),
        ("registry.example:5000/postgres:16.16-alpine@sha256:" + "d" * 64, True),
        ("postgres:16-alpine@sha256:" + "a" * 64, False),
        ("postgres:latest@sha256:" + "a" * 64, False),
        ("postgres:16.16-alpine", False),
        ("postgres@sha256:" + "a" * 64, False),
    ],
)
def test_a_pin_names_a_dotted_version_and_an_index_digest(reference: str, pinned: bool) -> None:
    assert is_pinned(reference) is pinned
