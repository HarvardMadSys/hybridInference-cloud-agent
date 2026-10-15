"""A fake model gateway that makes an agent CLI run one shell command.

Run inside a sandbox container as ``python3 shell_probe_gateway.py LOG``. It
serves the platform gateway on 127.0.0.1:8900 and a stand-in for the owner's
own provider on 127.0.0.1:8901, in all three dialects the pinned CLIs speak:
Anthropic Messages, OpenAI Responses and Chat Completions.

The first model request of a turn is answered with a call to the CLI's own
shell tool, chosen from the tools the request offers; once its result comes
back the answer is plain text. The command prints, for each name in
:data:`PROBED`, the value the command saw. The log holds one JSON line per
request (port, path and both credential headers) and per tool result.

Standard library only, so it runs with the image's ``python3``.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY_PORT = 8900
OWNER_PORT = 8901
PROBED = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
COMMAND = "echo PROBE " + " ".join(f"{name}=${{{name}:-unset}}" for name in PROBED)
SHELL_TOOLS = ("Bash", "bash", "exec_command", "shell", "shell_command")
_lock = threading.Lock()


def _log(path, entry):
    with _lock, open(path, "a") as out:
        out.write(json.dumps(entry) + "\n")


def _sse(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()


def _arguments(schema):
    """Fill the shell tool's own parameter shape with the probe command."""
    properties = (schema or {}).get("properties", {})
    arguments = {}
    if "cmd" in properties:
        arguments["cmd"] = COMMAND
    elif properties.get("command", {}).get("type") == "array":
        arguments["command"] = ["bash", "-lc", COMMAND]
    else:
        arguments["command"] = COMMAND
    if "description" in properties:
        arguments["description"] = "probe"
    return arguments


def _shell_tool(tools, name_of, schema_of):
    by_name = {name_of(tool): tool for tool in tools if name_of(tool)}
    for name in SHELL_TOOLS:
        if name in by_name:
            return name, _arguments(schema_of(by_name[name]))
    return None, None


def _handler(log_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _record(self):
            _log(
                log_path,
                {
                    "port": self.server.server_port,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "x-api-key": self.headers.get("x-api-key"),
                },
            )

        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._record()
            listing = {"object": "list", "data": [{"id": "test-model", "object": "model"}]}
            self._send(json.dumps(listing).encode(), "application/json")

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                request = {}
            self._record()
            path = self.path.split("?")[0]
            if path.endswith("/count_tokens"):
                self._send(b'{"input_tokens": 1}', "application/json")
            elif path.endswith("/messages"):
                self._send(self._messages(request), "text/event-stream")
            elif path.endswith("/responses"):
                self._send(self._responses(request), "text/event-stream")
            else:
                self._send(self._chat(request), "text/event-stream")

        def _result(self, content):
            _log(log_path, {"tool_result": json.dumps(content)})

        def _messages(self, request):
            results = [
                block
                for message in request.get("messages", [])
                if message.get("role") == "user" and isinstance(message.get("content"), list)
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            for block in results:
                self._result(block.get("content"))
            name, arguments = _shell_tool(
                request.get("tools", []), lambda t: t.get("name"), lambda t: t.get("input_schema")
            )
            call = bool(name and not results)
            if call:
                block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
            else:
                block = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": "done"}
            message = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "test-model",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            stop = {"stop_reason": "tool_use" if call else "end_turn", "stop_sequence": None}
            events = [
                ("message_start", {"message": message}),
                ("content_block_start", {"index": 0, "content_block": block}),
                ("content_block_delta", {"index": 0, "delta": delta}),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": stop, "usage": {"output_tokens": 1}}),
                ("message_stop", {}),
            ]
            return b"".join(_sse(kind, {"type": kind, **data}) for kind, data in events)

        def _responses(self, request):
            inputs = request.get("input")
            inputs = inputs if isinstance(inputs, list) else []
            results = [
                item
                for item in inputs
                if isinstance(item, dict) and item.get("type", "").endswith("_call_output")
            ]
            for item in results:
                self._result(item.get("output"))
            tools = [tool for tool in request.get("tools", []) if tool.get("type") == "function"]
            name, arguments = _shell_tool(
                tools, lambda t: t.get("name"), lambda t: t.get("parameters")
            )
            if name and not results:
                item = {
                    "id": "fc_1",
                    "call_id": "call_1",
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }
            else:
                text = {"type": "output_text", "text": "done", "annotations": []}
                item = {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [text],
                }
            response = {
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "test-model",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            started = {**response, "status": "in_progress", "output": []}
            events = [
                ("response.created", {"response": started}),
                ("response.output_item.added", {"output_index": 0, "item": item}),
                ("response.output_item.done", {"output_index": 0, "item": item}),
                ("response.completed", {"response": response}),
            ]
            return b"".join(
                _sse(kind, {"type": kind, "sequence_number": number, **data})
                for number, (kind, data) in enumerate(events)
            )

        def _chat(self, request):
            results = [m for m in request.get("messages", []) if m.get("role") == "tool"]
            for message in results:
                self._result(message.get("content"))
            name, arguments = _shell_tool(
                request.get("tools", []),
                lambda t: (t.get("function") or {}).get("name"),
                lambda t: (t.get("function") or {}).get("parameters"),
            )
            if name and not results:
                function = {"name": name, "arguments": json.dumps(arguments)}
                call = {"index": 0, "id": "call_1", "type": "function", "function": function}
                delta, finish = {"role": "assistant", "tool_calls": [call]}, "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "done"}, "stop"
            chunks = []
            for value, reason in ((delta, None), ({}, finish)):
                choice = {"index": 0, "delta": value, "finish_reason": reason}
                payload = {
                    "id": "chat_1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-model",
                    "choices": [choice],
                }
                chunks.append(("data: " + json.dumps(payload) + "\n\n").encode())
            chunks.append(b"data: [DONE]\n\n")
            return b"".join(chunks)

    return Handler


def main():
    log_path = sys.argv[1]
    for port in (GATEWAY_PORT, OWNER_PORT):
        server = ThreadingHTTPServer(("127.0.0.1", port), _handler(log_path))
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
