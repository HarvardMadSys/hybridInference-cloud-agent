"""Move memory switches and saved preferences onto durable memories.

Revision ID: 0070_memory_cutover
Revises: 0069_agent_memories

The 50-request memory is retired. Its per-scope switches move to
``agent_memory_settings``, and the preferences tasks were receiving become
durable memories: ``manual`` when the owner edited or consolidated them,
``agent`` otherwise. The old tables are no longer read; they stay one release
so a rollback still finds them, and a later revision drops them.
"""

from alembic import op

revision = "0070_memory_cutover"
down_revision = "0069_agent_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the switch table, then copy switches and preferences once."""
    op.execute(
        """
        CREATE TABLE agent_memory_settings (
            user_id TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, repo),
            CONSTRAINT ck_agent_memory_settings_repo CHECK (repo = '' OR repo ~ '[^[:space:]]')
        )
        """
    )
    # '' is the user scope; every other value is one repository.
    op.execute(
        """
        INSERT INTO agent_memory_settings (user_id, repo, enabled)
        SELECT user_id, '', enabled FROM agent_user_memories
        UNION ALL
        SELECT user_id, repo, enabled FROM agent_project_memories WHERE repo ~ '[^[:space:]]'
        ON CONFLICT (user_id, repo) DO NOTHING
        """
    )
    # The saved views already carry the owner's edits. User-scope preferences
    # come from the user view and repository-scope ones from that repository's
    # view: the same split tasks received. Ids are derived from the content, so
    # re-running the copy after a downgrade adds nothing twice.
    op.execute(
        """
        WITH saved AS (
            SELECT u.user_id, NULL::text AS repo, entry
            FROM agent_user_memories u
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(u.memories) = 'array' THEN u.memories ELSE '[]' END
            ) AS entry
            WHERE COALESCE(
                entry->>'scope', CASE WHEN entry->>'source_repo' IS NULL THEN 'user' END
            ) = 'user'
            UNION ALL
            SELECT p.user_id, p.repo, entry
            FROM agent_project_memories p
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(p.memories) = 'array' THEN p.memories ELSE '[]' END
            ) AS entry
            WHERE p.repo ~ '[^[:space:]]'
              AND COALESCE(entry->>'scope', 'repo') = 'repo'
        ),
        preferences AS (
            SELECT user_id, repo,
                   btrim(entry->>'content') AS content,
                   CASE WHEN entry->>'user_edited' = 'true' OR entry->>'edited' = 'true'
                        THEN 'manual' ELSE 'agent' END AS origin,
                   entry->>'source_job_id' AS source_job_id
            FROM saved
            WHERE entry->>'kind' = 'preference'
              AND char_length(btrim(entry->>'content')) BETWEEN 1 AND 240
        ),
        distinct_preferences AS (
            SELECT DISTINCT ON (user_id, repo, lower(rtrim(content, '.!? ')))
                   user_id, repo, content, origin, source_job_id
            FROM preferences
            ORDER BY user_id, repo, lower(rtrim(content, '.!? ')), origin DESC
        ),
        ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY user_id, repo ORDER BY origin DESC, content
            ) AS position
            FROM distinct_preferences
        )
        INSERT INTO agent_memories
            (id, user_id, repo, content, description, origin, source_job_ids)
        SELECT md5(user_id || chr(31) || COALESCE(repo, '') || chr(31) || content),
               user_id, repo, content,
               CASE WHEN char_length(content) <= 80 THEN content
                    ELSE rtrim(left(content, 79)) || '…' END,
               origin,
               CASE WHEN source_job_id IS NULL THEN '{}'::text[]
                    ELSE ARRAY[source_job_id] END
        FROM ranked
        WHERE position <= 30
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    """Remove the switch table; copied memories stay with the durable ones."""
    op.execute("DROP TABLE agent_memory_settings")
