"""SecretRedactor masks exactly what the per-character scan it replaced did (#751).

That scan became one compiled alternation, plus a search for the stream tail
that must be held back. The implementation it replaced is kept below, verbatim
apart from its name, as the oracle. Seeded random secret sets and texts —
overlapping and prefix-sharing values, values holding markers or marker
fragments, multi-line PEM-like keys, random chunkings — must give the same
``text()``, the same output and held-back text from every ``feed()``, and the
same ``flush()``.
"""

from __future__ import annotations

import itertools
import random
import string
from typing import TYPE_CHECKING

import pytest

from cloud_agent_shared.secret_redaction import SecretRedactor, secret_patterns

if TYPE_CHECKING:
    from collections.abc import Mapping


class _OriginalRedactor:
    """``SecretRedactor`` before #751, verbatim apart from its name: the oracle."""

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


# Names whose markers share prefixes: "[secret:A]" begins "[secret:A]B]".
NAMES = ("A", "B", "AB", "A]B", "TOKEN", "KEY@previous")
# Pieces that recombine into values, fragments of values and near misses.
ATOMS = ("a", "b", "ab", "ba", "aab", "[", "]", ":", "secret", "[secret:", "A]", "\n", " ", "é")
BASE64 = string.ascii_letters + string.digits + "+/"
# Assembled so that no credential scanner reads this file as holding a key.
KEY_KIND = "TEST " + "KEY"


def _pem(rng: random.Random, alphabet: str, lines: int, width: tuple[int, int]) -> str:
    body = ["".join(rng.choices(alphabet, k=rng.randint(*width))) for _ in range(lines)]
    newline = rng.choice(("\n", "\r\n"))
    return newline.join([f"-----BEGIN {KEY_KIND}-----", *body, f"-----END {KEY_KIND}-----"])


def _value(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.15:
        # A value that starts with, or is, another secret's marker.
        return f"[secret:{rng.choice(NAMES)}]" + "".join(rng.choices("ab]", k=rng.randint(0, 3)))
    if roll < 0.3:
        # Lines under eight characters, and the delimiters, are not patterns.
        return _pem(rng, "abAB12", rng.randint(1, 4), (3, 14))
    if roll < 0.4:
        # Longer than the probe, so a held tail is found by its first characters.
        return "".join(rng.choices("abc", k=rng.randint(9, 30)))
    if roll < 0.5:
        # Periodic, so those first characters recur overlapping themselves.
        return rng.choice(("a", "ab", "aab")) * rng.randint(3, 8) + rng.choice(("", "b", "-x"))
    return "".join(rng.choices(ATOMS, k=rng.randint(1, 6)))


def _secrets(rng: random.Random) -> dict[str, str]:
    return {rng.choice(NAMES): _value(rng) for _ in range(rng.randint(0, 5))}


def _text(rng: random.Random, secrets: Mapping[str, str], pieces: int) -> str:
    values = [pattern for value in secrets.values() for pattern in secret_patterns(value)]
    markers = [f"[secret:{name}]" for name in secrets]
    parts: list[str] = []
    for _ in range(rng.randint(0, pieces)):
        roll = rng.random()
        if roll < 0.3 and values:
            value = rng.choice(values)
            start, end = sorted((rng.randint(0, len(value)), rng.randint(0, len(value))))
            parts.append(rng.choice((value, value, value[:end], value[start:], value[start:end])))
        elif roll < 0.45 and markers:
            marker = rng.choice(markers)
            cut = rng.randint(0, len(marker))
            parts.append(rng.choice((marker, marker[:cut], marker[cut:])))
        else:
            parts.append(rng.choice(ATOMS))
    return "".join(parts)


def _assert_same_text(secrets: Mapping[str, str], text: str) -> None:
    expected = _OriginalRedactor(secrets).text(text)
    assert SecretRedactor(secrets).text(text) == expected, (secrets, text)


def _assert_same_stream(
    secrets: Mapping[str, str], chunks: list[tuple[str, bool]], *, flush: bool = True
) -> None:
    redactor, original = SecretRedactor(secrets), _OriginalRedactor(secrets)
    for step, (chunk, final) in enumerate(chunks):
        context = (secrets, chunks[: step + 1])
        assert redactor.feed(chunk, final=final) == original.feed(chunk, final=final), context
        assert redactor._pending == original._pending, context
    if flush:
        assert redactor.flush() == original.flush(), (secrets, chunks)
        assert redactor._pending == original._pending == ""


def _chunks(rng: random.Random, text: str, sizes: tuple[int, ...]) -> list[tuple[str, bool]]:
    chunks: list[tuple[str, bool]] = []
    pos = 0
    while pos < len(text) or rng.random() < 0.1:
        size = rng.choice(sizes)
        chunks.append((text[pos : pos + size], rng.random() < 0.05))
        pos += size
    return chunks


@pytest.mark.parametrize("seed", range(6))
def test_random_texts_and_streams_mask_exactly_as_the_original_scan(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(100):
        secrets = _secrets(rng)
        for _ in range(5):
            text = _text(rng, secrets, 25)
            _assert_same_text(secrets, text)
            _assert_same_stream(secrets, _chunks(rng, text, (0, 1, 1, 2, 3, 5, 8, 13, 40, 200)))


def test_long_output_with_keys_masks_exactly_as_the_original_scan() -> None:
    # Keys of realistic width, so most of a chunk lies before the held-back
    # tail and the tail itself can be a long prefix of a key or of one line.
    rng = random.Random(751)
    for _ in range(8):
        secrets = {
            "DEPLOY_KEY": _pem(rng, BASE64, rng.randint(2, 6), (64, 64)),
            "TOKEN": "".join(rng.choices(BASE64, k=40)),
            "AB": "".join(rng.choices("ab", k=12)),
        }
        text = _text(rng, secrets, 400)
        _assert_same_text(secrets, text)
        for sizes in ((1, 7, 64, 300), (512, 1024, 4096)):
            _assert_same_stream(secrets, _chunks(rng, text, sizes))


def test_every_short_token_sequence_masks_exactly_as_the_original_scan() -> None:
    # Values that overlap, share a prefix, start with a marker, end with the
    # start of one, and start inside the longer of two markers sharing a start.
    secrets = {
        "A": "abab",
        "B": "ababab-long",
        "A]B": "[secret:A]ab",
        "AB": "b[secret:",
        "T": "B]ab",
    }
    tokens = ("a", "b", "ab", "-long", "[secret:", "A]", "A]B]", "[secret:A]", "B]")
    for size in range(5):
        for parts in itertools.product(tokens, repeat=size):
            text = "".join(parts)
            _assert_same_text(secrets, text)
            if size > 3:
                continue
            for cut in range(len(text) + 1):
                _assert_same_stream(secrets, [(text[:cut], False), (text[cut:], False)])


def test_a_longer_marker_is_waited_for_even_where_a_shorter_one_matches() -> None:
    secrets = {"A": "alpha-secret-1", "A]B": "bravo-secret-2"}
    redactor = SecretRedactor(secrets)
    assert redactor.feed("x [secret:A]B") == "x "
    assert redactor.feed("]") == "[secret:A]B]"
    redactor = SecretRedactor(secrets)
    assert redactor.feed("x [secret:A]B") == "x "
    assert redactor.feed("!") == "[secret:A]B!"
    _assert_same_stream(secrets, [("x [secret:A]B", False), ("]", False)])
    _assert_same_stream(secrets, [("x [secret:A]B", False), ("!", False)])


def test_a_marker_passes_through_where_a_longer_pattern_could_still_start() -> None:
    # A marker is never rescanned, so a value that starts with one is never
    # matched at that position, and nothing is held waiting for it.
    secrets = {"A": "alpha-secret-1", "B": "[secret:A]-and-more"}
    redactor = SecretRedactor(secrets)
    assert redactor.feed("[secret:A]-and") == "[secret:A]-and"
    assert redactor._pending == ""
    assert redactor.text("[secret:A]-and-more") == "[secret:A]-and-more"
    _assert_same_stream(secrets, [("[secret:A]-and", False), ("-more", False)])


def test_a_longer_pattern_is_waited_for_even_where_a_shorter_one_matches() -> None:
    secrets = {"A": "secret-one", "B": "secret-one-extended"}
    redactor = SecretRedactor(secrets)
    assert redactor.feed("x secret-one-ext") == "x "
    assert redactor.feed("ended!") == "[secret:B]!"
    redactor = SecretRedactor(secrets)
    assert redactor.feed("x secret-one-ext") == "x "
    assert redactor.feed("!") == "[secret:A]-ext!"
    _assert_same_stream(secrets, [("x secret-one-ext", False), ("ended!", False)])
    _assert_same_stream(secrets, [("x secret-one-ext", False), ("!", False)])


def test_a_possible_start_inside_a_masked_value_is_not_held() -> None:
    # "secret" inside the first value begins the second, but the scan never
    # reaches it: it is inside the span the first value replaced.
    secrets = {"A": "first-secret", "B": "secret-tail-long"}
    redactor = SecretRedactor(secrets)
    assert redactor.feed("first-secret") == "[secret:A]"
    assert redactor._pending == ""
    _assert_same_stream(secrets, [("first-secret", False)], flush=False)


def test_empty_input_and_an_empty_secret_set() -> None:
    for secrets in ({}, {"EMPTY": ""}, {"A": "alpha-secret-1"}):
        redactor = SecretRedactor(secrets)
        assert redactor.text("") == ""
        assert redactor.feed("") == ""
        assert redactor.feed("", final=True) == ""
        assert redactor.flush() == ""
        _assert_same_stream(secrets, [("", False), ("", True), ("", False)])
    plain = SecretRedactor({})
    assert plain.text("value: alpha-secret-1") == "value: alpha-secret-1"
    assert plain.feed("[secret:") == "[secret:"
    # Nothing can be masked, so the value is not copied.
    value = {"text": "alpha-secret-1", "nested": ["[secret:A]"]}
    assert plain.payload(value) is value


def test_identical_secret_sets_share_one_compiled_matcher() -> None:
    secrets = {"A": "alpha-secret-1", "B": "bravo-secret-2"}
    first = SecretRedactor(secrets)
    assert SecretRedactor(dict(reversed(secrets.items())))._matcher is first._matcher
    assert SecretRedactor({**secrets, "C": "charlie-secret-3"})._matcher is not first._matcher
    assert SecretRedactor({})._matcher is None
