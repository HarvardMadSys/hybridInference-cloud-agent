"""A one-answer model server for checking a downloaded agent CLI.

Runs **inside** the probe container, as its main process, on loopback: the
probe has no network at all, so the CLI under test can reach this and nothing
else. The runner passes this file's source to ``python3 -I -c``; it therefore
imports only the standard library and reads its port from ``argv``.

Every request is answered with the same short text, in whichever dialect the
path names. That is all a compatibility check needs from a model: the CLI must
start, send a request, read a streamed answer and settle the turn, and the
production driver must understand everything it printed along the way.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: What the model says. The probe requires the turn's final output to carry it.
REPLY = "probe-ok"
#: The only model the server lists; the probe asks every CLI for it.
MODEL = "probe-model"


def _sse(event: str | None, data: dict[str, Any] | str) -> bytes:
    body = data if isinstance(data, str) else json.dumps(data)
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {body}\n\n".encode()


def _anthropic(stream: bool) -> tuple[str, bytes]:
    message: dict[str, Any] = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if not stream:
        done = {
            **message,
            "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn",
        }
        return "application/json", json.dumps(done).encode()
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"message": message}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": REPLY}}),
        ("content_block_stop", {"index": 0}),
        (
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {}),
    ]
    return "text/event-stream", b"".join(
        _sse(kind, {"type": kind, **data}) for kind, data in events
    )


def _responses(stream: bool) -> tuple[str, bytes]:
    item = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": REPLY, "annotations": []}],
    }
    response = {
        "id": "resp_probe",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": MODEL,
        "output": [item],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    if not stream:
        return "application/json", json.dumps(response).encode()
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
        ("response.output_item.added", {"output_index": 0, "item": item}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        ("response.completed", {"response": response}),
    ]
    return "text/event-stream", b"".join(
        _sse(kind, {"type": kind, "sequence_number": number, **data})
        for number, (kind, data) in enumerate(events)
    )


def _chat(stream: bool) -> tuple[str, bytes]:
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    if not stream:
        completion = {
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": REPLY},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return "application/json", json.dumps(completion).encode()

    def chunk(delta: dict[str, Any], finish: str | None, **extra: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-probe",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        }

    parts = [
        chunk({"role": "assistant", "content": ""}, None),
        chunk({"content": REPLY}, None),
        chunk({}, "stop"),
        {
            "id": "chatcmpl-probe",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "choices": [],
            "usage": usage,
        },
    ]
    return "text/event-stream", b"".join(_sse(None, part) for part in parts) + _sse(None, "[DONE]")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/models"):
            listing = {
                "object": "list",
                "data": [{"id": MODEL, "object": "model", "owned_by": "probe"}],
            }
            self._send(200, "application/json", json.dumps(listing).encode())
            return
        self._send(404, "application/json", b'{"error":{"message":"not found"}}')

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        stream = bool(body.get("stream")) if isinstance(body, dict) else False
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/count_tokens"):
            self._send(200, "application/json", b'{"input_tokens":1}')
        elif path.endswith("/messages"):
            self._send(200, *_anthropic(stream))
        elif path.endswith("/responses"):
            self._send(200, *_responses(stream))
        elif path.endswith("/chat/completions"):
            self._send(200, *_chat(stream))
        else:
            self._send(404, "application/json", b'{"error":{"message":"not found"}}')


def main() -> None:
    """Serve on ``127.0.0.1:<argv[1]>`` until the container is removed."""
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
