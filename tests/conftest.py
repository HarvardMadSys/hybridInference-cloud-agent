"""Every test starts from CI's environment, whatever shell or earlier test ran first.

CI runs the tests with a umask of 022 and no sandbox CLI version in the
environment. A developer's shell or an enclosing agent can differ in both: a
restrictive umask changes the modes of the files tests create and check, and
an inherited ``CODEX_VERSION`` reads to the deploy scripts as an operator's
version pin. A test can differ too: one that runs a CLI's ``main()`` in-process
takes on whatever umask that CLI sets for itself, and keeps it for every later
test in the same worker.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


def pytest_configure() -> None:
    """Give this process CI's values before collection.

    Code a test module runs at import then sees the same values as its tests.
    """
    os.umask(0o022)
    os.environ.pop("CODEX_VERSION", None)


@pytest.fixture(autouse=True)
def _ci_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test CI's values, and leave them for whatever runs next."""
    os.umask(0o022)
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    yield
    # Restored rather than left as the test left it, so a test that changed it
    # cannot decide the modes the next module's fixtures create files with.
    os.umask(0o022)
