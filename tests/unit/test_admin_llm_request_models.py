"""Attribution views and continuations for the admin request-detail panel.

These are the decisions that must hold before any HTTP call exists: which
stored references can be queried, what a source says when it cannot be, and
whether a continuation still describes the query it was minted for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from cloud_agent import identity
from cloud_agent.admin_observability import (
    MAX_ADMIN_RESPONSE_BYTES,
    MAX_REQUEST_CURSOR_BYTES,
    REQUEST_CURSOR_TTL,
    SOURCE_REASON_ATTRIBUTION_UNAVAILABLE,
    SOURCE_REASON_BINDING_CHANGED,
    SOURCE_REASON_INVALID_WINDOW,
    AdminLlmRequest,
    AdminLlmRequestPage,
    AdminLlmRequestWindow,
    LlmRequestCursor,
    RequestCursorError,
    RequestCursorTooLong,
    decode_llm_request_cursor,
    encode_llm_request_cursor,
    expected_gateway_job_id,
    llm_request_source_view,
    request_source_fingerprint,
    response_body_bytes,
)
from cloud_agent.grant_usage import MAX_REQUEST_PAGE_BYTES

START = datetime(2026, 9, 11, 3, 20, tzinfo=UTC)
END = START + timedelta(minutes=1)
BINDING = "gw_binding_one"
JOB_KEY = "thread:athr_one"
SECRET = "admin-llm-request-cursor-test-secret-value"


@pytest.fixture(autouse=True)
def configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign with a real derived key; an unset secret is a refusal, not a default."""
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, SECRET)


def source_row(**overrides: object) -> dict[str, object]:
    """One stored reference as the admin projection returns it."""
    row: dict[str, object] = {
        "attempt_id": 10,
        "attempt_no": 1,
        "attempt_status": "finished",
        "started_at": START,
        "finished_at": END,
        "thread_id": "athr_one",
        "ledger_id": 42,
        "grant_id": "agr_one",
        "binding_id": BINDING,
        "residency_generation": 3,
        "window_since": START,
        "window_until": END,
        "window_open": False,
    }
    row.update(overrides)
    return row


def test_the_gateway_key_is_derived_only_from_the_recorded_shape() -> None:
    assert expected_gateway_job_id(thread_id="athr_one") == JOB_KEY
    assert expected_gateway_job_id(thread_id=None) is None
    assert expected_gateway_job_id(thread_id="") is None


def test_a_source_without_a_reference_is_listed_rather_than_omitted() -> None:
    view = llm_request_source_view(
        source_row(
            ledger_id=None, grant_id=None, binding_id=None, window_since=None, window_until=None
        ),
        current_binding_id=BINDING,
        expected_external_job_id=JOB_KEY,
    )
    assert view.source_id is None
    assert view.attempt_id == 10
    assert view.window_state == "unknown"
    assert view.availability == "unavailable"
    assert view.reason == SOURCE_REASON_ATTRIBUTION_UNAVAILABLE


def test_history_from_another_installation_is_refused_not_requeried() -> None:
    view = llm_request_source_view(
        source_row(binding_id="gw_other"),
        current_binding_id=BINDING,
        expected_external_job_id=JOB_KEY,
    )
    assert view.source_id == "42"
    assert view.availability == "unavailable"
    assert view.reason == SOURCE_REASON_BINDING_CHANGED
    assert view.window_since == START


def test_an_unattributable_reference_is_not_queried_on_a_guess() -> None:
    view = llm_request_source_view(
        source_row(), current_binding_id=BINDING, expected_external_job_id=None
    )
    assert view.availability == "unavailable"
    assert view.reason == SOURCE_REASON_ATTRIBUTION_UNAVAILABLE


@pytest.mark.parametrize(
    ("since", "until"),
    [(START, START), (None, END), (START, None), (END, START)],
)
def test_an_empty_or_missing_window_is_pending_rather_than_sent(
    since: datetime | None, until: datetime | None
) -> None:
    view = llm_request_source_view(
        source_row(window_since=since, window_until=until),
        current_binding_id=BINDING,
        expected_external_job_id=JOB_KEY,
    )
    assert view.availability == "pending"
    assert view.reason == SOURCE_REASON_INVALID_WINDOW


def test_an_open_window_is_still_queryable_and_says_so() -> None:
    view = llm_request_source_view(
        source_row(finished_at=None, window_until=END, window_open=True, attempt_status="running"),
        current_binding_id=BINDING,
        expected_external_job_id=JOB_KEY,
    )
    assert view.availability == "queryable"
    assert view.window_state == "open"
    assert view.reason is None

    closed = llm_request_source_view(
        source_row(), current_binding_id=BINDING, expected_external_job_id=JOB_KEY
    )
    assert closed.window_state == "closed"


def test_the_fingerprint_covers_the_reference_but_not_the_running_clock() -> None:
    original = request_source_fingerprint(source_row())
    # A window end that only moved because time passed must not invalidate a
    # continuation: a running turn's second page would fail every time.
    assert (
        request_source_fingerprint(source_row(window_until=END + timedelta(minutes=5))) == original
    )
    assert request_source_fingerprint(source_row(window_open=True)) == original
    for change in (
        {"ledger_id": 43},
        {"attempt_id": 11},
        {"binding_id": "gw_other"},
        {"grant_id": "agr_other"},
        {"residency_generation": 4},
        {"window_since": START + timedelta(seconds=1)},
    ):
        assert request_source_fingerprint(source_row(**change)) != original, change


def cursor(**overrides: object) -> LlmRequestCursor:
    """One continuation envelope as a first page would mint it."""
    values: dict[str, object] = {
        "admin": "admin:admin-person",
        "job_id": "ajob_one",
        "source_id": "42",
        "fingerprint": request_source_fingerprint(source_row()),
        "since": START,
        "until": END,
        "limit": 50,
        "request_started_at": START + timedelta(seconds=4),
        "request_id": "req_last",
        "gateway_cursor": "opaque-gateway-cursor",
    }
    values.update(overrides)
    return LlmRequestCursor.model_validate(values)


def decode(token: str, **overrides: object) -> LlmRequestCursor:
    """Decode against the same query the fixture cursor describes."""
    values: dict[str, object] = {
        "admin": "admin:admin-person",
        "job_id": "ajob_one",
        "source_id": "42",
        "fingerprint": request_source_fingerprint(source_row()),
        "limit": 50,
    }
    values.update(overrides)
    return decode_llm_request_cursor(token, **values)  # type: ignore[arg-type]


def test_a_continuation_round_trips_with_its_gateway_cursor() -> None:
    decoded = decode(encode_llm_request_cursor(cursor()))
    assert decoded.request_id == "req_last"
    assert decoded.gateway_cursor == "opaque-gateway-cursor"
    assert decoded.until == END
    assert decoded.limit == 50


def test_a_continuation_is_not_a_credential_for_another_query() -> None:
    token = encode_llm_request_cursor(cursor())
    for change in (
        {"admin": "admin:someone-else"},
        {"job_id": "ajob_other"},
        {"source_id": "43"},
        {"limit": 10},
    ):
        with pytest.raises(RequestCursorError) as raised:
            decode(token, **change)
        assert raised.value.reason == "invalid", change


def test_a_replaced_reference_asks_for_a_restart_not_a_repair() -> None:
    token = encode_llm_request_cursor(cursor())
    with pytest.raises(RequestCursorError) as raised:
        decode(token, fingerprint=request_source_fingerprint(source_row(grant_id="agr_other")))
    assert raised.value.reason == "changed"


def test_an_expired_continuation_is_restarted_from_the_first_page() -> None:
    issued = datetime.now(UTC) - REQUEST_CURSOR_TTL - timedelta(seconds=1)
    token = encode_llm_request_cursor(cursor(), issued_at=issued)
    with pytest.raises(RequestCursorError) as raised:
        decode(token)
    assert raised.value.reason == "expired"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "a.b.c",
        "x" * (MAX_REQUEST_CURSOR_BYTES + 1),
        "😀" * (MAX_REQUEST_CURSOR_BYTES // 2),
    ],
)
def test_a_malformed_or_oversized_continuation_is_refused(token: str) -> None:
    with pytest.raises(RequestCursorError) as raised:
        decode(token)
    assert raised.value.reason == "invalid"


def test_a_tampered_continuation_is_refused() -> None:
    token = encode_llm_request_cursor(cursor())
    head, payload, signature = token.split(".")
    forged = ".".join([head, payload, ("A" if signature[0] != "A" else "B") + signature[1:]])
    with pytest.raises(RequestCursorError) as raised:
        decode(forged)
    assert raised.value.reason == "invalid"


def test_an_oversized_gateway_cursor_cannot_ride_inside_the_envelope() -> None:
    with pytest.raises(ValueError):
        cursor(gateway_cursor="c" * 2049)


def test_signing_without_a_configured_secret_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(identity.ENV_SESSION_SECRET, raising=False)
    with pytest.raises(identity.IdentityNotConfigured):
        encode_llm_request_cursor(cursor())
    with pytest.raises(identity.IdentityNotConfigured):
        decode_llm_request_cursor(
            "a.b.c", admin="a", job_id="b", source_id="42", fingerprint="f", limit=50
        )


def test_the_cursor_key_is_separated_from_sessions() -> None:
    """A cursor must never verify as a session, or the reverse."""
    token = encode_llm_request_cursor(cursor())
    with pytest.raises(identity.IdentityError):
        identity.verify_session(token)
    assert identity.admin_llm_request_cursor_secret() != SECRET


def test_a_cursor_this_service_would_refuse_is_never_signed() -> None:
    """Signing inflates; a page must not hand back a link it cannot resume."""
    with pytest.raises(RequestCursorTooLong):
        encode_llm_request_cursor(cursor(request_id="😀" * 256, gateway_cursor="c" * 2048))


def test_a_legal_cursor_stays_inside_the_input_ceiling() -> None:
    token = encode_llm_request_cursor(cursor(gateway_cursor="c" * 2048))
    assert len(token.encode("utf-8")) <= MAX_REQUEST_CURSOR_BYTES
    assert decode(token).gateway_cursor == "c" * 2048


def test_the_response_ceiling_matches_what_the_service_reads() -> None:
    """A page it could not forward is a page it must not claim to have answered."""
    assert MAX_ADMIN_RESPONSE_BYTES == MAX_REQUEST_PAGE_BYTES


def test_the_response_ceiling_counts_the_finished_body() -> None:
    page = AdminLlmRequestPage(
        job_id="ajob_one",
        source_id="42",
        attempt_id=10,
        attempt_no=1,
        window=AdminLlmRequestWindow(since=START, until=END),
        fetched_at=END,
        gateway_as_of=END,
        requests=[
            AdminLlmRequest(
                request_id="req_😀",
                request_started_at=START,
                logged_at=END,
                model='a"b\\c',
                spent_usd="0.00000001",
            )
        ],
        next_cursor="cursor",
    )
    serialized = json.dumps(
        page.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert response_body_bytes(page) == len(serialized)
    # The identifiers and the continuation are part of the body, so a ceiling
    # over the gateway's model alone would not bound what the browser receives.
    without_cursor = page.model_copy(update={"next_cursor": None})
    assert response_body_bytes(page) > response_body_bytes(without_cursor)
