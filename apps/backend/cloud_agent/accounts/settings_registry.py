"""The administrator settings local accounts read (``Admin → Settings``).

HybridInference keeps its signup switches as runtime settings an administrator
edits, and its mail, SMS and captcha configuration beside them; so does this
service, in its own database-backed registry (:mod:`cloud_agent.settings`).
Each value is imported once from its environment variable, the first time the
registry sees it, and the database is authoritative afterwards. Secrets are
encrypted and never returned to the browser.

Only ``AGENT_AUTH_MODE`` and ``AGENT_LOCAL_SESSION_SECRET`` stay process
configuration: choosing the identity source, and the key every local
credential is hashed under, are deployment decisions, not toggles.

Defaults follow HybridInference except where noted: approval stays manual
(this service's waiting list) until an administrator chooses domain policy,
and phone sign-in, captcha and SMTP are off until configured.
"""

from __future__ import annotations

import re

from cloud_agent.setting_definition import Setting, _numeric

CATEGORY = "Accounts"
_SES_TEMPLATE = "0 means no template. Verification and password reset are required."
_LOCAL = "Local accounts only (AGENT_AUTH_MODE=local). "

_DIGITS = re.compile(r"(?:[1-9][0-9]{0,2})?")
_PREFIXES = re.compile(r"(?:\s*\+[1-9][0-9]{0,6}\s*)(?:,\s*\+[1-9][0-9]{0,6}\s*)*|")


def _emails(value: str) -> bool:
    return all("@" in item and " " not in item.strip() for item in value.split(",") if item.strip())


ACCOUNT_SETTINGS: tuple[Setting, ...] = (
    # ── Sign-up policy ───────────────────────────────────────────────────
    Setting(
        "accounts.signup_enabled",
        "Public sign-up",
        CATEGORY,
        True,
        kind="boolean",
        env="AGENT_LOCAL_SIGNUP_ENABLED",
        description=_LOCAL
        + "Lets anyone create an account with an email or a phone number. Signing up never "
        "grants Agent use by itself.",
    ),
    Setting(
        "accounts.require_email_verification",
        "Require email verification",
        CATEGORY,
        True,
        kind="boolean",
        env="AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION",
        description=_LOCAL
        + "When off, a new email account is treated as verified at sign-up and no verification "
        "mail is sent, so sign-up works without SMTP. Accounts created while it was on still "
        "verify their address.",
    ),
    Setting(
        "accounts.approval_policy",
        "Agent approval",
        CATEGORY,
        "manual",
        env="AGENT_LOCAL_SIGNUP_APPROVAL",
        choices=("manual", "domains"),
        description=_LOCAL
        + "manual: every account waits for an administrator. domains: an account whose email "
        "domain is on the signup domain list is approved as soon as it is verified, and with "
        "an empty list every verified account is.",
    ),
    Setting(
        "accounts.signup_admin_notify",
        "Email administrators about new applications",
        CATEGORY,
        True,
        kind="boolean",
        env="AGENT_LOCAL_SIGNUP_ADMIN_NOTIFY",
        description=_LOCAL + "Sent when a sign-up waits for approval, if mail is configured.",
    ),
    Setting(
        "accounts.signup_notify_emails",
        "New application recipients",
        CATEGORY,
        "",
        env="AGENT_LOCAL_SIGNUP_NOTIFY_EMAILS",
        check=_emails,
        hint="Use comma-separated email addresses.",
        description=_LOCAL
        + "Comma-separated. Empty sends to every local administrator with a verified email.",
    ),
    Setting(
        "accounts.terms_url",
        "Terms of Service URL",
        CATEGORY,
        "",
        env="AGENT_LOCAL_TERMS_URL",
        url=True,
        description=_LOCAL
        + "Linked from the sign-up consent. Agreeing is required to sign up either way.",
    ),
    Setting(
        "accounts.terms_text",
        "Terms of Service text",
        CATEGORY,
        "",
        env="AGENT_LOCAL_TERMS_TEXT",
        description=_LOCAL
        + "Shown on the /terms page and in the sign-up consent step, when no Terms URL is "
        "set. Plain text; blank lines separate paragraphs.",
    ),
    Setting(
        "accounts.admin_emails",
        "Administrator email addresses",
        CATEGORY,
        "",
        env="AGENT_LOCAL_ADMIN_EMAILS",
        check=_emails,
        hint="Use comma-separated email addresses.",
        description=_LOCAL
        + "Comma-separated, as HybridInference's ADMIN_EMAILS: an account whose verified email "
        "is listed is approved and made an administrator when it signs in. An address is "
        "verified only by a link sent to it.",
    ),
    _numeric(
        "accounts.session_ttl_days",
        "Session lifetime (days)",
        CATEGORY,
        30,
        1,
        365,
        env="AGENT_LOCAL_SESSION_TTL_DAYS",
        description="How long a sign-in lasts. It is never extended by use.",
    ),
    # ── Abuse limits ─────────────────────────────────────────────────────
    _numeric(
        "accounts.login_per_15min",
        "Sign-in attempts per account per address (15 min)",
        CATEGORY,
        5,
        1,
        1000,
        env="AGENT_LOCAL_LOGIN_RATE_LIMIT_PER_15MIN",
        description="Also the window of a browser recognized by its device cookie.",
    ),
    _numeric(
        "accounts.login_account_per_15min",
        "Sign-in attempts per account from every address (15 min)",
        CATEGORY,
        50,
        1,
        10000,
        env="AGENT_LOCAL_LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN",
    ),
    _numeric(
        "accounts.login_ip_per_hour",
        "Sign-in attempts per address (hour)",
        CATEGORY,
        20,
        1,
        10000,
        env="AGENT_LOCAL_LOGIN_RATE_LIMIT_PER_HOUR_PER_IP",
    ),
    _numeric(
        "accounts.signup_per_hour",
        "Sign-ups per address (hour)",
        CATEGORY,
        5,
        1,
        1000,
        env="AGENT_LOCAL_SIGNUP_RATE_LIMIT_PER_HOUR",
    ),
    _numeric(
        "accounts.signup_per_day",
        "Sign-ups per address (day)",
        CATEGORY,
        10,
        1,
        10000,
        env="AGENT_LOCAL_SIGNUP_RATE_LIMIT_PER_DAY",
    ),
    Setting(
        "accounts.signup_require_resolved_ip",
        "Refuse sign-up without a client address",
        CATEGORY,
        False,
        kind="boolean",
        env="AGENT_LOCAL_SIGNUP_REQUIRE_RESOLVED_CLIENT_IP",
        description="When the client address cannot be told (behind an untrusted proxy), "
        "refuse sign-up instead of counting it in the shared unresolved budget.",
    ),
    _numeric(
        "accounts.unresolved_signup_per_hour",
        "Sign-ups without a client address (hour, all together)",
        CATEGORY,
        5,
        1,
        1000,
        env="AGENT_LOCAL_UNRESOLVED_SIGNUP_RATE_LIMIT_PER_HOUR",
    ),
    _numeric(
        "accounts.unresolved_signup_per_day",
        "Sign-ups without a client address (day, all together)",
        CATEGORY,
        10,
        1,
        10000,
        env="AGENT_LOCAL_UNRESOLVED_SIGNUP_RATE_LIMIT_PER_DAY",
    ),
    # ── Account email ────────────────────────────────────────────────────
    Setting(
        "accounts.email_provider",
        "Email provider",
        CATEGORY,
        "smtp",
        env="AGENT_LOCAL_EMAIL_PROVIDER",
        description="smtp, tencent_ses, or the name of a registered provider. No fallback "
        "between them.",
    ),
    Setting("accounts.smtp_host", "SMTP host", CATEGORY, "", env="AGENT_SMTP_HOST"),
    _numeric(
        "accounts.smtp_port",
        "SMTP port",
        CATEGORY,
        0,
        0,
        65535,
        env="AGENT_SMTP_PORT",
        description="0 uses 465 for implicit TLS and 587 otherwise.",
    ),
    Setting("accounts.smtp_username", "SMTP username", CATEGORY, "", env="AGENT_SMTP_USERNAME"),
    Setting(
        "accounts.smtp_password",
        "SMTP password",
        CATEGORY,
        "",
        env="AGENT_SMTP_PASSWORD",
        secret=True,
    ),
    Setting("accounts.smtp_from", "Sender address", CATEGORY, "", env="AGENT_SMTP_FROM"),
    Setting(
        "accounts.smtp_security",
        "SMTP security",
        CATEGORY,
        "starttls",
        env="AGENT_SMTP_SECURITY",
        description="starttls, tls (implicit) or none (a local relay). Anything else leaves "
        "mail unconfigured rather than falling back to plain SMTP.",
    ),
    Setting(
        "accounts.tencent_ses_region",
        "Tencent SES region",
        CATEGORY,
        "ap-guangzhou",
        env="AGENT_TENCENT_SES_REGION",
        choices=("ap-guangzhou", "ap-hongkong"),
    ),
    Setting(
        "accounts.tencent_ses_from_email",
        "Tencent SES sender address",
        CATEGORY,
        "",
        env="AGENT_TENCENT_SES_FROM_EMAIL",
    ),
    Setting(
        "accounts.tencent_ses_from_name",
        "Tencent SES sender name",
        CATEGORY,
        "",
        env="AGENT_TENCENT_SES_FROM_NAME",
    ),
    _numeric(
        "accounts.tencent_ses_template_verification",
        "Tencent SES verification template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_VERIFICATION",
        description=_SES_TEMPLATE,
    ),
    _numeric(
        "accounts.tencent_ses_template_password_reset",
        "Tencent SES password reset template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_PASSWORD_RESET",
        description=_SES_TEMPLATE,
    ),
    _numeric(
        "accounts.tencent_ses_template_approval",
        "Tencent SES approval template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_APPROVAL",
        description=_SES_TEMPLATE,
    ),
    _numeric(
        "accounts.tencent_ses_template_rejection",
        "Tencent SES rejection template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_REJECTION",
        description=_SES_TEMPLATE,
    ),
    _numeric(
        "accounts.tencent_ses_template_admin_signup",
        "Tencent SES new application template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_ADMIN_SIGNUP",
        description=_SES_TEMPLATE,
    ),
    _numeric(
        "accounts.tencent_ses_template_notice",
        "Tencent SES plain notice template ID",
        CATEGORY,
        0,
        0,
        2**31 - 1,
        env="AGENT_TENCENT_SES_TEMPLATE_NOTICE",
        description=_SES_TEMPLATE,
    ),
    # ── Phone sign-in ────────────────────────────────────────────────────
    Setting(
        "accounts.sms_enabled",
        "Phone sign-in",
        CATEGORY,
        False,
        kind="boolean",
        env="AGENT_LOCAL_SMS_ENABLED",
        description=_LOCAL
        + "Sign-up, sign-in and binding with a code sent by text message. Needs a configured "
        "provider.",
    ),
    Setting(
        "accounts.sms_provider",
        "Text message provider",
        CATEGORY,
        "none",
        env="AGENT_LOCAL_SMS_PROVIDER",
        choices=("none", "tencent"),
    ),
    Setting(
        "accounts.sms_default_country_code",
        "Default country code",
        CATEGORY,
        "",
        env="AGENT_LOCAL_SMS_DEFAULT_COUNTRY_CODE",
        check=lambda value: bool(_DIGITS.fullmatch(value)),
        hint="Use 1-3 digits without +, such as 86.",
        description="Prepended to a number entered without +.",
    ),
    Setting(
        "accounts.sms_allowed_prefixes",
        "Allowed number prefixes",
        CATEGORY,
        "",
        env="AGENT_LOCAL_SMS_ALLOWED_PREFIXES",
        check=lambda value: bool(_PREFIXES.fullmatch(value)),
        hint="Use comma-separated prefixes such as +86,+1.",
        description="Comma-separated. Empty allows every number the provider accepts.",
    ),
    _numeric("accounts.sms_code_ttl_s", "Code lifetime (seconds)", CATEGORY, 300, 60, 600),
    _numeric(
        "accounts.sms_send_cooldown_s",
        "Seconds between codes to one number",
        CATEGORY,
        60,
        60,
        3600,
    ),
    _numeric("accounts.sms_send_per_phone_hour", "Codes per number per hour", CATEGORY, 5, 1, 100),
    _numeric("accounts.sms_send_per_phone_day", "Codes per number per day", CATEGORY, 10, 1, 1000),
    _numeric("accounts.sms_send_per_ip_hour", "Codes per address per hour", CATEGORY, 20, 1, 1000),
    _numeric("accounts.sms_send_per_ip_day", "Codes per address per day", CATEGORY, 100, 1, 10000),
    _numeric("accounts.sms_max_attempts", "Checks per code", CATEGORY, 5, 1, 10),
    _numeric(
        "accounts.sms_verify_per_phone_15min",
        "Checks per number per 15 minutes",
        CATEGORY,
        10,
        1,
        1000,
    ),
    _numeric(
        "accounts.sms_verify_per_ip_hour", "Checks per address per hour", CATEGORY, 30, 1, 10000
    ),
    Setting(
        "accounts.tencent_secret_id",
        "Tencent Cloud SecretId",
        CATEGORY,
        "",
        env="AGENT_TENCENT_CLOUD_SECRET_ID",
        secret=True,
    ),
    Setting(
        "accounts.tencent_secret_key",
        "Tencent Cloud SecretKey",
        CATEGORY,
        "",
        env="AGENT_TENCENT_CLOUD_SECRET_KEY",
        secret=True,
    ),
    Setting(
        "accounts.tencent_token",
        "Tencent Cloud session token",
        CATEGORY,
        "",
        env="AGENT_TENCENT_CLOUD_TOKEN",
        secret=True,
        description="Only for temporary credentials.",
    ),
    Setting(
        "accounts.tencent_sms_region",
        "Tencent SMS region",
        CATEGORY,
        "ap-guangzhou",
        env="AGENT_TENCENT_SMS_REGION",
    ),
    Setting(
        "accounts.tencent_sms_app_id",
        "Tencent SMS application ID",
        CATEGORY,
        "",
        env="AGENT_TENCENT_SMS_SDK_APP_ID",
    ),
    Setting(
        "accounts.tencent_sms_sign_name",
        "Tencent SMS signature",
        CATEGORY,
        "",
        env="AGENT_TENCENT_SMS_SIGN_NAME",
    ),
    Setting(
        "accounts.tencent_sms_template_id",
        "Tencent SMS template ID",
        CATEGORY,
        "",
        env="AGENT_TENCENT_SMS_TEMPLATE_ID",
        description="An approved template taking the code and its lifetime in minutes.",
    ),
    # ── Captcha ──────────────────────────────────────────────────────────
    Setting(
        "accounts.turnstile_site_key",
        "Turnstile site key",
        CATEGORY,
        "",
        env="AGENT_TURNSTILE_SITE_KEY",
        description="Public; shown to the sign-up and phone-code forms.",
    ),
    Setting(
        "accounts.turnstile_secret_key",
        "Turnstile secret key",
        CATEGORY,
        "",
        env="AGENT_TURNSTILE_SECRET_KEY",
        secret=True,
        description="When empty, captcha is not checked (as HybridInference).",
    ),
)

__all__ = ["ACCOUNT_SETTINGS", "CATEGORY"]
