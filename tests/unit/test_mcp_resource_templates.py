"""URI template operators, encoded boundaries and bounded matching."""

import pytest
from uritemplate import URITemplate

from cloud_agent.mcp.resource_template_values import TemplateMatchLimit
from cloud_agent.mcp.resource_templates import matches_resource_template
from cloud_agent.mcp.resources import resource_uri, scope_resource_result, split_resource_uri


@pytest.mark.parametrize(
    "template,uri",
    [
        ("repo://files/{path}", "repo://files/README%20notes.md"),
        ("repo://files/{path}", "repo://files/a%2Fb"),
        ("repo://files/{+path}", "repo://files/a/b?revision=main"),
        ("repo://files{/path*}", "repo://files/a/b/c"),
        ("repo://files{/path*}/end", "repo://files/a/b/end"),
        ("repo://search{?q,page}", "repo://search?page=2"),
        ("repo://search{?q,page}", "repo://search?q=a%26b&page=2"),
        ("repo://search{?q,page}", "repo://search"),
        ("repo://search{?q}", "repo://search?q="),
        ("repo://search{?tags*}", "repo://search?tags=a&tags=b"),
        ("repo://search{?options*}", "repo://search?sort=date&limit=2"),
        ("repo://search?q=one{&page}", "repo://search?q=one&page=2"),
        ("repo://file{;mode,empty}", "repo://file;mode=text;empty"),
        ("repo://file{.ext}", "repo://file.md"),
        ("repo://file{#section}", "repo://file#overview"),
        ("repo://{x,y}", "repo://one,two"),
        ("repo://{name:2}", "repo://%E4%B8%AD%E6%96%87"),
        ("repo://{name:2}/{name}", "repo://ab/abcd"),
        ("repo://{name:2}/{name:3}/{name:4}", "repo://ab/abc/abcd"),
        ("repo://{name}/{name}", "repo://a/a"),
        ("repo://café/{name}", "repo://caf%C3%A9/file"),
        ("repo://{x:1}{y}", "repo://ab"),
        ("repo://{x}{x}", "repo://aa"),
        ("repo://{+x}/{+x}", "repo://a/b/a/b"),
        ("repo://{x*}/{x}", "repo://a,b/a,b"),
        ("repo://{x:4}/{x:2}/{x}", "repo://abcd/ab/abcde"),
        ("repo://{x}{?x}", "repo://?x="),
        ("repo://literal%2f/{x}/{x}", "repo://literal%2F/a%2fb/a%2Fb"),
        ("repo://search{?path%2f}", "repo://search?path%2F=a"),
    ],
)
def test_resource_template_expansions(template, uri):
    assert matches_resource_template(template, uri)


@pytest.mark.parametrize(
    "template,uri",
    [
        ("repo://files/{path}", "repo://files/a/b"),
        ("repo://files/{path}", "repo://files/a?secret=1"),
        ("repo://files/{path}", "repo://other/a"),
        ("repo://files/{path}", "repo://files/a%ZZ"),
        ("repo://files/{path}", "repo://files/a b"),
        ("repo://search{?q}", "repo://search?admin=1"),
        ("repo://search{?q}", "repo://search?q=x&admin=1"),
        ("repo://{name:2}", "repo://abc"),
        ("repo://{name}/{name}", "repo://a/b"),
        ("repo://{name:2}/{name}", "repo://xy/abcd"),
        ("repo://{name:2}/{name:3}/{name:4}", "repo://ab/abc/abXY"),
        ("repo://{path}", "repo://\ud800"),
        ("repo://{broken", "repo://x"),
        ("repo://{name..field}", "repo://x"),
        ("repo://{path:0}", "repo://x"),
        ("repo://{}", "repo://x"),
        ("repo://{path}{", "repo://x"),
        ("repo://{x}/{x*}", "repo://a/b"),
        ("repo://{x*}/{x}", "repo://a/b"),
        ("repo://{x}{?x}", "repo://a"),
        ("repo://{x}{?x}", "repo://?x=a"),
        ("repo://{x*}{?x}", "repo://a,b?x=a"),
        ("repo://search{?options*}", "repo://search?sort=1&sort=2"),
    ],
)
def test_resource_templates_do_not_widen_literal_or_encoding_boundaries(template, uri):
    assert not matches_resource_template(template, uri)


@pytest.mark.timeout(2)
def test_adversarial_templates_and_uris_are_bounded():
    template = "repo:" + "{/items*}" * 30 + "/end"
    assert not matches_resource_template(template, "repo:" + "/a,b,c" * 500 + "/no")
    assert not matches_resource_template("x" * 3000, "x")
    assert not matches_resource_template("repo://{+path}", "repo://" + "x" * 9000)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a",
        "a/b",
        "%2F",
        "中",
        ["a", "b"],
        {"z": "one", "a": "two"},
        ["a,b", "c"],
        ["a.b", "c"],
        {"semi": ";", "dot": ".", "comma": ","},
        {"key": "a=b,c=d"},
        {"a=b": "c,d"},
    ],
)
@pytest.mark.parametrize("operator", ["", "+", "#", ".", "/", ";", "?", "&"])
def test_repeated_variables_recreate_the_same_scalar_list_or_map(value, operator):
    template = "repo:" + "{" + operator + "x*}" + "/again/{x}"
    uri = URITemplate(template).expand({"x": value})
    assert matches_resource_template(template, uri)


@pytest.mark.timeout(2)
def test_ambiguous_search_exhaustion_is_distinct_from_a_non_match(monkeypatch):
    from cloud_agent.mcp import resource_template_values

    monkeypatch.setattr(resource_template_values, "MAX_MATCH_STEPS", 10)
    with pytest.raises(TemplateMatchLimit):
        matches_resource_template("repo://{a}{b}{c}{a}{b}{c}", "repo://different")


@pytest.mark.timeout(2)
@pytest.mark.parametrize(
    "template,uri",
    [
        ("repo://{+x}{+y}/{x}", "repo://" + "a" * 4000 + "/different"),
        ("repo://{+x*}/again/{x}", "repo://" + "=" * 4000 + "/again/x"),
    ],
)
def test_production_budget_stops_a_long_lexically_valid_ambiguous_uri(template, uri):
    with pytest.raises(TemplateMatchLimit):
        matches_resource_template(template, uri)


def test_scoped_uris_preserve_percent_encoding_and_template_syntax():
    original = "repo://files/{+path}?literal=%252F#section"
    assert split_resource_uri(resource_uri(7, original)) == ("7", original)


def test_result_scoping_keeps_arbitrary_tool_data_unchanged():
    result = {
        "content": [
            {"type": "resource", "resource": {"uri": "repo://file", "text": "body"}},
            {"type": "resource_link", "uri": "repo://file", "name": "File"},
            {"type": "text", "text": "repo://file"},
        ],
        "structuredContent": {"uri": "repo://file"},
    }
    scoped = scope_resource_result(result, 7)
    assert scoped["content"][0]["resource"]["uri"] == "mcp-resource://7/repo://file"
    assert scoped["content"][1]["uri"] == "mcp-resource://7/repo://file"
    assert scoped["content"][2] == result["content"][2]
    assert scoped["structuredContent"] == result["structuredContent"]
    assert result["content"][0]["resource"]["uri"] == "repo://file"
