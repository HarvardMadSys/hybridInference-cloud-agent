"""Web Search validates, routes, and bounds every supported provider call."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import httpx
import pytest

from cloud_agent import settings, web_search


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str = "auto",
    serper_key: str = "",
    brave_key: str = "",
    parallel_key: str = "",
) -> None:
    values = {
        web_search.PROVIDER_SETTING: provider,
        web_search.SERPER_API_KEY_SETTING: serper_key,
        web_search.BRAVE_API_KEY_SETTING: brave_key,
        web_search.PARALLEL_API_KEY_SETTING: parallel_key,
    }
    monkeypatch.setattr(
        web_search,
        "text_setting",
        lambda key, fallback="": values.get(key, fallback),
    )


def test_validated_options_translate_to_serper_and_brave_contracts() -> None:
    request = web_search.validate(
        {
            "query": "  current Python release  ",
            "num_results": 4,
            "country": "US",
            "language": "EN",
            "freshness": "week",
        }
    )

    assert web_search.request_body(request, "serper") == {
        "q": "current Python release",
        "num": 4,
        "gl": "us",
        "hl": "en",
        "tbs": "qdr:w",
    }
    assert web_search.request_body(request, "brave") == {
        "q": "current Python release",
        "count": 4,
        "country": "US",
        "search_lang": "en",
        "freshness": "pw",
        "safesearch": "strict",
        "result_filter": ["web", "faq"],
        "text_decorations": False,
        "extra_snippets": False,
        "summary": False,
        "enable_rich_callback": False,
        "include_fetch_metadata": False,
    }


def test_parallel_request_is_bounded_and_translates_location_and_freshness() -> None:
    request = web_search.validate(
        {
            "query": "current Python release",
            "num_results": 4,
            "country": "US",
            "freshness": "week",
        },
        provider="parallel",
    )

    assert web_search.request_body(
        request,
        "parallel",
        today=date(2026, 10, 11),
    ) == {
        "objective": "current Python release",
        "search_queries": ["current Python release"],
        "mode": "fast",
        "max_chars_total": 4800,
        "advanced_settings": {
            "max_results": 4,
            "excerpt_settings": {"max_chars_per_result": 1200},
            "location": "us",
            "source_policy": {"after_date": "2026-10-04"},
        },
    }


def test_multipart_language_codes_are_preserved_for_brave() -> None:
    request = web_search.validate(
        {"query": "current news", "language": "ZH-HANS"},
        provider="brave",
    )

    assert request.language == "zh-hans"
    assert web_search.request_body(request, "brave")["search_lang"] == "zh-hans"


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "current news", "country": "SG"},
        {"query": "current news", "country": "ZZ"},
        {"query": "current news", "language": "en-us"},
    ],
)
def test_brave_rejects_unsupported_country_and_language_before_request(arguments) -> None:
    with pytest.raises(web_search.WebSearchInputError, match="not supported by Brave Search"):
        web_search.validate(arguments, provider="brave")


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x" * 201},
        {"query": "current news", "country": "SG"},
        {"query": "current news", "language": "en"},
    ],
)
def test_parallel_rejects_unsupported_arguments_before_request(arguments) -> None:
    with pytest.raises(web_search.WebSearchInputError, match="Parallel Search"):
        web_search.validate(arguments, provider="parallel")


def test_serper_keeps_its_own_country_contract() -> None:
    request = web_search.validate(
        {"query": "current news", "country": "SG"},
        provider="serper",
    )

    assert web_search.request_body(request, "serper")["gl"] == "sg"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "x" * 601},
        {"query": " ".join(["word"] * 76)},
        {"query": "x", "num_results": True},
        {"query": "x", "num_results": 11},
        {"query": "x", "country": "USA"},
        {"query": "x", "freshness": "hour"},
        {"query": "x", "language": "english"},
        {"query": "x", "freshness": "forever"},
        {"query": "x", "unexpected": "value"},
    ],
)
def test_validate_rejects_invalid_or_unbounded_arguments(arguments) -> None:
    with pytest.raises(web_search.WebSearchInputError):
        web_search.validate(arguments)


@pytest.mark.parametrize(
    ("provider", "serper_key", "brave_key", "parallel_key", "expected"),
    [
        ("auto", "", "", "", None),
        ("auto", "serper-secret", "", "", "serper"),
        ("auto", "", "brave-secret", "", "brave"),
        ("auto", "", "", "parallel-secret", "parallel"),
        ("auto", "serper-secret", "brave-secret", "parallel-secret", "serper"),
        ("auto", "", "brave-secret", "parallel-secret", "brave"),
        ("serper", "serper-secret", "brave-secret", "parallel-secret", "serper"),
        ("brave", "serper-secret", "brave-secret", "parallel-secret", "brave"),
        ("parallel", "serper-secret", "brave-secret", "parallel-secret", "parallel"),
        ("serper", "", "brave-secret", "parallel-secret", None),
        ("brave", "serper-secret", "", "parallel-secret", None),
        ("parallel", "serper-secret", "brave-secret", "", None),
        ("unknown", "serper-secret", "brave-secret", "parallel-secret", None),
    ],
)
def test_provider_selection_is_deterministic_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    serper_key: str,
    brave_key: str,
    parallel_key: str,
    expected: str | None,
) -> None:
    _configure(
        monkeypatch,
        provider=provider,
        serper_key=serper_key,
        brave_key=brave_key,
        parallel_key=parallel_key,
    )

    assert web_search.selected_provider() == expected
    assert web_search.configured() is (expected is not None)


async def test_serper_uses_only_its_header_and_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "serper-test-secret"
    _configure(monkeypatch, provider="serper", serper_key=secret, brave_key="unused-brave")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["serper_key"] = request.headers.get("x-api-key")
        seen["brave_key"] = request.headers.get("x-subscription-token")
        seen["api_version"] = request.headers.get("api-version")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Result 1",
                        "link": "https://example.com/1",
                        "snippet": "useful snippet",
                        "position": 1,
                        "providerOnly": secret,
                    },
                    {
                        "title": "Unsafe URL is dropped",
                        "link": "https://user:password@example.com/private",
                    },
                    {
                        "title": "Result 2",
                        "link": "https://example.com/2",
                        "snippet": "another snippet",
                        "position": 3,
                    },
                ],
                "answerBox": {"answer": "42", "providerOnly": secret},
                "peopleAlsoAsk": [{"question": "Why?", "snippet": "Because."}],
                "relatedSearches": [{"query": "another query", "unknown": secret}],
                "providerOnly": secret,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search.search(
            {"query": "answer", "num_results": 3},
            client=client,
        )

    assert seen == {
        "url": web_search.SERPER_API_URL,
        "serper_key": secret,
        "brave_key": None,
        "api_version": None,
        "body": {"q": "answer", "num": 3},
    }
    assert result == {
        "provider": "serper",
        "query": "answer",
        "organic": [
            {
                "title": "Result 1",
                "link": "https://example.com/1",
                "snippet": "useful snippet",
                "position": 1,
            },
            {
                "title": "Result 2",
                "link": "https://example.com/2",
                "snippet": "another snippet",
                "position": 3,
            },
        ],
        "answer_box": {"answer": "42"},
        "people_also_ask": [{"question": "Why?", "snippet": "Because."}],
        "related_searches": [{"query": "another query"}],
    }
    assert secret not in json.dumps(result)


async def test_brave_uses_only_its_header_and_normalizes_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "brave-test-secret"
    _configure(monkeypatch, provider="brave", serper_key="unused-serper", brave_key=secret)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["serper_key"] = request.headers.get("x-api-key")
        seen["brave_key"] = request.headers.get("x-subscription-token")
        seen["api_version"] = request.headers.get("api-version")
        seen["cache_control"] = request.headers.get("cache-control")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave result",
                            "url": "https://example.com/brave",
                            "description": "independent index",
                            "page_age": "2026-10-10",
                            "providerOnly": secret,
                        },
                        {
                            "title": "Unsafe URL is dropped",
                            "url": "javascript:alert(1)",
                        },
                    ]
                },
                "faq": {
                    "results": [
                        {
                            "question": "What is Brave Search?",
                            "title": "FAQ",
                            "answer": "A search engine.",
                            "url": "https://example.com/faq",
                        }
                    ]
                },
                "query": {"related_queries": ["Brave API", "Brave index"]},
                "providerOnly": secret,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search.search(
            {
                "query": "brave search",
                "num_results": 2,
                "country": "gb",
                "language": "en",
                "freshness": "day",
            },
            client=client,
        )

    assert seen == {
        "url": web_search.BRAVE_API_URL,
        "method": "POST",
        "serper_key": None,
        "brave_key": secret,
        "api_version": "2023-01-01",
        "cache_control": "no-cache",
        "body": {
            "q": "brave search",
            "count": 2,
            "country": "GB",
            "search_lang": "en",
            "freshness": "pd",
            "safesearch": "strict",
            "result_filter": ["web", "faq"],
            "text_decorations": False,
            "extra_snippets": False,
            "summary": False,
            "enable_rich_callback": False,
            "include_fetch_metadata": False,
        },
    }
    assert result == {
        "provider": "brave",
        "query": "brave search",
        "organic": [
            {
                "title": "Brave result",
                "link": "https://example.com/brave",
                "snippet": "independent index",
                "date": "2026-10-10",
                "position": 1,
            }
        ],
        "people_also_ask": [
            {
                "question": "What is Brave Search?",
                "title": "FAQ",
                "snippet": "A search engine.",
                "link": "https://example.com/faq",
            }
        ],
        "related_searches": [{"query": "Brave API"}, {"query": "Brave index"}],
    }
    assert secret not in json.dumps(result)


async def test_parallel_uses_only_its_header_and_normalizes_bounded_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "parallel-test-secret"
    _configure(
        monkeypatch,
        provider="parallel",
        serper_key="unused-serper",
        brave_key="unused-brave",
        parallel_key=secret,
    )
    seen: dict[str, Any] = {}
    long_excerpt = "x" * 1300

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["api_key"] = request.headers.get("x-api-key")
        seen["brave_key"] = request.headers.get("x-subscription-token")
        seen["api_version"] = request.headers.get("api-version")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "search_id": "ignored",
                "session_id": "ignored",
                "results": [
                    {
                        "title": "Parallel result",
                        "url": "https://example.com/parallel",
                        "publish_date": "2026-10-10",
                        "excerpts": ["first excerpt", long_excerpt, None],
                        "providerOnly": secret,
                    },
                    {
                        "title": "Unsafe URL is dropped",
                        "url": "https://user:password@example.com/private",
                        "excerpts": ["must not survive"],
                    },
                    "malformed",
                    {
                        "title": None,
                        "url": "https://example.com/second",
                        "publish_date": None,
                        "excerpts": [None, "second excerpt"],
                    },
                ],
                "warnings": [{"message": secret}],
                "usage": [{"name": secret, "count": 1}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search.search(
            {"query": "parallel search", "num_results": 4, "country": "gb"},
            client=client,
        )

    assert seen == {
        "url": web_search.PARALLEL_API_URL,
        "method": "POST",
        "api_key": secret,
        "brave_key": None,
        "api_version": None,
        "body": {
            "objective": "parallel search",
            "search_queries": ["parallel search"],
            "mode": "fast",
            "max_chars_total": 4800,
            "advanced_settings": {
                "max_results": 4,
                "excerpt_settings": {"max_chars_per_result": 1200},
                "location": "gb",
            },
        },
    }
    assert result == {
        "provider": "parallel",
        "query": "parallel search",
        "organic": [
            {
                "title": "Parallel result",
                "link": "https://example.com/parallel",
                "snippet": ("first excerpt\n\n" + long_excerpt)[:1200],
                "date": "2026-10-10",
                "position": 1,
            },
            {
                "link": "https://example.com/second",
                "snippet": "second excerpt",
                "position": 4,
            },
        ],
    }
    assert secret not in json.dumps(result)


async def test_selected_provider_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        provider="parallel",
        serper_key="serper-must-not-be-used",
        brave_key="brave-must-not-be-used",
        parallel_key="parallel-selected",
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(503, text="do not expose provider details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "provider_unavailable"
    assert seen == [web_search.PARALLEL_API_URL]


@pytest.mark.parametrize(
    ("provider", "secret", "url", "header"),
    [
        ("serper", "serper-redirect-secret", web_search.SERPER_API_URL, "x-api-key"),
        (
            "brave",
            "brave-redirect-secret",
            web_search.BRAVE_API_URL,
            "x-subscription-token",
        ),
        (
            "parallel",
            "parallel-redirect-secret",
            web_search.PARALLEL_API_URL,
            "x-api-key",
        ),
    ],
)
async def test_search_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    secret: str,
    url: str,
    header: str,
) -> None:
    _configure(
        monkeypatch,
        provider=provider,
        serper_key=secret if provider == "serper" else "",
        brave_key=secret if provider == "brave" else "",
        parallel_key=secret if provider == "parallel" else "",
    )
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get(header)))
        if request.url.host == "attacker.invalid":
            raise AssertionError("the provider credential followed a redirect")
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "unexpected_redirect"
    assert seen == [(url, secret)]


async def test_search_stops_streaming_after_the_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, serper_key="configured-key")
    monkeypatch.setattr(web_search, "_MAX_RESPONSE_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=b"x" * 33)
        del response.headers["content-length"]
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "response_too_large"


async def test_search_rejects_a_key_reflected_in_an_allowed_provider_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "brave-reflected-secret"
    _configure(monkeypatch, provider="brave", brave_key=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": f"echoed {secret}",
                            "url": "https://example.com",
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "unsafe_response"
    assert secret not in str(caught.value)


async def test_parallel_rejects_a_key_reflected_in_an_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "parallel-reflected-secret"
    _configure(monkeypatch, provider="parallel", parallel_key=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.com",
                        "excerpts": [f"echoed {secret}"],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "unsafe_response"
    assert secret not in str(caught.value)


async def test_search_has_an_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, brave_key="configured-key")
    monkeypatch.setattr(web_search, "_REQUEST_TIMEOUT_S", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "timeout"


async def test_search_refuses_to_call_without_a_selected_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, provider="brave", serper_key="other-provider-key")

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unconfigured search must not make an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(web_search.WebSearchError, match="not configured") as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == "not_configured"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "credential_rejected"),
        (403, "credential_rejected"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
async def test_provider_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    kind: str,
) -> None:
    secret = "must-never-appear"
    _configure(monkeypatch, provider="brave", brave_key=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"provider echoed {secret}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == kind
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "credential_rejected"),
        (403, "credential_rejected"),
        (402, "quota_exhausted"),
        (408, "timeout"),
        (422, "request_rejected"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
async def test_parallel_provider_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    kind: str,
) -> None:
    secret = "parallel-must-never-appear"
    _configure(monkeypatch, provider="parallel", parallel_key=secret)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, text=f"provider echoed {secret}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(web_search.WebSearchError) as caught:
            await web_search.search({"query": "anything"}, client=client)

    assert caught.value.kind == kind
    assert secret not in str(caught.value)
    assert seen == [web_search.PARALLEL_API_URL]


def test_admin_registry_and_tool_schema_are_safe() -> None:
    provider = settings.SETTINGS_BY_KEY[web_search.PROVIDER_SETTING]
    serper = settings.SETTINGS_BY_KEY[web_search.SERPER_API_KEY_SETTING]
    brave = settings.SETTINGS_BY_KEY[web_search.BRAVE_API_KEY_SETTING]
    parallel = settings.SETTINGS_BY_KEY[web_search.PARALLEL_API_KEY_SETTING]
    definition = web_search.TOOL_DEFINITION

    assert provider.choices == ("auto", "serper", "brave", "parallel")
    with pytest.raises(ValueError, match="Invalid value"):
        provider.validate("another-provider")
    assert serper.secret is True
    assert serper.env == "SERPER_API_KEY"
    assert brave.secret is True
    assert brave.env == "BRAVE_SEARCH_API_KEY"
    assert parallel.secret is True
    assert parallel.env == "PARALLEL_API_KEY"
    assert definition["name"] == "web_search"
    assert "Parallel Search" in definition["description"]
    assert definition["inputSchema"]["properties"]["query"]["maxLength"] == 600
    assert definition["inputSchema"]["properties"]["num_results"]["maximum"] == 10
    assert definition["inputSchema"]["properties"]["freshness"]["enum"] == [
        "day",
        "week",
        "month",
        "year",
    ]
    assert definition["inputSchema"]["additionalProperties"] is False
