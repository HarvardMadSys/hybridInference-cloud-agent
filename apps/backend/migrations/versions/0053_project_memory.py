"""Separate owner-wide memory from repository-scoped memory.

Revision ID: 0053_project_memory
Revises: 0052_memory_overrides
"""

import hashlib
import json
import re
import unicodedata

import sqlalchemy as sa
from alembic import context, op

revision = "0053_project_memory"
down_revision = "0052_memory_overrides"
branch_labels = None
depends_on = None


def _source_ids(content: str, title: str, repo: str) -> dict[str, tuple[str, str]]:
    """Resolve legacy source keys without importing evolving application code."""

    def clean(value: str, limit: int) -> str:
        value = " ".join(
            "".join(
                char
                for char in value
                if char.isspace() or not unicodedata.category(char).startswith("C")
            ).split()
        )
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"

    repo = clean(repo, 200)
    lines = []
    fence_char = None
    fence_length = 0
    for line in content[:8000].splitlines():
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_char:
            if (
                fence
                and fence[1][0] == fence_char
                and len(fence[1]) >= fence_length
                and not fence[2].strip()
            ):
                fence_char = None
            continue
        if fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
            continue
        if line.startswith(("    ", "\t")) or line.lstrip().startswith((">", '"', "'", "`")):
            continue
        lines.append(line)
    values = [("task", clean(title, 160))]
    values.extend(
        ("preference", clean(sentence, 500))
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", "\n".join(lines))
        if not sentence.lstrip().startswith((">", "`", '"', "'"))
    )
    return {
        hashlib.sha256(f"{repo}:{kind}:{value.casefold().rstrip('.!?… ')}".encode()).hexdigest()[
            :20
        ]: (kind, value)
        for kind, value in values
        if value
    }


def _split_overlays() -> None:
    """Keep owner edits and suppressions attached to their exact owned sources."""
    conn = op.get_bind()
    sources: dict[tuple[str, str], set[str]] = {}
    owned_jobs: dict[tuple[str, str], str] = {}
    source_entries: dict[tuple[str, str, str], tuple[str, str]] = {}
    for row in conn.execute(
        sa.text("""
        SELECT t.user_id, t.repo, t.title, j.id AS job_id, left(m.content, 8000) AS content
        FROM agent_jobs j
        JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
        JOIN agent_user_memories u ON u.user_id = j.user_id
        LEFT JOIN agent_thread_messages m
          ON m.job_id = j.id AND m.thread_id = t.id AND m.role = 'user'
        WHERE jsonb_array_length(u.overrides) > 0
           OR EXISTS (SELECT 1 FROM agent_user_memory_edits e WHERE e.user_id = u.user_id)
    """)
    ).mappings():
        owned_jobs[row["user_id"], row["job_id"]] = row["repo"]
        for source_id, entry in _source_ids(
            row["content"] or "", row["title"], row["repo"]
        ).items():
            sources.setdefault((row["user_id"], source_id), set()).add(row["repo"])
            source_entries[row["user_id"], row["job_id"], source_id] = entry

    profiles = list(
        conn.execute(
            sa.text("SELECT user_id, memories, overrides FROM agent_user_memories")
        ).mappings()
    )
    for profile in profiles:
        owner = profile["user_id"]
        projects: dict[str, list[dict]] = {}
        user_overrides = []
        for original in profile["overrides"]:
            override = dict(original)
            memory = override.get("memory")
            if memory:
                memory = {**memory, "scope": "user" if memory.get("scope") == "user" else "repo"}
                override["memory"] = memory
            # User-side overlays also suppress their original source if it is
            # reclassified as user-wide later. Repo replacements remain internal.
            user_overrides.append(override)
            source_ids = override.get("source_ids", [])
            by_repo: dict[str, list[str]] = {}
            for source_id in source_ids:
                repos = sources.get((owner, source_id), set())
                if len(repos) == 1:
                    repo = next(iter(repos))
                    if repo:
                        by_repo.setdefault(repo, []).append(source_id)
            repo = memory.get("source_repo") if memory else None
            if (
                memory
                and memory["scope"] == "repo"
                and repo
                and owned_jobs.get((owner, memory.get("source_job_id"))) == repo
                and by_repo.get(repo) == source_ids
            ):
                projects.setdefault(repo, []).append(override)
            else:
                # A user replacement may combine A and B. Each source project
                # receives only its own suppression, never the other repo's data.
                for source_repo, ids in by_repo.items():
                    projects.setdefault(source_repo, []).append({"source_ids": ids, "memory": None})
        conn.execute(
            sa.text(
                "UPDATE agent_user_memories SET overrides = CAST(:overrides AS jsonb) "
                "WHERE user_id = :owner"
            ),
            {"owner": owner, "overrides": json.dumps(user_overrides)},
        )
        for repo, overrides in projects.items():
            conn.execute(
                sa.text(
                    "UPDATE agent_project_memories SET overrides = CAST(:overrides AS jsonb) "
                    "WHERE user_id = :owner AND repo = :repo"
                ),
                {"owner": owner, "repo": repo, "overrides": json.dumps(overrides)},
            )

    for edit in list(conn.execute(sa.text("SELECT * FROM agent_user_memory_edits")).mappings()):
        owner = edit["user_id"]
        repo = owned_jobs.get((owner, edit["source_job_id"]))
        if not repo:
            continue
        profile = next(profile for profile in profiles if profile["user_id"] == owner)
        memory = next(
            (item for item in profile["memories"] if item["id"] == edit["memory_id"]), None
        )
        user_scoped = memory is not None and memory.get("scope") == "user"
        if edit["content"] is not None:
            # Native edits predate explicit scope classification. Keep the saved
            # scope even if globally-worded raw text is now classified user-wide.
            # Sources outside the old account window may enter a project window;
            # reconstruct only a matching owned source key, preserving its kind.
            entry = source_entries.get((owner, edit["source_job_id"], edit["memory_id"]))
            if memory is None and entry is not None:
                memory = {
                    "id": edit["memory_id"],
                    "kind": entry[0],
                    "source_job_id": edit["source_job_id"],
                    "source_repo": repo,
                    "scope": "repo",
                }
            ids = (memory.get("source_ids") or [edit["memory_id"]]) if memory else []
            claimed = {
                source_id for overlay in profile["overrides"] for source_id in overlay["source_ids"]
            }
            if (
                memory
                and entry
                and ids
                and not claimed.intersection(ids)
                and all(sources.get((owner, source_id)) == {repo} for source_id in ids)
            ):
                scoped_source = {
                    **memory,
                    "content": entry[1],
                    "source_job_id": edit["source_job_id"],
                    "scope": "user" if user_scoped else "repo",
                    "edited": False,
                    "user_edited": False,
                    "source_ids": ids,
                }
                overlay = {
                    "source_ids": ids,
                    "memory": scoped_source,
                    "native_source_job_id": edit["source_job_id"],
                }
                conn.execute(
                    sa.text(
                        "UPDATE agent_user_memories SET overrides = overrides || CAST(:overlay AS jsonb) "
                        "WHERE user_id = :owner"
                    ),
                    {"owner": owner, "overlay": json.dumps([overlay])},
                )
                project_overlay = (
                    {
                        "source_ids": ids,
                        "memory": None,
                        "native_source_job_id": edit["source_job_id"],
                    }
                    if user_scoped
                    else overlay
                )
                conn.execute(
                    sa.text(
                        "UPDATE agent_project_memories SET overrides = overrides || CAST(:overlay AS jsonb) "
                        "WHERE user_id = :owner AND repo = :repo"
                    ),
                    {"owner": owner, "repo": repo, "overlay": json.dumps([project_overlay])},
                )
        conn.execute(
            sa.text("""
            INSERT INTO agent_project_memory_edits
                (user_id, repo, memory_id, source_job_id, content, created_at, updated_at)
            VALUES (:owner, :repo, :memory_id, :source_job_id, :content, :created_at, :updated_at)
        """),
            {
                **dict(edit),
                "owner": owner,
                "repo": repo,
                "content": None if user_scoped else edit["content"],
            },
        )
        # Keep no-content markers in both scopes. They carry no exposed data
        # and prevent old user-wide preferences from returning after a split.
        if edit["content"] is not None and not user_scoped:
            conn.execute(
                sa.text(
                    "DELETE FROM agent_user_memory_edits WHERE user_id = :owner AND memory_id = :memory_id"
                ),
                {"owner": owner, "memory_id": edit["memory_id"]},
            )


def _merged_overlays() -> dict[str, list[dict]]:
    """Keep canonical decisions once without preferring refreshed hidden copies."""
    rows = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT user_id, overrides, updated_at, NULL AS repo FROM agent_user_memories
        UNION ALL
        SELECT user_id, overrides, updated_at, repo FROM agent_project_memories
    """)
        )
        .mappings()
    )
    decisions = []
    owners = set()
    for row in rows:
        owners.add(row["user_id"])
        for overlay in row["overrides"]:
            # The previous runtime ignores native guards. Its original edit
            # table restores job-bound corrections after the scopes are merged.
            if overlay.get("native_source_job_id") is not None:
                continue
            memory = overlay.get("memory")
            canonical = memory and (
                (memory.get("scope") == "user" and row["repo"] is None)
                or (memory.get("scope") == "repo" and row["repo"] == memory.get("source_repo"))
            )
            decisions.append(
                {
                    "owner": row["user_id"],
                    "overlay": overlay,
                    "updated_at": row["updated_at"],
                    "priority": 3 if canonical else 2 if memory is None else 1,
                }
            )
    decisions.sort(key=lambda item: (item["priority"], item["updated_at"]), reverse=True)
    result: dict[str, list[dict]] = {owner: [] for owner in owners}
    claimed: dict[str, set[str]] = {}
    for decision in decisions:
        owner = decision["owner"]
        selected = claimed.setdefault(owner, set())
        original = decision["overlay"]
        ids = original.get("source_ids", [])
        remaining = [source_id for source_id in ids if source_id not in selected]
        if not remaining:
            continue
        # A canonical correction/removal that overlaps an older consolidation
        # invalidates the older replacement. Suppress its remaining sources
        # too, so neither stale replacement nor forgotten text returns.
        overlay = original if remaining == ids else {"source_ids": remaining, "memory": None}
        result.setdefault(owner, []).append(overlay)
        selected.update(remaining)
    return result


def upgrade() -> None:
    """Split derived entries while retaining existing privacy controls and fences."""
    if context.is_offline_mode():
        # Offline SQL can install an empty schema (including the legacy copy
        # workflow). Populated overlay attribution requires frozen Python source
        # normalization, so refuse manual application rather than lose edits.
        op.execute("""
            DO $migration$
            BEGIN
                IF EXISTS (SELECT 1 FROM agent_user_memory_edits)
                   OR EXISTS (SELECT 1 FROM agent_user_memories WHERE overrides <> '[]'::jsonb)
                THEN
                    RAISE EXCEPTION '0053_project_memory requires online Alembic upgrade for populated memory edits or overrides';
                END IF;
            END
            $migration$
        """)
    op.execute(
        """
        CREATE TABLE agent_project_memories (
            user_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            portrait JSONB NOT NULL,
            memories JSONB NOT NULL DEFAULT '[]'::jsonb,
            suggestions JSONB NOT NULL DEFAULT '[]'::jsonb,
            overrides JSONB NOT NULL DEFAULT '[]'::jsonb,
            activity_digest TEXT NOT NULL DEFAULT '',
            cleared_message_id BIGINT NOT NULL DEFAULT 0 CHECK (cleared_message_id >= 0),
            cleared_at TIMESTAMPTZ,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, repo)
        )
        """
    )
    op.execute("""
        CREATE TABLE agent_project_memory_edits (
            user_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            content TEXT CHECK (content IS NULL OR char_length(content) BETWEEN 1 AND 500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, repo, memory_id),
            FOREIGN KEY (user_id, repo) REFERENCES agent_project_memories (user_id, repo)
                ON DELETE CASCADE
        )
    """)
    # A forgotten or deleted repository must inherit the old reset fence too:
    # undeleting its thread cannot recover input the owner already forgot.
    # JSON-derived repositories also cover profiles whose source was removed.
    op.execute(
        """
        WITH old_memories AS (
            SELECT u.user_id, m.entry, m.ordinality
            FROM agent_user_memories u
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(u.memories) = 'array'
                     THEN u.memories ELSE '[]'::jsonb END
            ) WITH ORDINALITY AS m(entry, ordinality)
        ), projects AS (
            SELECT user_id, repo FROM agent_threads WHERE repo <> ''
            UNION
            SELECT user_id, entry->>'source_repo' AS repo FROM old_memories
            WHERE jsonb_typeof(entry->'source_repo') = 'string'
              AND entry->>'source_repo' <> ''
            UNION
            SELECT u.user_id, o.entry->'memory'->>'source_repo' AS repo
            FROM agent_user_memories u
            CROSS JOIN LATERAL jsonb_array_elements(u.overrides) AS o(entry)
            WHERE jsonb_typeof(o.entry->'memory'->'source_repo') = 'string'
              AND o.entry->'memory'->>'source_repo' <> ''
            UNION
            SELECT u.user_id, p.repo #>> '{}' AS repo
            FROM agent_user_memories u
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(u.portrait->'projects') = 'array'
                     THEN u.portrait->'projects' ELSE '[]'::jsonb END
            ) AS p(repo)
            WHERE jsonb_typeof(p.repo) = 'string' AND p.repo #>> '{}' <> ''
        )
        INSERT INTO agent_project_memories (
            user_id, repo, enabled, portrait, memories, suggestions, activity_digest,
            cleared_message_id, cleared_at, revision, created_at, updated_at
        )
        SELECT p.user_id, p.repo, COALESCE(u.enabled, TRUE),
               jsonb_build_object(
                   'summary', 'No recent work recorded yet.',
                   'projects', jsonb_build_array(p.repo),
                   'focus_areas', '[]'::jsonb,
                   'preferred_runtime', NULL,
                   'preferred_model', NULL
               ),
               COALESCE((
                   SELECT jsonb_agg(m.entry || '{"scope":"repo"}'::jsonb
                                    ORDER BY m.ordinality)
                   FROM old_memories m
                   WHERE m.user_id = p.user_id AND m.entry->>'source_repo' = p.repo
                     AND COALESCE(m.entry->>'scope', '') <> 'user'
               ), '[]'::jsonb),
               '[]'::jsonb, '', COALESCE(u.cleared_message_id, 0), u.cleared_at,
               COALESCE(u.revision, 0), COALESCE(u.created_at, NOW()),
               COALESCE(u.updated_at, NOW())
        FROM projects p
        LEFT JOIN agent_user_memories u ON u.user_id = p.user_id
        """
    )
    if not context.is_offline_mode():
        _split_overlays()
    # Do not infer owner-wide scope from the wording of disabled legacy entries.
    # Only entries already marked user-wide, or lacking a repository, remain.
    # Preserve aggregate portrait and suggestions; refresh will rebuild them.
    op.execute(
        """
        UPDATE agent_user_memories u
        SET memories = COALESCE((
                SELECT jsonb_agg(m.entry || '{"scope":"user"}'::jsonb
                                 ORDER BY m.ordinality)
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(u.memories) = 'array'
                         THEN u.memories ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS m(entry, ordinality)
                WHERE m.entry->>'scope' = 'user'
                   OR NULLIF(m.entry->>'source_repo', '') IS NULL
            ), '[]'::jsonb),
            activity_digest = ''
        """
    )


def downgrade() -> None:
    """Merge derived entries and use the stricter controls when scopes differ."""
    # The old schema has one switch and reset fence. Taking the disabled state
    # and largest fences preserves forgetting if project controls have diverged.
    if context.is_offline_mode():
        op.execute("""
            DO $migration$
            BEGIN
                IF EXISTS (SELECT 1 FROM agent_user_memories WHERE overrides <> '[]'::jsonb)
                   OR EXISTS (SELECT 1 FROM agent_project_memories WHERE overrides <> '[]'::jsonb)
                THEN
                    RAISE EXCEPTION '0053_project_memory requires online Alembic downgrade for populated memory overrides';
                END IF;
            END
            $migration$
        """)
        overlays = {}
    else:
        overlays = _merged_overlays()
    op.execute(
        """
        WITH controls AS (
            SELECT user_id, bool_and(enabled) AS enabled,
                   max(cleared_message_id) AS cleared_message_id,
                   max(cleared_at) AS cleared_at, max(revision) AS revision,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM (
                SELECT user_id, enabled, cleared_message_id, cleared_at, revision,
                       created_at, updated_at FROM agent_user_memories
                UNION ALL
                SELECT user_id, enabled, cleared_message_id, cleared_at, revision,
                       created_at, updated_at FROM agent_project_memories
            ) all_scopes
            GROUP BY user_id
        ), entries AS (
            SELECT u.user_id, m.entry, 0 AS origin, '' AS repo, m.ordinality
            FROM agent_user_memories u
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(u.memories) = 'array'
                     THEN u.memories ELSE '[]'::jsonb END
            ) WITH ORDINALITY AS m(entry, ordinality)
            UNION ALL
            SELECT p.user_id,
                   m.entry || jsonb_build_object('source_repo', p.repo, 'scope', 'repo'),
                   1 AS origin, p.repo, m.ordinality
            FROM agent_project_memories p
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(p.memories) = 'array'
                     THEN p.memories ELSE '[]'::jsonb END
            ) WITH ORDINALITY AS m(entry, ordinality)
        )
        INSERT INTO agent_user_memories (
            user_id, enabled, portrait, memories, suggestions, activity_digest,
            cleared_message_id, cleared_at, revision, created_at, updated_at
        )
        SELECT c.user_id, c.enabled,
               COALESCE(u.portrait, jsonb_build_object(
                   'summary', 'No recent work recorded yet.',
                   'projects', '[]'::jsonb, 'focus_areas', '[]'::jsonb,
                   'preferred_runtime', NULL, 'preferred_model', NULL
               )),
               COALESCE((
                   SELECT jsonb_agg(e.entry ORDER BY e.origin, e.repo, e.ordinality)
                   FROM entries e WHERE e.user_id = c.user_id
               ), '[]'::jsonb),
               COALESCE(u.suggestions, '[]'::jsonb), '', c.cleared_message_id,
               c.cleared_at, c.revision, c.created_at, c.updated_at
        FROM controls c
        LEFT JOIN agent_user_memories u ON u.user_id = c.user_id
        ON CONFLICT (user_id) DO UPDATE
        SET enabled = EXCLUDED.enabled, memories = EXCLUDED.memories,
            activity_digest = '', cleared_message_id = EXCLUDED.cleared_message_id,
            cleared_at = EXCLUDED.cleared_at, revision = EXCLUDED.revision,
            created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at
        """
    )
    conn = op.get_bind()
    for owner, entries in overlays.items():
        conn.execute(
            sa.text(
                "UPDATE agent_user_memories SET overrides = CAST(:overrides AS jsonb) "
                "WHERE user_id = :owner"
            ),
            {"owner": owner, "overrides": json.dumps(entries)},
        )
    op.execute("""
        INSERT INTO agent_user_memory_edits
            (user_id, memory_id, source_job_id, content, created_at, updated_at)
        SELECT user_id, memory_id, source_job_id, content, created_at, updated_at
        FROM agent_project_memory_edits
        ON CONFLICT (user_id, memory_id) DO NOTHING
    """)
    op.execute("DROP TABLE agent_project_memory_edits")
    op.execute("DROP TABLE agent_project_memories")
