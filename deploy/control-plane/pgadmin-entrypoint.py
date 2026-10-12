"""Wait for the dashboard's first-time setup, then start native pgAdmin."""

import os
import time
from pathlib import Path

root = Path("/run/cloud-agent/pgadmin-bootstrap")
database = Path("/var/lib/pgadmin/pgadmin4.db")
if not database.is_file():
    print("Waiting for first-time setup in Admin -> Database.", flush=True)
    while not (root / "email").is_file():
        time.sleep(1)
    os.environ["PGADMIN_DEFAULT_EMAIL"] = (root / "email").read_text()
    os.environ["PGADMIN_DEFAULT_PASSWORD_FILE"] = str(root / "password")
else:
    # Native pgAdmin uses its existing database. Do not seed or reset accounts.
    os.environ.pop("PGADMIN_DEFAULT_PASSWORD_FILE", None)
os.execv("/entrypoint.sh", ["/entrypoint.sh"])
