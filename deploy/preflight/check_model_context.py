"""Opt-in Flash recall check against the inference gateway used by a deployment.

Only synthetic messages are sent. This checks model behavior, complementing
(but not replacing) the pinned native CLI and retained-container regressions.
A nonzero exit means the deployment's model path has not passed recall checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cloud_agent_host.runtimes import ClaudeCodeRuntime

MODEL = "deepseek-v4-flash"
QUESTION = "我刚才让你做什么了"
SYSTEM = (
    "You are a coding assistant. You can use persistent files for memories between conversations."
)
DENIAL = re.compile(
    r"第一次交流|这是我看到的第一条消息|"
    r"没有(?:任何|之前的|此前的)对话记录|每次对话.{0,20}(?:全新|独立)|"
    r"(?:会话|聊天).{0,8}(?:已经|已被|被)重置|"
    r"this is (?:our|the) first (?:exchange|conversation)|"
    r"I (?:don't|do not) have (?:any )?(?:memory|record|history) of",
    re.IGNORECASE,
)


def recall_payload(case: str) -> tuple[dict, str]:
    """Use the same continuation wire builder as the production Claude driver."""
    original = "hi" if case != "task" else "请把待实现的函数命名为 comet_731;现在只确认名字。"
    answer = "Hello! How can I help you today?" if case != "task" else "收到,函数名为 comet_731。"
    messages = [
        {"role": "user", "content": original},
        {"role": "assistant", "content": answer},
    ]
    if case == "repeat":
        messages += [
            json.loads(ClaudeCodeRuntime.user_message_line(QUESTION, continuing=True))["message"],
            {"role": "assistant", "content": "你之前只说了 hi,还没有布置具体任务。"},
        ]
    # After a repeated question, recalling that question itself is a valid
    # answer to "what did I just ask?". Ask explicitly for the first message
    # when checking that the greeting survived additional conversation turns.
    question = "请原样引用我在这个对话中的第一条用户消息。" if case == "repeat" else QUESTION
    messages.append(
        json.loads(ClaudeCodeRuntime.user_message_line(question, continuing=True))["message"]
    )
    return {
        "model": MODEL,
        "system": SYSTEM,
        "messages": messages,
        "max_tokens": 768,
        "stream": True,
    }, "comet_731" if case == "task" else "hi"


def validate_answer(answer: str, expected: str) -> None:
    """Conservative acceptance: recall the fixture and reject known reset claims.

    This is a regression check for the reported failure, not a general-purpose
    semantic judge. A quoted denial can also fail; inspect the printed answer.
    """
    if not re.search(
        rf"(?<![a-zA-Z0-9_]){re.escape(expected)}(?![a-zA-Z0-9_])", answer, re.IGNORECASE
    ):
        raise ValueError(f"reply did not recall {expected!r}")
    if DENIAL.search(answer):
        raise ValueError("reply contains a missing-history/reset claim")


def read_answer(response: httpx.Response) -> str:
    """Require a complete, successful stream rather than accepting partial text."""
    answer = ""
    stopped = False
    reason = None
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event.get("type") == "error":
            raise ValueError("gateway returned a streaming error")
        if event.get("type") == "content_block_delta":
            answer += event.get("delta", {}).get("text", "")
        if event.get("type") == "message_delta":
            reason = event.get("delta", {}).get("stop_reason")
        if event.get("type") == "message_stop":
            stopped = True
    if not stopped or reason != "end_turn":
        raise ValueError("incomplete or truncated model response")
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gateway_url")
    parser.add_argument("--key-file", default=os.environ.get("CLOUD_AGENT_TEST_API_KEY_FILE"))
    parser.add_argument("--proxy", default=os.environ.get("CLOUD_AGENT_TEST_PROXY"))
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    parsed = urlsplit(args.gateway_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        parser.error("use HTTPS for a remote gateway, or HTTP on loopback")
    if not args.key_file:
        parser.error("set CLOUD_AGENT_TEST_API_KEY_FILE or --key-file (never pass a key itself)")
    key = Path(args.key_file).read_text().strip()
    if not key:
        parser.error("key file is empty")
    endpoint = args.gateway_url.rstrip("/").removesuffix("/v1") + "/v1/messages"
    failures = 0
    with httpx.Client(timeout=120, trust_env=False, proxy=args.proxy) as client:
        for repetition in range(args.repetitions):
            for case in ("hi", "repeat", "task"):
                body, expected = recall_payload(case)
                result = {"case": case, "repetition": repetition + 1, "model": MODEL}
                try:
                    with client.stream(
                        "POST",
                        endpoint,
                        json=body,
                        headers={
                            "x-api-key": key,
                            "anthropic-version": "2023-06-01",
                            "User-Agent": "FreeInference-Context-Preflight/1.0",
                        },
                    ) as response:
                        result["status"] = response.status_code
                        result["request_id"] = response.headers.get("x-request-id")
                        if response.status_code != 200:
                            raise ValueError("gateway returned a non-200 response")
                        answer = read_answer(response)
                        result["answer"] = answer
                        validate_answer(answer, expected)
                    result["passed"] = True
                except (httpx.HTTPError, ValueError) as exc:
                    # Network exception text can contain URLs; log the type only.
                    result["error"] = (
                        str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    )
                    result["passed"] = False
                    failures += 1
                print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
