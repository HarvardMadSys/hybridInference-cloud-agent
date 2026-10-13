"""Saved logging changes update service-owned output without taking over embeddings."""

from __future__ import annotations

import io
import json
import logging
from contextlib import contextmanager

import pytest

from cloud_agent import logging_utils, settings
from cloud_agent_shared.logging_utils import UVICORN_LOGGERS


@contextmanager
def _isolated_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    owned = logging_utils._process_handler
    owns_uvicorn = logging_utils._process_owns_uvicorn
    server_loggers = [logging.getLogger(name) for name in UVICORN_LOGGERS]
    server_states = [(logger.handlers[:], logger.propagate) for logger in server_loggers]
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    logging_utils._process_handler = None
    logging_utils._process_owns_uvicorn = False
    settings.reset_settings()
    try:
        yield root
    finally:
        settings.reset_settings()
        root.handlers[:] = handlers
        root.setLevel(level)
        logging_utils._process_handler = owned
        logging_utils._process_owns_uvicorn = owns_uvicorn
        for logger, (saved_handlers, propagate) in zip(server_loggers, server_states, strict=True):
            logger.handlers[:] = saved_handlers
            logger.propagate = propagate


@pytest.fixture
def pristine_logging():
    return _isolated_logging


def test_saved_changes_update_output_level_and_format_without_adding_handlers(pristine_logging):
    with pristine_logging() as root:
        logging_utils.configure_process_logging(level="INFO", log_format="text")
        handler = root.handlers[0]
        stream = io.StringIO()
        handler.setStream(stream)
        logger = logging.getLogger("cloud_agent.live_logging_settings_test")
        settings.apply_settings(1, {"log_level": "DEBUG", "log_format": "json"})
        logger.debug("started", extra={"event": "started"})
        assert root.handlers == [handler]
        assert root.level == logging.DEBUG
        assert json.loads(stream.getvalue())["event"] == "started"

        stream.seek(0)
        stream.truncate()
        settings.apply_settings(2, {"log_level": "ERROR", "log_format": "text"})
        logger.info("hidden")
        logger.error("failed")
        assert root.handlers == [handler]
        assert root.level == logging.ERROR
        assert "failed" in stream.getvalue()
        assert "hidden" not in stream.getvalue()
        assert not isinstance(handler.formatter, logging_utils.JsonLogFormatter)
        settings.apply_settings(1, {"log_level": "DEBUG", "log_format": "json"})
        assert root.level == logging.ERROR


def test_existing_embedding_handlers_retain_their_formatter_and_threshold(pristine_logging):
    with pristine_logging() as root:
        sentinel = logging.NullHandler()
        formatter = logging.Formatter("embedding: %(message)s")
        sentinel.setFormatter(formatter)
        root.addHandler(sentinel)
        root.setLevel(logging.ERROR)
        access = logging.getLogger("uvicorn.access")
        access_handler = logging.NullHandler()
        access.handlers[:] = [access_handler]
        access.propagate = False
        logging_utils.configure_process_logging(level="INFO", log_format="text")
        settings.apply_settings(1, {"log_level": "DEBUG", "log_format": "json"})
        assert root.handlers == [sentinel]
        assert sentinel.formatter is formatter
        assert root.level == logging.ERROR
        assert access.handlers == [access_handler]
        assert access.propagate is False


def test_embedding_handlers_added_later_are_also_left_alone(pristine_logging):
    with pristine_logging() as root:
        logging_utils.configure_process_logging(level="INFO", log_format="text")
        owned = root.handlers[0]
        formatter = owned.formatter
        capture = logging.NullHandler()
        root.addHandler(capture)
        root.setLevel(logging.WARNING)
        settings.apply_settings(1, {"log_level": "DEBUG", "log_format": "json"})
        assert root.handlers == [owned, capture]
        assert owned.formatter is formatter
        assert root.level == logging.WARNING


def test_repeated_snapshots_leave_formatter_and_new_server_handlers_alone(pristine_logging):
    with pristine_logging() as root:
        logging_utils.configure_process_logging(level="INFO", log_format="json")
        settings.apply_settings(1, {"log_level": "INFO", "log_format": "json"})
        formatter = root.handlers[0].formatter
        server = logging.getLogger("uvicorn.access")
        additional = logging.NullHandler()
        server.addHandler(additional)
        settings.apply_settings(1, {"log_level": "INFO", "log_format": "json"})
        assert root.handlers[0].formatter is formatter
        settings.apply_settings(2, {"log_level": "DEBUG", "log_format": "json"})
        assert server.handlers == [additional]


@pytest.mark.parametrize(
    "level,format_name,expected_level,expected_format",
    [("debug", "JSON", "DEBUG", "json"), ("warn", "jsom", "WARNING", "text")],
)
def test_initial_environment_import_preserves_legacy_logging_normalization(
    monkeypatch, level, format_name, expected_level, expected_format
):
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_CONFIG", raising=False)
    monkeypatch.setenv("AGENT_LOG_LEVEL", level)
    monkeypatch.setenv("AGENT_LOG_FORMAT", format_name)
    imported = settings.initial_settings()
    assert imported["log_level"] == expected_level
    assert imported["log_format"] == expected_format


@pytest.mark.parametrize("changes", [{"log_level": "verbose"}, {"log_format": "jsom"}])
def test_invalid_admin_logging_choices_are_rejected(changes):
    with pytest.raises(ValueError):
        settings.encode_changes(changes)
