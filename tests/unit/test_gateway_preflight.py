"""The readiness check tells the four failures apart.

Each of them currently arrives as a 404, and reading the status code alone is
how the routing gap was misdiagnosed the first time: `/internal/model-catalog`
answered 404 with the frontend's HTML, and that was written up as evidence
about the dispatch token. It was evidence the request never reached the
backend.

So the judgements are pure functions over one `Answer` — status, body,
content type, and whether the origin redirected — and tested here. The network
part is a thin wrapper with nothing to decide.

Content type and redirect joined that tuple because status and body alone let
a JSON 404 from any proxy or CDN count as proof that the backend answered.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "preflight" / "check_gateway.py"
_spec = importlib.util.spec_from_file_location("check_gateway", _PATH)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

HTML_404 = "<!DOCTYPE html><html><head><title>404: This page could not be found</title>"


def _gateway_error(kind: str) -> str:
    return json.dumps({"error": {"type": kind, "message": "..."}})


def _answer(status: int, body: str, *, json_ct: bool = True, redirected: bool = False):
    """One response, shaped the way the network wrapper produces them."""
    return check.Answer(
        status,
        body,
        "application/json" if json_ct else "text/html; charset=utf-8",
        redirected,
    )


# ---------------------------------------------------------------------------
# The distinction the first probe missed
# ---------------------------------------------------------------------------


def test_the_frontends_404_is_not_the_gateways() -> None:
    """Same status, opposite meanings, and only the body says which."""
    frontend = check.judge_routing(_answer(404, HTML_404, json_ct=False))
    backend = check.judge_routing(_answer(404, _gateway_error("internal_api_not_configured")))
    assert frontend.state == check.FAIL
    assert "not forwarded" in frontend.remedy
    assert backend.state == check.OK


def test_an_unauthorized_json_refusal_proves_the_backend_answered() -> None:
    """A refusal is the *positive* result for the routing check.

    Only the backend produces one, so this is checked without a token on
    purpose — needing a credential to find out whether a path is routed would
    conflate two questions.
    """
    assert check.judge_routing(_answer(401, _gateway_error("unauthorized"))).state == check.OK


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_an_unconfigured_key_is_named_as_such() -> None:
    verdict = check.judge_jwks(_answer(404, _gateway_error("identity_not_configured")))
    assert verdict.state == check.FAIL
    assert "IDENTITY_JWT_PRIVATE_KEY" in verdict.remedy


def test_a_broken_key_is_not_reported_as_a_missing_one() -> None:
    """The gateway answers 500 for a key it cannot use, deliberately.

    A deployment with a malformed PEM has a different problem from one with no
    PEM, and telling an operator to set a variable they already set is how an
    afternoon goes.
    """
    verdict = check.judge_jwks(_answer(500, ""))
    assert verdict.state == check.FAIL
    assert "cannot be used" in verdict.detail


def test_a_published_key_passes_and_an_empty_set_does_not() -> None:
    assert check.judge_jwks(_answer(200, json.dumps({"keys": [{"kid": "a"}]}))).state == check.OK
    assert check.judge_jwks(_answer(200, json.dumps({"keys": []}))).state == check.FAIL


def test_html_from_the_jwks_path_is_not_read_as_a_key_problem() -> None:
    """Wrong origin entirely — say that rather than blame the key."""
    verdict = check.judge_jwks(_answer(404, HTML_404, json_ct=False))
    assert "not serving the gateway's API" in verdict.remedy


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected", "phrase"),
    [
        # A 200 has to carry the catalog. `{}` was a pass here until the
        # success case got the same scepticism as the error envelope: any
        # proxy or placeholder can answer 200 with JSON.
        (200, json.dumps({"models": ["glm-5.1"]}), check.OK, ""),
        (200, "{}", check.FAIL, "not the model catalog"),
        (403, _gateway_error("subject_unavailable"), check.OK, ""),
        (401, _gateway_error("unauthorized"), check.FAIL, "does not match"),
        (
            404,
            _gateway_error("internal_api_not_configured"),
            check.FAIL,
            "no dispatch token set",
        ),
    ],
)
def test_authorization_outcomes(status: int, body: str, expected: str, phrase: str) -> None:
    """403 is a pass: the token was accepted and the probe user is not real."""
    verdict = check.judge_internal_api(_answer(status, body))
    assert verdict.state == expected
    assert phrase in (verdict.detail + verdict.remedy)


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def test_a_failure_exits_non_zero(monkeypatch, capsys) -> None:
    """So it can gate a deploy step rather than only inform a human."""
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: [("x", check.Verdict(check.FAIL, "d", "r"))],
    )
    assert check.main(["https://gw.test"]) == 1
    assert "not ready" in capsys.readouterr().out


def test_a_skipped_check_does_not_open_the_gate(monkeypatch, capsys) -> None:
    """A check that could not run is not a check that passed.

    This asserted the opposite, on the reasoning that an unset token is a gap
    in the *check* rather than a fault in the gateway. True, and beside the
    point for something whose exit code gates a cutover: the credential most
    likely to be missing is the one that makes the authorized checks runnable
    at all, so exiting 0 meant the gate opened widest exactly when least had
    been verified. The module docstring already promised "0 only when every
    check passes".

    Distinct from a failure — 3, not 1 — so a caller can tell "the gateway is
    wrong" from "you did not give me enough to look".
    """
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: [
            ("a", check.Verdict(check.OK, "d")),
            ("b", check.Verdict(check.SKIP, "d", "r")),
        ],
    )

    assert check.main(["https://gw.test"]) == 3
    output = capsys.readouterr().out
    assert "could not run" in output
    # Named, so the operator knows which credential to go and set.
    assert "- b" in output


def test_the_token_is_never_taken_from_argv(monkeypatch) -> None:
    """`argv` is readable by every process on the host."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        check,
        "run",
        lambda origin, token: seen.update(origin=origin, token=token) or [],
    )
    monkeypatch.setenv(check.ENV_TOKEN, "from-the-environment")
    assert check.main(["https://gw.test"]) == 2  # no checks ran
    assert seen["token"] == "from-the-environment"
    # Two arguments is a usage error, not a second way to pass the token.
    assert check.main(["https://gw.test", "a-token"]) == 2


# ---------------------------------------------------------------------------
# What "the backend answered" is allowed to mean
# ---------------------------------------------------------------------------


def test_json_alone_is_not_proof_the_backend_answered() -> None:
    """Any proxy or CDN can produce a JSON 404.

    The check accepted `status in (200, 401, 403, 404)` with a non-HTML body,
    which is a description of almost every error page on the internet. Only
    the gateway's own error envelope counts.
    """
    generic = _answer(404, json.dumps({"error": "not found"}))
    assert check.judge_routing(generic).state == check.FAIL

    ours = _answer(404, _gateway_error("internal_api_not_configured"))
    assert check.judge_routing(ours).state == check.OK


def test_a_json_body_served_as_html_is_refused() -> None:
    """The content type is part of the answer, not decoration."""
    mislabelled = check.Answer(401, _gateway_error("unauthorized"), "text/html")
    assert check.judge_routing(mislabelled).state == check.FAIL


def test_a_redirect_is_never_a_pass() -> None:
    """A check that follows a bounce has stopped asking about this origin.

    A gateway that sends `/internal/*` to a login page is precisely the
    misconfiguration being looked for; following the redirect and judging
    whatever answers there would report it as healthy.
    """
    bounced = check.Answer(302, "", "text/html", redirected=True)
    assert check.judge_routing(bounced).state == check.FAIL


def test_the_client_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judgement above only means anything if the wrapper agrees.

    The origin bounces ``/internal/*`` to a page whose answer would pass the
    routing check. Followed, the bounce would be judged healthy.
    """
    from types import SimpleNamespace

    import httpx

    asked: list[str] = []

    def origin(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.startswith("/internal/"):
            return httpx.Response(302, headers={"location": "/login"})
        return httpx.Response(404, json=json.loads(_gateway_error("internal_api_not_configured")))

    def client(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(origin), **kwargs)

    monkeypatch.setattr(check, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError))
    verdicts = dict(check.run("https://gateway.example", ""))

    assert verdicts["/internal/* reaches the backend"].state == check.FAIL
    assert "/login" not in asked


# ---------------------------------------------------------------------------
# A 200 is not by itself the catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("judge", ["judge_routing", "judge_internal_api"])
def test_a_200_that_is_not_the_catalog_fails(judge: str) -> None:
    """The success case deserves the scepticism the error case already gets.

    Both judgements read the error *envelope* rather than trusting a status,
    for exactly the reason in this file's header — and then accepted any 200
    without looking. A cache, a proxy, a placeholder app or a half-deployed
    frontend route can all answer `200 {"ok": true}`, and this check exists to
    gate a cutover on the gateway being there.
    """
    verdict = getattr(check, judge)(_answer(200, json.dumps({"ok": True})))
    assert verdict.state is check.FAIL, verdict
    assert "not the model catalog" in verdict.detail


@pytest.mark.parametrize("judge", ["judge_routing", "judge_internal_api"])
def test_a_200_carrying_models_passes(judge: str) -> None:
    """`models` is the field the control plane actually reads.

    `GatewayClient.visible_models` raises without it, so checking that rather
    than a shape heuristic keeps the check and the caller agreeing about what
    a usable answer is.
    """
    body = json.dumps({"models": ["glm-5.1", "qwen3.6-35b"], "aliases": {}})
    verdict = getattr(check, judge)(_answer(200, body))
    assert verdict.state is check.OK, verdict
    assert "2 models" in verdict.detail


@pytest.mark.parametrize("judge", ["judge_routing", "judge_internal_api"])
def test_an_empty_catalog_passes_but_says_so(judge: str) -> None:
    """A gateway serving nothing to the probe user is configured, not absent.

    Failing here would conflate "this is not the gateway" with "this user can
    reach no model" — two different problems with two different fixes, and the
    second one is not what this check is for.
    """
    verdict = getattr(check, judge)(_answer(200, json.dumps({"models": []})))
    assert verdict.state is check.OK, verdict
    assert "no models" in verdict.detail


@pytest.mark.parametrize("judge", ["judge_routing", "judge_internal_api"])
def test_a_200_with_a_json_content_type_but_no_json_fails(judge: str) -> None:
    """Content type is a claim; the body is the evidence."""
    verdict = getattr(check, judge)(_answer(200, "not json at all"))
    assert verdict.state is check.FAIL, verdict
