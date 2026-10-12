"""Import each host's legacy environment configuration into admin settings once.

Only validated nondefault values without a global or per-host administrator
override are imported. A partial runner report waits for the broker's original
three settings before the import is complete; later heartbeats finish hosts
whose environment has not yet been observed.
"""

from __future__ import annotations

from alembic import op

revision = "0036_host_settings_import"
down_revision = "0035_admin_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve observed legacy values before defaults become authoritative."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "ADD COLUMN settings_imported BOOLEAN NOT NULL DEFAULT FALSE, "
        "ADD COLUMN settings_import_excluded TEXT[] NOT NULL DEFAULT '{}'::text[]"
    )
    op.execute(
        "ALTER TABLE agent_runner_policy "
        "ADD COLUMN host_settings_import_excluded TEXT[] NOT NULL DEFAULT '{}'::text[]"
    )
    # Frozen bounds from this release: migrations must not depend on the
    # application's registry changing in a later version.
    op.execute(
        """
        WITH definitions(key, default_value, minimum, maximum) AS (
            VALUES
                ('agent_timeout_s', 3600, 60, 86400),
                ('source_import_max_mib', 1024, 1, 65536),
                ('chat_idle_timeout_s', 1800, 60, 604800),
                ('shared_resident_capacity', 8, 2, 128),
                ('terminal_idle_ttl_s', 1800, 60, 86400),
                ('terminal_hard_ttl_s', 28800, 60, 86400),
                ('terminal_max_sessions', 64, 1, 4096),
                ('terminal_max_sessions_per_workspace', 4, 1, 4),
                ('workspace_command_timeout_s', 60, 1, 120),
                ('workspace_file_write_max_mib', 2, 1, 2),
                ('workspace_file_preview_max_kib', 512, 1, 512),
                ('workspace_command_output_max_mib', 2, 1, 2),
                ('workspace_directory_max_entries', 5000, 1, 5000),
                ('terminal_buffer_max_mib', 2, 1, 2),
                ('terminal_buffer_max_events', 4096, 2, 4096),
                ('terminal_output_max_mib', 32, 1, 32),
                ('terminal_input_max_kib', 64, 1, 64),
                ('terminal_max_streams_per_session', 4, 1, 4)
        ), observations AS (
            SELECT h.host, d.*, CASE
                WHEN jsonb_typeof(local.value) = 'number'
                    AND (local.value #>> '{}') ~ '^[0-9]+([.]0+)?$'
                THEN (local.value #>> '{}')::numeric
            END AS value
            FROM agent_runner_hosts h
            CROSS JOIN LATERAL jsonb_each(h.settings_local) local
            JOIN definitions d ON d.key = local.key
            WHERE NOT h.settings_desired ? d.key
              AND NOT COALESCE(
                (SELECT host_settings FROM agent_runner_policy WHERE id), '{}'::jsonb
              ) ? d.key
        ), imports AS (
            SELECT host, jsonb_object_agg(key, to_jsonb(value::bigint)) AS values
            FROM observations
            WHERE value BETWEEN minimum AND maximum AND value <> default_value
            GROUP BY host
        )
        UPDATE agent_runner_hosts h
        SET settings_desired = h.settings_desired || i.values
        FROM imports i WHERE h.host = i.host
        """
    )
    op.execute(
        """
        UPDATE agent_runner_hosts SET settings_imported = TRUE
        WHERE settings_local ?& ARRAY[
            'agent_timeout_s', 'source_import_max_mib',
            'chat_idle_timeout_s', 'shared_resident_capacity'
        ] AND (
            broker_url IS NULL OR settings_local ?& ARRAY[
                'terminal_idle_ttl_s', 'terminal_hard_ttl_s', 'terminal_max_sessions'
            ]
        )
        """
    )


def downgrade() -> None:
    """Remove the marker while keeping the imported settings as admin overrides."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "DROP COLUMN settings_imported, DROP COLUMN settings_import_excluded"
    )
    op.execute("ALTER TABLE agent_runner_policy DROP COLUMN host_settings_import_excluded")
