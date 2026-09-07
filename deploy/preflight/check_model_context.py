"""Opt-in Flash recall check against the inference gateway used by a deployment.

Only synthetic messages are sent. This checks model behavior, complementing
(but not replacing) the pinned native CLI and retained-container regressions.
A nonzero exit means a response/recall/thinking-output check failed. Results
report recall and thinking separately: neither missing thinking output nor a
model's missing-history claim alone proves session data was lost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from dataclasses import dataclass
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


def recall_payload(case: str) -> tuple[dict, str | dict[str, str]]:
    """Use the same unmodified user-message wire builder as the production Claude driver."""
    if case in {"state_update", "no_history"}:
        tag = secrets.token_hex(4)
        expected = {
            "project_code": f"quartz_{tag}",
            "original_function_name": f"emit_{tag}",
            "original_format": "CSV",
            "current_format": "JSONL",
        }
        messages = [
            {
                "role": "user",
                "content": f"我们在设计导出功能,项目代号 {expected['project_code']},最初用 CSV。请起一个函数名。",
            },
            {
                "role": "assistant",
                "content": f"函数名建议用 {expected['original_function_name']}。",
            },
            {"role": "user", "content": "改用 JSONL。17+26是多少?"},
            {"role": "assistant", "content": "43。"},
        ]
        question = (
            "请只返回 JSON:project_code 是项目代号,original_function_name 是你第一条回复起的函数名,"
            "original_format 是最初导出格式,current_format 是最新格式。对未提供的信息填 UNKNOWN。"
        )
        if case == "no_history":
            messages = []
            expected = dict.fromkeys(expected, "UNKNOWN")
        messages.append(json.loads(ClaudeCodeRuntime.user_message_line(question))["message"])
        return {
            "model": MODEL,
            "system": SYSTEM,
            "messages": messages,
            "max_tokens": 2048,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "stream": True,
        }, expected
    original = "hi" if case != "task" else "请把待实现的函数命名为 comet_731;现在只确认名字。"
    answer = "Hello! How can I help you today?" if case != "task" else "收到,函数名为 comet_731。"
    messages = [
        {"role": "user", "content": original},
        {"role": "assistant", "content": answer},
    ]
    if case == "repeat":
        messages += [
            json.loads(ClaudeCodeRuntime.user_message_line(QUESTION))["message"],
            {"role": "assistant", "content": "你之前只说了 hi,还没有布置具体任务。"},
        ]
    # After a repeated question, recalling that question itself is a valid
    # answer to "what did I just ask?". Ask explicitly for the first message
    # when checking that the greeting survived additional conversation turns.
    question = "请原样引用我在这个对话中的第一条用户消息。" if case == "repeat" else QUESTION
    messages.append(json.loads(ClaudeCodeRuntime.user_message_line(question))["message"])
    return {
        "model": MODEL,
        "system": SYSTEM,
        "messages": messages,
        "max_tokens": 2048,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "stream": True,
    }, "comet_731" if case == "task" else "hi"


def validate_answer(answer: str, expected: str | dict[str, str]) -> None:
    """Conservative acceptance: recall the fixture and reject known reset claims.

    This is a regression check for the reported failure, not a general-purpose
    semantic judge. A quoted denial can also fail; inspect the printed answer.
    """
    if isinstance(expected, dict):
        # Accept a JSON code fence, but not keyword matches in an incorrect
        # explanation. Each role's fact and both old/new values must be right.
        text = answer.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            actual = json.loads(text)
        except ValueError as exc:
            raise ValueError("reply did not return a factual JSON object") from exc
        if actual != expected:
            raise ValueError("reply did not recall the required facts and updates")
        return
    if not re.search(
        rf"(?<![a-zA-Z0-9_]){re.escape(expected)}(?![a-zA-Z0-9_])", answer, re.IGNORECASE
    ):
        raise ValueError(f"reply did not recall {expected!r}")
    if DENIAL.search(answer):
        raise ValueError("reply contains a missing-history/reset claim")


@dataclass(frozen=True)
class ModelReply:
    """Keep observed thinking separate from the answer's recall accuracy."""

    text: str
    thinking_observed: bool


def read_reply(response: httpx.Response) -> ModelReply:
    """Require a complete, successful stream rather than accepting partial text."""
    answer = ""
    stopped = False
    reason = None
    saw_thinking = False
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event.get("type") == "error":
            raise ValueError("gateway returned a streaming error")
        if event.get("type") == "content_block_delta":
            answer += event.get("delta", {}).get("text", "")
            saw_thinking |= bool(event.get("delta", {}).get("thinking"))
        if event.get("type") == "message_delta":
            reason = event.get("delta", {}).get("stop_reason")
        if event.get("type") == "message_stop":
            stopped = True
    if not stopped or reason != "end_turn":
        raise ValueError("incomplete or truncated model response")
    return ModelReply(answer, saw_thinking)


def assess_reply(reply: ModelReply, expected: str | dict[str, str]) -> dict:
    """Evaluate both checks even when one fails; never infer history loss."""
    result = {
        "answer": reply.text,
        "thinking_observed": reply.thinking_observed,
        "recall_passed": True,
        "passed": False,
    }
    errors = []
    try:
        validate_answer(reply.text, expected)
    except ValueError as exc:
        result["recall_passed"] = False
        errors.append(str(exc))
    if not reply.thinking_observed:
        errors.append("no thinking output observed for the requested adaptive/high mode")
    result["passed"] = result["recall_passed"] and reply.thinking_observed
    if errors:
        result["errors"] = errors
    return result


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
            for case in ("hi", "repeat", "task", "state_update", "no_history"):
                body, expected = recall_payload(case)
                result = {
                    "case": case,
                    "check": "factual_context"
                    if isinstance(expected, dict)
                    else "reported_symptom",
                    "repetition": repetition + 1,
                    "model": MODEL,
                }
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
                        result.update(assess_reply(read_reply(response), expected))
                except (httpx.HTTPError, ValueError) as exc:
                    # Network exception text can contain URLs; log the type only.
                    result["error"] = (
                        str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    )
                    result["passed"] = False
                failures += int(not result["passed"])
                print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
