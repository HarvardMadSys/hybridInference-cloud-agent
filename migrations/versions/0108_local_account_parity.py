"""Local accounts reach HybridInference's account features (``AGENT_AUTH_MODE=local``).

What changes, all within the local-account tables:

- **Sign-in identities.** ``agent_local_password_logins`` (one email and one
  password per account) becomes ``agent_local_login_identities`` (an email, a
  login name and a phone number, each optional, one row per kind) and an
  optional ``agent_local_password_credentials`` row. Every existing row is
  carried over: its email keeps its verification time and its password hash.
- **One access rule.** The view ``agent_local_account_access`` answers whether
  an account's identity is verified, whether it may sign in and whether it may
  use Agent. Every SQL gate reads it, and it is built from ``LEFT JOIN`` and
  ``coalesce``, so an account missing any row has no access: fail closed.
- **Deleted accounts.** ``deleted`` joins the account states, and a deletion
  opens the same termination record a suspension does. ``erased_at`` marks a
  deleted account whose identity an administrator erased for good.
- **New state**: phone verification codes, browser sessions holding several
  accounts, sign-in events, the signup domain allowlist, first-run setup,
  each account's interface language, and administrator mail to accounts.

None of the new columns is named ``user_id`` or ``target_user_id``: the
namespace guard discovers owner data by those names, and an account record is
not owner data.

The downgrade restores ``agent_local_password_logins`` only when every account
still has exactly an email and a password, and refuses otherwise rather than
silently dropping an account that signs in some other way.
"""

from alembic import op

revision = "0108_local_account_parity"
down_revision = "0107_local_accounts"
branch_labels = None
depends_on = None

#: Login names are 3-32 lowercase letters, digits, ``.``, ``_`` or ``-``
#: starting with a letter or digit, as HybridInference accepts. They never
#: contain ``@`` or ``+``, so no login name can be mistaken for the other kinds.
LOGIN_NAME_RE = "^[a-z0-9][a-z0-9_.-]{2,31}$"
#: E.164: a plus sign and 8 to 15 digits, the first not zero.
PHONE_RE = "^\\+[1-9][0-9]{7,14}$"

_ACCESS_VIEW = """
CREATE VIEW agent_local_account_access AS
SELECT a.id AS account_id,
       v.identity_verified,
       (a.status = 'active' AND v.identity_verified) AS may_sign_in,
       (a.status = 'active' AND v.identity_verified
        AND coalesce(m.status = 'approved', FALSE)) AS has_use
FROM agent_local_accounts a
LEFT JOIN agent_local_admissions m ON m.account_id = a.id
CROSS JOIN LATERAL (
    SELECT coalesce(
        NOT EXISTS (
            SELECT 1 FROM agent_local_login_identities i
            WHERE i.account_id = a.id AND i.kind = 'email' AND i.verified_at IS NULL
        )
        AND EXISTS (
            SELECT 1 FROM agent_local_login_identities i
            WHERE i.account_id = a.id AND i.verified_at IS NOT NULL
        ),
        FALSE
    ) AS identity_verified
) v
"""

_UPGRADE: tuple[str, ...] = (
    # ── Identities and credentials ───────────────────────────────────────
    f"""
    CREATE TABLE agent_local_login_identities (
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('email', 'login_name', 'phone')),
        value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 320),
        value_normalized TEXT NOT NULL,
        verified_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (account_id, kind),
        UNIQUE (kind, value_normalized),
        CHECK (
            (kind = 'email' AND position('@' IN value_normalized) > 1
                AND value_normalized = lower(value_normalized)
                AND length(value_normalized) <= 320)
            OR (kind = 'login_name' AND value_normalized ~ '{LOGIN_NAME_RE}'
                AND verified_at IS NOT NULL)
            OR (kind = 'phone' AND value_normalized ~ '{PHONE_RE}'
                AND value = value_normalized AND verified_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE agent_local_password_credentials (
        account_id TEXT PRIMARY KEY REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        password_hash TEXT NOT NULL CHECK (left(password_hash, 10) = '$argon2id$'),
        password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    INSERT INTO agent_local_login_identities
        (account_id, kind, value, value_normalized, verified_at, created_at)
    SELECT account_id, 'email', email, email_normalized, email_verified_at, created_at
    FROM agent_local_password_logins
    """,
    """
    INSERT INTO agent_local_password_credentials (account_id, password_hash, password_changed_at)
    SELECT account_id, password_hash, password_changed_at FROM agent_local_password_logins
    """,
    "DROP TABLE agent_local_password_logins",
    # ── The account: deletion, notes, display name, terms ────────────────
    "ALTER TABLE agent_local_accounts DROP CONSTRAINT agent_local_accounts_status_check",
    "ALTER TABLE agent_local_accounts ADD CONSTRAINT agent_local_accounts_status_check "
    "CHECK (status IN ('active', 'suspended', 'deleted'))",
    """
    ALTER TABLE agent_local_accounts
        ADD COLUMN user_name TEXT CHECK (user_name IS NULL OR length(user_name) BETWEEN 1 AND 50),
        ADD COLUMN admin_note TEXT CHECK (admin_note IS NULL OR length(admin_note) <= 2000),
        ADD COLUMN suspension_message TEXT
            CHECK (suspension_message IS NULL OR length(suspension_message) <= 2000),
        ADD COLUMN tos_accepted_at TIMESTAMPTZ,
        ADD COLUMN last_login_at TIMESTAMPTZ,
        ADD COLUMN deleted_at TIMESTAMPTZ,
        ADD COLUMN erased_at TIMESTAMPTZ,
        ADD COLUMN locale TEXT CHECK (locale IS NULL OR locale IN ('en', 'zh-CN')),
        ADD COLUMN locale_chosen_at TIMESTAMPTZ,
        ADD CONSTRAINT agent_local_accounts_deleted_at_check
            CHECK ((status = 'deleted') = (deleted_at IS NOT NULL)),
        ADD CONSTRAINT agent_local_accounts_erased_at_check
            CHECK (erased_at IS NULL OR status = 'deleted')
    """,
    "ALTER TABLE agent_local_access_terminations "
    "DROP CONSTRAINT agent_local_access_terminations_action_check",
    "ALTER TABLE agent_local_access_terminations ADD CONSTRAINT "
    "agent_local_access_terminations_action_check "
    "CHECK (action IN ('revoke', 'suspend', 'delete'))",
    # ── Emailed tokens: binding a new address ────────────────────────────
    "ALTER TABLE agent_local_auth_tokens DROP CONSTRAINT agent_local_auth_tokens_purpose_check",
    "ALTER TABLE agent_local_auth_tokens ADD CONSTRAINT agent_local_auth_tokens_purpose_check "
    "CHECK (purpose IN ('verify_email', 'reset_password', 'bind_email'))",
    """
    ALTER TABLE agent_local_auth_tokens
        ADD COLUMN pending_email TEXT CHECK (pending_email IS NULL OR length(pending_email) <= 320),
        ADD COLUMN pending_email_normalized TEXT,
        ADD CONSTRAINT agent_local_auth_tokens_pending_check CHECK (
            (purpose = 'bind_email') = (pending_email_normalized IS NOT NULL)
            AND (pending_email IS NULL) = (pending_email_normalized IS NULL)
        )
    """,
    _ACCESS_VIEW,
    # ── Phone verification codes ─────────────────────────────────────────
    # One open code per (number, purpose, account), kept only as a keyed hash.
    # A code is reserved inactive, activated only once the provider accepted
    # the message, and consumed once; every check counts against ``attempts``.
    """
    CREATE TABLE agent_local_phone_codes (
        phone_hash BYTEA NOT NULL CHECK (length(phone_hash) = 32),
        purpose TEXT NOT NULL CHECK (purpose IN
            ('signup', 'login', 'bind', 'delete_account', 'bind_email', 'set_password')),
        account_key TEXT NOT NULL DEFAULT '',
        challenge_id TEXT NOT NULL UNIQUE,
        code_hash BYTEA NOT NULL CHECK (length(code_hash) = 32),
        expires_at TIMESTAMPTZ NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        active BOOLEAN NOT NULL DEFAULT FALSE,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (phone_hash, purpose, account_key)
    )
    """,
    "CREATE INDEX idx_agent_local_phone_codes_expiry ON agent_local_phone_codes (expires_at)",
    # ── Browser sessions: one cookie, up to five signed-in accounts ──────
    """
    CREATE TABLE agent_local_browser_sessions (
        id TEXT PRIMARY KEY,
        token_hash BYTEA NOT NULL UNIQUE CHECK (length(token_hash) = 32),
        active_account_id TEXT REFERENCES agent_local_accounts (id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX idx_agent_local_browser_sessions_expiry "
    "ON agent_local_browser_sessions (expires_at)",
    """
    CREATE TABLE agent_local_browser_accounts (
        browser_id TEXT NOT NULL REFERENCES agent_local_browser_sessions (id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES agent_local_sessions (id) ON DELETE CASCADE,
        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (browser_id, account_id),
        UNIQUE (session_id)
    )
    """,
    "CREATE INDEX idx_agent_local_browser_accounts_account "
    "ON agent_local_browser_accounts (account_id)",
    # ── Sign-in events ───────────────────────────────────────────────────
    """
    CREATE TABLE agent_local_login_events (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT REFERENCES agent_local_accounts (id) ON DELETE SET NULL,
        method TEXT NOT NULL CHECK (method IN ('password', 'phone', 'setup')),
        identifier TEXT NOT NULL CHECK (length(identifier) <= 320),
        outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
        failure_reason TEXT CHECK (failure_reason IS NULL OR length(failure_reason) <= 64),
        ip TEXT NOT NULL CHECK (length(ip) <= 64),
        user_agent TEXT CHECK (user_agent IS NULL OR length(user_agent) <= 512),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_local_login_events_account "
    "ON agent_local_login_events (account_id, created_at DESC)",
    "CREATE INDEX idx_agent_local_login_events_created ON agent_local_login_events (created_at)",
    # ── Signup domain allowlist ──────────────────────────────────────────
    # A wildcard row is stored without its ``*.`` and matches subdomains only.
    """
    CREATE TABLE agent_local_signup_domains (
        domain TEXT NOT NULL CHECK (
            length(domain) BETWEEN 1 AND 253 AND domain = lower(domain)
            AND domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
        ),
        is_wildcard BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by TEXT NOT NULL,
        PRIMARY KEY (domain, is_wildcard)
    )
    """,
    # ── First-run setup ──────────────────────────────────────────────────
    # While no local account exists: the one setup code every process logs at
    # boot. ``completed_at`` closes setup for good.
    """
    CREATE TABLE agent_local_setup (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        setup_code TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        CHECK ((setup_code IS NULL) = (completed_at IS NOT NULL))
    )
    """,
)

_BROADCAST: tuple[str, ...] = (
    # Administrator mail to accounts: a snapshot of recipients taken when it is
    # created, sent once (now or at ``scheduled_at``) by whichever process
    # claims it. Each recipient row records its own outcome, so a send that
    # was interrupted resumes with the recipients still pending.
    """
    CREATE TABLE agent_local_broadcasts (
        id TEXT PRIMARY KEY,
        subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 300),
        body_html TEXT NOT NULL CHECK (length(body_html) <= 200000),
        body_text TEXT NOT NULL CHECK (length(body_text) <= 100000),
        template_key TEXT,
        target_statuses TEXT[] NOT NULL,
        target_admissions TEXT[] NOT NULL,
        status TEXT NOT NULL DEFAULT 'scheduled'
            CHECK (status IN ('scheduled', 'sending', 'sent', 'failed', 'cancelled')),
        recipient_count INTEGER NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
        scheduled_at TIMESTAMPTZ,
        claimed_at TIMESTAMPTZ,
        sent_at TIMESTAMPTZ,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_local_broadcasts_due ON agent_local_broadcasts (scheduled_at) "
    "WHERE status IN ('scheduled', 'sending')",
    """
    CREATE TABLE agent_local_broadcast_recipients (
        broadcast_id TEXT NOT NULL REFERENCES agent_local_broadcasts (id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
        error TEXT CHECK (error IS NULL OR length(error) <= 500),
        sent_at TIMESTAMPTZ,
        PRIMARY KEY (broadcast_id, account_id)
    )
    """,
    "CREATE INDEX idx_agent_local_broadcast_recipients_account "
    "ON agent_local_broadcast_recipients (account_id)",
)

_UPGRADE = _UPGRADE + _BROADCAST

_DOWNGRADE_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_local_accounts a
        WHERE a.status = 'deleted'
           OR NOT EXISTS (SELECT 1 FROM agent_local_password_credentials c WHERE c.account_id = a.id)
           OR NOT EXISTS (
               SELECT 1 FROM agent_local_login_identities i
               WHERE i.account_id = a.id AND i.kind = 'email')
           OR EXISTS (
               SELECT 1 FROM agent_local_login_identities i
               WHERE i.account_id = a.id AND i.kind <> 'email')
    ) THEN
        RAISE EXCEPTION 'refusing to downgrade: an account signs in without exactly an email '
            'and a password, or is deleted';
    END IF;
END
$$
"""

_DOWNGRADE: tuple[str, ...] = (
    _DOWNGRADE_GUARD,
    "DROP TABLE agent_local_broadcast_recipients",
    "DROP TABLE agent_local_broadcasts",
    "DROP TABLE agent_local_setup",
    "DROP TABLE agent_local_signup_domains",
    "DROP TABLE agent_local_login_events",
    "DROP TABLE agent_local_browser_accounts",
    "DROP TABLE agent_local_browser_sessions",
    "DROP TABLE agent_local_phone_codes",
    "DROP VIEW agent_local_account_access",
    "DELETE FROM agent_local_auth_tokens WHERE purpose = 'bind_email'",
    """
    ALTER TABLE agent_local_auth_tokens
        DROP CONSTRAINT agent_local_auth_tokens_pending_check,
        DROP COLUMN pending_email_normalized,
        DROP COLUMN pending_email
    """,
    "ALTER TABLE agent_local_auth_tokens DROP CONSTRAINT agent_local_auth_tokens_purpose_check",
    "ALTER TABLE agent_local_auth_tokens ADD CONSTRAINT agent_local_auth_tokens_purpose_check "
    "CHECK (purpose IN ('verify_email', 'reset_password'))",
    "DELETE FROM agent_local_access_terminations WHERE action = 'delete'",
    "ALTER TABLE agent_local_access_terminations "
    "DROP CONSTRAINT agent_local_access_terminations_action_check",
    "ALTER TABLE agent_local_access_terminations ADD CONSTRAINT "
    "agent_local_access_terminations_action_check CHECK (action IN ('revoke', 'suspend'))",
    """
    ALTER TABLE agent_local_accounts
        DROP CONSTRAINT agent_local_accounts_erased_at_check,
        DROP CONSTRAINT agent_local_accounts_deleted_at_check,
        DROP COLUMN locale_chosen_at,
        DROP COLUMN locale,
        DROP COLUMN erased_at,
        DROP COLUMN deleted_at,
        DROP COLUMN last_login_at,
        DROP COLUMN tos_accepted_at,
        DROP COLUMN suspension_message,
        DROP COLUMN admin_note,
        DROP COLUMN user_name
    """,
    "ALTER TABLE agent_local_accounts DROP CONSTRAINT agent_local_accounts_status_check",
    "ALTER TABLE agent_local_accounts ADD CONSTRAINT agent_local_accounts_status_check "
    "CHECK (status IN ('active', 'suspended'))",
    """
    CREATE TABLE agent_local_password_logins (
        account_id TEXT PRIMARY KEY REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        email TEXT NOT NULL CHECK (length(email) BETWEEN 3 AND 320),
        email_normalized TEXT NOT NULL UNIQUE
            CHECK (email_normalized = lower(email_normalized) AND length(email_normalized) <= 320),
        password_hash TEXT NOT NULL CHECK (left(password_hash, 10) = '$argon2id$'),
        email_verified_at TIMESTAMPTZ,
        password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    INSERT INTO agent_local_password_logins
        (account_id, email, email_normalized, password_hash, email_verified_at,
         password_changed_at, created_at)
    SELECT i.account_id, i.value, i.value_normalized, c.password_hash, i.verified_at,
           c.password_changed_at, i.created_at
    FROM agent_local_login_identities i
    JOIN agent_local_password_credentials c ON c.account_id = i.account_id
    WHERE i.kind = 'email'
    """,
    "DROP TABLE agent_local_password_credentials",
    "DROP TABLE agent_local_login_identities",
)


def upgrade() -> None:
    """Split identities from credentials, keeping every account; add the new tables."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Restore the email-and-password table, or refuse when an account would be lost."""
    for statement in _DOWNGRADE:
        op.execute(statement)
