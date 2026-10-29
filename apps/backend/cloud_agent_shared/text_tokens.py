"""A tokenizer-free estimate of how many tokens a text takes.

No tokenizer of the providers the platform serves ships with this project,
and their vocabularies differ, so an exact count is not available here. This
estimate is used only where a CLI did not report its own count, and every
number it produces is labelled an estimate.

The rule is the reference plugin's four characters per token, refined for the
text it misjudges most: CJK characters, which BPE vocabularies such as
cl100k and o200k encode at roughly one token per character (often more for
rare characters), and other non-ASCII text, which takes about one token per
two characters. Code is ASCII and stays at four characters per token, which
undercounts dense code (punctuation-heavy lines encode closer to three).
"""

from __future__ import annotations

import re

#: CJK ideographs, kana, Hangul, and full-width forms.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿＀-￯]")
_ASCII = re.compile(r"[\x00-\x7f]")
CHARS_PER_TOKEN = 4

ESTIMATE_BASIS = "chars_cjk_v1"


def split_counts(text: str) -> tuple[int, int, int]:
    """(ASCII characters, CJK characters, other characters) of ``text``."""
    cjk = len(_CJK.findall(text))
    ascii_chars = len(_ASCII.findall(text))
    return ascii_chars, cjk, max(0, len(text) - ascii_chars - cjk)


def estimate_from_counts(ascii_chars: int, cjk: int, other: int) -> int:
    """Estimated tokens from the three character counts."""
    ascii_tokens = -(-max(0, ascii_chars) // CHARS_PER_TOKEN)
    other_tokens = -(-max(0, other) // 2)
    return ascii_tokens + max(0, cjk) + other_tokens


def estimate_text_tokens(text: str) -> int:
    """Estimated tokens of ``text``; 0 for the empty string."""
    if not text:
        return 0
    return estimate_from_counts(*split_counts(text))
