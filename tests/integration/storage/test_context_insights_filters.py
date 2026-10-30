"""Context insights' search, project, day, heatmap and composition, against Postgres.

What only the database can settle:

- **Filters narrow the whole range before it is ranked and paged.** A search
  matches every conversation of the range, not a loaded page; it combines with
  one project (or the explicit no-repository bucket), one UTC day, the sort and
  the keyset.
- **The heatmap and the project groups see the whole range.** The day filter
  does not shrink the heatmap it was chosen from, nor the project filter the
  groups it was chosen from.
- **Composition counts each stored item once.** A retried attempt's content is
  replaced by the kept attempt's, while every attempt's reported requests are
  summed, because they were sent.
- **Ownership.** Another owner's conversation and a deleted one never appear,
  whatever the search.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.storage.test_attempt_ledger import db as db
from tests.integration.storage.test_context_queries import (
    BASE,
    OTHER,
    OWNER,
    _bulk_history,
    _claude,
    _events,
    _thread,
    store as store,
)

from cloud_agent.context_insights import (
    ContextInsightsCursor,
    InsightsFilters,
    compose,
    context_cursor_scope,
)
from cloud_agent.context_profile import CONTEXT_PROFILE_VERSION
from cloud_agent.storage.repositories import context_queries
from cloud_agent.storage.repositories.context_queries import PostgresContextQueries
from cloud_agent_shared.text_tokens import estimate_text_tokens

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

SINCE, UNTIL = BASE - timedelta(days=1), BASE + timedelta(days=5)
DAY = timedelta(days=1)


async def _set(db, thread_id: str, *, repo: str | None, title: str, deleted: bool = False):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET repo = $2, title = $3,"
            " deleted_at = CASE WHEN $4 THEN NOW() END WHERE id = $1",
            thread_id,
            repo,
            title,
            deleted,
        )


async def _retry(db, job_id: str, rows: list[tuple[str, dict]]) -> None:
    """A second attempt of ``job_id`` that replaced the first."""
    async with db.acquire() as conn:
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,
                lease_generation, lease_expires_at, status, started_at, finished_at)
            SELECT id, thread_id, 2, 'ctx-test', 2, NOW(), 'finished', created_at, created_at
            FROM agent_jobs WHERE id = $1
            RETURNING id
            """,
            job_id,
        )
        for seq, (kind, payload) in enumerate(rows, start=1):
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, $3, $4, $5::jsonb)",
                job_id,
                attempt_id,
                seq,
                kind,
                json.dumps(payload),
            )


_STARTED = (
    "lifecycle",
    {
        "phase": "started",
        "context_injections": [
            {"producer": "memory", "items": 1, "chars": 40, "ascii": 40, "cjk": 0},
        ],
    },
)


async def _seed(db, store) -> None:
    web = await _thread(db, store, "athr_web", turns=2, at=BASE)
    await _set(db, "athr_web", repo="acme/web", title="Fix LOGIN redirect")
    await _events(db, web[0], [_STARTED, _claude("w1", 5_000)])
    await _events(db, web[1], [_claude("w2", 9_000)])

    scratch = await _thread(db, store, "athr_tmp", at=BASE + DAY)
    await _set(db, "athr_tmp", repo=None, title="Scratch notes")
    await _events(db, scratch[0], [_claude("t1", 2_000)])

    api = await _thread(db, store, "athr_api", at=BASE + 2 * DAY)
    await _set(db, "athr_api", repo="acme/api", title="Refactor auth")
    # The first attempt's answer was replaced by the retry's; both requests were sent.
    await _events(db, api[0], [_claude("a1", 1_000, text="x" * 4000)])
    await _retry(db, api[0], [_claude("a2", 3_000)])

    stranger = await _thread(db, store, "athr_other", user_id=OTHER, at=BASE)
    await _set(db, "athr_other", repo="acme/web", title="Login for someone else")
    await _events(db, stranger[0], [_claude("o1", 999_000)])

    gone = await _thread(db, store, "athr_gone", at=BASE)
    await _events(db, gone[0], [_claude("g1", 1)])
    await _set(db, "athr_gone", repo="acme/web", title="Login deleted", deleted=True)


async def _read(
    store, filters: InsightsFilters | None = None, *, cursor=None, limit=25, sort="peak"
):
    return await store.context_queries.insights_page(
        user_id=OWNER,
        since=SINCE,
        until=UNTIL,
        sort=sort,
        cursor=cursor,
        limit=limit,
        refresh_attempts=50,
        refresh_events=100_000,
        pending_cap=100,
        filters=filters,
    )


def _threads(result) -> list[str]:
    return [row["thread_id"] for row in result["page"]]


async def test_search_project_and_day_narrow_the_whole_range(db, store):
    await _seed(db, store)
    everything = await _read(store)
    assert everything["pending"] == 0
    assert _threads(everything) == ["athr_web", "athr_api", "athr_tmp"]
    assert [(row["day"], row["turns"]) for row in everything["activity"]] == [
        (BASE.date(), 2),
        ((BASE + DAY).date(), 1),
        ((BASE + 2 * DAY).date(), 1),
    ]
    projects = {row["repo"]: row for row in everything["projects"]}
    assert set(projects) == {"acme/web", "acme/api", None}
    assert projects[None]["conversations"] == 1 and projects["acme/web"]["turns"] == 2

    # Case-insensitive, title or repository; never another owner's or a deleted one.
    login = await _read(store, InsightsFilters(q="login"))
    assert _threads(login) == ["athr_web"]
    acme = await _read(store, InsightsFilters(q="ACME"))
    assert _threads(acme) == ["athr_web", "athr_api"]
    assert acme["summary"]["conversations"] == 2
    # The project groups follow the search but not the project filter.
    one = await _read(store, InsightsFilters(q="acme", repo="acme/api"))
    assert _threads(one) == ["athr_api"]
    assert {row["repo"] for row in one["projects"]} == {"acme/web", "acme/api"}

    temporary = await _read(store, InsightsFilters(temporary=True))
    assert _threads(temporary) == ["athr_tmp"]
    assert temporary["summary"]["conversations"] == 1

    # One day narrows the list and the totals, not the heatmap it was picked from.
    day = await _read(
        store,
        InsightsFilters(
            day_since=BASE + DAY - timedelta(hours=9), day_until=BASE + 2 * DAY - timedelta(hours=9)
        ),
    )
    assert _threads(day) == ["athr_tmp"]
    assert len(day["activity"]) == 3
    assert day["summary"]["turns"] == 1

    nothing = await _read(store, InsightsFilters(q="login", temporary=True))
    assert nothing["page"] == [] and nothing["summary"]["conversations"] == 0


async def test_filtered_pages_continue_without_overlap(db, store):
    await _seed(db, store)
    filters = InsightsFilters(q="acme")
    await _read(store, filters)  # profile everything first
    first = await _read(store, filters, limit=1)
    assert len(first["page"]) == 2  # one listed, one more beyond it
    last = first["page"][0]
    cursor = ContextInsightsCursor(
        scope=context_cursor_scope("peak", "7d", filters),
        since=SINCE,
        until=UNTIL,
        number=int(last["sort_number"]),
        at=last["last_turn_at"],
        thread_id=last["thread_id"],
    )
    rest = await _read(store, filters, cursor=cursor, limit=1)
    assert _threads(rest) == ["athr_api"]
    assert "activity" not in rest and "projects" not in rest
    # A later page reads only its own conversations' composition.
    assert [row["thread_id"] for row in rest["composition"]] == ["athr_api"]


async def test_composition_counts_each_item_once_and_every_request(db, store):
    await _seed(db, store)
    result = await _read(store)
    rows = {row["thread_id"]: row for row in result["composition"]}
    assert set(rows) == {"athr_web", "athr_tmp", "athr_api"}

    web = compose([rows["athr_web"]])
    prompt = estimate_text_tokens("p" * 400)
    shares = {share.key: share.tokens for share in web.recorded.categories}
    assert shares["turn_prompts"] == 2 * prompt
    assert shares["platform_context"] == 10  # 40 ASCII characters of memory
    assert shares["assistant"] == 2 * estimate_text_tokens("answer")
    assert web.provider.requests == 2
    assert web.provider.prompt_tokens == 5_010 + 9_010

    api = compose([rows["athr_api"]])
    shares = {share.key: share.tokens for share in api.recorded.categories}
    # The kept attempt's answer only: the retried 4,000 characters are gone.
    assert shares["assistant"] == estimate_text_tokens("answer")
    # Both attempts' requests were sent, so both are summed.
    assert api.provider.requests == 2
    assert api.provider.prompt_tokens == 1_010 + 3_010
    assert api.recorded.turns == 1 and api.recorded.turns_profiled == 1

    total = compose(result["composition"])
    assert total.recorded.turns == 4
    assert total.provider.prompt_tokens == 5_010 + 9_010 + 2_010 + 1_010 + 3_010


async def test_composition_estimates_each_prompt_as_the_shared_estimator(db, store):
    # ASCII prompts take a shortcut past the two character-class rewrites;
    # every kind of prompt must still read as the Python estimator counts it.
    prompts = {
        "ascii": "Fix the login redirect, then run make check.",
        "cjk": "先看测试再改代码\uff0c然后提交。",  # a full-width comma, too
        "latin": "Résumé of the naïve café rewrite",
        "emoji": "ship it 🚀✨ when green",
        "mixed": "Review PR 42: 先 run tests, é then 한국어 ok",
        "empty": "",
    }
    for name, prompt in prompts.items():
        [job] = await _thread(db, store, f"athr_prompt_{name}")
        async with db.acquire() as conn:
            await conn.execute("UPDATE agent_jobs SET task_prompt = $2 WHERE id = $1", job, prompt)
    result = await _read(store)
    rows = {row["thread_id"]: row for row in result["composition"]}
    for name, prompt in prompts.items():
        assert rows[f"athr_prompt_{name}"]["prompt_tokens"] == estimate_text_tokens(prompt), name


class _SettingsSeen:
    """The connection, noting the JIT and plan settings each insights statement ran with."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.seen: dict[str, set[tuple[str, str]]] = {}

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def _note(self, sql: str) -> None:
        tag = re.search(r"/\* (context_insights_\w+) \*/", sql)
        if tag:
            jit = await self.conn.fetchval("SHOW jit")
            plans = await self.conn.fetchval("SHOW plan_cache_mode")
            self.seen.setdefault(tag.group(1), set()).add((jit, plans))

    async def fetch(self, sql, *args, **kwargs):
        await self._note(sql)
        return await self.conn.fetch(sql, *args, **kwargs)

    async def fetchrow(self, sql, *args, **kwargs):
        await self._note(sql)
        return await self.conn.fetchrow(sql, *args, **kwargs)

    async def fetchval(self, sql, *args, **kwargs):
        await self._note(sql)
        return await self.conn.fetchval(sql, *args, **kwargs)


async def test_every_insights_statement_runs_without_jit_on_any_pool(db, store):
    # The service's pool opens sessions with JIT off; the dbtest pools keep
    # the server's default. The read must run the same way on either.
    await _seed(db, store)
    async with db.acquire() as conn:
        before = (await conn.fetchval("SHOW jit"), await conn.fetchval("SHOW plan_cache_mode"))
        watched = _SettingsSeen(conn)
        result = await PostgresContextQueries(_OneConnection(watched)).insights_page(
            user_id=OWNER,
            since=SINCE,
            until=UNTIL,
            sort="peak",
            cursor=None,
            limit=25,
            refresh_attempts=50,
            refresh_events=100_000,
            pending_cap=100,
            filters=InsightsFilters(),
        )
        assert len(result["composition"]) == 3
        assert set(watched.seen) >= {
            "context_insights_stale",
            "context_insights_page",
            "context_insights_summary",
            "context_insights_pending",
            "context_insights_activity",
            "context_insights_projects",
            "context_insights_composition",
        }
        assert all(seen == {("off", "force_custom_plan")} for seen in watched.seen.values())
        # Nothing is left on the pooled connection for the next caller.
        assert (await conn.fetchval("SHOW jit"), await conn.fetchval("SHOW plan_cache_mode")) == (
            before
        )


class _OneConnection:
    """A pool of one connection, so every read reuses its prepared statements."""

    def __init__(self, conn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_repeated_reads_stay_planned_for_their_filters(db, store):
    await _bulk_history(db, store, user_id=OWNER, runs=12_000, span_days=900)
    async with db.acquire() as conn:
        # Some of the runs were profiled, as a long history is part way through.
        await conn.execute(
            """
            INSERT INTO agent_attempt_context_profiles (attempt_id, event_seq, reducer_version,
                main_requests, subagent_requests, peak_prompt_tokens, latest_prompt_tokens,
                latest_observed_at, context_window, compactions, profile)
            SELECT a.id, 1, $2::int, 1, 0, 1000, 1000, a.started_at, NULL, 0, jsonb_build_object(
                'v', $2::int, 'events', '[]'::jsonb,
                'content', jsonb_build_object('categories', jsonb_build_object(
                    'assistant', jsonb_build_object('tokens', 5, 'chars', 20, 'items', 1))),
                'totals', jsonb_build_object('main', jsonb_build_object(
                    'requests', 1, 'prompt_tokens', 1000)))
            FROM agent_attempts a JOIN agent_jobs j ON j.id = a.job_id
            WHERE j.user_id = $1 AND a.id % 33 = 0
            """,
            OWNER,
            CONTEXT_PROFILE_VERSION,
        )
        await conn.execute("ANALYZE")
        profiled = await conn.fetchval("SELECT COUNT(*) FROM agent_attempt_context_profiles")
    first = await store.owner_usage.first_record_at(OWNER)
    since = first.replace(hour=0, minute=0, second=0, microsecond=0)
    async with db.acquire() as conn:
        queries = PostgresContextQueries(_OneConnection(conn))
        # Postgres may switch a prepared statement to a generic plan after
        # five runs; the sixth and seventh are the reads that timed out.
        for _ in range(7):
            result = await queries.insights_page(
                user_id=OWNER,
                since=since,
                until=datetime.now(UTC),
                sort="recent",
                cursor=None,
                limit=25,
                refresh_attempts=40,
                refresh_events=40_000,
                pending_cap=10_000,
                filters=InsightsFilters(),
            )
            assert len(result["composition"]) == 12_000
            assert sum(row["assistant"] for row in result["composition"]) == 5 * profiled
            # Runs that stored no events have nothing to read: they count as read.
            assert compose(result["composition"]).recorded.turns_profiled == 12_000
        # Nothing is left on the pooled connection for the next caller.
        assert await conn.fetchval("SHOW plan_cache_mode") == "auto"
        assert await conn.fetchval("SHOW statement_timeout") == "0"


def _row_visits(node: dict) -> int:
    """Rows every node of an executed plan produced, over all of its loops."""
    own = int(node.get("Actual Rows", 0)) * int(node.get("Actual Loops", 1))
    return own + sum(_row_visits(child) for child in node.get("Plans", []))


#: The indexes of ``agent_threads`` that lead with its key.
_THREAD_KEY_INDEXES = {"agent_threads_pkey", "uq_agent_threads_owner"}


def _repeated_thread_scans(node: dict) -> list[dict]:
    """Every scan of ``agent_threads`` an executed plan ran more than once."""
    found = [node] if node.get("Relation Name") == "agent_threads" else []
    found = found if int(node.get("Actual Loops", 1)) > 1 else []
    return found + [
        scan for child in node.get("Plans", []) for scan in _repeated_thread_scans(child)
    ]


async def test_composition_stays_linear_whatever_the_planner_knows(db, store):
    # A range's runs are often underestimated: freshly written, or one owner's
    # share of the table. With a row estimate of one, the composition once
    # joined whole-range sets with a nested loop that rescanned one for every
    # row of the other; CI timed out when only the attempts had statistics.
    runs = 3_000
    await _bulk_history(db, store, user_id=OWNER, runs=runs, span_days=400)
    async with db.acquire() as conn:
        # Every fifth run was retried, and every attempt has a stored profile,
        # so both the kept attempt and every attempt's requests have rows.
        await conn.execute(
            """
            INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,
                lease_generation, lease_expires_at, status, started_at, finished_at)
            SELECT a.job_id, a.thread_id, 2, 'bulk', 2, NOW(), 'finished',
                   a.started_at, a.finished_at
            FROM agent_attempts a WHERE a.id % 5 = 0
            """
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_context_profiles (attempt_id, event_seq, reducer_version,
                main_requests, subagent_requests, compactions, profile)
            SELECT a.id, 1, $1::int, 1, 0, 0, jsonb_build_object(
                'v', $1::int, 'events', '[]'::jsonb,
                'content', jsonb_build_object('categories', jsonb_build_object(
                    'assistant', jsonb_build_object('tokens', a.attempt_no, 'chars', 4,
                                                    'items', 1))),
                'totals', jsonb_build_object('main', jsonb_build_object(
                    'requests', 1, 'prompt_tokens', 1000)))
            FROM agent_attempts a
            """,
            CONTEXT_PROFILE_VERSION,
        )
        retried = await conn.fetchval("SELECT COUNT(*) FROM agent_attempts WHERE attempt_no = 2")
        first = await store.owner_usage.first_record_at(OWNER)
        since = first.replace(hour=0, minute=0, second=0, microsecond=0)
        params = [
            OWNER,
            since,
            datetime.now(UTC),
            CONTEXT_PROFILE_VERSION,
            *InsightsFilters().params(),
            None,
        ]
        tables = ("agent_threads", "agent_jobs", "agent_attempts", "agent_attempt_context_profiles")
        cases = (
            # What a test-db-local run left: the conversations vacuumed and
            # counted (all-visible, so index-only scans look free) but no
            # column statistics, and the runs never counted.
            (("agent_threads",), ()),
            ((), ()),
            ((), ("agent_attempts", "agent_attempt_context_profiles")),
            ((), tables),
        )
        for vacuumed, known in cases:
            for table in vacuumed:
                await conn.execute(f"VACUUM ANALYZE {table}")
            # Statistics survive the reset's TRUNCATE; forget them, then
            # gather only those this case knows.
            await conn.execute(
                "DELETE FROM pg_statistic WHERE starelid = ANY($1::regclass[])", list(tables)
            )
            for table in known:
                await conn.execute(f"ANALYZE {table}")
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '30s'")
                await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
                plan = await conn.fetchval(
                    "EXPLAIN (ANALYZE, FORMAT JSON) " + context_queries._COMPOSITION_SQL, *params
                )
                rows = await conn.fetch(context_queries._COMPOSITION_SQL, *params)
            [explained] = json.loads(plan)
            case = (vacuumed, known)
            # A few index lookups per run; the rescanning plan visited about
            # a thousand rows per run here, and more the longer the history.
            assert _row_visits(explained["Plan"]) < 40 * runs, case
            # Each run's conversation is read by its key. Scanning another
            # index of it for every run shows as one row per loop while it
            # walks the whole index (1.1 of 1.2 s on 10,250 runs).
            for scan in _repeated_thread_scans(explained["Plan"]):
                assert scan.get("Index Name") in _THREAD_KEY_INDEXES, (case, scan.get("Index Name"))
                assert "Index Cond" in scan, (case, scan)
            total = compose(rows)
            assert total.recorded.turns == total.recorded.turns_profiled == runs
            # Each turn's content is its kept (latest) attempt's...
            assert sum(row["assistant"] for row in rows) == runs + retried
            # ...and every attempt's requests count, the retried ones too.
            assert total.provider.requests == runs + retried
            assert total.provider.prompt_tokens == 1000 * (runs + retried)


async def test_dna_bands_are_the_items_each_request_carried(db, store):
    [job] = await _thread(db, store, "athr_dna")
    await _events(
        db,
        job,
        [
            _claude("d1", 1_000, text="a" * 400),
            ("tool_use", {"name": "Read", "id": "t1", "input": {"p": "x"}}),
            ("tool_result", {"tool_use_id": "t1", "content": "r" * 800}),
            _claude("d2", 2_000, text="b" * 40),
        ],
    )
    view = await store.context_queries.thread_context(job_id=job, user_id=OWNER)
    assert view is not None
    bands = await store.context_queries.dna_bands(job_id=job, user_id=OWNER, first=0, last=1)
    assert bands is not None and [r.index for r in bands.requests] == [0, 1]
    first, second = bands.requests
    assert [item[1] for item in first.items] == ["user_messages"]  # its own answer is not in it
    keys = [item[0] for item in second.items]
    # Item identity is the stored event: the earlier answer and the tool pair.
    assert keys[0] == f"p{job}" and len(keys) == 4
    assert set(keys[1:]).isdisjoint(item[0] for item in first.items)
    assert {item[1] for item in second.items} >= {"assistant", "tool_calls", "tool_results"}
    stranger = await store.context_queries.dna_bands(job_id=job, user_id=OTHER, first=0, last=1)
    assert stranger is None
