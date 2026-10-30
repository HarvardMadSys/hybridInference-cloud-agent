"""Automatic names are compact, isolated and safe to show."""

from __future__ import annotations

import pytest

from cloud_agent.task_titles import fallback_title, is_warmup_title


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("\n # \n 修复  登录问题 \n More details", "修复 登录问题"),
        ("a" * 160, "a" * 35 + "…"),
        ("问" * 160, "问" * 35 + "…"),
        ("\n\n", "New task"),
        ("修复‮ 登录", "修复 登录"),
        ("Fix\x1b\x00\tlogin\ud800 error", "Fix login error"),
    ],
)
def test_fallback_is_short_and_keeps_first_meaningful_line(prompt, expected):
    assert fallback_title(prompt) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("⏳ The model is starting up — this takes about 14 minutes. Please wait…", True),
        ("\"⏳ The model is starting up", True),
        ("Investigate model startup", False),
        (None, False),
    ],
)
def test_a_warmup_banner_is_recognized_and_a_real_title_is_not(value, expected):
    assert is_warmup_title(value) is expected
