"""No tracked file may carry a real credential.

CI runs gitleaks over the tree and the full history. Its config allowlists only
one exact fake fixture line — never a path — so history remains covered. This
test adds the repository's own language: it scans every tracked file for the
credential formats this project and its gateway actually issue, which the
scanner's default rules do not all know, and it exempts fixtures by literal
value rather than by directory.

The layout is inherited from the source repository, which learned it the hard
way: a wholesale path exemption hid a live key there for months. This
repository is public by design, so a key that lands in a commit is published,
not merely readable — removing it afterwards helps nobody who already pulled.
A finding here means revoke first, then clean up.

Fixtures that need to *look* like a key should be assembled by concatenation
(``"ghp_" + "a" * 36``) so the shape never appears whole in the file — that is
how the migrated patch gate tests already do it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Credential shapes worth failing on. Each is deliberately narrow: enough
# random-looking payload that a hand-written fixture will not match.
SECRET_SHAPES = {
    "FreeInference gateway API key": re.compile(r"hyi-[A-Za-z0-9_-]{32,}"),
    "Agent control/worker token": re.compile(r"ajt\.[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{24,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{40,}"),
    "OpenAI key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{40,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "Slack bot token": re.compile(r"xoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    "RSA/EC private key block": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\n"),
}

# Values that match a shape above but are demonstrably not credentials. Keep
# this list short and literal — a fixture that needs a random-looking key is a
# fixture that should use an obviously fake or concatenated one instead.
KNOWN_FIXTURES = {
    # AWS publishes this in its own documentation as the example access key.
    # It reaches this repository through test_agent_patch_gate.py, whose
    # subject is secret detection, so it necessarily carries samples.
    "AKIAIOSFODNN7EXAMPLE",
}

# Binary and vendored trees carry no hand-written credentials and cost time.
SKIP_PREFIXES = ("apps/frontend/node_modules/", "apps/frontend/.next/")
SKIP_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    names = [n.decode() for n in out.split(b"\0") if n]
    return [
        REPO / n
        for n in names
        if not n.startswith(SKIP_PREFIXES) and not n.lower().endswith(SKIP_SUFFIXES)
    ]


@pytest.mark.parametrize("label", sorted(SECRET_SHAPES))
def test_no_tracked_file_carries_a_credential(label: str) -> None:
    """One case per shape, so a failure names which kind of key leaked."""
    pattern = SECRET_SHAPES[label]
    findings: list[str] = []

    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text:
            continue
        for match in pattern.finditer(text):
            value = match.group()
            if value in KNOWN_FIXTURES:
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{path.relative_to(REPO)}:{line} ({value[:12]}...)")

    assert not findings, (
        f"{label} committed to the repository. Revoke it — this repository is "
        f"public by design, so it is already published — then remove it here: "
        f"{findings}"
    )


def test_the_shapes_actually_discriminate() -> None:
    """A pattern that matches nothing real is a test that guards nothing.

    Samples are split by concatenation so the literal never appears whole in
    this file — the scan reads tracked files, and this file is one of them.
    """
    gateway = SECRET_SHAPES["FreeInference gateway API key"]
    # ``generate_api_key`` uses secrets.token_urlsafe(32), so both URL-safe
    # punctuation characters are part of the real alphabet. An alphanumeric-
    # only sample would let the detector silently miss most keys it protects.
    sample = "hyi-" + "QQ7mKp2XvR9tLbN4_wZcE6yHsA1-dJfG3uT8oViM5rPkB"
    assert gateway.fullmatch(sample)
    # Short, obviously-fake fixtures must keep passing.
    for fixture in ("hyi-not-a-real-key", "hyi-" + "d" * 8):
        assert not gateway.fullmatch(fixture), fixture

    worker = SECRET_SHAPES["Agent control/worker token"]
    real_shaped = "ajt." + "eyJqIjoiam9iXzEiLCJhIjox" + "." + "mF9pQ2xYw8kTzR4vB6nC1dHs"
    assert worker.fullmatch(real_shaped)
    # The patch gate's fixture token is deliberately too short to match.
    assert not worker.fullmatch("ajt.abcdefghijkl.mnopqrstuvwx")
