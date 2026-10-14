#!/usr/bin/env python3
"""Ask a gateway whether it is ready to be cut over to.

Four things have to be true before section 3 of `docs/developer/smoke.md` can start, and
each of them currently fails in a way that reads as something else:

* **No identity key.** `/v1/identity/jwks` answers 404. A 404 from a route that
  exists reads as a route that does not, so this reports the difference.
* **`/internal/*` not forwarded.** Nginx sends unmatched paths to the frontend,
  whose 404 is an HTML page. A control plane reading that concludes the gateway
  is down. The rewrites landed in the gateway's #1202; this is how you tell
  whether the deployment carrying them is live yet.
* **No dispatch token.** Every internal path answers 404
  `internal_api_not_configured` — correct, and indistinguishable from a build
  with no internal API.
* **A dispatch token that does not match.** 401, which at least says so.

Run it after setting the variables and before scheduling anything:

    python deploy/preflight/check_gateway.py https://staging.freeinference.org

With a token, so the authorized paths are exercised too:

    GATEWAY_GRANT_DISPATCH_TOKEN=... python deploy/preflight/check_gateway.py <url>

**The token is read from the environment, never from a flag.** `argv` is
readable by every process on the host.

Read-only: it fetches JWKS and reads a catalog. It mints nothing and changes
nothing, so it is safe against production.

Exit code is 0 only when every check passes, so it can gate a deploy step.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NamedTuple

import httpx

ENV_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"

OK = "pass"
FAIL = "fail"
SKIP = "skip"


class Verdict(NamedTuple):
    """One check's outcome and what to do about it."""

    state: str
    detail: str
    remedy: str = ""


def _looks_like_html(body: str) -> bool:
    """Whether a response body is a web page rather than an API answer.

    The distinction the original probe missed. A gateway that refuses says so
    in JSON; a frontend that never forwarded the request answers in HTML, and
    both arrive as 404.
    """
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<title>" in head


def _error_type(body: str) -> str:
    """The gateway's error type, or an empty string if this is not one."""
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("type") or "")


def judge_jwks(answer: Answer) -> Verdict:
    """Whether this gateway can sign identity tokens."""
    if answer.status == 200:
        try:
            keys = json.loads(answer.body).get("keys")
        except ValueError:
            return Verdict(FAIL, "JWKS is not JSON", "check what is fronting this origin")
        if not isinstance(keys, list) or not keys:
            return Verdict(FAIL, "JWKS is empty", "IDENTITY_JWT_PRIVATE_KEY is set but has no key")
        return Verdict(OK, f"{len(keys)} signing key(s) published")
    if _error_type(answer.body) == "identity_not_configured":
        return Verdict(
            FAIL,
            "identity is not configured",
            "set IDENTITY_JWT_PRIVATE_KEY, IDENTITY_ISSUER and "
            "IDENTITY_ALLOWED_REDIRECTS on the gateway",
        )
    if answer.status == 500:
        return Verdict(
            FAIL,
            "the configured key cannot be used",
            "a malformed, encrypted, non-RSA or undersized PEM — the gateway "
            "reports this as a server error on purpose, so it is not mistaken "
            "for 'not configured'",
        )
    if _looks_like_html(answer.body):
        return Verdict(
            FAIL, f"HTML at {answer.status}", "this origin is not serving the gateway's API"
        )
    return Verdict(FAIL, f"unexpected {answer.status}", answer.body[:120])


def judge_routing(answer: Answer) -> Verdict:
    """Whether `/internal/*` reaches the backend at all.

    Answered without a token on purpose: an unauthorized *JSON* refusal is the
    proof, because only the backend produces one.
    """
    if answer.redirected:
        return Verdict(
            FAIL,
            f"redirected ({answer.status})",
            "the origin bounced this request instead of serving it; whatever "
            "answers after the redirect is not what a control plane will call",
        )
    if _looks_like_html(answer.body):
        return Verdict(
            FAIL,
            f"the frontend answered ({answer.status}, HTML)",
            "/internal/model-catalog is not forwarded to the backend — the "
            "rewrites in the gateway's #1202 are not on this deployment yet",
        )
    if not answer.is_json:
        return Verdict(
            FAIL,
            f"{answer.status} with content-type {answer.content_type or 'none'!r}",
            "the backend answers this path in JSON; something else is serving it",
        )
    # **The gateway's own error envelope, not merely JSON.** Any proxy, CDN or
    # error page can answer `{"error": "not found"}` with a 404 — accepting
    # that as proof of reaching the backend is how this check passed against
    # the frontend for a whole afternoon. Only these shapes are ours.
    kind = _error_type(answer.body)
    if kind in ("unauthorized", "internal_api_not_configured"):
        return Verdict(OK, f"the backend answered ({answer.status}, {kind})")
    if answer.status == 200:
        return _judge_catalog_body(answer)
    return Verdict(
        FAIL,
        f"{answer.status}, JSON, but not the gateway's error envelope",
        answer.body[:120],
    )


def _judge_catalog_body(answer: Answer) -> Verdict:
    """Whether a 200 is the model catalog, or merely a 200.

    The same reasoning the error envelope already gets, applied to the success
    case: any proxy, cache or placeholder can answer 200 with JSON, and reading
    only the status is how this check first passed against the frontend. What
    the control plane needs from this route is a `models` list
    (`GatewayClient.visible_models` raises without one), so that is what is
    checked — the field the caller actually reads, not a heuristic about the
    body looking about right.

    An empty list is a pass with a warning rather than a failure: a gateway
    that serves no model to the probe user is correctly configured and
    unhelpful, which is a different problem from one that is not the gateway.
    """
    try:
        body = json.loads(answer.body)
    except ValueError:
        return Verdict(FAIL, "200, but the body is not JSON", answer.body[:120])
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return Verdict(
            FAIL,
            "200, JSON, but not the model catalog",
            "no `models` list — something is answering this path that is not "
            f"the gateway: {answer.body[:120]}",
        )
    count = len(body["models"])
    if count == 0:
        return Verdict(
            OK,
            "the catalog answered, with no models for the probe user",
            "the route is right; whether the user can reach a model is a "
            "separate question this check does not settle",
        )
    return Verdict(OK, f"the catalog answered ({count} models)")


def judge_internal_api(answer: Answer) -> Verdict:
    """Whether the dispatch token this environment holds is the gateway's."""
    kind = _error_type(answer.body)
    if answer.status == 200:
        return _judge_catalog_body(answer)
    if kind == "internal_api_not_configured":
        return Verdict(
            FAIL,
            "the gateway has no dispatch token set",
            f"set {ENV_TOKEN} on the gateway to the same value used here, and "
            "to a different value in every environment",
        )
    if answer.status == 401:
        return Verdict(
            FAIL,
            "the dispatch token does not match",
            "the gateway has one set; it is not this one",
        )
    if answer.status == 403:
        # The token was accepted. The user id is a probe value.
        return Verdict(OK, "authorized (the probe user is unknown, which is expected)")
    if _looks_like_html(answer.body):
        return Verdict(
            FAIL, f"the frontend answered ({answer.status})", "see the routing check above"
        )
    return Verdict(FAIL, f"unexpected {answer.status}", answer.body[:120])


class Answer(NamedTuple):
    """What one request came back as.

    Carries the content type and whether the response redirected, because both
    are how a check tells "the backend refused me" from "something else
    answered instead" — and both were being thrown away.
    """

    status: int
    body: str
    content_type: str = ""
    redirected: bool = False

    @property
    def is_json(self) -> bool:
        return self.content_type.split(";")[0].strip().lower() == "application/json"


def _get(client: httpx.Client, url: str, **kwargs: Any) -> Answer:
    try:
        response = client.get(url, **kwargs)
    except httpx.HTTPError as exc:
        return Answer(0, f"unreachable: {exc}")
    return Answer(
        response.status_code,
        response.text,
        response.headers.get("content-type", ""),
        # `follow_redirects` is off, so this is a bounce we did not take. Where
        # it points is not the question: a check that has to follow a redirect
        # to succeed is no longer asking about the origin it was given.
        redirected=300 <= response.status_code < 400,
    )


def run(origin: str, token: str) -> list[tuple[str, Verdict]]:
    """Run every check against one gateway origin."""
    origin = origin.rstrip("/")
    results: list[tuple[str, Verdict]] = []
    # **Redirects are not followed.** A check that lands somewhere else and
    # likes what it finds has stopped asking about the origin it was given —
    # and a gateway that bounces `/internal/*` to a login page is exactly the
    # misconfiguration this exists to catch.
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        answer = _get(client, f"{origin}/v1/identity/jwks")
        results.append(("identity signing key", judge_jwks(answer)))

        answer = _get(client, f"{origin}/internal/model-catalog")
        results.append(("/internal/* reaches the backend", judge_routing(answer)))

        if not token:
            results.append(
                (
                    "internal API authorization",
                    Verdict(SKIP, f"{ENV_TOKEN} not set here", f"re-run with {ENV_TOKEN} exported"),
                )
            )
        else:
            answer = _get(
                client,
                f"{origin}/internal/model-catalog",
                params={"user_id": "preflight-probe"},
                headers={"Authorization": f"Bearer {token}"},
            )
            results.append(("internal API authorization", judge_internal_api(answer)))
    return results


def main(argv: list[str] | None = None) -> int:
    """Check one origin and report. Returns 0 only if nothing failed."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
        print(f"\nusage: {sys.argv[0]} <gateway-origin>", file=sys.stderr)
        return 2

    origin = args[0]
    results = run(origin, os.environ.get(ENV_TOKEN, "").strip())

    print(f"\n{origin}\n")
    if not results:
        print("no checks ran", file=sys.stderr)
        return 2
    width = max(len(name) for name, _ in results)
    for name, verdict in results:
        mark = {OK: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[verdict.state]
        print(f"[{mark}] {name:<{width}}  {verdict.detail}")
        if verdict.remedy and verdict.state != OK:
            print(f"{'':>{width + 11}}{verdict.remedy}")

    failed = [name for name, verdict in results if verdict.state == FAIL]
    skipped = [name for name, verdict in results if verdict.state == SKIP]
    print()
    if failed:
        print(f"not ready: {len(failed)} check(s) failed")
        return 1
    if skipped:
        # **A skipped check is not a passed one.** This gates a cutover, and
        # the thing most likely to be missing is the credential that makes the
        # authorized checks runnable at all — so exiting 0 here meant the gate
        # opened widest exactly when least had been verified. The docstring
        # promised "0 only when every check passes" while the code returned 0
        # for a run that checked two things out of four.
        print(f"not ready: {len(skipped)} check(s) could not run")
        for name in skipped:
            print(f"  - {name}")
        return 3
    print("ready for section 3 of docs/developer/smoke.md")
    # **Said out loud, because a green run reads as "everything is set".**
    # `IDENTITY_ISSUER` and `IDENTITY_ALLOWED_REDIRECTS` cannot be checked from
    # outside: the consent page is served by the frontend, and the endpoint
    # that would refuse a bad redirect (`POST /v1/identity/code`) requires a
    # signed-in session, so an unauthenticated probe is turned away before
    # reaching the configuration. A gateway can publish a key here and still
    # be unable to issue anything.
    #
    # A check was written for this and removed: it read the consent page's
    # ordinary HTML as a misconfiguration and told operators to go and inspect
    # a variable that was fine. A probe that cannot tell those apart is worse
    # than the gap it was covering.
    print(
        "\nnot checked: IDENTITY_ISSUER and IDENTITY_ALLOWED_REDIRECTS —\n"
        "  not observable without a signed-in session. Section 3 of\n"
        "  docs/developer/smoke.md exercises them by signing in for real."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
