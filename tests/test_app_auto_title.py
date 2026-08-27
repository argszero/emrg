"""Tests for _auto_title_from_prompt (Codex #40492 borrow).

New sessions previously displayed the raw session id until the host ran
`/rename` manually. The auto-title helper derives a short descriptive
title from the first user message — deterministic, no LLM call.
"""

from emrg.client.app import _auto_title_from_prompt


def test_plain_prompt_kept_verbatim():
    assert _auto_title_from_prompt("fix the billing bug") == "fix the billing bug"


def test_long_prompt_truncated_with_ellipsis():
    title = _auto_title_from_prompt("x" * 100)
    assert len(title) == 30
    assert title.endswith("…")


def test_slash_command_skipped():
    assert _auto_title_from_prompt("/sessions") is None
    assert _auto_title_from_prompt("  /rename foo bar") is None


def test_multiline_uses_first_line():
    assert _auto_title_from_prompt("first line\nsecond line") == "first line"


def test_whitespace_only_returns_none():
    assert _auto_title_from_prompt("") is None
    assert _auto_title_from_prompt("   \n  ") is None


def test_cjk_prompt_truncates_safely():
    text = "请修复这个 bug，它发生在登录流程中，需要检查 token 刷新逻辑"
    title = _auto_title_from_prompt(text)
    assert isinstance(title, str)
    assert len(title) == 30
    assert title.endswith("…")


def test_collapses_internal_whitespace():
    assert _auto_title_from_prompt("  hello    world  ") == "hello world"


def test_max_len_custom():
    assert len(_auto_title_from_prompt("a" * 50, max_len=12)) == 12
    assert _auto_title_from_prompt("short", max_len=12) == "short"
