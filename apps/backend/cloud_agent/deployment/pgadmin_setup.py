"""Commit first-start credentials privately, without replacing an existing setup."""

from __future__ import annotations

import fcntl
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def initialize_pgadmin(root: Path, email: str, password: str) -> None:
    """Publish the email last, after a complete, group-readable password file."""
    if root.is_symlink() or not root.is_dir():
        raise OSError("setup volume is unavailable")
    descriptor = os.open(root / ".setup.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root / "email").exists():
            raise FileExistsError("pgAdmin is already configured")
        for name, value in (("password", password), ("email", email)):
            fd, temporary = tempfile.mkstemp(prefix=".setup-", dir=root)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o640)
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, root / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
