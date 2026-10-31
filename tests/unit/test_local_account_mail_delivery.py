"""Account mail transports: Tencent SES templates, their size bound, and audiences.

Review finding N8: SES sends only approved templates, and a message without
its own template goes through the ``notice`` template with its whole text as
one variable, which Tencent bounds at 800 bytes. A long administrator message
used to be accepted and then fail for every recipient; it is now refused when
it is previewed and created, with the reason. N7: a deleted account is no
audience. Every request here goes to an in-process mock; no mail is sent.
"""

from __future__ import annotations

import json

import httpx
import pydantic
import pytest

from cloud_agent.accounts import delivery, ses_tencent
from cloud_agent.accounts.broadcast import broadcast_mail, render_broadcast
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.schemas.accounts import BroadcastPreviewRequest


@pytest.fixture
def ses(monkeypatch):
    for name, value in {
        "AGENT_LOCAL_EMAIL_PROVIDER": "tencent_ses",
        "AGENT_TENCENT_CLOUD_SECRET_ID": "synthetic-id",
        "AGENT_TENCENT_CLOUD_SECRET_KEY": "synthetic-key",
        "AGENT_TENCENT_SES_FROM_EMAIL": "noreply@example.edu",
        "AGENT_TENCENT_SES_TEMPLATE_VERIFICATION": "11",
        "AGENT_TENCENT_SES_TEMPLATE_PASSWORD_RESET": "12",
        "AGENT_TENCENT_SES_TEMPLATE_NOTICE": "17",
    }.items():
        monkeypatch.setenv(name, value)


def _broadcast(body: str) -> AccountMail:
    rendered = render_broadcast(
        template_key=None, template_vars={}, subject="Update", body_markdown=body
    )
    return broadcast_mail(rendered, to="reader@example.edu")


def test_a_short_broadcast_fits_the_notice_template(ses):
    assert delivery.delivery_refusal(_broadcast("Back soon.")) is None


def test_a_long_broadcast_is_refused_with_the_size_and_the_remedy(ses):
    refusal = delivery.delivery_refusal(_broadcast("A long update. " * 80))
    assert refusal is not None
    assert "notice template" in refusal and "800 bytes" in refusal
    assert "SMTP" in refusal


def test_without_a_notice_template_ses_refuses_what_it_has_no_template_for(ses, monkeypatch):
    monkeypatch.setenv("AGENT_TENCENT_SES_TEMPLATE_NOTICE", "0")
    refusal = delivery.delivery_refusal(_broadcast("Back soon."))
    assert refusal is not None and "accounts.tencent_ses_template_notice" in refusal
    # A kind with its own template is unaffected.
    verify = AccountMail(kind="verify_email", to="a@example.edu", link="https://x/#token=t")
    assert delivery.delivery_refusal(verify) is None


def test_smtp_takes_any_message(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_EMAIL_PROVIDER", "smtp")
    assert delivery.delivery_refusal(_broadcast("A long update. " * 80)) is None


def test_ses_sends_a_fitting_broadcast_through_the_notice_template(ses):
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Response": {"MessageId": "m-1", "RequestId": "r"}})

    mailer = ses_tencent.TencentSesMailer(transport=httpx.MockTransport(answer))
    assert mailer.configured
    assert mailer.send(_broadcast("Agent is **back**."))
    payload = json.loads(seen[0].content)
    assert payload["Template"]["TemplateID"] == 17
    data = json.loads(payload["Template"]["TemplateData"])
    assert "Agent is **back**." in data["body"]
    assert payload["Destination"] == ["reader@example.edu"]


def test_ses_never_calls_out_for_a_message_over_the_bound(ses):
    mailer = ses_tencent.TencentSesMailer(
        transport=httpx.MockTransport(lambda request: pytest.fail("sent"))
    )
    assert not mailer.send(_broadcast("A long update. " * 80))


def test_a_deleted_account_is_no_audience():
    with pytest.raises(pydantic.ValidationError):
        BroadcastPreviewRequest(
            subject="s",
            body_markdown="b",
            target_statuses=["deleted"],
            target_admissions=["approved"],
        )
    accepted = BroadcastPreviewRequest(
        subject="s",
        body_markdown="b",
        target_statuses=["active", "suspended"],
        target_admissions=["approved"],
    )
    assert accepted.target_statuses == ["active", "suspended"]
