"""pgAdmin remains opt-in, private, persistent, and part of release recipes."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import yaml
from deploy.release.build_recipe import build

ROOT = Path(__file__).resolve().parents[2]


def test_pgadmin_has_no_direct_public_port_or_database_password():
    compose = yaml.safe_load((ROOT / "deploy/control-plane/docker-compose.yml").read_text())
    pgadmin = compose["services"]["pgadmin"]
    assert pgadmin["profiles"] == ["pgadmin"]
    assert "ports" not in pgadmin
    assert pgadmin["networks"] == ["control-plane"]
    assert "agent-pgadmin:/var/lib/pgadmin" in pgadmin["volumes"]
    assert "PGADMIN_DEFAULT_PASSWORD" not in pgadmin["environment"]
    assert (
        pgadmin["environment"]["PGADMIN_DEFAULT_PASSWORD_FILE"] == "/run/secrets/pgadmin-password"
    )
    assert pgadmin["secrets"] == ["pgadmin-password"]
    assert pgadmin["environment"]["PGADMIN_CONFIG_SERVER_MODE"] == "True"
    assert pgadmin["environment"]["PGADMIN_CONFIG_SESSION_COOKIE_SECURE"].endswith(":-True}")
    assert "@sha256:" in pgadmin["image"]


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
