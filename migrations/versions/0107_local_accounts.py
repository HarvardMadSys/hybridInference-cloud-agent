"""Local accounts, sessions and the Agent waiting list (``AGENT_AUTH_MODE=local``).

Every table is new and empty, so this rewrites nothing and changes no existing
row. A Gateway-mode deployment never writes to them; the namespace check at
startup keeps one database from serving both identity sources.

None of the columns is named ``user_id``: the namespace guard discovers owner
data by that column name, and an account record is not owner data.

It follows ``0106_user_model_connections``: the two features were developed
in parallel and this one was renumbered when they were integrated.
"""

from alembic import op

revision = "0107_local_accounts"
down_revision = "0106_user_model_connections"
branch_labels = None
depends_on = None

_STATEMENTS: tuple[str, ...] = (
    # Present only once local identity owns this database. A Gateway issuer
    # binding and this marker never coexist; the startup checks refuse either
    # mode on a database the other one initialized.
    """
    CREATE TABLE agent_local_identity_namespace (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # The account and the one version every account or admission edit bumps.
    # Ids are ULIDs minted here; nothing infers an account's origin from them.
    """
    CREATE TABLE agent_local_accounts (
        id TEXT PRIMARY KEY CHECK (id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
        version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_local_accounts_created ON agent_local_accounts (created_at DESC, id)",
    # Email and password sign-in. The password is stored only as an Argon2id hash.
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
    # The waiting-list application and its review, separate from the account.
    """
    CREATE TABLE agent_local_admissions (
        account_id TEXT PRIMARY KEY REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'not_applied'
            CHECK (status IN ('not_applied', 'pending', 'approved', 'rejected', 'revoked')),
        purpose TEXT NOT NULL DEFAULT '' CHECK (length(purpose) <= 2000),
        review_reason TEXT CHECK (review_reason IS NULL OR length(review_reason) <= 2000),
        submitted_at TIMESTAMPTZ,
        reviewed_at TIMESTAMPTZ,
        reviewed_by TEXT,
        CHECK (status <> 'not_applied' OR submitted_at IS NULL)
    )
    """,
    "CREATE INDEX idx_agent_local_admissions_status ON agent_local_admissions (status)",
    # Server-side sessions. Only a keyed hash of the cookie value is stored.
    """
    CREATE TABLE agent_local_sessions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        token_hash BYTEA NOT NULL UNIQUE CHECK (length(token_hash) = 32),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        revoke_reason TEXT
    )
    """,
    "CREATE INDEX idx_agent_local_sessions_live ON agent_local_sessions (account_id) "
    "WHERE revoked_at IS NULL",
    "CREATE INDEX idx_agent_local_sessions_expiry ON agent_local_sessions (expires_at)",
    # Single-use email verification and password reset tokens, hashed.
    """
    CREATE TABLE agent_local_auth_tokens (
        token_hash BYTEA PRIMARY KEY CHECK (length(token_hash) = 32),
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        purpose TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX idx_agent_local_auth_tokens_open ON agent_local_auth_tokens "
    "(account_id, purpose) WHERE consumed_at IS NULL",
    "CREATE INDEX idx_agent_local_auth_tokens_expiry ON agent_local_auth_tokens (expires_at)",
    # Fixed-window abuse counters shared by every worker. Keys are keyed hashes,
    # never a raw email address or client address.
    """
    CREATE TABLE agent_local_auth_rate_limits (
        bucket TEXT NOT NULL,
        key_hash BYTEA NOT NULL CHECK (length(key_hash) = 32),
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        hits INTEGER NOT NULL CHECK (hits > 0),
        PRIMARY KEY (bucket, key_hash, window_start)
    )
    """,
    "CREATE INDEX idx_agent_local_auth_rate_limits_end ON agent_local_auth_rate_limits "
    "(window_end)",
    # One row per revocation or suspension, written in the administrator's
    # transaction at the account version it produced. It stays open until
    # every step of stopping the account's work has been acknowledged, and
    # while one is open the account cannot be approved or made an admin again,
    # so a retry can never stop work started under a newer approval. Reopening
    # it (a terminal opened late) bumps its revision; a pass closes it only at
    # the revision it read before it started, so a reopen during a pass stays
    # open for the next one.
    """
    CREATE TABLE agent_local_access_terminations (
        account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
        generation BIGINT NOT NULL CHECK (generation > 0),
        action TEXT NOT NULL CHECK (action IN ('revoke', 'suspend')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        last_attempt_at TIMESTAMPTZ,
        last_outcome TEXT,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        completed_at TIMESTAMPTZ,
        PRIMARY KEY (account_id, generation)
    )
    """,
    "CREATE INDEX idx_agent_local_access_terminations_open ON agent_local_access_terminations "
    "(created_at) WHERE completed_at IS NULL",
)

_TABLES: tuple[str, ...] = (
    "agent_local_access_terminations",
    "agent_local_auth_rate_limits",
    "agent_local_auth_tokens",
    "agent_local_sessions",
    "agent_local_admissions",
    "agent_local_password_logins",
    "agent_local_accounts",
    "agent_local_identity_namespace",
)


def upgrade() -> None:
    """Create the empty local-account tables."""
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop them. Local accounts, sessions and applications are lost."""
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
