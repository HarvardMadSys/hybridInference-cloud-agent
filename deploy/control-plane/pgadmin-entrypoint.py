"""Wait for the dashboard's first-time setup, then start native pgAdmin.

pgAdmin imports its server definitions once, at that first start. The bundled
definition names the deployment's database and user as ``${AGENT_POSTGRES_DB}``
and ``${AGENT_POSTGRES_USER}``, filled in here from the values Compose gives the
``postgres`` service, so it opens the database the deployment uses. A file
without those placeholders is imported as it is.
"""

import json
import os
import tempfile
import time
from pathlib import Path

root = Path("/run/cloud-agent/pgadmin-bootstrap")
database = Path("/var/lib/pgadmin/pgadmin4.db")
#: Each placeholder's variable, and Compose's default when the deployment sets none.
PLACEHOLDERS = {"AGENT_POSTGRES_DB": "cloudagent", "AGENT_POSTGRES_USER": "cloudagent"}


def _fill(value, values):
    if isinstance(value, str):
        for placeholder, replacement in values.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_fill(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, values) for key, item in value.items()}
    return value


def render_servers(text, environ):
    """The server definitions with every placeholder filled in, or None if they have none."""
    values = {
        "${" + name + "}": (environ.get(name) or "").strip() or default
        for name, default in PLACEHOLDERS.items()
    }
    if not any(placeholder in text for placeholder in values):
        return None
    return json.dumps(_fill(json.loads(text), values), indent=2)


def main():
    if not database.is_file():
        print("Waiting for first-time setup in Admin -> Database.", flush=True)
        while not (root / "email").is_file():
            time.sleep(1)
        os.environ["PGADMIN_DEFAULT_EMAIL"] = (root / "email").read_text()
        os.environ["PGADMIN_DEFAULT_PASSWORD_FILE"] = str(root / "password")
        # The file pgAdmin's own entrypoint imports, under its own default.
        servers = Path(os.environ.get("PGADMIN_SERVER_JSON_FILE") or "/pgadmin4/servers.json")
        rendered = render_servers(servers.read_text(), os.environ) if servers.is_file() else None
        if rendered is not None:
            # No password is in it; readable, as the mounted original is.
            descriptor, path = tempfile.mkstemp(prefix="servers-", suffix=".json")
            with os.fdopen(descriptor, "w") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(rendered)
            os.environ["PGADMIN_SERVER_JSON_FILE"] = path
    else:
        # Native pgAdmin uses its existing database. Do not seed or reset accounts.
        os.environ.pop("PGADMIN_DEFAULT_PASSWORD_FILE", None)
    os.execv("/entrypoint.sh", ["/entrypoint.sh"])


if __name__ == "__main__":
    main()
