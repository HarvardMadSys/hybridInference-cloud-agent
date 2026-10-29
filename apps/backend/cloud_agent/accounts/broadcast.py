"""Administrator mail to accounts: templates, Markdown bodies and audiences.

Adapted from HybridInference ``routers/admin/broadcast.py``, ``utils/email.py``
(``render_broadcast_template``, ``render_markdown_email``) and
``utils/email_scheduler.py`` at ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7``
(MIT License, Copyright (c) 2026 Harvard SEAS). An administrator previews the
audience and the rendered message, sends it to themselves as a test, then
sends it to every matching account now or at a time they choose. The
recipients are fixed when it is created, so who receives it is who was
previewed; each recipient's outcome is recorded once.

What differs: the audience is chosen by account state and Agent application
state (this service has no billing roles or spend), only a **verified**
email receives it, and a custom body is Markdown with raw HTML disabled
rather than arbitrary HTML. HybridInference's ``quota_change`` template
belongs to its billing and has no counterpart.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from markdown_it import MarkdownIt

from cloud_agent.accounts.mail import SITE_NAME

#: Account states and application states an audience may name.
ACCOUNT_STATES = ("active", "suspended", "deleted")
ADMISSION_STATES = ("not_applied", "pending", "approved", "rejected", "revoked")

_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])

#: The ready-made messages, HybridInference's account-wide ones.
TEMPLATES: dict[str, dict[str, str]] = {
    "maintenance": {
        "subject": "Scheduled maintenance on {date}",
        "heading": "Scheduled Maintenance",
        "body": "We are planning scheduled maintenance on {date} lasting approximately "
        "{duration}.\n\nDuring this time the service will be unavailable. We apologize for "
        "any inconvenience.",
    },
    "announcement": {
        "subject": "Announcing {feature_name}",
        "heading": "New: {feature_name}",
        "body": "{description}",
    },
}


class _Missing(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class RenderedBroadcast:
    """A message ready to send: its subject, HTML and plain text."""

    subject: str
    html: str
    text: str


def _wrap(fragment: str) -> str:
    return (
        '<html><body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">'
        f'<div style="max-width: 600px; margin: 0 auto; padding: 20px;">{fragment}'
        '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
        '<p style="color: #999; font-size: 12px;">You received this because you have a '
        f"{html.escape(SITE_NAME)} account.</p></div></body></html>"
    )


def render_broadcast(
    *,
    template_key: str | None,
    template_vars: dict[str, str],
    subject: str,
    body_markdown: str,
) -> RenderedBroadcast:
    """Render a template with its variables, or a custom subject and Markdown body.

    Raises:
        ValueError: An unknown template, or a custom message without a
            subject or a body.
    """
    if template_key is not None:
        template = TEMPLATES.get(template_key)
        if template is None:
            raise ValueError(f"Unknown template {template_key!r}.")
        values = _Missing({"site_name": SITE_NAME, **template_vars})
        text_values = _Missing({key: str(value) for key, value in values.items()})
        html_values = _Missing({key: html.escape(str(value)) for key, value in values.items()})
        text = template["heading"].format_map(text_values) + "\n\n"
        text += template["body"].format_map(text_values)
        paragraphs = "".join(
            f"<p>{part}</p>" for part in template["body"].format_map(html_values).split("\n\n")
        )
        heading = f"<h2>{template['heading'].format_map(html_values)}</h2>"
        return RenderedBroadcast(
            subject=template["subject"].format_map(text_values),
            html=_wrap(heading + paragraphs),
            text=text,
        )
    if not subject.strip() or not body_markdown.strip():
        raise ValueError("A custom message needs a subject and a body.")
    return RenderedBroadcast(
        subject=subject.strip(),
        html=_wrap(_md.render(body_markdown)),
        text=body_markdown.strip(),
    )


__all__ = [
    "ACCOUNT_STATES",
    "ADMISSION_STATES",
    "TEMPLATES",
    "RenderedBroadcast",
    "render_broadcast",
]
