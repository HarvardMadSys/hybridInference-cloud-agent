"""Pins the logging shim's contract — including the gap the entrypoint closes.

The source repo's ``get_logger`` initialised root logging as a side effect;
the host shim deliberately does not. These tests make that difference a stated
contract instead of an accident: if someone adds side-effect configuration to
the shim, or the runner entrypoint stops configuring logging, one of these
assertions (or the entrypoint's own required test, below) is the tripwire.
"""

from __future__ import annotations

import logging

from cloud_agent_host.logging_utils import configure_process_logging, get_logger


def test_returns_the_named_stdlib_logger() -> None:
    logger = get_logger("cloud_agent_host.sandbox")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "cloud_agent_host.sandbox"
    assert logger is logging.getLogger("cloud_agent_host.sandbox")


def test_configures_nothing_as_a_side_effect() -> None:
    """The shim must not touch global logging state.

    Handler/level configuration belongs to the process entrypoint (the
    runner's). If this test starts failing because the shim grew a root
    handler, that configuration moved to import time — which breaks any
    embedding process that configures logging itself.
    """
    root_handlers_before = list(logging.getLogger().handlers)
    root_level_before = logging.getLogger().level

    get_logger("cloud_agent_host.probe")

    assert list(logging.getLogger().handlers) == root_handlers_before
    assert logging.getLogger().level == root_level_before


def test_get_logger_alone_configures_no_output_path() -> None:
    """The shim adds nothing — output only exists once an entrypoint asks.

    This held before the entrypoint configured logging, as the record of the
    gap; it still holds because configuration lives in
    ``configure_process_logging``, called from ``main()``, never at import.
    """
    logger = get_logger("cloud_agent_host.sandbox")
    # No handlers of its own, and root's default threshold hides INFO.
    assert logger.handlers == []
    assert not logger.isEnabledFor(logging.INFO) or logging.getLogger().handlers


def _pristine_root():
    """Context: run with a factory-fresh root logger, restoring it after."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        try:
            yield root
        finally:
            root.handlers.clear()
            root.handlers.extend(saved_handlers)
            root.setLevel(saved_level)

    return ctx()


def test_configure_process_logging_makes_info_reach_a_handler(monkeypatch) -> None:
    """The deferred register's required positive test (the runner-logging row).

    In a process shaped like the runner (no logging configured yet), one
    ``configure_process_logging()`` call must give a host module's INFO event
    a real handler — the property the source repo used to provide through
    import side effects.
    """
    monkeypatch.delenv("AGENT_LOG_LEVEL", raising=False)
    with _pristine_root() as root:
        configure_process_logging()

        assert root.handlers, "entrypoint configuration produced no handler"
        records: list[logging.LogRecord] = []
        capture = logging.Handler()
        capture.emit = records.append  # type: ignore[method-assign]
        root.addHandler(capture)

        get_logger("cloud_agent_host.sandbox").info(
            "agent_sandbox_spawn", extra={"event": "agent_sandbox_spawn"}
        )

        assert any(r.getMessage() == "agent_sandbox_spawn" for r in records)


def test_configure_process_logging_defers_to_existing_configuration() -> None:
    """An embedding process that configured logging first keeps its setup."""
    with _pristine_root() as root:
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        root.setLevel(logging.ERROR)

        configure_process_logging()

        assert root.handlers == [sentinel]
        assert root.level == logging.ERROR


def test_configure_process_logging_honours_the_env_level(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOG_LEVEL", "debug")
    with _pristine_root() as root:
        configure_process_logging()
        assert root.level == logging.DEBUG
