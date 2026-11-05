"""The model protocol compatibility helper, as a member's sandbox runs it.

A translated plan starts the member's CLI behind the launcher in
``cloud_agent_host.protocol_translator``. The sandbox image has no copy of it;
the host builds a zipapp of exactly that module and the pure package it uses
(``cloud_agent_shared.protocol_compat``), from the very files it imports
itself, and installs it root-owned before every translated launch, so a
container retained from an older image runs exactly this host's code. The
archive is deterministic -- fixed member order, timestamps and modes -- so an
unchanged helper is recognized by its digest and not copied again.
"""

from __future__ import annotations

import functools
import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import cloud_agent_host
import cloud_agent_host.protocol_translator as protocol_translator
import cloud_agent_shared
import cloud_agent_shared.protocol_compat as protocol_compat
from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.runtimes.base import COMPAT_LAUNCHER
from cloud_agent_host.trusted_files import install_trusted_file
from cloud_agent_shared.protocol_compat import ENV_TOKEN

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

#: What a translated member's argv starts with: the image's own interpreter,
#: isolated from the environment and the working directory, running the helper.
LAUNCHER = COMPAT_LAUNCHER
HELPER_PATH = COMPAT_LAUNCHER[-1]
HELPER_DIRECTORY, HELPER_NAME = HELPER_PATH.rsplit("/", 1)

_MAIN = (
    b"import sys\n"
    b"from cloud_agent_host.protocol_translator import main\n"
    b"sys.exit(main(sys.argv[1:]))\n"
)
_EPOCH = (1980, 1, 1, 0, 0, 0)


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o444 << 16
    return info


def _sources() -> list[tuple[str, Path]]:
    """Each archived file and its name in the archive, in archive order.

    The two packages' ``__init__`` files come along so their imports resolve;
    neither imports anything. The pure package comes whole, with its license
    and notice; of the host package, only the translator.
    """
    shared = Path(protocol_compat.__file__).resolve().parent
    package = sorted(
        path
        for path in shared.iterdir()
        if path.is_file() and (path.suffix in (".py", ".md") or path.name.startswith("LICENSE"))
    )
    return [
        ("cloud_agent_host/__init__.py", Path(cloud_agent_host.__file__)),
        ("cloud_agent_host/protocol_translator.py", Path(protocol_translator.__file__)),
        ("cloud_agent_shared/__init__.py", Path(cloud_agent_shared.__file__)),
        *((f"cloud_agent_shared/protocol_compat/{path.name}", path) for path in package),
    ]


@functools.cache
def helper_archive() -> bytes:
    """The zipapp: the translator, the pure package, its license and notice, and a ``__main__``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_entry("__main__.py"), _MAIN)
        for name, path in _sources():
            archive.writestr(_entry(name), path.read_bytes())
    return buffer.getvalue()


def compat_token_env(token: str) -> dict[str, str]:
    """A translated launch's token, for its launcher alone; nothing for a direct one.

    The launcher removes it before it becomes the CLI, and writes it only
    where the CLI reads its credential.
    """
    return {ENV_TOKEN: token} if token else {}


def install_compat_helper(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
    """Make the container's helper this host's, before a translated member starts."""
    with install_lock(spec.container_name):
        install_trusted_file(
            manager,
            spec,
            directory=HELPER_DIRECTORY,
            name=HELPER_NAME,
            content=helper_archive(),
            mode=0o444,
            what="model protocol compatibility helper",
        )
