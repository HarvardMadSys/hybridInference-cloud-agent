"""The JSON log format — and that both copies of it behave the same.

The two packages keep their own ``logging_utils`` by decision: separate
deployment units, and no import from the host into the web service. So the
formatter is duplicated, and every case here runs against both copies. That
parametrisation is what keeps "duplicated deliberately" from becoming
"diverged quietly".

The format exists because the text format renders ``%(message)s`` and nothing
else, so the ``extra={"event": ..., "job_id": ...}`` fields that call sites
across both packages already pass were discarded before reaching a log.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import logging

import pytest

from cloud_agent import logging_utils as control_plane
from cloud_agent_host import logging_utils as host


@pytest.fixture(params=[control_plane, host], ids=["control_plane", "host"])
def module(request):
    """Each test runs once per copy of the shim."""
    return request.param


@contextlib.contextmanager
def _isolated_logger(module, name: str):
    """A logger wired to a JSON handler over a buffer, detached from root."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(module.JsonLogFormatter())
    logger = logging.getLogger(f"test_log_json_formatter.{name}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, stream
    finally:
        logger.handlers = []


@contextlib.contextmanager
def _pristine_root():
    """Run with a factory-fresh root logger, restoring the real one after."""
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


def test_extra_fields_land_at_the_top_level(module) -> None:
    """The point of the format: `jq .duration_ms` has to reach the value."""
    with _isolated_logger(module, "extras") as (logger, stream):
        logger.info(
            "agent_workspace_files",
            extra={"event": "agent_workspace_files", "job_id": "job-7", "duration_ms": 41},
        )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "agent_workspace_files"
    assert payload["job_id"] == "job-7"
    assert payload["duration_ms"] == 41
    assert payload["message"] == "agent_workspace_files"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test_log_json_formatter.extras"
    assert payload["time"].endswith("+00:00")


def test_the_stdlib_envelope_is_not_reported_as_caller_data(module) -> None:
    """Only what a caller passed shows up beside the four envelope keys."""
    with _isolated_logger(module, "envelope") as (logger, stream):
        logger.warning("agent_probe", extra={"event": "agent_probe"})

    payload = json.loads(stream.getvalue())
    assert set(payload) == {"time", "level", "logger", "message", "event"}


def test_a_value_json_cannot_encode_is_rendered_rather_than_raised(module) -> None:
    """A formatter that raises loses the record and prints from inside logging."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    with _isolated_logger(module, "opaque") as (logger, stream):
        logger.info("agent_probe", extra={"event": "agent_probe", "detail": Opaque()})

    assert json.loads(stream.getvalue())["detail"] == "<opaque>"


def test_an_exception_is_carried_in_the_object(module) -> None:
    with _isolated_logger(module, "exc") as (logger, stream):
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("agent_probe_failed", extra={"event": "agent_probe_failed"})

    payload = json.loads(stream.getvalue())
    assert "ValueError: boom" in payload["exc"]


def test_text_is_still_the_default(module, monkeypatch) -> None:
    """An operator reading `docker logs` by eye sees what they saw before."""
    monkeypatch.delenv(module.ENV_LOG_FORMAT, raising=False)
    with _pristine_root() as root:
        module.configure_process_logging()
        assert root.handlers
        assert not isinstance(root.handlers[0].formatter, module.JsonLogFormatter)


def test_the_env_var_selects_json(module, monkeypatch) -> None:
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "JSON")
    with _pristine_root() as root:
        module.configure_process_logging()
        assert isinstance(root.handlers[0].formatter, module.JsonLogFormatter)


def test_an_explicit_argument_beats_the_env_var(module, monkeypatch) -> None:
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "json")
    with _pristine_root() as root:
        module.configure_process_logging(log_format="text")
        assert not isinstance(root.handlers[0].formatter, module.JsonLogFormatter)


def test_an_unrecognised_format_serves_text_instead_of_refusing(module, monkeypatch) -> None:
    """A misspelled log format must not be the reason a process will not start."""
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "jsom")
    with _pristine_root() as root:
        module.configure_process_logging()
        assert root.handlers
        assert not isinstance(root.handlers[0].formatter, module.JsonLogFormatter)


def test_json_still_defers_to_configuration_already_in_place(module, monkeypatch) -> None:
    """The no-op-when-configured contract is not a casualty of the new branch."""
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "json")
    with _pristine_root() as root:
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        root.setLevel(logging.ERROR)

        module.configure_process_logging()

        assert root.handlers == [sentinel]
        assert root.level == logging.ERROR


def test_the_two_copies_have_not_drifted() -> None:
    """Behaviour is checked above; this catches wording and shape.

    The duplication is deliberate, so the duplicate has to stay a duplicate.
    A parametrised case only proves the behaviours it happens to cover —
    comparing the source catches the change nobody thought to test, which is
    the one that will be made.
    """
    for name in ("JsonLogFormatter", "_adopt_uvicorn_loggers"):
        assert inspect.getsource(getattr(control_plane, name)) == inspect.getsource(
            getattr(host, name)
        ), f"{name} differs between the two shims"


def test_uvicorn_is_handed_to_the_json_handler(module, monkeypatch) -> None:
    """Otherwise the stream is half JSON and `jq` fails on uvicorn's first line."""
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "json")
    with _pristine_root() as root:
        access = logging.getLogger("uvicorn.access")
        saved_handlers, saved_propagate = list(access.handlers), access.propagate
        access.handlers = [logging.NullHandler()]
        access.propagate = False
        try:
            module.configure_process_logging()

            assert access.handlers == []
            assert access.propagate is True
            assert isinstance(root.handlers[0].formatter, module.JsonLogFormatter)
        finally:
            access.handlers = saved_handlers
            access.propagate = saved_propagate


def test_uvicorn_is_left_alone_when_this_call_configured_nothing(module, monkeypatch) -> None:
    """Reshaping loggers around a handler we did not install is not ours to do."""
    monkeypatch.setenv(module.ENV_LOG_FORMAT, "json")
    with _pristine_root() as root:
        root.addHandler(logging.NullHandler())
        access = logging.getLogger("uvicorn.access")
        sentinel = logging.NullHandler()
        saved_handlers, saved_propagate = list(access.handlers), access.propagate
        access.handlers = [sentinel]
        access.propagate = False
        try:
            module.configure_process_logging()
            assert access.handlers == [sentinel]
            assert access.propagate is False
        finally:
            access.handlers = saved_handlers
            access.propagate = saved_propagate


def test_text_leaves_uvicorn_as_uvicorn_configured_it(module, monkeypatch) -> None:
    """The default must not quietly reshape another library's logging."""
    monkeypatch.delenv(module.ENV_LOG_FORMAT, raising=False)
    with _pristine_root():
        access = logging.getLogger("uvicorn.access")
        sentinel = logging.NullHandler()
        saved_handlers, saved_propagate = list(access.handlers), access.propagate
        access.handlers = [sentinel]
        access.propagate = False
        try:
            module.configure_process_logging()
            assert access.handlers == [sentinel]
            assert access.propagate is False
        finally:
            access.handlers = saved_handlers
            access.propagate = saved_propagate
