"""Stateful exact-value redaction before output is truncated or persisted."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


MIN_SECRET_CHARS = 8


def secret_patterns(value: str) -> tuple[str, ...]:
    """Match complete values and substantive multiline fragments, never PEM delimiters."""
    if not value:
        return ()
    lines = (
        line
        for line in value.splitlines()
        if len(line.strip()) >= MIN_SECRET_CHARS
        and not re.fullmatch(r"-----[A-Z0-9 -]+-----", line.strip())
    )
    return tuple(dict.fromkeys((value, *lines)))


class SecretRedactor:
    """Mask known values, including values crossing output chunk boundaries."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        patterns: dict[str, str] = {}
        for name, value in secrets.items():
            for pattern in secret_patterns(value):
                if pattern:
                    patterns.setdefault(pattern, f"[secret:{name}]")
        self._patterns = sorted(patterns.items(), key=lambda item: -len(item[0]))
        self._pending = ""
        self._markers = sorted(set(patterns.values()), key=len, reverse=True)

    def text(self, text: str) -> str:
        """Mask complete text without rescanning replacement markers."""
        if not self._patterns:
            return text
        result: list[str] = []
        pos = 0
        while pos < len(text):
            marker = next((m for m in self._markers if text.startswith(m, pos)), None)
            if marker:
                result.append(marker)
                pos += len(marker)
                continue
            match = next(
                ((p, marker) for p, marker in self._patterns if text.startswith(p, pos)), None
            )
            if match is None:
                result.append(text[pos])
                pos += 1
            else:
                result.append(match[1])
                pos += len(match[0])
        return "".join(result)

    def feed(self, chunk: str, *, final: bool = False) -> str:
        """Retain any possible trailing secret prefix until more output arrives."""
        if not self._patterns:
            return chunk
        self._pending += chunk
        result: list[str] = []
        pos = 0
        while pos < len(self._pending):
            remaining = len(self._pending) - pos
            if not final and any(
                len(m) > remaining and m.startswith(self._pending[pos:]) for m in self._markers
            ):
                break
            marker = next((m for m in self._markers if self._pending.startswith(m, pos)), None)
            if marker:
                result.append(marker)
                pos += len(marker)
                continue
            if not final and any(
                len(p) > remaining and p.startswith(self._pending[pos:]) for p, _ in self._patterns
            ):
                break
            match = next(
                ((p, marker) for p, marker in self._patterns if self._pending.startswith(p, pos)),
                None,
            )
            if match is None:
                result.append(self._pending[pos])
                pos += 1
            else:
                result.append(match[1])
                pos += len(match[0])
        self._pending = self._pending[pos:]
        return "".join(result)

    def flush(self) -> str:
        """Finish a logical stream without uploading unchecked pending text."""
        return self.feed("", final=True)

    def payload(self, value: Any, *, text_data: bool = False) -> Any:
        """Mask text content while preserving protocol control fields."""
        if isinstance(value, list):
            return [
                self.text(item)
                if isinstance(item, str)
                else self.payload(item, text_data=text_data)
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        protected = {
            "id",
            "job_id",
            "thread_id",
            "binding_id",
            "sandbox_id",
            "host_id",
            "measurement_id",
            "event_type",
            "state",
            "phase",
            "status",
            "sha256",
            "digest",
            "token",
            "owner",
            "credential",
            "runtime",
            "runtime_id",
            "runtime_version",
            "kind",
            "type",
            "schema_version",
            "version",
            "preview_id",
            "native_session_id",
            "model",
            "models",
            "model_alias",
            "model_name",
            "model_allowlist",
            "stage",
            "container_id",
            "base_sha",
        }
        return {
            key: (
                self.text(item)
                if isinstance(item, str)
                and (
                    text_data
                    or (
                        key not in protected
                        and not key.endswith(
                            ("_id", "_sha", "_sha256", "_digest", "_token", "_credential")
                        )
                    )
                )
                else item
                if key in protected and not text_data
                else self.payload(
                    item,
                    text_data=text_data
                    or key
                    in {
                        "args",
                        "arguments",
                        "parameters",
                        "result",
                        "output",
                        "input",
                        "text",
                        "content",
                        "data",
                        "raw",
                        "stdout",
                        "stderr",
                    },
                )
            )
            for key, item in value.items()
        }

    def contains(self, value: bytes) -> bool:
        """Inspect staged file bytes, including non-text files."""
        return any(pattern.encode("utf-8") in value for pattern, _ in self._patterns)
